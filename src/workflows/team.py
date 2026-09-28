"""A checked team of in-session helpers: split, dispatch, check, repair, stop.

The `omh_team` plugin tool is the only caller. What it adds over the ordinary
`delegate_task` fan-out is one rule: a part is done only when OMH has run that
part's frozen check command itself, in this session's workspace, and seen it
exit 0. A helper's own summary, its `schema_valid` flag and the host's child
status are reports; none of them moves a unit.

The record (`omh_team/v1`)
--------------------------
One JSON file per team under ``<omh_home>/runtime/teams/<session key>/``,
keyed by the DURABLE session the reading-session rule resolves (#1794), so a
created TUI's transport id and its durable key find the same team. Every
write is a temp file, ``fsync``, then ``os.replace``, under the record's OS
lock. The record is metadata: titles, the plan item each unit was approved
under, frozen commands, attempt keys, exit codes and times. No helper prompt,
summary or command output is stored -- a check keeps a digest of its output
tail, never the tail.

Team state (``running`` / ``done`` / ``blocked``) is never stored. It is
derived from the units on every read (`team_state`), so no writer can declare
a team finished.

Attempts are write-ahead
------------------------
Before a prepared `delegate_task` entry is returned, the attempt it belongs to
is written to the record with its deterministic key
``<team_id>/<unit_id>/attempt-<n>``. The key rides in the entry's goal as a
marker, which is how the host's `subagent_start` / `subagent_stop` callbacks
(not the model) tell OMH that the attempt is in flight and when it came back.
A marker only ever binds to an attempt the record already reserved; it is an
identifier OMH minted, looked up exactly, never text that is interpreted.

Check outcomes
--------------
``passed`` accepts the unit. ``failed`` (the command ran and exited non-zero)
spends an attempt. ``inconclusive`` (timeout, command not found, missing
workspace, or the working tree changed while the check ran) spends nothing,
and three of them on one attempt block the unit with that reason so the team
still reaches a stop.

What this does not see
----------------------
Helpers share one checkout. A check is attributed to the unit whose command it
is, but a different unit's edits are in the same tree; the barrier (no check
while any team delegation is in flight) narrows that, it does not remove it.
A check executes code the helpers wrote, as the operator's OS user with the
real HOME. Both are stated in `team_status` and in the approval prompt.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from typing import Any, Final

from ..system.local_store import _with_windows_retry, ensure_dir, file_lock

TEAM_SCHEMA_VERSION: Final = "omh_team/v1"
TEAM_RESULT_SCHEMA_VERSION: Final = "omh_team_result/v1"
# The check timeout, and the reason it is not the evidence tool's 180 s cap: a
# real repository suite runs for minutes, and a timeout here is inconclusive
# rather than a failure, so a cap below the suite's length would only ever
# produce "blocked: the check kept timing out". Ten minutes covers the suites
# this repository has measured while still ending a hung check inside one turn.
TEAM_CHECK_TIMEOUT_SECONDS: Final = 600
# A lease outlives the longest check by this margin, so a crashed checker's
# lease expires and the check is re-run without spending an attempt.
TEAM_LEASE_MARGIN_SECONDS: Final = 120
DEFAULT_MAX_REPAIR_ATTEMPTS: Final = 2
MAX_REPAIR_ATTEMPTS_CAP: Final = 3
MAX_TEAM_UNITS: Final = 8
MAX_TEAMS_PER_SESSION: Final = 16
# Three inconclusive checks on one attempt block the unit with that reason.
MAX_INCONCLUSIVE_PER_ATTEMPT: Final = 3
# A reserved attempt the host never reported dispatching is handed back at
# most this many times before the unit blocks as `dispatch_not_observed`.
MAX_REEMITS: Final = 3
# A dispatched helper the host never reported returning is treated as lost
# after this long; the unit blocks rather than holding the barrier forever.
DELEGATION_STALE_SECONDS: Final = 4 * 3600
MAX_TEAM_RECORD_BYTES: Final = 262_144
MAX_TITLE_CHARS: Final = 80
MAX_COMMAND_CHARS: Final = 200
UNIT_STATES: Final = (
    "waiting", "prepared", "dispatched", "awaiting_check", "repairing", "accepted", "blocked",
)
TEAM_STATES: Final = ("running", "done", "blocked")
CHECK_OUTCOMES: Final = ("passed", "failed", "inconclusive")
EVIDENCE_KIND: Final = "team_check"
# Renderable team events: a header (`teammate` + `event`), one plain summary
# line, and a `detail_ref` into this record. Append-only with a monotonic
# `seq`; only the newest MAX_TEAM_EVENTS are kept (the oldest drop first), so
# a record's event history is bounded at 64 entries. Metadata only: no helper
# summary and no command output is ever an event.
TEAM_EVENTS: Final = ("started", "finished", "check_passed", "check_failed", "repairing", "blocked", "done")
MAX_TEAM_EVENTS: Final = 64
TEAM_CAVEAT: Final = (
    "Helpers run under this same Hermes profile, and each check runs the code they wrote as your own "
    "user account with your real home folder. Helpers share this one workspace, so a check can see "
    "another part's edits."
)
# INVARIANT 3 (`tests/test_handoff_safety_contract_enforcement.py`) applied to
# the one command-execution surface that does NOT go through the evidence
# allowlist: a team command is approved by the person, not matched against a
# prefix list, so these are refused by program wherever they appear in argv.
TEAM_FORGE_PROGRAMS: Final = frozenset({"gh", "hub", "glab", "tea"})
TEAM_FORBIDDEN_GIT_VERBS: Final = frozenset({"push", "merge", "rebase", "remote", "fetch", "pull"})
# A shell or `env` as the program turns one approved line into any line.
TEAM_SHELL_PROGRAMS: Final = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "env", "cmd", "cmd.exe",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe",
})
_SHELL_METACHARACTERS: Final = re.compile(r"[\n\r;&|`$<>(){}]")
_TEAM_ID: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_UNIT_ID: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_MARKER: Final = re.compile(
    r"\A\[omh-team:([a-z0-9][a-z0-9-]{0,47})/([a-z0-9][a-z0-9_-]{0,47})/attempt-([1-9][0-9]?)\]"
)
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_FINGERPRINT_UNAVAILABLE: Final = frozenset({"timed_out", "unavailable"})


class TeamRefusal(ValueError):
    """A closed refusal. ``reason`` is a code; ``say`` is one plain sentence."""

    def __init__(self, reason: str, say: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.say = say


@dataclass(frozen=True)
class CheckRun:
    """What the runner observed: ``exited`` with a code, ``not_found`` or ``timeout``."""

    outcome: str
    exit_code: int | None
    output_tail: str


Runner = Callable[[list[str], Path, int], CheckRun]
Fingerprint = Callable[[Path], tuple[str, str | None]]


@dataclass(frozen=True)
class TeamContext:
    omh_home: Path
    session_ref: str
    runner: Runner
    fingerprint: Fingerprint
    now: Callable[[], float]


# --------------------------------------------------------------------------
# Command policy
# --------------------------------------------------------------------------


def validate_team_command(command: object) -> list[str]:
    """The argv of an approvable team check, or a refusal naming why not.

    Deliberately stricter than "not on a denylist": the command is refused when
    ANY argv word is a forge program, a shell, or `git` followed by a verb that
    moves a remote, because `uv run gh ...` runs `gh` just as surely as `gh`.
    """
    if not isinstance(command, str) or not command.strip():
        raise TeamRefusal("command_required", "Every part needs a check command.")
    text = command.strip()
    if len(text) > MAX_COMMAND_CHARS or _CONTROL.search(text):
        raise TeamRefusal("command_too_long", "A check command must be one short line.")
    if _SHELL_METACHARACTERS.search(text):
        raise TeamRefusal("command_shell_syntax", "A check command cannot use shell syntax such as pipes or ';'.")
    try:
        tokens = shlex.split(text)
    except ValueError:
        raise TeamRefusal("command_unparseable", "A check command could not be read as one command.") from None
    if not tokens:
        raise TeamRefusal("command_required", "Every part needs a check command.")
    names = [_program_name(token) for token in tokens]
    for index, name in enumerate(names):
        if name in TEAM_SHELL_PROGRAMS:
            raise TeamRefusal("command_runs_a_shell", "A check command cannot start a shell.")
        if name in TEAM_FORGE_PROGRAMS:
            raise TeamRefusal("command_talks_to_a_forge", "A check command cannot call GitHub or another forge.")
        if name == "git" and set(tokens[index + 1:]) & TEAM_FORBIDDEN_GIT_VERBS:
            raise TeamRefusal("command_moves_a_remote", "A check command cannot push, pull, merge or rebase.")
        if name.startswith("python") and "-c" in tokens[index + 1:]:
            raise TeamRefusal("command_inline_program", "A check command cannot run inline Python code.")
    return tokens


def _program_name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def frozen_commands_digest(units: list[Mapping[str, Any]]) -> str:
    """One digest over every unit's id and command, in unit order."""
    pairs = [[str(unit.get("unit_id", "")), str(unit.get("verification_command", ""))] for unit in units]
    return _digest(pairs)


def approval_rule_key(commands: list[str]) -> str:
    """The `[a]lways` grain for one exact command list, never for the tool."""
    return "omh_team_start:" + _digest(commands)[:16]


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _digest(value: object) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def session_key(session_ref: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", session_ref).strip("_-")[:48]
    digest = hashlib.sha256(session_ref.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}" if slug else digest


def teams_dir(omh_home: Path, session_ref: str) -> Path:
    return Path(omh_home) / "runtime" / "teams" / session_key(session_ref)


def team_path(omh_home: Path, session_ref: str, team_id: str) -> Path:
    if not isinstance(team_id, str) or not _TEAM_ID.fullmatch(team_id):
        raise TeamRefusal("invalid_team_id", "A team name must be short lowercase letters, digits and dashes.")
    return teams_dir(omh_home, session_ref) / f"{team_id}.json"


def _reject_links(path: Path, omh_home: Path) -> None:
    current = path
    root = Path(omh_home)
    while True:
        if current.is_symlink():
            raise TeamRefusal("team_store_unsafe", "The team record location is not safe to write.")
        if current == root or current.parent == current:
            return
        current = current.parent


def read_team(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_TEAM_RECORD_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise TeamRefusal("team_store_unreadable", "The team record could not be read.") from None
    if len(raw) > MAX_TEAM_RECORD_BYTES:
        raise TeamRefusal("team_store_unreadable", "The team record could not be read.")
    try:
        record = json.loads(raw)
    except ValueError:
        raise TeamRefusal("team_store_unreadable", "The team record could not be read.") from None
    if not isinstance(record, dict) or record.get("schema_version") != TEAM_SCHEMA_VERSION:
        raise TeamRefusal("team_store_unreadable", "The team record could not be read.")
    return record


def write_team(path: Path, record: Mapping[str, Any]) -> None:
    """Temp file, fsync, `os.replace`: a reader sees the old record or the new one."""
    content = json.dumps(record, indent=2, sort_keys=True) + "\n"
    data = content.encode("utf-8")
    if len(data) > MAX_TEAM_RECORD_BYTES:
        raise TeamRefusal("team_record_too_large", "The team record grew past its size limit.")
    ensure_dir(path.parent, private=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _with_windows_retry(lambda: os.replace(temporary, path))
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


@contextmanager
def locked_team(omh_home: Path, session_ref: str, team_id: str) -> Iterator[Path]:
    path = team_path(omh_home, session_ref, team_id)
    _reject_links(path.parent, Path(omh_home))
    ensure_dir(path.parent, private=True)
    _reject_links(path.parent, Path(omh_home))
    try:
        with file_lock(path, private=True, timeout_seconds=5.0) as lock:
            if not lock.get("enforced"):
                raise TeamRefusal("team_lock_unavailable", "This system cannot lock the team record.")
            yield path
    except TimeoutError:
        raise TeamRefusal("team_record_busy", "Another step is updating this team; try again.") from None


# --------------------------------------------------------------------------
# Derived state
# --------------------------------------------------------------------------


def _latest(unit: Mapping[str, Any]) -> dict[str, Any] | None:
    attempts = unit.get("attempts") or []
    return attempts[-1] if attempts else None


def unit_state(unit: Mapping[str, Any]) -> str:
    if unit.get("blocked"):
        return "blocked"
    attempt = _latest(unit)
    if attempt is None:
        return "waiting"
    check = attempt.get("check")
    if check and check.get("outcome") == "passed":
        return "accepted"
    if attempt.get("returned_at"):
        return "awaiting_check"
    if attempt.get("n", 1) > 1:
        return "repairing"
    if attempt.get("dispatch"):
        return "dispatched"
    return "prepared"


def _blocked_ancestry(units: list[Mapping[str, Any]]) -> set[str]:
    """Unit ids that can never run: blocked, or downstream of a blocked unit."""
    by_id = {str(unit["unit_id"]): unit for unit in units}
    dead: set[str] = set()
    changed = True
    while changed:
        changed = False
        for unit_id, unit in by_id.items():
            if unit_id in dead:
                continue
            if unit.get("blocked") or any(parent in dead for parent in unit.get("depends_on", [])):
                dead.add(unit_id)
                changed = True
    return dead


def team_state(record: Mapping[str, Any]) -> str:
    """`done`, `blocked` or `running`, derived from the units -- never read from a field."""
    units = list(record.get("units") or [])
    if units and all(unit_state(unit) == "accepted" for unit in units):
        return "done"
    dead = _blocked_ancestry(units)
    live = [unit for unit in units if unit_state(unit) != "accepted" and str(unit["unit_id"]) not in dead]
    if not live and any(unit.get("blocked") for unit in units):
        return "blocked"
    return "running"


def _attempt_limit(record: Mapping[str, Any]) -> int:
    return 1 + int(record.get("max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS))


def attempt_key(team_id: str, unit_id: str, n: int) -> str:
    return f"{team_id}/{unit_id}/attempt-{n}"


def evidence_ref(team_id: str, unit_id: str, n: int) -> dict[str, str]:
    return {"kind": EVIDENCE_KIND, "ref": f"{attempt_key(team_id, unit_id, n)}/check"}


# --------------------------------------------------------------------------
# Prepared delegate entries
# --------------------------------------------------------------------------


def _delegate_entry(team_id: str, unit: Mapping[str, Any], attempt: Mapping[str, Any]) -> dict[str, str]:
    key = attempt_key(team_id, str(unit["unit_id"]), int(attempt["n"]))
    command = str(unit["verification_command"])
    lines = [
        f"Part of a checked team: {unit['plan_item']}",
        f"When you stop, OMH runs this command in the shared workspace and the part counts as done "
        f"only if it exits 0: {command}",
        "Stay inside this part; other parts are being worked on in the same workspace.",
    ]
    failing = attempt.get("repairs_check")
    if isinstance(failing, Mapping):
        lines.append("The previous try failed its check: " + json.dumps(
            {"command": failing.get("command"), "exit_code": failing.get("exit_code")}, sort_keys=True))
        lines.append("Fix the cause and stop when that command would exit 0.")
    return {"goal": f"[omh-team:{key}] {unit['title']}", "context": "\n".join(lines)}


def _reserve(unit: dict[str, Any], now: float, *, repairs_check: Mapping[str, Any] | None = None) -> dict[str, Any]:
    attempts = unit.setdefault("attempts", [])
    attempt: dict[str, Any] = {
        "n": len(attempts) + 1,
        "reserved_at": _iso(now),
        "reemits": 0,
        "dispatch": None,
        "returned_at": None,
        "check": None,
        "inconclusive": [],
    }
    if repairs_check is not None:
        attempt["repairs_check"] = {
            "command": repairs_check["command"], "exit_code": repairs_check["exit_code"],
        }
    attempts.append(attempt)
    return attempt


def _emit(
    record: dict[str, Any],
    now: float,
    *,
    unit: Mapping[str, Any] | None,
    event: str,
    summary: str,
    detail_ref: str,
) -> None:
    """Append one renderable event; `seq` never repeats, even after the cap drops old ones."""
    seq = int(record.get("next_seq", 1))
    record["next_seq"] = seq + 1
    events = record.setdefault("events", [])
    events.append({
        "seq": seq,
        "at": _iso(now),
        "unit_id": str(unit["unit_id"]) if unit is not None else "",
        "teammate": str(unit["title"]) if unit is not None else "The team",
        "event": event,
        "summary": summary,
        "detail_ref": detail_ref,
    })
    del events[:-MAX_TEAM_EVENTS]


def _block(record: dict[str, Any], unit: dict[str, Any], now: float, blocked: dict[str, Any]) -> None:
    unit["blocked"] = blocked
    why = _BLOCKED_SAY.get(str(blocked.get("reason")), "it cannot continue")
    _emit(record, now, unit=unit, event="blocked", summary=f"Stopped: {why}.",
          detail_ref=f"{record['team_id']}/{unit['unit_id']}/blocked")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _ready(record: Mapping[str, Any], unit: Mapping[str, Any]) -> bool:
    """Every parent accepted -- ALL of them, and accepted by a check, not by a report."""
    by_id = {str(item["unit_id"]): item for item in record["units"]}
    return all(unit_state(by_id[parent]) == "accepted" for parent in unit.get("depends_on", []))


# --------------------------------------------------------------------------
# team_start
# --------------------------------------------------------------------------


def _plain(value: object, limit: int, reason: str, say: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or _CONTROL.search(value):
        raise TeamRefusal(reason, say)
    return value.strip()


def _validated_units(raw_units: object) -> list[dict[str, Any]]:
    if not isinstance(raw_units, list) or not 1 <= len(raw_units) <= MAX_TEAM_UNITS:
        raise TeamRefusal("invalid_units", f"A team needs between 1 and {MAX_TEAM_UNITS} parts.")
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_units:
        if not isinstance(raw, Mapping) or set(raw) - {"unit_id", "title", "depends_on", "verification_command"}:
            raise TeamRefusal("invalid_units", "Each part takes only unit_id, title, depends_on and verification_command.")
        unit_id = raw.get("unit_id")
        if not isinstance(unit_id, str) or not _UNIT_ID.fullmatch(unit_id) or unit_id in seen:
            raise TeamRefusal("invalid_unit_id", "Each part needs its own short lowercase id.")
        seen.add(unit_id)
        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
            raise TeamRefusal("invalid_depends_on", "depends_on must list other parts by id.")
        command = raw.get("verification_command")
        validate_team_command(command)
        units.append({
            "unit_id": unit_id,
            "title": _plain(raw.get("title"), MAX_TITLE_CHARS, "invalid_title", "Each part needs a short title."),
            "depends_on": sorted(set(depends_on)),
            "verification_command": str(command).strip(),
        })
    known = {unit["unit_id"] for unit in units}
    for unit in units:
        if unit["unit_id"] in unit["depends_on"] or set(unit["depends_on"]) - known:
            raise TeamRefusal("invalid_depends_on", "depends_on must name other parts of this team.")
    _require_acyclic(units)
    return units


def _require_acyclic(units: list[Mapping[str, Any]]) -> None:
    remaining = {str(unit["unit_id"]): set(unit["depends_on"]) for unit in units}
    while remaining:
        free = [unit_id for unit_id, parents in remaining.items() if not parents & set(remaining)]
        if not free:
            raise TeamRefusal("dependency_cycle", "The parts depend on each other in a circle.")
        for unit_id in free:
            del remaining[unit_id]


def _bind_to_plan(units: list[dict[str, Any]], plan: Mapping[str, Any], plan_ref: object) -> None:
    """The approval binding (R7): accepted plan, same digest, every command verbatim in it."""
    if not plan.get("own_record") or plan.get("status") not in ("established", "all_done"):
        raise TeamRefusal("plan_not_found", "Start a team only from this session's own current plan.")
    if plan.get("plan_stage") != "accepted":
        raise TeamRefusal("plan_not_accepted", "The plan has not been accepted yet, so no team can start.")
    items = plan.get("items")
    if not isinstance(plan_ref, str) or not plan_ref or plan_ref != plan.get("items_digest"):
        raise TeamRefusal("plan_ref_mismatch", "The plan changed since it was accepted, or plan_ref names another plan.")
    texts = [str(item.get("text", "")) for item in items or [] if isinstance(item, Mapping)]
    for unit in units:
        item = next((text for text in texts if unit["verification_command"] in text), None)
        if item is None:
            raise TeamRefusal(
                "command_not_in_accepted_plan",
                f"The check for '{unit['title']}' is not written in the accepted plan, so it was never approved.",
            )
        unit["plan_item"] = item


def team_start(
    ctx: TeamContext,
    *,
    team_id: object,
    units: object,
    plan: Mapping[str, Any],
    plan_ref: object,
    workdir: Path | None,
    max_repair_attempts: object = None,
) -> dict[str, Any]:
    validated = _validated_units(units)
    repairs = DEFAULT_MAX_REPAIR_ATTEMPTS if max_repair_attempts is None else max_repair_attempts
    if isinstance(repairs, bool) or not isinstance(repairs, int) or not 0 <= repairs <= MAX_REPAIR_ATTEMPTS_CAP:
        raise TeamRefusal("invalid_max_repair_attempts", f"Fix-up tries can be 0 to {MAX_REPAIR_ATTEMPTS_CAP}.")
    _bind_to_plan(validated, plan, plan_ref)
    if workdir is None or not Path(workdir).is_dir():
        raise TeamRefusal("workspace_missing", "This session has no workspace folder to run checks in.")
    tid = str(team_id)
    with locked_team(ctx.omh_home, ctx.session_ref, tid) as path:
        record = read_team(path)
        now = ctx.now()
        if record is not None:
            if (record.get("commands_digest") != frozen_commands_digest(validated)
                    or record.get("plan_ref") != plan_ref
                    or [unit["depends_on"] for unit in record["units"]] != [unit["depends_on"] for unit in validated]):
                raise TeamRefusal(
                    "commands_frozen",
                    "This team already started with different parts or checks; they cannot change after start.",
                )
            entries = _reemit(record, now)
            write_team(path, record)
            return _result(record, "team_start", entries=entries, say=_say(record, resumed=True))
        if sum(1 for _ in path.parent.glob("*.json")) >= MAX_TEAMS_PER_SESSION:
            raise TeamRefusal("too_many_teams", "This session already has as many teams as it can keep.")
        record = {
            "schema_version": TEAM_SCHEMA_VERSION,
            "team_id": tid,
            "session_ref": ctx.session_ref,
            "plan_ref": plan_ref,
            "workdir": str(Path(workdir).resolve()),
            "created_at": _iso(now),
            "max_repair_attempts": repairs,
            "commands_digest": frozen_commands_digest(validated),
            "check_timeout_seconds": TEAM_CHECK_TIMEOUT_SECONDS,
            "units": [{**unit, "attempts": [], "blocked": None, "lease": None} for unit in validated],
            "events": [],
            "next_seq": 1,
        }
        entries = []
        for unit in record["units"]:
            if not unit["depends_on"]:
                entries.append(_delegate_entry(tid, unit, _reserve(unit, now)))
        write_team(path, record)
    started = len(entries)
    say = (f"Split into {len(validated)} parts; {started} start now in parallel, and each part is done "
           f"only when its check passes.")
    return _result(record, "team_start", entries=entries, say=say)


def _reemit(record: dict[str, Any], now: float, *, reserved_now: frozenset[str] = frozenset()) -> list[dict[str, str]]:
    """Hand back every reserved attempt the host has not reported dispatching (H5.1).

    ``reserved_now`` names the attempts this same call just reserved: they are
    handed out once as new entries, not counted as a repeat of themselves.
    """
    entries = []
    for unit in record["units"]:
        attempt = _latest(unit)
        if unit.get("blocked") or attempt is None or attempt.get("dispatch") or attempt.get("returned_at"):
            continue
        if attempt.get("check"):
            continue
        if attempt_key(str(record["team_id"]), str(unit["unit_id"]), int(attempt["n"])) in reserved_now:
            continue
        attempt["reemits"] = int(attempt.get("reemits", 0)) + 1
        if attempt["reemits"] > MAX_REEMITS:
            _block(record, unit, now, {"reason": "dispatch_not_observed", "observed_at": _iso(now)})
            continue
        entries.append(_delegate_entry(str(record["team_id"]), unit, attempt))
    return entries


# --------------------------------------------------------------------------
# team_reconcile
# --------------------------------------------------------------------------


def _load(ctx: TeamContext, team_id: object) -> tuple[Path, dict[str, Any]]:
    path = team_path(ctx.omh_home, ctx.session_ref, str(team_id))
    record = read_team(path)
    if record is None or record.get("session_ref") != ctx.session_ref:
        raise TeamRefusal("team_not_found", "No team with that name was started in this session.")
    if record.get("commands_digest") != frozen_commands_digest(record["units"]):
        raise TeamRefusal("frozen_commands_changed", "The team's check commands were changed after start, so nothing runs.")
    return path, record


def _in_flight(record: dict[str, Any], now: float) -> list[str]:
    """Units with a helper out, after expiring ones the host never reported back."""
    flying = []
    for unit in record["units"]:
        attempt = _latest(unit)
        if unit.get("blocked") or attempt is None or not attempt.get("dispatch") or attempt.get("returned_at"):
            continue
        started = _epoch(attempt["dispatch"].get("dispatched_at"))
        if started is not None and now - started > DELEGATION_STALE_SECONDS:
            _block(record, unit, now, {"reason": "delegation_not_returned", "observed_at": _iso(now)})
            continue
        flying.append(str(unit["unit_id"]))
    return flying


def _lease_live(unit: Mapping[str, Any], now: float) -> bool:
    lease = unit.get("lease")
    if not isinstance(lease, Mapping):
        return False
    started = _epoch(lease.get("started_at"))
    return started is not None and now - started < TEAM_CHECK_TIMEOUT_SECONDS + TEAM_LEASE_MARGIN_SECONDS


def team_reconcile(ctx: TeamContext, *, team_id: object, since_seq: object = 0) -> dict[str, Any]:
    # Phase A, under the lock: barrier, then lease every unit that needs a check.
    with locked_team(ctx.omh_home, ctx.session_ref, str(team_id)):
        path, record = _load(ctx, team_id)
        now = ctx.now()
        flying = _in_flight(record, now)
        if flying:
            write_team(path, record)
            return _result(record, "team_reconcile", entries=[], reason="delegations_in_flight",
                           say=f"{len(flying)} part(s) are still being worked on; nothing is checked until they come back.",
                           since_seq=since_seq)
        nonce = secrets.token_hex(8)
        to_check: list[str] = []
        busy: list[str] = []
        for unit in record["units"]:
            if unit_state(unit) != "awaiting_check":
                continue
            if _lease_live(unit, now):
                busy.append(str(unit["unit_id"]))
                continue
            unit["lease"] = {"attempt": _latest(unit)["n"], "nonce": nonce, "started_at": _iso(now)}
            to_check.append(str(unit["unit_id"]))
        write_team(path, record)
        workdir = Path(str(record["workdir"]))
        commands = {str(unit["unit_id"]): str(unit["verification_command"]) for unit in record["units"]}
    # Phase B, outside the lock: run each leased check in the session workspace.
    results = {unit_id: _run_check(ctx, commands[unit_id], workdir) for unit_id in to_check}
    # Phase C, under the lock: apply only what this call leased, then release.
    with locked_team(ctx.omh_home, ctx.session_ref, str(team_id)):
        path, record = _load(ctx, team_id)
        now = ctx.now()
        entries: list[dict[str, str]] = []
        events: list[str] = []
        before = {str(unit["unit_id"]): len(unit.get("attempts") or []) for unit in record["units"]}
        for unit in record["units"]:
            unit_id = str(unit["unit_id"])
            lease = unit.get("lease")
            if unit_id not in results or not isinstance(lease, Mapping) or lease.get("nonce") != nonce:
                continue
            unit["lease"] = None
            attempt = _latest(unit)
            if attempt is None or attempt["n"] != lease.get("attempt") or attempt.get("check"):
                continue
            events.extend(_apply_check(record, unit, attempt, results[unit_id], now, entries))
        for unit in record["units"]:
            if unit_state(unit) == "waiting" and not unit.get("blocked") and _ready(record, unit):
                entries.append(_delegate_entry(str(record["team_id"]), unit, _reserve(unit, now)))
        reserved_now = frozenset(
            attempt_key(str(record["team_id"]), str(unit["unit_id"]), int(attempt["n"]))
            for unit in record["units"]
            for attempt in (unit.get("attempts") or [])[before[str(unit["unit_id"])]:]
        )
        entries.extend(_reemit(record, now, reserved_now=reserved_now))
        if team_state(record) == "done" and not any(item["event"] == "done" for item in record.get("events", [])):
            _emit(record, now, unit=None, event="done",
                  summary=f"All {len(record['units'])} parts passed their checks.", detail_ref=str(record["team_id"]))
        write_team(path, record)
    reason = "check_in_progress" if busy and not to_check else ""
    return _result(record, "team_reconcile", entries=entries, reason=reason, say=_say(record, events=events),
                   since_seq=since_seq)


def _run_check(ctx: TeamContext, command: str, workdir: Path) -> dict[str, Any]:
    started = ctx.now()
    if not workdir.is_dir():
        return {"outcome": "inconclusive", "reason": "workspace_missing", "observed_at": _iso(started)}
    tokens = validate_team_command(command)
    before = ctx.fingerprint(workdir)
    run = ctx.runner(tokens, workdir, TEAM_CHECK_TIMEOUT_SECONDS)
    after = ctx.fingerprint(workdir)
    observed_at = _iso(ctx.now())
    if before[0] in _FINGERPRINT_UNAVAILABLE or after[0] in _FINGERPRINT_UNAVAILABLE:
        return {"outcome": "inconclusive", "reason": "workspace_fingerprint_unavailable", "observed_at": observed_at}
    if before != after:
        return {"outcome": "inconclusive", "reason": "workspace_changed_during_check", "observed_at": observed_at}
    if run.outcome == "timeout":
        return {"outcome": "inconclusive", "reason": "check_timeout", "observed_at": observed_at}
    if run.outcome == "not_found" or run.exit_code is None:
        return {"outcome": "inconclusive", "reason": "command_not_found", "observed_at": observed_at}
    return {
        "outcome": "passed" if run.exit_code == 0 else "failed",
        "command": command,
        "exit_code": run.exit_code,
        "observed_at": observed_at,
        "output_tail_digest": hashlib.sha256(run.output_tail.encode("utf-8", "replace")).hexdigest(),
        "workdir": str(workdir),
    }


def _apply_check(
    record: dict[str, Any],
    unit: dict[str, Any],
    attempt: dict[str, Any],
    result: Mapping[str, Any],
    now: float,
    entries: list[dict[str, str]],
) -> list[str]:
    title = str(unit["title"])
    if result["outcome"] == "inconclusive":
        attempt.setdefault("inconclusive", []).append(
            {"reason": result["reason"], "observed_at": result["observed_at"]})
        if len(attempt["inconclusive"]) >= MAX_INCONCLUSIVE_PER_ATTEMPT:
            _block(record, unit, now, {"reason": "check_inconclusive", "last_reason": result["reason"],
                                       "observed_at": result["observed_at"]})
            return [f"Stopped '{title}': its check could not give a clear answer ({_REASON_SAY[result['reason']]})."]
        return [f"The check for '{title}' could not give a clear answer ({_REASON_SAY[result['reason']]}); "
                "no try was used, and it will run again."]
    attempt["check"] = dict(result)
    check_ref = f"{attempt_key(str(record['team_id']), str(unit['unit_id']), int(attempt['n']))}/check"
    if result["outcome"] == "passed":
        _emit(record, now, unit=unit, event="check_passed", summary="Its check passed (exit code 0).",
              detail_ref=check_ref)
        return [f"'{title}' passed its check."]
    _emit(record, now, unit=unit, event="check_failed",
          summary=f"Its check failed with exit code {result['exit_code']}.", detail_ref=check_ref)
    if int(attempt["n"]) < _attempt_limit(record):
        repair = _reserve(unit, now, repairs_check=result)
        entries.append(_delegate_entry(str(record["team_id"]), unit, repair))
        _emit(record, now, unit=unit, event="repairing",
              summary=f"Sent back for a fix, try {repair['n']} of {_attempt_limit(record)}.",
              detail_ref=attempt_key(str(record["team_id"]), str(unit["unit_id"]), int(repair["n"])))
        return [f"The check for '{title}' failed (exit code {result['exit_code']}); sending it back for a fix, "
                f"try {repair['n']} of {_attempt_limit(record)}."]
    _block(record, unit, now, {
        "reason": "repair_budget_exhausted",
        "last_check": {key: result[key] for key in ("command", "exit_code", "observed_at")},
    })
    return [f"Stopped '{title}': it still fails its check after {attempt['n']} tries "
            f"(exit code {result['exit_code']})."]


_REASON_SAY: Final[dict[str, str]] = {
    "workspace_missing": "the workspace folder is gone",
    "workspace_fingerprint_unavailable": "the workspace could not be read before and after",
    "workspace_changed_during_check": "files changed while it ran",
    "check_timeout": f"it ran past {TEAM_CHECK_TIMEOUT_SECONDS // 60} minutes",
    "command_not_found": "the command was not found",
}

_BLOCKED_SAY: Final[dict[str, str]] = {
    "repair_budget_exhausted": "it still fails its check after every allowed fix",
    "check_inconclusive": "its check never gave a clear answer",
    "dispatch_not_observed": "its helper was never seen starting",
    "delegation_not_returned": "its helper never came back",
}


# --------------------------------------------------------------------------
# Host lifecycle observations
# --------------------------------------------------------------------------


def observe_dispatch(omh_home: Path, session_ref: str, *, child_session_id: object, goal: object, now: float) -> bool:
    """`subagent_start` saw a goal carrying a team marker: bind that attempt to the child."""
    match = _MARKER.match(goal) if isinstance(goal, str) else None
    if match is None or not isinstance(child_session_id, str) or not child_session_id.strip():
        return False
    team_id, unit_id, n = match.group(1), match.group(2), int(match.group(3))
    with locked_team(omh_home, session_ref, team_id) as path:
        record = read_team(path)
        if record is None or record.get("session_ref") != session_ref:
            return False
        unit = next((item for item in record["units"] if item["unit_id"] == unit_id), None)
        attempt = _latest(unit) if unit is not None else None
        if attempt is None or attempt["n"] != n or attempt.get("dispatch") or attempt.get("returned_at"):
            return False
        attempt["dispatch"] = {"child_session_id": child_session_id.strip()[:160], "dispatched_at": _iso(now)}
        _emit(record, now, unit=unit, event="started",
              summary="Started a fix for this part." if n > 1 else "Started working on this part.",
              detail_ref=attempt_key(team_id, unit_id, n))
        write_team(path, record)
    return True


def observe_return(omh_home: Path, session_ref: str, *, child_session_id: object, now: float) -> bool:
    """`subagent_stop` for a child bound above: its attempt is back and may be checked."""
    if not isinstance(child_session_id, str) or not child_session_id.strip():
        return False
    child = child_session_id.strip()[:160]
    directory = teams_dir(omh_home, session_ref)
    if not directory.is_dir() or directory.is_symlink():
        return False
    for candidate in sorted(directory.glob("*.json"))[:MAX_TEAMS_PER_SESSION]:
        with locked_team(omh_home, session_ref, candidate.stem) as path:
            record = read_team(path)
            if record is None or record.get("session_ref") != session_ref:
                continue
            for unit in record["units"]:
                attempt = _latest(unit)
                dispatch = attempt.get("dispatch") if attempt else None
                if dispatch and dispatch.get("child_session_id") == child and not attempt.get("returned_at"):
                    attempt["returned_at"] = _iso(now)
                    _emit(record, now, unit=unit, event="finished", summary="Came back; its check runs next.",
                          detail_ref=attempt_key(str(record["team_id"]), str(unit["unit_id"]), int(attempt["n"])))
                    write_team(path, record)
                    return True
    return False


# --------------------------------------------------------------------------
# team_status and results
# --------------------------------------------------------------------------


def team_status(
    ctx: TeamContext, *, team_id: object, cost: Mapping[str, Any] | None = None, since_seq: object = 0,
) -> dict[str, Any]:
    _, record = _load(ctx, team_id)
    result = _result(record, "team_status", entries=[], say=_say(record), since_seq=since_seq)
    result["cost"] = dict(cost) if cost is not None else {"status": "not_observed"}
    result["caveat"] = TEAM_CAVEAT
    result["check_timeout_seconds"] = TEAM_CHECK_TIMEOUT_SECONDS
    return result


def _unit_view(record: Mapping[str, Any], unit: Mapping[str, Any]) -> dict[str, Any]:
    attempts = list(unit.get("attempts") or [])
    last_check = next((attempt["check"] for attempt in reversed(attempts) if attempt.get("check")), None)
    view: dict[str, Any] = {
        "unit_id": unit["unit_id"],
        "title": unit["title"],
        "state": unit_state(unit),
        "attempts_used": len(attempts),
        "attempts_max": _attempt_limit(record),
        "verification_command": unit["verification_command"],
        "last_check": ({key: last_check[key] for key in ("command", "exit_code", "observed_at", "outcome")}
                       if last_check else None),
    }
    if unit.get("blocked"):
        view["blocked"] = dict(unit["blocked"])
    if view["state"] == "accepted":
        view["evidence"] = evidence_ref(str(record["team_id"]), str(unit["unit_id"]), len(attempts))
    return view


def _result(
    record: Mapping[str, Any],
    action: str,
    *,
    entries: list[dict[str, str]],
    say: str,
    reason: str = "",
    since_seq: object = 0,
) -> dict[str, Any]:
    units = [_unit_view(record, unit) for unit in record["units"]]
    counts = {state: 0 for state in UNIT_STATES}
    for view in units:
        counts[view["state"]] += 1
    result: dict[str, Any] = {
        "schema_version": TEAM_RESULT_SCHEMA_VERSION,
        "action": action,
        "status": "ok",
        "team_id": record["team_id"],
        "team_state": team_state(record),
        "counts": counts,
        "units": units,
        "say": say,
        "delegate_task": {"tool_name": "delegate_task", "arguments": {"tasks": entries}} if entries else None,
    }
    result.update(_events_since(record, since_seq))
    if reason:
        result["reason"] = reason
    return result


def _events_since(record: Mapping[str, Any], since_seq: object) -> dict[str, Any]:
    """Events newer than the caller's last seen `seq`, and whether the cap dropped some."""
    seen = since_seq if isinstance(since_seq, int) and not isinstance(since_seq, bool) and since_seq > 0 else 0
    events = [dict(item) for item in record.get("events") or [] if int(item["seq"]) > seen]
    oldest = int(events[0]["seq"]) if events else int(record.get("next_seq", 1))
    return {
        "events": events,
        "last_seq": int(record.get("next_seq", 1)) - 1,
        "events_truncated": oldest > seen + 1,
    }


def _say(record: Mapping[str, Any], *, events: list[str] | None = None, resumed: bool = False) -> str:
    state = team_state(record)
    units = list(record["units"])
    if state == "done":
        return f"All {len(units)} parts passed their checks."
    if events:
        return " ".join(events)
    if state == "blocked":
        blocked = [unit for unit in units if unit.get("blocked")]
        first = blocked[0]
        why = _BLOCKED_SAY.get(str(first["blocked"].get("reason")), "it cannot continue")
        return f"Stopped: '{first['title']}' is blocked because {why}."
    accepted = sum(1 for unit in units if unit_state(unit) == "accepted")
    if resumed:
        return f"This team is already running: {accepted} of {len(units)} parts have passed their checks."
    return f"{accepted} of {len(units)} parts have passed their checks so far."


def refusal_result(action: str, refusal: TeamRefusal) -> dict[str, Any]:
    return {
        "schema_version": TEAM_RESULT_SCHEMA_VERSION,
        "action": action,
        "status": "refused",
        "reason": refusal.reason,
        "say": refusal.say,
        "delegate_task": None,
    }
