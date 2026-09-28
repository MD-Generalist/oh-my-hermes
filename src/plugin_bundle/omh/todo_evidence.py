"""Whether a plan item marked done is closed by a recorded fact.

A done mark on the plan todo is a declaration: it says done was claimed, not
that anything ran. The continuation rule stops a plan when every item is done,
so a run that marked an item done over a failed command, or ticked several
items off one command, stopped its own loop without the work behind it. This
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
``unknown`` an unknown, the host's own two dispositions. A row Hermes rewound
out of the transcript (``active = 0`` and not ``compacted``) is not a record
of anything the session still stands on, so no query here reads one.

``pr``, ``ci_run`` and ``team_check`` are accepted and stored so every lane
writes one vocabulary, and none of them resolves yet: OMH makes no network
call, so it holds no record of a PR or a CI run, and the teammate lane that
will own ``team_check`` (``<team_id>/<unit_id>/attempt-<n>/check``) has not
landed. An unresolved reference keeps the item ``done_unverified``; a PR or
a CI run closes an item today through the ``tool_call`` that observed it --
``gh pr create``, ``gh run watch --exit-status``.

Who names the call. Models do not reliably see tool call ids -- no assistant
reply in the owner's session store quoted a ``toolu_`` id (measured
2026-09-28) -- and a reference a writer could send would be a reference it
could copy, so the ``omh_todo`` tool binds it from the records at the moment
of the done write and ignores any it is sent. Each item marked done gets the
item's WINDOW -- from the plan's previous write to the done write -- and at
most one evidence-capable call recorded inside it that no other item holds
(`observed_calls`). That is association by time, not by content, and it is
stated as such: it proves a command ran and how it ended inside the item's
window, never that the command tested the item.

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
# The most calls one done write binds: one per item, and a plan holds at most
# this many (`todo_store.MAX_TODO_ITEMS`, restated to keep this module free of
# the store it is imported by).
MAX_BOUND_CALLS: Final = 20
_CONNECT_TIMEOUT_SECONDS: Final = 0.5

# Store readings.
STORE_READ: Final = "read"
STORE_ABSENT: Final = "absent"
STORE_UNREADABLE: Final = "unreadable"

# Per-item verdicts. Only `closed` and `window_empty` close an item.
EVIDENCE_CLOSED: Final = "closed"
EVIDENCE_FAILED: Final = "failed"
EVIDENCE_UNRESOLVED: Final = "unresolved"
# No reference, and no evidence-capable call recorded inside the item's own
# window: nothing a command could have closed, so the done mark stands. This
# is what keeps a conversational item after a command-backed one from being
# held open for good.
WINDOW_EMPTY: Final = "window_empty"
# No reference, and calls WERE recorded inside the window -- each one already
# holding another item. One command closes at most one item.
WINDOW_HAS_COMMANDS: Final = "window_has_commands"


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
    """One string per reference, for keying and de-duplicating."""
    return f"{evidence['kind']}:{evidence['ref']}"


def item_verdicts(
    hermes_home: str | Path | None, session_ref: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Judge each item against the records of ``session_ref`` and its ancestors.

    Each entry of ``items`` is ``{"evidence": dict | None, "from": epoch | None,
    "to": epoch | None}`` -- the reference and the item's window. Returns
    ``{"store": ..., "verdicts": [verdict per item]}``; on ``absent`` and
    ``unreadable`` the list is empty and the caller decides what each means.

    A reference closes only when its result row lies inside the item's window
    (after ``from``): a call from before the window was already there when the
    previous item was written, and binding it now would be a copy.
    """
    session = str(session_ref or "").strip()
    if not session or not hermes_home:
        return {"store": STORE_ABSENT, "verdicts": []}
    opened = _open_readonly(hermes_home)
    if opened is None:
        return {"store": STORE_ABSENT, "verdicts": []}
    connection, store = opened
    if connection is None:
        return {"store": store, "verdicts": []}
    try:
        live = _live_rows_clause(connection)
        lineage = _lineage(connection, session)
        verdicts = [_item_verdict(connection, lineage, live, item) for item in items]
    except sqlite3.Error:
        return {"store": STORE_UNREADABLE, "verdicts": []}
    finally:
        connection.close()
    return {"store": STORE_READ, "verdicts": verdicts}


def observed_calls(
    hermes_home: str | Path | None,
    session_ref: str,
    *,
    after_epoch: float,
    exclude: set[str],
    limit: int,
) -> list[dict[str, str]]:
    """Evidence-capable calls recorded after ``after_epoch``, oldest first, as evidence.

    Read at a done write, so a call it names is one whose RESULT Hermes had
    already persisted: the host flushes a round's results before it runs the
    next round's tools, so a call from an earlier round is on disk and a
    sibling call in the same round as the ``omh_todo`` call is not. Calls in
    ``exclude`` (keys from `evidence_key`) already hold another item and are
    skipped, so no call is bound twice.
    """
    session = str(session_ref or "").strip()
    if not session or not hermes_home or limit <= 0:
        return []
    opened = _open_readonly(hermes_home)
    if opened is None or opened[0] is None:
        return []
    connection = opened[0]
    try:
        live = _live_rows_clause(connection)
        lineage = _lineage(connection, session)
        rows = connection.execute(
            f"SELECT tool_name, tool_call_id FROM messages WHERE session_id IN ({_marks(lineage)}) "
            f"AND role = 'tool' AND tool_name IN ({_marks(EVIDENCE_TOOLS)}) "
            f"AND COALESCE(tool_call_id, '') <> '' AND timestamp > ?{live} ORDER BY id",
            (*lineage, *EVIDENCE_TOOLS, after_epoch),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    calls: list[dict[str, str]] = []
    seen: set[str] = set(exclude)
    for tool_name, call_id in rows:
        kind = EVIDENCE_KIND_TOOL_CALL if tool_name == "terminal" else EVIDENCE_KIND_FILE_WRITE
        evidence = valid_evidence({"kind": kind, "ref": call_id})
        if evidence is None or evidence_key(evidence) in seen:
            continue
        seen.add(evidence_key(evidence))
        calls.append(evidence)
        if len(calls) >= min(limit, MAX_BOUND_CALLS):
            break
    return calls


def _marks(values: tuple[str, ...] | list[str]) -> str:
    return ",".join("?" for _ in values)


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


def _live_rows_clause(connection: sqlite3.Connection) -> str:
    """The filter that drops rewound rows, on a store that records rewinds at all.

    Hermes marks a rewound turn's rows ``active = 0`` and keeps rows a
    compaction folded as ``compacted = 1`` (`hermes_state_messages.py`); a
    store older than those columns has rewound nothing.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()}
    if {"active", "compacted"} <= columns:
        return " AND (active = 1 OR compacted = 1)"
    return ""


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


def _item_verdict(
    connection: sqlite3.Connection, lineage: list[str], live: str, item: dict[str, Any]
) -> str:
    evidence = item.get("evidence")
    start = item.get("from")
    end = item.get("to")
    if isinstance(evidence, dict):
        return _evidence_verdict(connection, lineage, live, evidence, start)
    window = ""
    params: list[Any] = [*lineage, *EVIDENCE_TOOLS]
    if isinstance(start, (int, float)):
        window += " AND timestamp > ?"
        params.append(start)
    if isinstance(end, (int, float)):
        window += " AND timestamp <= ?"
        params.append(end)
    row = connection.execute(
        f"SELECT 1 FROM messages WHERE session_id IN ({_marks(lineage)}) AND role = 'tool' "
        f"AND tool_name IN ({_marks(EVIDENCE_TOOLS)}){window}{live} LIMIT 1",
        params,
    ).fetchone()
    return WINDOW_HAS_COMMANDS if row is not None else WINDOW_EMPTY


def _evidence_verdict(
    connection: sqlite3.Connection,
    lineage: list[str],
    live: str,
    evidence: dict[str, str],
    start: object,
) -> str:
    kind = evidence.get("kind")
    if kind not in _KIND_TOOLS:
        return EVIDENCE_UNRESOLVED
    columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()}
    # A store older than Hermes' effect_disposition column has none to read.
    disposition = "effect_disposition" if "effect_disposition" in columns else "NULL"
    # A compaction re-persists a result under a new row id; the first row by
    # id decides, the rule #1922 states for the same duplication.
    row = connection.execute(
        f"SELECT tool_name, content, {disposition}, timestamp FROM messages "
        f"WHERE session_id IN ({_marks(lineage)}) AND role = 'tool' AND tool_call_id = ?{live} "
        "ORDER BY id LIMIT 1",
        (*lineage, evidence.get("ref")),
    ).fetchone()
    if row is None:
        return EVIDENCE_UNRESOLVED
    tool_name, content, effect, timestamp = row
    if isinstance(start, (int, float)) and not (
        isinstance(timestamp, (int, float)) and timestamp > start
    ):
        return EVIDENCE_UNRESOLVED
    return _result_verdict(str(kind), (tool_name, content, effect))


def _result_verdict(kind: str, row: tuple[Any, Any, Any]) -> str:
    """One recorded result's verdict. An unknown id is `unresolved`, never `failed`."""
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
