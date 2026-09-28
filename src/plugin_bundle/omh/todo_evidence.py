"""Whether a plan item marked done is closed by a recorded fact.

A done mark on the plan todo is a declaration: it says done was claimed, not
that anything ran. The continuation rule stops a plan when every item is done,
so a run that marks its items done in words -- three advances in a row with no
command between them -- stopped its own loop without doing the work. This
module is what the stop criterion reads instead of the word.

An item's evidence is a typed reference, ``{"kind": ..., "ref": ...}``, and
only two kinds resolve today, both against the result Hermes itself persisted
to ``<hermes_home>/state.db`` for one tool call id:

* ``tool_call``: a ``terminal`` call whose recorded ``exit_code`` is the
  integer 0 -- the field `remote_wait_nudge` reads;
* ``file_write``: a ``write_file`` result carrying ``bytes_written``, or a
  ``patch`` result whose ``success`` is true, neither with an ``error`` --
  #1922's `session_file_activity` outcome rules.

``effect_disposition`` ``none`` (the call had no effect) is a failure and
``unknown`` an unknown, the host's own two dispositions.

``pr``, ``ci_run`` and ``team_check`` are accepted and stored so every lane
writes one vocabulary, and none of them resolves yet: OMH makes no network
call, so it holds no record of a PR or a CI run, and the teammate lane that
will own ``team_check`` (``<team_id>/<unit_id>/attempt-<n>/check``) has not
landed. An unresolved reference keeps the item ``done_unverified``; a PR or
a CI run closes an item today through the ``tool_call`` that observed it --
``gh pr create``, ``gh run watch --exit-status``.

Who names the call. Models do not reliably see tool call ids -- no assistant
reply in the owner's session store quoted a ``toolu_`` id (measured
2026-09-28) -- so the ``omh_todo`` tool picks it from the records at the moment
of the done write: the latest evidence-capable call this session recorded
since the plan was last written (`latest_observed_evidence`). That is
association by time, not by content, and it is stated as such: it proves a
command ran and how it ended between the previous plan write and this one,
never that the command tested the item.

What is never read: the item's text, the command's text, its output, or the
model's reply. A verdict comes from a result field or it does not come.

Nothing here raises into a host. A store that exists and cannot be read is a
reading of its own -- ``unreadable`` -- so a caller can say so instead of
treating it as evidence, and a missing store is ``absent``: a host with no
session store has recorded no commands, which is the conversational lane the
stop criterion leaves alone.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

EVIDENCE_KIND_TOOL_CALL: Final = "tool_call"
EVIDENCE_KIND_FILE_WRITE: Final = "file_write"
EVIDENCE_KIND_PR: Final = "pr"
EVIDENCE_KIND_CI_RUN: Final = "ci_run"
EVIDENCE_KIND_TEAM_CHECK: Final = "team_check"
EVIDENCE_KINDS: Final = (
    EVIDENCE_KIND_TOOL_CALL,
    EVIDENCE_KIND_FILE_WRITE,
    EVIDENCE_KIND_PR,
    EVIDENCE_KIND_CI_RUN,
    EVIDENCE_KIND_TEAM_CHECK,
)
# The host tools whose recorded result can close an item, by the kind that
# names them. Copied from Hermes rather than imported: the bundle may only
# import from inside itself.
_KIND_TOOLS: Final = {
    EVIDENCE_KIND_TOOL_CALL: ("terminal",),
    EVIDENCE_KIND_FILE_WRITE: ("write_file", "patch"),
}
EVIDENCE_TOOLS: Final = ("terminal", "write_file", "patch")
MAX_EVIDENCE_REF_CHARS: Final = 128
# Every ref is one token a reader can show without escaping, bounded before it
# is stored or queried. A call id (`toolu_...`, `call_...`) has no slash; a PR
# or run reference may (`owner/repo#12`); a team check has exactly its shape.
_CALL_ID: Final = re.compile(r"[A-Za-z0-9_.:-]{1,%d}" % MAX_EVIDENCE_REF_CHARS)
_REMOTE_REF: Final = re.compile(r"[A-Za-z0-9_.:/#-]{1,%d}" % MAX_EVIDENCE_REF_CHARS)
_TEAM_CHECK: Final = re.compile(r"[A-Za-z0-9_.:-]+/[A-Za-z0-9_.:-]+/attempt-[0-9]+/check")
_REF_SHAPES: Final = {
    EVIDENCE_KIND_TOOL_CALL: _CALL_ID,
    EVIDENCE_KIND_FILE_WRITE: _CALL_ID,
    EVIDENCE_KIND_PR: _REMOTE_REF,
    EVIDENCE_KIND_CI_RUN: _REMOTE_REF,
    EVIDENCE_KIND_TEAM_CHECK: _TEAM_CHECK,
}
# A compaction continues a session under a new id whose `parent_session_id`
# is the old one, and the calls recorded before it stay under the old id. The
# walk is bounded so a cycle in a hand-edited store costs a fixed number of
# queries.
MAX_LINEAGE_HOPS: Final = 8
_CONNECT_TIMEOUT_SECONDS: Final = 0.5

# Store readings.
STORE_READ: Final = "read"
STORE_ABSENT: Final = "absent"
STORE_UNREADABLE: Final = "unreadable"

# Per-reference verdicts. Only `closed` closes an item.
EVIDENCE_CLOSED: Final = "closed"
EVIDENCE_FAILED: Final = "failed"
EVIDENCE_UNRESOLVED: Final = "unresolved"


def valid_evidence(value: object) -> dict[str, str] | None:
    """``value`` as a storable ``{"kind", "ref"}``, or ``None`` when it is not one."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    ref = value.get("ref")
    if not isinstance(kind, str) or not isinstance(ref, str) or kind not in _REF_SHAPES:
        return None
    ref = ref.strip()
    if len(ref) > MAX_EVIDENCE_REF_CHARS or not _REF_SHAPES[kind].fullmatch(ref):
        return None
    return {"kind": kind, "ref": ref}


def evidence_key(evidence: dict[str, str]) -> str:
    """One string per reference, for keying verdicts."""
    return f"{evidence['kind']}:{evidence['ref']}"


def evidence_reading(
    hermes_home: str | Path | None, session_ref: str, evidence: list[dict[str, str]]
) -> dict[str, Any]:
    """Judge ``evidence`` against the records of ``session_ref`` and its ancestors.

    Returns ``{"store": ..., "observable_lane": bool, "verdicts": {key: verdict}}``
    keyed by `evidence_key`. ``observable_lane`` says whether the session
    recorded ANY evidence-capable call, which is what separates a plan worked
    with commands from a conversational one. On ``absent`` and ``unreadable``
    it is false and ``verdicts`` is empty; the caller decides what each of
    those means, because only it knows whether a done item without a
    reference is a claim to check.
    """
    empty: dict[str, Any] = {"store": STORE_ABSENT, "observable_lane": False, "verdicts": {}}
    session = str(session_ref or "").strip()
    if not session or not hermes_home:
        return empty
    opened = _open_readonly(hermes_home)
    if opened is None:
        return empty
    connection, store = opened
    if connection is None:
        return {**empty, "store": store}
    try:
        lineage = _lineage(connection, session)
        observable = _observable_lane(connection, lineage)
        verdicts = _verdicts(connection, lineage, evidence)
    except sqlite3.Error:
        return {**empty, "store": STORE_UNREADABLE}
    finally:
        connection.close()
    return {"store": STORE_READ, "observable_lane": observable, "verdicts": verdicts}


def latest_observed_evidence(
    hermes_home: str | Path | None, session_ref: str, *, since_epoch: float | None
) -> dict[str, str] | None:
    """The latest evidence-capable call recorded after ``since_epoch``, as evidence, or ``None``.

    Read at a done write, so the call it names is one whose RESULT Hermes had
    already persisted: the host flushes a round's results before it runs the
    next round's tools, so a call from an earlier round is on disk and a
    sibling call in the same round as the ``omh_todo`` call is not. ``None``
    for ``since_epoch`` means no window -- a plan declared for the first time.
    """
    session = str(session_ref or "").strip()
    if not session or not hermes_home:
        return None
    opened = _open_readonly(hermes_home)
    if opened is None or opened[0] is None:
        return None
    connection = opened[0]
    try:
        lineage = _lineage(connection, session)
        sessions = ",".join("?" for _ in lineage)
        tools = ",".join("?" for _ in EVIDENCE_TOOLS)
        query = (
            f"SELECT tool_name, tool_call_id FROM messages WHERE session_id IN ({sessions}) "
            f"AND role = 'tool' AND tool_name IN ({tools}) AND COALESCE(tool_call_id, '') <> ''"
        )
        params: list[Any] = [*lineage, *EVIDENCE_TOOLS]
        if since_epoch is not None:
            query += " AND timestamp > ?"
            params.append(since_epoch)
        row = connection.execute(query + " ORDER BY id DESC LIMIT 1", params).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row:
        return None
    kind = EVIDENCE_KIND_TOOL_CALL if row[0] == "terminal" else EVIDENCE_KIND_FILE_WRITE
    return valid_evidence({"kind": kind, "ref": row[1]})


def _open_readonly(hermes_home: str | Path) -> tuple[sqlite3.Connection | None, str] | None:
    """``None`` when there is no store, else a connection or the reading that replaced one."""
    try:
        path = Path(hermes_home).expanduser() / "state.db"
        if not path.exists() and not path.is_symlink():
            return None
        # The kanban reader refuses a linked store for the same reason: a link
        # decides which database is read, and nothing here chose it.
        if path.is_symlink() or not path.is_file():
            return None, STORE_UNREADABLE
        connection = sqlite3.connect(
            f"file:{quote(str(path))}?mode=ro", uri=True, timeout=_CONNECT_TIMEOUT_SECONDS
        )
    except (OSError, ValueError, sqlite3.Error):
        return None, STORE_UNREADABLE
    return connection, STORE_READ


def _lineage(connection: sqlite3.Connection, session: str) -> list[str]:
    lineage = [session]
    current = session
    for _ in range(MAX_LINEAGE_HOPS):
        row = connection.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?", (current,)
        ).fetchone()
        parent = str(row[0]) if row and row[0] else ""
        if not parent or parent in lineage:
            break
        lineage.append(parent)
        current = parent
    return lineage


def _observable_lane(connection: sqlite3.Connection, lineage: list[str]) -> bool:
    sessions = ",".join("?" for _ in lineage)
    tools = ",".join("?" for _ in EVIDENCE_TOOLS)
    row = connection.execute(
        f"SELECT 1 FROM messages WHERE session_id IN ({sessions}) "
        f"AND role = 'tool' AND tool_name IN ({tools}) LIMIT 1",
        (*lineage, *EVIDENCE_TOOLS),
    ).fetchone()
    return row is not None


def _verdicts(
    connection: sqlite3.Connection, lineage: list[str], evidence: list[dict[str, str]]
) -> dict[str, str]:
    verdicts = {evidence_key(entry): EVIDENCE_UNRESOLVED for entry in evidence}
    resolvable = sorted({entry["ref"] for entry in evidence if entry["kind"] in _KIND_TOOLS})
    if not resolvable:
        return verdicts
    columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()}
    # A store older than Hermes' effect_disposition column has none to read.
    disposition = "effect_disposition" if "effect_disposition" in columns else "NULL"
    sessions = ",".join("?" for _ in lineage)
    refs = ",".join("?" for _ in resolvable)
    rows = connection.execute(
        f"SELECT tool_call_id, tool_name, content, {disposition} FROM messages "
        f"WHERE session_id IN ({sessions}) AND role = 'tool' AND tool_call_id IN ({refs}) "
        "ORDER BY id",
        (*lineage, *resolvable),
    ).fetchall()
    # A compaction re-persists a result under a new row id; the first row by
    # id decides, the rule #1922 states for the same duplication.
    first: dict[str, tuple[Any, Any, Any]] = {}
    for call_id, tool_name, content, effect in rows:
        first.setdefault(str(call_id), (tool_name, content, effect))
    for entry in evidence:
        if entry["kind"] in _KIND_TOOLS:
            verdicts[evidence_key(entry)] = _verdict(entry["kind"], first.get(entry["ref"]))
    return verdicts


def _verdict(kind: str, row: tuple[Any, Any, Any] | None) -> str:
    """One recorded result's verdict. An unknown id is `unresolved`, never `failed`."""
    if row is None:
        return EVIDENCE_UNRESOLVED
    tool_name, content, effect = row
    # A kind names which host tool may answer for it, so a `file_write`
    # pointing at a terminal call (or the reverse) is not a reference to
    # anything this kind can judge.
    if tool_name not in _KIND_TOOLS[kind]:
        return EVIDENCE_UNRESOLVED
    if effect == "none":
        return EVIDENCE_FAILED
    if effect == "unknown":
        return EVIDENCE_UNRESOLVED
    try:
        result = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        result = None
    if not isinstance(result, dict):
        return EVIDENCE_UNRESOLVED
    if result.get("error"):
        return EVIDENCE_FAILED
    if tool_name == "terminal":
        exit_code = result.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            # `None` is the yield-to-background result: the command was never
            # observed finishing, which is not the same thing as failing.
            return EVIDENCE_UNRESOLVED
        return EVIDENCE_CLOSED if exit_code == 0 else EVIDENCE_FAILED
    if tool_name == "write_file":
        return EVIDENCE_CLOSED if "bytes_written" in result else EVIDENCE_UNRESOLVED
    success = result.get("success")
    if success is True:
        return EVIDENCE_CLOSED
    return EVIDENCE_FAILED if success is False else EVIDENCE_UNRESOLVED
