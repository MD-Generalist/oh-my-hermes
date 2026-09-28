"""``agent_debug_report/v1``: cited findings for one Hermes session, read from ``state.db``.

The ``agent-debug`` skill diagnoses a stuck, looping or failing agent run, and
until this reader its report was a declared name with nothing behind it: a
plausible narrative looked the same as a diagnosis. This reader gives the
diagnosis a floor of observed rows. It opens Hermes' own session store
``mode=ro`` through ``hermes_state``, the open the reply lint and the
session-usage report share, and derives a closed set of finding kinds from
record fields only:

- ``tool_error``: a tool result whose JSON object records an error in a typed
  field -- a non-zero integer ``exit_code``, ``success`` false, or a non-empty
  string ``error``. No word of the result is matched.
- ``identical_retry_after_error``: the next call of the same tool after a
  ``tool_error`` carried byte-identical arguments (compared by the sha256 of
  the canonical JSON of the assistant row's ``tool_calls`` arguments).
- ``background_without_notify``: a tool result that records a started process
  (an integer ``pid``) with ``notify_on_complete`` not true, so nothing will
  bring its completion back into the turn.
- ``compaction_boundary``: a message row Hermes marks ``_compressed_summary``,
  the summary a compaction put in place of the rows before it.

Every finding carries a citation -- session id, message ids with their
timestamps, tool_call ids, tool name, error class, exit code, argument digest
-- and nothing else. No prompt, reply, argument or tool output is quoted.
``agent_debug_report_errors`` refuses a report whose finding lacks the
citation its kind requires, cites another session, or carries a key outside
the closed citation shape; the builder runs it on every report it returns.

A compaction re-persists rows under new ids, so a tool call is counted once
per distinct ``tool_call_id``, the first row by id deciding what it was. A
column this Hermes build does not have leaves its kind ``unavailable`` rather
than silently clean.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .hermes_state import TOOL_CALL_KEY_SQL, hermes_epoch, open_state_db_readonly, resolve_session_id


AGENT_DEBUG_REPORT_SCHEMA_VERSION = "agent_debug_report/v1"

TOOL_ERROR = "tool_error"
IDENTICAL_RETRY_AFTER_ERROR = "identical_retry_after_error"
BACKGROUND_WITHOUT_NOTIFY = "background_without_notify"
COMPACTION_BOUNDARY = "compaction_boundary"
FINDING_KINDS: tuple[str, ...] = (
    TOOL_ERROR,
    IDENTICAL_RETRY_AFTER_ERROR,
    BACKGROUND_WITHOUT_NOTIFY,
    COMPACTION_BOUNDARY,
)
ERROR_CLASSES: tuple[str, ...] = ("nonzero_exit", "success_false", "error_field")
ARGUMENT_DIGEST_CHARS = 16

CITATION_KEYS: tuple[str, ...] = (
    "session_id",
    "message_ids",
    "timestamps",
    "tool_call_ids",
    "tool_name",
    "error_class",
    "exit_code",
    "arguments_sha256",
)
# What each kind must cite beyond the session id, message ids and timestamps
# every finding carries, and how many rows it spans.
_REQUIRED_CITATION: dict[str, tuple[int, tuple[str, ...]]] = {
    TOOL_ERROR: (1, ("tool_call_ids", "tool_name", "error_class")),
    IDENTICAL_RETRY_AFTER_ERROR: (2, ("tool_call_ids", "tool_name", "error_class", "arguments_sha256")),
    BACKGROUND_WITHOUT_NOTIFY: (1, ("tool_call_ids", "tool_name")),
    COMPACTION_BOUNDARY: (1, ()),
}
_FINDING_KEYS = frozenset({"finding_id", "kind", "citation"})
_SUMMARY_COLUMN = "_compressed_summary"

AGENT_DEBUG_REPORT_CLAIM_BOUNDARY = (
    "An agent debug report cites rows Hermes persisted for one session, read mode=ro from its own "
    "session store, and quotes no prompt, reply, argument, or tool output. A finding is an observed "
    "record, not a diagnosis: it does not show why the agent acted as it did, that a retry was wrong, "
    "that a compaction lost what mattered, or that any recovery worked, and it is not execution, "
    "review, CI, or merge evidence. A kind listed as unavailable was not checked."
)


class AgentDebugReportError(ValueError):
    """The session could not be read, or a report failed validation; the message says which."""


def build_agent_debug_report(hermes_home: str | Path, session_id: str) -> dict[str, Any]:
    """Read one session (an id, or ``latest``) and return a validated ``agent_debug_report/v1``."""
    path, connection = open_state_db_readonly(hermes_home, error=AgentDebugReportError)
    try:
        resolved = resolve_session_id(connection, session_id, None, error=AgentDebugReportError)
        cursor = connection.execute("SELECT * FROM sessions WHERE id = ?", (resolved,))
        session_row = cursor.fetchone()
        if session_row is None:
            raise AgentDebugReportError(f"no Hermes session {resolved}")
        session_fields = dict(zip((column[0] for column in cursor.description), session_row))
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")}
        tool_rows = connection.execute(
            f"SELECT id, {TOOL_CALL_KEY_SQL}, tool_call_id, tool_name, content, timestamp "
            "FROM messages WHERE session_id = ? AND role = 'tool' ORDER BY id",
            (resolved,),
        ).fetchall()
        call_rows = (
            connection.execute(
                "SELECT tool_calls FROM messages WHERE session_id = ? AND role = 'assistant' "
                "AND tool_calls IS NOT NULL ORDER BY id",
                (resolved,),
            ).fetchall()
            if "tool_calls" in columns
            else None
        )
        summary_rows = (
            connection.execute(
                f"SELECT id, timestamp FROM messages WHERE session_id = ? AND {_SUMMARY_COLUMN} = 1 ORDER BY id",
                (resolved,),
            ).fetchall()
            if _SUMMARY_COLUMN in columns
            else None
        )
    except sqlite3.Error as exc:
        raise AgentDebugReportError(f"could not read {path}: {exc}") from exc
    finally:
        connection.close()

    arguments = _argument_digests(call_rows or ())
    calls = _distinct_calls(tool_rows)
    findings: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    previous_by_tool: dict[str, dict[str, Any]] = {}
    for call in calls:
        tool_call_id = call["tool_call_id"]
        if tool_call_id is None:
            continue
        result = call["result"]
        error_class, exit_code = _error_class(result)
        if error_class is not None:
            findings.append(_finding(TOOL_ERROR, resolved, [call], error_class=error_class, exit_code=exit_code))
        if _started_without_notify(result):
            findings.append(_finding(BACKGROUND_WITHOUT_NOTIFY, resolved, [call]))
        digest = arguments.get(tool_call_id)
        previous = previous_by_tool.get(call["tool_name"])
        if previous is not None and previous["error_class"] is not None and digest is not None and digest == previous["digest"]:
            findings.append(
                _finding(
                    IDENTICAL_RETRY_AFTER_ERROR,
                    resolved,
                    [previous["call"], call],
                    error_class=previous["error_class"],
                    exit_code=previous["exit_code"],
                    arguments_sha256=digest,
                )
            )
        previous_by_tool[call["tool_name"]] = {
            "call": call,
            "error_class": error_class,
            "exit_code": exit_code,
            "digest": digest,
        }
    if call_rows is None:
        unavailable.append({"kind": IDENTICAL_RETRY_AFTER_ERROR, "reason": "messages.tool_calls column not present"})
    if summary_rows is None:
        unavailable.append({"kind": COMPACTION_BOUNDARY, "reason": f"messages.{_SUMMARY_COLUMN} column not present"})
    for message_id, stamp in summary_rows or ():
        findings.append(
            _finding(COMPACTION_BOUNDARY, resolved, [{"message_id": int(message_id), "timestamp": hermes_epoch(stamp)}])
        )
    findings.sort(key=lambda item: (item["citation"]["message_ids"][0], FINDING_KINDS.index(item["kind"])))

    report = {
        "schema_version": AGENT_DEBUG_REPORT_SCHEMA_VERSION,
        "source": {"kind": "hermes_state_db", "path": str(path), "requested_session": str(session_id)},
        "session": {
            "id": resolved,
            "source": session_fields.get("source") or None,
            "started_at": hermes_epoch(session_fields.get("started_at")),
            "ended_at": hermes_epoch(session_fields.get("ended_at")),
            "end_reason": session_fields.get("end_reason") or None,
        },
        "counts": {
            "tool_calls": len(calls),
            "tool_calls_without_id": sum(1 for call in calls if call["tool_call_id"] is None),
            "tool_calls_with_arguments": sum(1 for call in calls if call["tool_call_id"] in arguments),
        },
        "checked_kinds": [kind for kind in FINDING_KINDS if kind not in {item["kind"] for item in unavailable}],
        "unavailable": unavailable,
        "findings": findings,
        "finding_counts": {kind: sum(1 for item in findings if item["kind"] == kind) for kind in FINDING_KINDS},
        "observed": True,
        "claim_boundary": AGENT_DEBUG_REPORT_CLAIM_BOUNDARY,
    }
    errors = agent_debug_report_errors(report)
    if errors:
        raise AgentDebugReportError("agent_debug_report/v1 failed validation: " + "; ".join(errors))
    return report


def agent_debug_report_errors(report: Mapping[str, Any]) -> list[str]:
    """Every reason ``report`` is not a valid ``agent_debug_report/v1``; empty when it is.

    A finding must name a kind from ``FINDING_KINDS`` and carry a citation
    with exactly ``CITATION_KEYS``: the report's own session id, one message
    id and one timestamp per row the kind spans, and the fields that kind
    requires. A key outside that shape is refused, so text cannot ride along.
    """
    errors: list[str] = []
    if report.get("schema_version") != AGENT_DEBUG_REPORT_SCHEMA_VERSION:
        errors.append(f"schema_version must be {AGENT_DEBUG_REPORT_SCHEMA_VERSION}")
    session = report.get("session")
    session_id = session.get("id") if isinstance(session, Mapping) else None
    if not isinstance(session_id, str) or not session_id:
        errors.append("session.id is missing")
    findings = report.get("findings")
    if not isinstance(findings, list):
        return errors + ["findings must be a list"]
    seen_ids: set[str] = set()
    for index, finding in enumerate(findings):
        label = f"finding {index}"
        if not isinstance(finding, Mapping):
            errors.append(f"{label} is not an object")
            continue
        extra = sorted(set(finding) - _FINDING_KEYS)
        if extra:
            errors.append(f"{label} carries keys outside the finding shape: {', '.join(extra)}")
        kind = finding.get("kind")
        if kind not in _REQUIRED_CITATION:
            errors.append(f"{label} has unknown kind {kind!r}")
            continue
        citation = finding.get("citation")
        if not isinstance(citation, Mapping):
            errors.append(f"{label} ({kind}) has no citation")
            continue
        errors.extend(f"{label} ({kind}) {problem}" for problem in _citation_errors(kind, citation, session_id))
        finding_id = finding.get("finding_id")
        message_ids = citation.get("message_ids")
        first = message_ids[0] if isinstance(message_ids, list) and message_ids else None
        if finding_id != f"{kind}:{first}":
            errors.append(f"{label} ({kind}) finding_id must be {kind}:<first cited message id>")
        elif finding_id in seen_ids:
            errors.append(f"{label} ({kind}) repeats finding_id {finding_id}")
        seen_ids.add(str(finding_id))
    return errors


def format_agent_debug_report(report: Mapping[str, Any]) -> str:
    """Plain-text rendering: the session, one line per finding, unavailable kinds, the boundary."""
    session = report.get("session") or {}
    counts = report.get("counts") or {}
    findings = list(report.get("findings") or ())
    out = [f"OMH agent debug report: session {session.get('id')} (source {session.get('source') or '(none)'})"]
    out.append(
        f"  started {_stamp(session.get('started_at'))}    ended {_stamp(session.get('ended_at'))}"
        f"    end reason {session.get('end_reason') or '(none)'}"
    )
    out.append(
        f"  tool calls {int(counts.get('tool_calls', 0))} (distinct tool_call_id)    findings {len(findings)}"
    )
    out.append("Findings")
    if not findings:
        out.append("  none of the checked kinds")
    for finding in findings:
        citation = finding.get("citation") or {}
        parts = [
            str(finding.get("finding_id")),
            "messages " + ",".join(str(item) for item in citation.get("message_ids") or ()),
            "at " + ",".join(_stamp(item) for item in citation.get("timestamps") or ()),
        ]
        if citation.get("tool_call_ids"):
            parts.append("calls " + ",".join(str(item) for item in citation["tool_call_ids"]))
        if citation.get("tool_name"):
            parts.append(f"tool {citation['tool_name']}")
        if citation.get("error_class"):
            parts.append(str(citation["error_class"]))
        if citation.get("exit_code") is not None:
            parts.append(f"exit {citation['exit_code']}")
        if citation.get("arguments_sha256"):
            parts.append(f"args sha256 {citation['arguments_sha256']}")
        out.append("  " + "  ".join(parts))
    unavailable = list(report.get("unavailable") or ())
    if unavailable:
        out.append("Unavailable")
        out.extend(f"  {item.get('kind')}: {item.get('reason')}" for item in unavailable)
    out.append("Boundary")
    out.append(f"  {report.get('claim_boundary', AGENT_DEBUG_REPORT_CLAIM_BOUNDARY)}")
    return "\n".join(out)


def _distinct_calls(tool_rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """One entry per distinct call key, the first row by id deciding what the call was."""
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message_id, call_key, tool_call_id, tool_name, content, stamp in tool_rows:
        if str(call_key) in seen:
            continue
        seen.add(str(call_key))
        calls.append(
            {
                "message_id": int(message_id),
                "timestamp": hermes_epoch(stamp),
                "tool_call_id": str(tool_call_id) if tool_call_id else None,
                "tool_name": str(tool_name or ""),
                "result": _json_object(content),
            }
        )
    return calls


def _argument_digests(call_rows: Any) -> dict[str, str]:
    """``tool_call_id -> sha256 prefix`` of each call's canonical arguments, first row deciding."""
    digests: dict[str, str] = {}
    for (raw,) in call_rows:
        try:
            calls = json.loads(raw or "")
        except ValueError:
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or call["id"] in digests:
                continue
            function = call.get("function")
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(arguments, str):
                continue
            try:
                canonical = json.dumps(json.loads(arguments), sort_keys=True, separators=(",", ":"))
            except ValueError:
                canonical = arguments
            digests[call["id"]] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:ARGUMENT_DIGEST_CHARS]
    return digests


def _json_object(content: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(content or ""))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _error_class(result: Mapping[str, Any] | None) -> tuple[str | None, int | None]:
    """The error class a result's typed fields record, and its integer exit code."""
    if result is None:
        return None, None
    exit_code = result.get("exit_code")
    exit_code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
    if exit_code is not None and exit_code != 0:
        return "nonzero_exit", exit_code
    if result.get("success") is False:
        return "success_false", exit_code
    error = result.get("error")
    if isinstance(error, str) and error.strip():
        return "error_field", exit_code
    return None, exit_code


def _started_without_notify(result: Mapping[str, Any] | None) -> bool:
    if result is None:
        return False
    pid = result.get("pid")
    started = isinstance(pid, int) and not isinstance(pid, bool)
    return started and result.get("notify_on_complete") is not True


def _finding(
    kind: str,
    session_id: str,
    rows: list[Mapping[str, Any]],
    *,
    error_class: str | None = None,
    exit_code: int | None = None,
    arguments_sha256: str | None = None,
) -> dict[str, Any]:
    tool_call_ids = [str(row["tool_call_id"]) for row in rows if row.get("tool_call_id")]
    return {
        "finding_id": f"{kind}:{rows[0]['message_id']}",
        "kind": kind,
        "citation": {
            "session_id": session_id,
            "message_ids": [int(row["message_id"]) for row in rows],
            "timestamps": [row.get("timestamp") for row in rows],
            "tool_call_ids": tool_call_ids,
            "tool_name": rows[0].get("tool_name") or None,
            "error_class": error_class,
            "exit_code": exit_code,
            "arguments_sha256": arguments_sha256,
        },
    }


def _citation_errors(kind: str, citation: Mapping[str, Any], session_id: Any) -> list[str]:
    errors: list[str] = []
    if set(citation) != set(CITATION_KEYS):
        missing = sorted(set(CITATION_KEYS) - set(citation))
        extra = sorted(set(citation) - set(CITATION_KEYS))
        if missing:
            errors.append(f"citation is missing {', '.join(missing)}")
        if extra:
            errors.append(f"citation carries keys outside the citation shape: {', '.join(extra)}")
    if citation.get("session_id") != session_id:
        errors.append("citation names another session")
    rows, required = _REQUIRED_CITATION[kind]
    message_ids = citation.get("message_ids")
    if not (
        isinstance(message_ids, list)
        and len(message_ids) == rows
        and all(isinstance(item, int) and not isinstance(item, bool) for item in message_ids)
    ):
        errors.append(f"citation must cite {rows} message id{'s' if rows != 1 else ''}")
    timestamps = citation.get("timestamps")
    if not (
        isinstance(timestamps, list)
        and len(timestamps) == rows
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in timestamps)
    ):
        errors.append(f"citation must carry {rows} timestamp{'s' if rows != 1 else ''}")
    tool_call_ids = citation.get("tool_call_ids")
    if "tool_call_ids" in required:
        if not (
            isinstance(tool_call_ids, list)
            and len(tool_call_ids) == rows
            and all(isinstance(item, str) and item for item in tool_call_ids)
        ):
            errors.append(f"citation must cite {rows} tool_call id{'s' if rows != 1 else ''}")
    elif tool_call_ids:
        errors.append("citation cites tool_call ids this kind does not span")
    if "tool_name" in required and not (isinstance(citation.get("tool_name"), str) and citation["tool_name"]):
        errors.append("citation must name the tool")
    if "error_class" in required and citation.get("error_class") not in ERROR_CLASSES:
        errors.append(f"citation must carry an error_class from {', '.join(ERROR_CLASSES)}")
    if "arguments_sha256" in required:
        digest = citation.get("arguments_sha256")
        if not (isinstance(digest, str) and len(digest) == ARGUMENT_DIGEST_CHARS and all(c in "0123456789abcdef" for c in digest)):
            errors.append("citation must carry the arguments sha256 prefix")
    exit_code = citation.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        errors.append("citation exit_code must be an integer or null")
    return errors


def _stamp(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "(unknown)"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
