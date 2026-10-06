"""Scheduling for memory consolidation -- the deterministic half of "dreaming".

Letta surfaces sleep-time compute as dreaming: background subagents that review
recent turns, consolidate what was learned, and rewrite memory without blocking
the foreground session. The consolidation is model work. The *trigger* is not --
it fires after a configured number of user messages, or on a context-compaction
event.

That split is the whole reason this file exists. OMH makes no model call, so it
cannot dream. It can decide precisely when dreaming is due, on what evidence,
and hand Hermes a prepared brief -- and Hermes already runs the consolidation
loop itself (``agent/background_review.py`` forks after each turn with the
memory and skill tools). The gap this closes is that the loop runs with no
statement of what is duplicated, expiring, or out of room. It supplies that.

Counting turns is deliberate and unglamorous. The sleep-time compute paper is
explicit that the technique pays off in proportion to how predictable the next
query is, so a scheduler that fires on a fixed interval should not be sold as
one that fires when consolidation would help most.

Stdlib only; vendored in the bundle because it is imported inside the Hermes
process, where the ``omh`` package does not exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .memory_state_files import state_file_lock, write_text_atomic, write_text_locked

DREAMING_STATE_SCHEMA_VERSION = "omh_memory_dreaming_state/v1"
DREAMING_HANDOFF_SCHEMA_VERSION = "omh_memory_consolidation_handoff/v1"

# Letta's server-side sleeptime default is every 5 steps; matched so the cadence
# has a stated origin rather than being picked here.
DEFAULT_TURN_INTERVAL = 5

# Free characters below which the memory file is treated as under pressure,
# whatever the turn count says. A full file is the failure this is meant to
# reach before it happens, not after.
DEFAULT_HEADROOM_FLOOR_CHARS = 300

# Modes. There is no auto-launch: OMH cannot start Hermes' consolidation, so
# offering the word would claim a capability the wrapper does not have.
DREAMING_MODES = ("off", "reminder")
DEFAULT_DREAMING_MODE = "reminder"

# Reasons that describe a moment rather than a state. These clear themselves by
# firing -- the turn counter resets, the compaction flag is lowered -- so they
# may fire again the moment they recur. Everything not listed here is a standing
# condition: it stays true until something outside OMH changes it, so repeating
# it every turn says nothing the previous brief did not.
_EVENT_REASONS = frozenset(
    {
        "turn_interval_reached",
        "session_ending_with_unconsolidated_turns",
        "context_compaction_observed",
    }
)


def dreaming_state_path(omh_home: str | Path) -> Path:
    return Path(omh_home).expanduser() / "memory" / "dreaming.json"


def empty_dreaming_state() -> dict[str, object]:
    return {
        "schema_version": DREAMING_STATE_SCHEMA_VERSION,
        "turns_since_consolidation": 0,
        "compaction_pending": False,
        "memory_writes_observed": 0,
        "last_consolidated_at": "",
        "last_reasons": [],
    }


def read_dreaming_state(omh_home: str | Path) -> dict[str, object]:
    """Persisted counters, or a fresh state when absent or unusable.

    Never raises. A corrupt counter file must cost at most one missed
    consolidation, not the turn that read it.
    """
    path = dreaming_state_path(omh_home)
    try:
        if path.is_symlink() or not path.is_file():
            return empty_dreaming_state()
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return empty_dreaming_state()
    if not isinstance(data, dict) or data.get("schema_version") != DREAMING_STATE_SCHEMA_VERSION:
        return empty_dreaming_state()
    state = empty_dreaming_state()
    state["turns_since_consolidation"] = _non_negative_int(data.get("turns_since_consolidation"))
    state["compaction_pending"] = bool(data.get("compaction_pending"))
    state["memory_writes_observed"] = _non_negative_int(data.get("memory_writes_observed"))
    state["last_consolidated_at"] = str(data.get("last_consolidated_at", "") or "")
    state["last_reasons"] = [str(item) for item in data.get("last_reasons", []) if isinstance(item, str)]
    return state


def write_dreaming_state(omh_home: str | Path, state: dict[str, Any]) -> Path:
    """Replace the counters whole, under the state lock. Raises ``OSError``."""
    path = dreaming_state_path(omh_home)
    write_text_locked(path, json.dumps(state, ensure_ascii=False, sort_keys=True))
    return path


def update_dreaming_state(
    omh_home: str | Path,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Read, mutate and replace the counters as one step; returns what was written.

    Two sessions on one home -- a CLI and a gateway -- each used to read the
    counters, add one, and write them back, so the later write erased the
    earlier increment. The read now happens inside the same lock as the
    write. A ``mutate`` that raises leaves the previous file untouched.
    Raises ``OSError`` (a lock timeout is one).
    """
    path = dreaming_state_path(omh_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with state_file_lock(path):
        updated = mutate(read_dreaming_state(omh_home))
        write_text_atomic(path, json.dumps(updated, ensure_ascii=False, sort_keys=True))
    return updated


def record_turn(state: dict[str, Any]) -> dict[str, object]:
    """One more user turn since the last consolidation."""
    updated = dict(state)
    updated["turns_since_consolidation"] = _non_negative_int(state.get("turns_since_consolidation")) + 1
    return updated


def record_compaction(state: dict[str, Any]) -> dict[str, object]:
    """Hermes is about to discard messages; consolidation is due next chance."""
    updated = dict(state)
    updated["compaction_pending"] = True
    return updated


def record_memory_write(state: dict[str, Any]) -> dict[str, object]:
    """Hermes wrote its own memory; the two stores may have diverged."""
    updated = dict(state)
    updated["memory_writes_observed"] = _non_negative_int(state.get("memory_writes_observed")) + 1
    return updated


def record_consolidation_observed(state: dict[str, Any]) -> dict[str, object]:
    """A consolidation-shaped write happened; the clock restarts here.

    `turns_since_consolidation` reset only when a BRIEF fired, never when the
    consolidation itself did -- despite the name. Observed live: a session
    whose only work was consolidating memory still ended with turns > 0, so
    `session_ending_with_unconsolidated_turns` raised a fresh brief at the
    exit of the very session that had just consolidated. Every tidy-up ended
    by requesting the next one.

    A replace/remove from Hermes' memory tool is the consolidation the counter
    is named after, so it zeroes the turn count and lowers the compaction
    flag: both describe work waiting to be consolidated, and it just was.
    """
    updated = dict(state)
    updated["turns_since_consolidation"] = 0
    updated["compaction_pending"] = False
    return updated


def clear_after_consolidation(state: dict[str, Any], *, at: str, reasons: list[str]) -> dict[str, object]:
    updated = dict(state)
    updated["turns_since_consolidation"] = 0
    updated["compaction_pending"] = False
    updated["memory_writes_observed"] = 0
    updated["last_consolidated_at"] = at
    updated["last_reasons"] = list(reasons)
    return updated


def consolidation_reasons(
    state: dict[str, Any],
    *,
    mode: str = DEFAULT_DREAMING_MODE,
    turn_interval: int = DEFAULT_TURN_INTERVAL,
    headroom_chars: int | None = None,
    headroom_floor_chars: int = DEFAULT_HEADROOM_FLOOR_CHARS,
    duplicate_count: int = 0,
    expiring_count: int = 0,
    session_ending: bool = False,
    suppress: bool = True,
) -> list[str]:
    """Why consolidation is due now, or an empty list when it is not.

    Reasons are returned rather than a bare boolean so the handoff can state its
    own cause. "It is time" and "the file is nearly full" ask for different work,
    and a brief that cannot tell them apart is a brief that says nothing.

    ``session_ending`` lowers the bar to a single unconsolidated turn, because
    the interval assumes a later turn will come and a closing session is exactly
    where that assumption fails. Three turns and a closed laptop would otherwise
    leave nothing behind.

    A reason that describes a *condition* is suppressed until the condition
    actually changes. Observed live: 19 consecutive briefs, every one of them
    reading ``headroom_below_floor:289<=300``. Turn counts and compaction flags
    clear themselves when they fire, but headroom only clears when somebody
    consolidates -- and OMH cannot, by design. So a standing condition re-fired
    on every single turn and the journal filled with the same sentence. Each
    reason encodes its own value, so an unchanged condition is an unchanged
    string, and that is what the suppression compares.
    """
    if normalize_dreaming_mode(mode) == "off":
        return []
    reasons: list[str] = []
    interval = turn_interval if isinstance(turn_interval, int) and turn_interval > 0 else DEFAULT_TURN_INTERVAL
    turns = _non_negative_int(state.get("turns_since_consolidation"))
    if turns >= interval:
        reasons.append(f"turn_interval_reached:{turns}/{interval}")
    elif session_ending and turns > 0:
        reasons.append(f"session_ending_with_unconsolidated_turns:{turns}")
    if bool(state.get("compaction_pending")):
        reasons.append("context_compaction_observed")
    if headroom_chars is not None and headroom_chars <= max(headroom_floor_chars, 0):
        reasons.append(f"headroom_below_floor:{headroom_chars}<={headroom_floor_chars}")
    if duplicate_count > 0:
        reasons.append(f"duplicate_records:{duplicate_count}")
    if expiring_count > 0:
        reasons.append(f"expiring_records:{expiring_count}")
    # ``suppress=False`` answers a different question: not "should a new brief
    # fire now" but "is anything still true at all". Retirement needs the
    # second -- a suppressed standing condition is still standing, and retiring
    # its brief because the scheduler declined to repeat it would clear a
    # notice whose fact had not cleared.
    if not suppress:
        return reasons
    return reasons if _has_unfired_reason(reasons, state) else []


def _has_unfired_reason(reasons: list[str], state: dict[str, Any]) -> bool:
    """Is anything here new since the last brief?

    An event reason always counts: it describes a moment, and the moment
    happened again. A condition reason counts only when its exact string has
    changed, because an unchanged string means the same standing condition
    nobody has cleared.

    Returning the full reason list -- rather than only the unfired part -- when
    anything is fresh is deliberate. A brief woken by the turn interval while
    memory is also nearly full should still say memory is nearly full.
    """
    already = {str(reason) for reason in state.get("last_reasons", []) if isinstance(reason, str)}
    return any(_is_event_reason(reason) or reason not in already for reason in reasons)


def _is_event_reason(reason: str) -> bool:
    return reason.split(":", 1)[0] in _EVENT_REASONS


# What the executor is asked to do differs by which moment woke it. A brief
# written because the context is about to be discarded is asking for something
# a routine interval brief is not.
_TRIGGER_INSTRUCTIONS = {
    "compaction": (
        "Context compression is about to discard messages. Extract anything durable from them "
        "FIRST -- that material does not survive this turn."
    ),
    "shutdown": (
        "Hermes is closing. Save what is worth keeping now; there is no later turn in this session."
    ),
    "session_end": "The session ended. Consolidate what it produced before it is only a transcript.",
    "session_start_recovery": (
        "A previous session ended without consolidating -- a closed laptop, a killed process, a lost "
        "gateway. Its counters are below and its work was never reviewed."
    ),
    "turn": "The turn interval came due. Review recent work and consolidate what proved durable.",
}


def normalize_record_expiry(value: Any) -> dict[str, int]:
    """Non-negative expired/expiring_soon counts, whatever was on disk.

    Briefs written before this key existed -- and briefs a hand edit mangled --
    must read as zeros, not as a TypeError in every consumer.
    """
    source = value if isinstance(value, dict) else {}
    return {
        "expired": _non_negative_int(source.get("expired")),
        "expiring_soon": _non_negative_int(source.get("expiring_soon")),
    }


def build_consolidation_handoff(
    reasons: list[str],
    *,
    block_summaries: list[dict[str, object]] | None = None,
    eviction_plan: dict[str, object] | None = None,
    record_expiry: dict[str, int] | None = None,
    mode: str = DEFAULT_DREAMING_MODE,
    trigger: str = "manual",
    messages_at_risk: int = 0,
    session_id: str = "",
    raised_at: str = "",
) -> dict[str, object]:
    """A prepared brief for whoever actually consolidates.

    OMH never executes this. It states what it observed and what it would like
    decided; only the model can act on it -- durable facts through
    `omh_memory(action="capture")`, Hermes memory through Hermes' own memory tool.
    """
    requested = [_TRIGGER_INSTRUCTIONS.get(trigger, "Review what is below and consolidate what is durable.")]
    if messages_at_risk:
        requested.append(f"{messages_at_risk} message(s) are in the buffer being compressed.")
    requested.extend(
        [
            (
                'Move durable facts, preferences, and decisions from this session into OMH with '
                'omh_memory(action="capture"), one bounded line each.'
            ),
            "When Hermes memory headroom is low, trim or merge its entries with Hermes' own memory tool.",
            "Leave anything you cannot source; an unsourced entry is not evidence it is wrong.",
        ]
    )
    return {
        "schema_version": DREAMING_HANDOFF_SCHEMA_VERSION,
        "mode": normalize_dreaming_mode(mode),
        "due": bool(reasons),
        "trigger": trigger,
        # The brief's own timestamp. `last_consolidated_at` in the dreaming
        # state is when the PREVIOUS brief fired, which is strictly earlier;
        # labelling that "raised_at" mislabelled it for every consumer.
        "raised_at": raised_at,
        "session_id": session_id,
        "messages_at_risk": messages_at_risk,
        "reasons": list(reasons),
        "record_expiry": normalize_record_expiry(record_expiry),
        "blocks": list(block_summaries or []),
        "eviction_plan": dict(eviction_plan or {}),
        "requested_of_executor": requested,
        "redaction_policy": "metadata_only",
        "claim_boundary": (
            "A consolidation handoff is prepared context. It is not evidence that memory was "
            "consolidated, that Hermes read it, or that any entry was written, changed, or removed."
        ),
    }


def consolidation_path(omh_home: str | Path) -> Path:
    return Path(omh_home).expanduser() / "memory" / "consolidation.json"


def read_latest_consolidation(omh_home: str | Path) -> dict[str, Any] | None:
    """The newest brief on disk, or None when there is none to read.

    Never raises, and never returns fields a consumer has to type-check. The
    first version validated only that the file was a dict with the right
    schema string and passed everything else through -- so a brief whose
    ``reasons`` was an int survived the read and raised ``TypeError`` in the
    consumer, which for the chat notice meant one malformed local file took
    down every reply on every messenger. Fields are normalized here, once,
    for every consumer.
    """
    path = consolidation_path(omh_home)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != DREAMING_HANDOFF_SCHEMA_VERSION:
        return None
    raw_reasons = data.get("reasons")
    normalized = dict(data)
    normalized["due"] = bool(data.get("due"))
    normalized["reasons"] = (
        [reason for reason in raw_reasons if isinstance(reason, str)] if isinstance(raw_reasons, list) else []
    )
    normalized["trigger"] = str(data.get("trigger", "") or "")
    normalized["raised_at"] = str(data.get("raised_at", "") or "")
    normalized["record_expiry"] = normalize_record_expiry(data.get("record_expiry"))
    if normalized["due"] and not normalized["reasons"]:
        # A brief that claims to be due but cannot say why is not actionable.
        normalized["due"] = False
    return normalized


def normalize_dreaming_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in DREAMING_MODES else DEFAULT_DREAMING_MODE


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
