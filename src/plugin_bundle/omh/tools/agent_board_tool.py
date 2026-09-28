"""Prepare/status only. Native tools are invoked exclusively by Hermes' loop."""
from __future__ import annotations

from collections.abc import Mapping
import json

from ..orchestration_say import board_say, with_say

OMH_AGENT_BOARD_SCHEMA = {
    "name": "omh_agent_board",
    "description": "Prepare a durable native Kanban action or inspect its metadata-only receipt. Preparation is not authorization or execution; invoke the returned native tool through Hermes' normal tool loop. A durable create may state lane_role (builder, verifier, reviewer, docs or qa): OMH fills that lane's skills and workspace_kind from the role, refuses a verifier or reviewer that declares no parents, and removes the role before the native action. Bounded child research uses delegation instead. No uploads, downloads, dispatch or JSON grants. Relay any `say` field to the user once, in their language and your own words.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["prepare", "status"]},
            "request_id": {"type": "string"},
            "coordination": {"type": "string", "enum": ["durable", "bounded_research"]},
            "operation": {"type": "string"},
            "board": {"type": "string"},
            "profile": {"type": "string"},
            "arguments": {"type": "object"},
            "task_id": {"type": "string"},
            "expected_observation_ref": {"type": "string"},
        },
        "required": ["action", "request_id"],
        "additionalProperties": False,
    },
}


def omh_agent_board_handler(args: Mapping[str, object], **kwargs: object) -> str:
    # Lazy import permits a standalone bundle without the OMH command package
    # to register and report the exact missing component, not disappear. The
    # catch is `ImportError`, not `ModuleNotFoundError` with a name check: a
    # bundle module Hermes could not exec stays cached as a stub, and the
    # import then fails on the NAME with the bundle's own dotted path rather
    # than on `omh` (#1623). Either way this host has no board engine, which
    # is what the reason below says.
    try:
        from ..agent_board_bridge import (
            BoardCoreUnavailable, handler_identity, host_capabilities, installed_bridge, installed_status,
        )
    except ImportError:
        return _unavailable("omh_agent_board_core_unavailable")
    try:
        identity = handler_identity(args, kwargs)
        if args.get("action") == "status":
            if set(args) != {"action", "request_id"} or not isinstance(args.get("request_id"), str):
                return _unavailable("invalid_status_input")
            return json.dumps(installed_status(str(args["request_id"])), sort_keys=True)
        board = args.get("board")
        if not isinstance(board, str):
            return _unavailable("board_required")
        bridge = installed_bridge(board)
        schemas, hooks = host_capabilities()
        prepared = bridge.prepare(args, host=identity, schemas=schemas, hooks=hooks)
        return json.dumps(with_say(dict(prepared), board_say(prepared)), sort_keys=True)
    except BoardCoreUnavailable:
        # The module imported, but this host cannot import the engine behind
        # it; that is a missing component, not an invalid request.
        return _unavailable("omh_agent_board_core_unavailable")
    except (ValueError, OSError):
        # Never echo exception strings, arguments, root paths or native output.
        return _unavailable("invalid_request_or_board_store")


def _unavailable(reason: str) -> str:
    return json.dumps({"schema_version": "agent_board_tool_result/v1", "state": "unavailable",
                       "reason": reason, "native_action": None, "observed_receipts": [],
                       "claim_boundary": "Unavailable preparation is not authorization or execution."}, sort_keys=True)
