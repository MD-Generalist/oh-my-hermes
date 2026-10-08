"""`omh_team`: split accepted work into helper parts that count as done only on a passing check.

A separate tool from `omh_agent_board` on purpose. That tool prepares and
never runs anything, and its description says so; this one runs each part's
frozen check command. Keeping them apart keeps both descriptions true and
leaves `omh_agent_board` byte-identical (`tests/fixtures/agent_board_golden.json`).

The engine is `omh.workflows.team`, imported inside the handler with the
#1623 guard: Hermes execs this directory with its own interpreter, and a
module-level `omh` import would turn "this host has no team engine" into an
ImportError on every call.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
from typing import Any

from .. import runtime_paths
from ..host_observation import host_session_id

KANBAN_TASK_ENV = "HERMES_KANBAN_TASK"
# Hermes marks a cron run with this; its approval can be an automatic `approve`.
CRON_SESSION_ENV = "HERMES_CRON_SESSION"
_OUTPUT_TAIL_CHARS = 4000

# Process memory, never disk (`omh.workflows.team.TeamContext`): the exact
# command lists a person approved through the host gate since this Hermes
# started, keyed (durable session, team_id, commands_digest), and the nonce of
# every check this process ran. Only a successful `team_start` below adds to
# the first, and the host runs that handler only after its approval gate.
_APPROVED_TEAMS: set[tuple[str, str, str]] = set()
_OBSERVED_CHECKS: set[str] = set()

OMH_TEAM_SCHEMA = {
    "name": "omh_team",
    "description": (
        "Run an accepted plan as parallel helper parts that each count as done only when OMH runs "
        "that part's check command itself and it exits 0. team_start needs the accepted plan's "
        "plan_ref and each check command written in that plan as check: `<command>`; the person approves the "
        "command list. Dispatch the returned delegate_task entries unchanged, call team_reconcile "
        "after helpers return, and follow its fix-up entries until team_state is done or blocked. "
        "Relay each say line; events newer than since_seq describe each helper's progress."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["team_start", "team_reconcile", "team_status"]},
            "team_id": {"type": "string"},
            "plan_ref": {"type": "string"},
            "units": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "unit_id": {"type": "string"},
                        "title": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "verification_command": {"type": "string"},
                    },
                    "required": ["unit_id", "title", "verification_command"],
                },
            },
            "max_repair_attempts": {"type": "integer"},
            "since_seq": {"type": "integer"},
        },
        "required": ["action", "team_id"],
        "additionalProperties": False,
    },
}

_ACTIONS = ("team_start", "team_reconcile", "team_status")


def omh_team_handler(args: Mapping[str, object], **kwargs: object) -> str:
    action = str(args.get("action", ""))
    if action not in _ACTIONS:
        return _refused(action, "invalid_action", "Use team_start, team_reconcile or team_status.")
    if os.environ.get(KANBAN_TASK_ENV, "").strip():
        # A board worker's own session: it must never accept its own work (R8).
        return _refused(action, "called_from_board_worker", "A board worker cannot start or check a team.")
    try:
        from omh.workflows import team
    except ImportError:
        return _refused(action, "omh_team_core_unavailable", "This install has no team engine.")
    if _BUNDLE_IMPORT_FAILURE:
        return _refused(action, "omh_team_core_unavailable", "This install has no team engine.",
                        detail=f"bundle_module_unimportable:{_BUNDLE_IMPORT_FAILURE}")
    # A sibling Hermes could not exec can sit in sys.modules as a stub with
    # none of its names (#1623): a missing component, not an AttributeError.
    try:
        session_is_delegated = _nudge_budget.session_is_delegated
        read_omh_todo = _runtime_reader.read_omh_todo
        reading_session_id = _runtime_reader.reading_session_id
        todo_items_digest = _todo_store.todo_items_digest
        _ = (_evidence_tool.run_verification_command, _cost_receipt.build_cost_receipt)
    except AttributeError:
        return _refused(action, "omh_team_core_unavailable", "This install has no team engine.",
                        detail="bundle_module_incomplete")
    try:
        session = host_session_id(dict(kwargs))
        hermes_home = runtime_paths.default_hermes_home()
        omh_home = runtime_paths.default_omh_home()
        if session and session_is_delegated(session, omh_home=str(runtime_paths.plugin_home(None))):
            # The host reported this session as a delegate child: a helper
            # must not reconcile, and so accept, its own part (R8).
            return _refused(action, "called_from_helper", "A helper cannot start or check its own team.")
        durable = reading_session_id(hermes_home, session)
        if not durable:
            return _refused(action, "session_required", "A team needs a named chat session.")
        ctx = team.TeamContext(
            omh_home=omh_home, session_ref=durable, runner=_runner(team),
            fingerprint=_fingerprint, now=_now, host_guard=_host_floor,
            approved=_APPROVED_TEAMS, observed_checks=_OBSERVED_CHECKS,
        )
        if action == "team_start" and _cron_session():
            # A cron run's approval can be answered by `approve` mode with no
            # person reading the command list.
            return _refused(action, "called_from_cron", "A scheduled run cannot start a team; start it in a chat.")
        if action == "team_start":
            plan = dict(read_omh_todo(omh_home, hermes_home, session_ref=session))
            plan["items_digest"] = todo_items_digest(plan.get("items"))
            try:
                result = team.team_start(
                    ctx, team_id=args.get("team_id"), units=args.get("units"), plan=plan,
                    plan_ref=args.get("plan_ref"), workdir=runtime_paths.runtime_cwd(),
                    max_repair_attempts=args.get("max_repair_attempts"),
                )
            except team.TeamRefusal as refusal:
                payload = team.refusal_result(action, refusal)
                if refusal.reason == "plan_ref_mismatch" and plan.get("own_record") and plan.get("plan_stage") == "accepted":
                    # The accepted plan's reference, so the caller can bind to
                    # the plan the person accepted. Approval still rests on the
                    # verbatim command match and the person's own prompt.
                    payload["plan_ref"] = plan["items_digest"]
                return json.dumps(payload, sort_keys=True)
            _APPROVED_TEAMS.add((durable, str(result["team_id"]), str(result["commands_digest"])))
        elif action == "team_reconcile":
            result = team.team_reconcile(ctx, team_id=args.get("team_id"), since_seq=args.get("since_seq", 0))
        else:
            result = team.team_status(ctx, team_id=args.get("team_id"), cost=_cost(hermes_home, omh_home, durable),
                                      since_seq=args.get("since_seq", 0))
        return json.dumps(result, sort_keys=True)
    except team.TeamRefusal as refusal:
        return json.dumps(team.refusal_result(action, refusal), sort_keys=True)
    except (runtime_paths.RuntimeBindingError, OSError, ValueError):
        # Never echo exception strings, paths or command output.
        return _refused(action, "team_store_unavailable", "The team record could not be reached.")
    except (KeyError, TypeError):
        # A record that passed the shape check yet misses a field it is read by.
        return _refused(action, "team_store_unreadable", "The team record could not be read.")


def _host_floor(command: str) -> str | None:
    """Hermes' own command floor and the person's `approvals.deny`, or a refusal.

    The plugin runs inside the Hermes process, so `tools.approval` is the
    host's own module: its hardline floor ("never, even under yolo") and the
    operator's deny rules. Without it there is no second layer, and no team
    command is allowed at all.
    """
    try:
        from tools.approval import _floor_block, _user_deny_block
    except ImportError:
        return "host_command_floor_unavailable"
    try:
        if _floor_block(command) is not None or _user_deny_block(command) is not None:
            return "command_refused_by_host"
    except Exception:
        # A floor that raises has not said yes: fail closed, reason only.
        return "host_command_floor_unavailable"
    return None


def _cron_session() -> bool:
    try:
        from tools.approval_context import _is_cron_approval_context
    except ImportError:
        return os.environ.get(CRON_SESSION_ENV, "").strip().lower() in ("1", "true", "yes", "on")
    return bool(_is_cron_approval_context())


def _runner(team: Any) -> Any:
    run_verification_command = _evidence_tool.run_verification_command

    def run(tokens: list[str], workdir: Path, timeout: int) -> Any:
        observed = run_verification_command(tokens, workdir=workdir, timeout=timeout)
        output = str(observed.get("output", ""))
        return team.CheckRun(
            outcome=str(observed["outcome"]),
            exit_code=observed["exit_code"] if isinstance(observed["exit_code"], int) else None,
            output_tail=output[-_OUTPUT_TAIL_CHARS:],
        )

    return run


def _fingerprint(workdir: Path) -> tuple[str, str | None]:
    from omh.quality.working_tree_fingerprint import working_tree_content_fingerprint

    observed = working_tree_content_fingerprint(workdir)
    return (str(observed.state.value), observed.fingerprint)


def _now() -> float:
    import time

    return time.time()


def _cost(hermes_home: Path, omh_home: Path, session: str) -> dict[str, Any]:
    receipt = _cost_receipt.build_cost_receipt(hermes_home=hermes_home, omh_home=omh_home, session_id=session)
    if receipt.get("status") != "observed":
        return {"status": "not_observed", "say": "Cost for this conversation is not available."}
    helpers = (receipt.get("sources") or {}).get("delegated_children") or {}
    total = receipt.get("observed_cost_usd")
    return {
        "status": "observed",
        "conversation_cost_usd": total,
        "helpers_cost_usd": helpers.get("cost_usd"),
        "unpriced_tokens": receipt.get("unpriced_tokens"),
        "say": ("Cost so far for this whole conversation, helpers included: "
                + (f"${float(total):,.4f}." if total is not None else "no priced usage recorded yet.")),
    }


def _refused(action: str, reason: str, say: str, *, detail: str = "") -> str:
    payload: dict[str, object] = {"schema_version": "omh_team_result/v1", "action": action, "status": "refused",
                                  "reason": reason, "say": say, "delegate_task": None}
    if detail:
        # Which bundle module failed to import: a module name, never a message or a path.
        payload["detail"] = detail
    return json.dumps(payload, sort_keys=True)


# Module scope, not call time: Hermes can evict the bundle from sys.modules
# while this handler stays registered, and a call-time relative import then
# has no parent package (#1979). Guarded so a host missing one still registers
# the tool and names the module instead of failing to load (#1623). Last, so
# an import cycle finds every name.
try:
    from .. import cost_receipt as _cost_receipt
    from .. import runtime_reader as _runtime_reader
    from .. import todo_store as _todo_store
    from ..hooks import nudge_budget as _nudge_budget
    from . import evidence_tool as _evidence_tool
except ImportError as _bundle_import_error:
    _BUNDLE_IMPORT_FAILURE = _bundle_import_error.name or "unknown"
else:
    _BUNDLE_IMPORT_FAILURE = ""
