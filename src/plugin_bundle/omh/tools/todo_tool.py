from __future__ import annotations

from .. import runtime_paths

import json
from typing import Any

from ..host_observation import (
    OBSERVATION_SCHEMA,
    attach_public_observation,
    host_session_id,
    observe_plugin_tool_call,
)
from ..dispatch_outcomes import _parse_timestamp
from ..runtime_reader import default_omh_home, read_omh_todo
from ..todo_evidence import MAX_BOUND_CALLS, evidence_key, observed_calls, valid_evidence
from ..todo_reconciliation import unverified_done_items
from ..todo_store import (
    TODO_CLAIM_BOUNDARY,
    TODO_ITEM_STATES,
    TODO_PLAN_STAGES,
    TodoContendedError,
    TodoStoreError,
    TodoValidationError,
    advance_todo_item,
    bind_done_items,
    build_todo_record,
    clear_todo,
    read_todo_record,
    todo_timestamp,
    write_todo,
)
from ..todo_templates import CODE_STORY_TEMPLATE
from ..completion_store import completion_action

_COMPLETION_FIELDS = {
    "checkpoint_id": {"type": "string", "description": "ID returned by checkpoint; recall without it lists this profile/project's dossiers."},
    "accepted": {"type": "boolean", "description": "checkpoint only: the person accepted exactly the current todo scope. Not a host approval or permission grant."},
    "rejected": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                 "description": "checkpoint: short summaries of rejected ideas, kept outside accepted scope; no transcript."},
    "revision": {"type": "string", "maxLength": 128,
                 "description": "Required for checkpoint, record and keyed recall: exact revision/worktree fingerprint claimed or checked. Caller-declared, not host-attested."},
    "environment": {"type": "string", "maxLength": 128,
                    "description": "Required with revision: environment/toolchain fingerprint; a change makes old results stale. Never environment values or secrets."},
    "result": {
        "type": "object", "additionalProperties": False,
        "description": "record: one verification verdict, review finding set or QA result for one frozen item. No logs, prompts, transcripts or raw command output; provenance is claimed, never attested by storage.",
        "properties": {
            "kind": {"type": "string", "enum": ["verification", "review", "qa"]},
            "item": {"type": "integer", "minimum": 1},
            "verdict": {"type": "string", "enum": ["PASS", "HOLD", "BLOCK"]},
            "summary": {"type": "string", "maxLength": 200},
            "findings": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 20},
            "claimed_source": {"type": "string", "enum": ["model", "host_exit", "independent_review", "ci"]},
            "claimed_evidence_state": {"type": "string", "enum": ["prepared_not_observed", "observed"]},
            "references": {"type": "array", "maxItems": 8, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"type": {"type": "string", "enum": ["verification_receipt/v1", "artifact", "ci_run"]},
                               "id": {"type": "string", "maxLength": 128}},
                "required": ["type", "id"]}},
        },
        "required": ["kind", "item", "verdict", "summary", "findings", "claimed_source", "claimed_evidence_state", "references"],
    },
}

# Longer guidance for this tool lives in the `todo-checklist` skill's
# references: `checklist-discipline.md` (phases, outcomes, blocked_reason vs
# deferred_reason, advance vs set) and `closing-a-story.md` (checkpoint,
# record, recall). This schema is sent with every request that carries the
# tool, so it keeps the rules a model needs to drive the list to its stop
# without that skill loaded, and not the reasons behind them.
OMH_TODO_SCHEMA = {
    "name": "omh_todo",
    "description": (
        "Declare, advance, clear, or read this session's metadata-only plan todo list, which the OMH HUD "
        "renders above the prompt; other sessions neither see nor overwrite it. "
        "Declare it BEFORE starting engine work (todo init): numbered phases in delivery order covering setup, "
        "implement/verify/deliver per work unit, independent review, and an evidence-and-cleanup close, "
        "one item per observable outcome. Keep exactly one item active and advance it as work completes. "
        "Items are plan declarations, never execution evidence. "
        "To finish or resume accepted work, read the plan and recall its checkpoint; do exactly the "
        "accepted items, never rejected ideas. No action executes work or grants approval; templates are "
        "optional and tiny tasks need fewer phases."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_COMPLETION_FIELDS,
            "action": {
                "type": "string",
                "enum": ["set", "advance", "clear", "show", "checkpoint", "record", "recall"],
                "description": (
                    "set writes the whole list: items left out are dropped. advance changes one item; "
                    "clear removes the list; show reads it. Change a state with advance, not set. "
                    "checkpoint freezes the accepted scope; record appends a result declaration; "
                    "recall reads scope and declarations across sessions without resuming work, "
                    "and comes before reporting completion."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional plan title for the todo panel header.",
            },
            "template": {
                "type": "string",
                "enum": [CODE_STORY_TEMPLATE],
                "description": (
                    "For action=set: 'code-story' is the ten phases I. Story through X. Close, "
                    "for a change the person asks to carry from story to close. With no items the "
                    "ten are declared for you. Re-send it on a later set: every write must cover "
                    "all ten, and an unneeded phase is kept as state=done with a blocked_reason, "
                    "never dropped. "
                    "Omit this field for an ordinary plan."
                ),
            },
            "plan_stage": {
                "type": "string",
                "enum": list(TODO_PLAN_STAGES),
                "description": (
                    "For action=set on a PLANNING run (ralplan, plan, deep-interview) only. "
                    "'awaiting_acceptance' when declaring the planning checklist: while it holds, "
                    "write_file or patch in this session goes to the human-approval gate. "
                    "'accepted' once the person gives an explicit go-ahead in this conversation, "
                    "ending that. Omit it for a delivery or execution plan, which is never gated. "
                    "advance carries it forward; a set without it drops it."
                ),
            },
            "deferred_reason": {
                "type": "string",
                "description": (
                    "Omit this field unless the PERSON redirected this session away from the plan "
                    "('do Y first', 'forget that for now'); name what they asked for instead. "
                    "Sent with set or advance. While it holds, the plan stops asking you to advance "
                    "the next item. It CLEARS ITSELF: it is bound to the item list sent with it, so "
                    "the next write that omits it, or any item change that does not re-send it, ends "
                    "it. Re-send it with a changed list only while the person is still steering. "
                    "It is not blocked_reason, which is per item for work that CANNOT proceed and "
                    "wins when both apply."
                ),
            },
            "item": {
                "type": "integer",
                "minimum": 1,
                "description": "For action=advance: which item to change, 1 for the first.",
            },
            "item_text": {
                "type": "string",
                "description": (
                    "For action=advance: the start of that item's current text; a mismatch "
                    "(stale index) is refused."
                ),
            },
            "state": {
                "type": "string",
                "enum": list(TODO_ITEM_STATES),
                "description": "For action=advance: the item's new state.",
            },
            "blocked_reason": {
                "type": "string",
                "description": (
                    "For action=advance: items[].blocked_reason for the changed item; omitting it "
                    "clears one."
                ),
            },
            "items": {
                "type": "array",
                "description": "Todo items for action=set, in display order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Item text."},
                        "state": {
                            "type": "string",
                            "enum": ["pending", "active", "done"],
                            "description": "Defaults to pending.",
                        },
                        "phase": {
                            "type": "string",
                            "description": (
                                "Optional phase label numbered in delivery order, e.g. 'I. Bootstrap'. "
                                "Items sharing a phase render as one section."
                            ),
                        },
                        "blocked_reason": {
                            "type": "string",
                            "description": (
                                "Omit this field unless the item CANNOT proceed; name what it waits on "
                                "(a review, an approval, a missing credential, another item). Any value "
                                "stops the plan advancing past this item, so an unstarted, slow or "
                                "mid-work item carries none, nor one whose text merely discusses "
                                "blocking. Remove it once the thing it names arrives."
                            ),
                        },
                        "depth": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 3,
                            "description": (
                                "Optional nesting: 0 = top-level; 1-3 render as subtasks under the "
                                "preceding shallower item and may omit phase."
                            ),
                        },
                    },
                    "required": ["text"],
                },
            },
            "omh_home": {
                "type": "string",
                "description": "Standalone operator override for action=show only; writes reject it. Native Hermes calls reject this field; omit it to use the active profile.",
            },
            "observation": OBSERVATION_SCHEMA,
        },
        "required": ["action"],
    },
}


def omh_todo_handler(args: dict[str, Any], **kwargs) -> str:
    if error := runtime_paths.tool_home_error(args):
        return json.dumps(error, sort_keys=True)
    observation = observe_plugin_tool_call("omh_todo", args, kwargs)
    # Hermes passes its stable session/thread id as a keyword on every tool
    # call, so a plan declared in chat is stored for, and read back for, the
    # session that declared it. A host that supplies none leaves this empty:
    # the record is the home-wide one, exactly like a CLI write.
    session_ref = host_session_id(kwargs)
    home_arg = str(args.get("omh_home", "") or "")
    action = str(args.get("action", ""))
    if action in {"checkpoint", "record", "recall"}:
        return json.dumps(attach_public_observation(
            completion_action(args, session=session_ref), observation), sort_keys=True)
    payload: dict[str, Any] = {
        "schema_version": "omh_todo_result/v1",
        "action": action,
        "claim_boundary": TODO_CLAIM_BOUNDARY,
    }
    # Mutations bind to the environment-configured home only: a caller-chosen
    # path would turn this metadata tool into an arbitrary-location
    # file-create/delete primitive.
    if action in {"set", "advance", "clear"} and home_arg:
        payload["status"] = "invalid_todo"
        payload["error"] = "omh_home override is read-only; set and clear use the configured OMH home"
        payload["todo"] = read_omh_todo(session_ref=session_ref)
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if action == "set":
        try:
            items = args.get("items")
            if isinstance(items, list):
                # OMH binds done items itself and ignores any binding the
                # writer sent (`bind_done_items`). A first declaration's
                # window opens at this write, so nothing the session ran
                # before the plan existed can close one of its items.
                prior = read_todo_record(default_omh_home(), session_ref) or {}
                now = todo_timestamp()
                prior_stamp = prior.get("updated_at")
                window_start = (
                    prior_stamp
                    if isinstance(prior_stamp, str) and _parse_timestamp(prior_stamp)
                    else now
                )
                calls = (
                    _calls_since(session_ref, window_start, _held_keys(prior.get("items")))
                    if window_start != now and _declares_done(items)
                    else []
                )
                items = bind_done_items(
                    items,
                    prior_items=prior.get("items"),
                    calls=calls,
                    window_start=window_start,
                    done_at=now,
                )
            record = build_todo_record(
                args.get("title", ""),
                items,
                source="omh_todo",
                session_ref=session_ref,
                deferred_reason=args.get("deferred_reason", ""),
                template=args.get("template", ""),
                plan_stage=args.get("plan_stage", ""),
            )
            write_todo(default_omh_home(), record)
            payload["status"] = "written"
        # Before the generic branch, and it is not a nicety. `invalid_todo`
        # tells a writer its payload was wrong, and a writer that believes
        # that rewrites the list it just sent -- the whole-list rewrite this
        # action exists to stop -- when the record is simply busy for a few
        # milliseconds and the same call would land.
        except TodoContendedError as error:
            payload["status"] = "contended"
            payload["error"] = str(error)
        except (TodoValidationError, TodoStoreError) as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "advance":
        # Same store call, same validator, same stamp as `set` above: the
        # single-item path is a narrower way to reach one write, never a
        # second way to write a record.
        try:
            _ = advance_todo_item(
                default_omh_home(),
                item=args.get("item"),
                item_text=args.get("item_text", ""),
                state=args.get("state", ""),
                source="omh_todo",
                session_ref=session_ref,
                blocked_reason=args.get("blocked_reason", ""),
                deferred_reason=args.get("deferred_reason", ""),
                observed_calls=lambda stamp, held: _calls_since(session_ref, stamp, held),
            )
            payload["status"] = "written"
        except TodoContendedError as error:
            payload["status"] = "contended"
            payload["error"] = str(error)
        except (TodoValidationError, TodoStoreError) as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "clear":
        try:
            payload["status"] = (
                "cleared" if clear_todo(default_omh_home(), session_ref) else "already_absent"
            )
        except TodoStoreError as error:
            payload["status"] = "invalid_todo"
            payload["error"] = str(error)
    elif action == "show":
        payload["status"] = "read"
    else:
        payload["status"] = "invalid_action"
        payload["error"] = 'action must be set, advance, clear, show, checkpoint, record, or recall'
    payload["todo"] = read_omh_todo(runtime_paths.plugin_home(home_arg), session_ref=session_ref)
    # What the stop criterion reads for this plan, returned where the writer
    # can act on it: each done item no recorded fact closes, with the reason.
    # Omitted when there is none, so a plan without one reads as it did.
    unverified = _unverified(payload["todo"], session_ref)
    if unverified:
        payload["done_unverified"] = unverified
    return json.dumps(attach_public_observation(payload, observation), sort_keys=True)


def _declares_done(items: object) -> bool:
    return isinstance(items, list) and any(
        isinstance(item, dict) and item.get("state") == "done" for item in items
    )


def _calls_since(session_ref: str, stamp: object, held: set[str]) -> list[dict[str, str]]:
    """The calls this session recorded after ``stamp`` that no item holds, oldest first.

    An unparseable ``stamp`` is no window at all and binds nothing. A host
    that cannot bind its home has no records to name, which is the same
    answer as a session that ran nothing.
    """
    parsed = _parse_timestamp(stamp) if isinstance(stamp, str) and stamp else None
    if parsed is None:
        return []
    try:
        hermes_home = runtime_paths.plugin_home(None, hermes=True)
    except (runtime_paths.RuntimeBindingError, OSError, RuntimeError):
        return []
    return observed_calls(
        hermes_home, session_ref, after_epoch=parsed.timestamp(), exclude=held, limit=MAX_BOUND_CALLS
    )


def _held_keys(items: object) -> set[str]:
    held: set[str] = set()
    for item in items if isinstance(items, list) else []:
        evidence = valid_evidence(item.get("evidence")) if isinstance(item, dict) else None
        if evidence is not None:
            held.add(evidence_key(evidence))
    return held


def _unverified(todo: object, session_ref: str) -> list[dict[str, Any]]:
    try:
        hermes_home = str(runtime_paths.plugin_home(None, hermes=True))
    except (runtime_paths.RuntimeBindingError, OSError, RuntimeError):
        hermes_home = ""
    if not isinstance(todo, dict):
        return []
    return [
        {key: entry[key] for key in ("item", "state", "reason")}
        for entry in unverified_done_items(todo, hermes_home=hermes_home, session_ref=session_ref)
    ]
