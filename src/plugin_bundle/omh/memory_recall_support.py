"""Shared recall projections, freshness and ranking signals (stdlib only).

Extracted from the control-plane facade. No store mutation or host imports.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .hermes_memory import classify_record_expiry as _classify_record_expiry
from .memory_governance import (
    MEMORY_SCOPE_SCHEMA_VERSION as _V2_MEMORY_SCOPE_SCHEMA_VERSION,
    PROJECT_MEMORY_RECORD_SCHEMA_VERSION as _V2_PROJECT_MEMORY_RECORD_SCHEMA_VERSION,
    contains_credential_like_material,
    stable_artifact_identity,
)

PROJECT_MEMORY_RECALL_PACK_SCHEMA_VERSION = "project_memory_recall_pack/v1"

_SAFE_TAG = re.compile(r"^[\w.:/-]{1,120}$", re.UNICODE)

_ADMISSION_VERACITY_WEIGHT_PCT = {"approved_manual": 100, "approved_auto_safe": 90}

_ADMISSION_VERACITY_DEFAULT_PCT = 90

_TEMPORAL_QUERY_CUES = frozenset({"yesterday", "today", "recent", "recently", "ago"})

_TEMPORAL_QUERY_PHRASES = ("most recent", "right now", "as of now", "last week", "last month", "up to date")

_AGE_TIER_BOUNDS_DAYS = (30, 180)

_AGE_TIER_WEIGHTS = (1.0, 0.5, 0.25)

_MEMORY_PINS_LIMIT = 12

MEMORY_ATTENTION_TIERS = ("active", "reference", "archive")

DEFAULT_MEMORY_ATTENTION_TIER = "active"

_MEMORY_ATTENTION_RANK = {"active": 0, "reference": 1, "archive": 2}

_DEFAULT_PERSPECTIVE_OBSERVER = "hermes"

_RECALL_RRF_K = 60

_RECALL_RRF_WEIGHTS = {"relevance": 2.0, "recency": 1.0, "usage": 1.0}

_SOURCE_EVIDENCE_MAX_BYTES = 4 * 1024 * 1024

_FRESHNESS_WARNING_LIMIT = 12

_FRESHNESS_NEXT_ACTION = (
    "Confirm, replace, or retire this record before it steers the plan; "
    "`omh memory confirm <record-id>` resets its review deadline."
)

_REVIEW_DUE_SOON_DAYS = 14

_DUE_SOON_NEXT_ACTION = (
    "Run `omh memory confirm <record-id>` (or correct/retire it) before the deadline passes; "
    "until then the record still recalls normally."
)

_EXPIRES_SOON_DAYS = 14

_EXPIRES_SOON_NEXT_ACTION = (
    "Its retention TTL is about to end and confirmation cannot extend a TTL: "
    "re-capture it (`omh memory capture --ttl-days N ...`) or correct it to keep the content, or let it expire."
)

# An open record is a question, not a fact. Past its review deadline it is
# still delivered, so the notice is advisory, and every answer is one of the
# three verbs -- the reminder never resolves anything by itself.
_UNRESOLVED_OPEN_NEXT_ACTION = (
    "Answer it: `omh memory confirm <record-id>` (resolved), `omh memory keep-open <record-id>` "
    "(still open), or `omh memory retire <record-id>` (drop it). Until then it is delivered as open, never as settled."
)

_ADVISORY_NEXT_ACTIONS = {
    "review_due_soon": _DUE_SOON_NEXT_ACTION,
    "expires_soon": _EXPIRES_SOON_NEXT_ACTION,
    "unresolved_open": _UNRESOLVED_OPEN_NEXT_ACTION,
}

_FRESHNESS_REASON_TEXT = {
    "review_due_soon": "Its revalidation deadline is approaching; unconfirmed, it will leave default recall packs then.",
    "expires_soon": "Its retention TTL is approaching; once it expires the record leaves recall entirely.",
    "stale_review_required": "Its revalidation deadline passed, so nobody has confirmed the record since then.",
    "source_changed": "The local source it cites changed after the record was approved.",
    "source_unverifiable": "The local source it cites cannot be read now, so its freshness is unobservable.",
    "superseded": "A newer revision supersedes this record.",
    "expired_standard": "Its retention deadline passed.",
    "expired_volatile": "Its retention deadline passed.",
    "expired_durable": "Its retention deadline passed.",
    "unresolved_open": "It is marked unresolved and its review deadline passed; it is delivered as an open question, never read as decided.",
    "unresolved_expired": "It was marked unresolved and stayed open past the open ceiling, so the question died unanswered; the record is expired.",
    "freshness_unconfirmed": "Its freshness could not be confirmed from stored metadata and local source evidence.",
}

ADVISORY_FRESHNESS_REASONS = frozenset({"review_due_soon", "expires_soon", "unresolved_open"})

# Notice windows and ask cadences are bounded at a year: accepting the
# 100-year retention ceiling would let one config typo turn a notice into a
# permanent banner. The open ceiling is a retention bound, so it keeps the
# retention maximum.
_MEMORY_CADENCE_MAX = {"due_soon_days": 365, "open_ask_days": 365}

def _cadence_value(policy: dict[str, object], key: str) -> int | None:
    """One validated cadence day-count from a policy mapping, or None."""
    value = policy.get(key)
    maximum = _MEMORY_CADENCE_MAX.get(key, MAX_RETENTION_DAYS)
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum:
        return value
    return None

def _ranking_field(item: dict[str, object], key: str) -> int:
    ranking = item.get("ranking")
    value = ranking.get(key, 0) if isinstance(ranking, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0

def _ranking_flag(item: dict[str, object], key: str) -> bool:
    ranking = item.get("ranking")
    return bool(ranking.get(key, False)) if isinstance(ranking, dict) else False

def _competition_ranks(items: list[dict[str, object]], value_fn: Any) -> dict[str, int]:
    """1-based competition ranks, best first; equal values share a rank."""
    ordered = sorted(items, key=lambda item: str(item.get("record_id", "")))
    ordered.sort(key=value_fn, reverse=True)
    ranks: dict[str, int] = {}
    previous_value: object = object()
    previous_rank = 1
    for position, item in enumerate(ordered, start=1):
        value = value_fn(item)
        if value != previous_value:
            previous_rank = position
            previous_value = value
        ranks[str(item.get("record_id", ""))] = previous_rank
    return ranks

def _attach_recall_ranking(
    items: list[dict[str, object]],
    usage: Mapping[str, Mapping[str, object]],
    *,
    pins: set[str] | None = None,
    now: datetime | None = None,
    recency_weight: float | None = None,
) -> None:
    """Fuse relevance, recency, and delivery usage into one recall order.

    Without a query every relevance score ties at 1, so recency and usage
    decide the order instead of the record-id accident the pure keyword sort
    fell back to. Recency ranks on the approved_at ISO string, which sorts
    lexicographically; a record missing approved_at ranks oldest. The fused
    score is then degraded by age tier so old records reorder below young
    peers of equal relevance.
    """
    if not items:
        return
    pins = pins or set()
    now = now if now is not None else datetime.now(timezone.utc)
    recency_weight = recency_weight if recency_weight is not None else _RECALL_RRF_WEIGHTS["recency"]
    def _times_recalled(item: dict[str, object]) -> int:
        entry = usage.get(str(item.get("record_id", "")), {})
        times = entry.get("times_recalled", 0)
        return times if isinstance(times, int) and not isinstance(times, bool) else 0

    relevance = _competition_ranks(items, lambda item: int(item.get("score", 0) or 0))
    recency = _competition_ranks(items, lambda item: str(item.get("approved_at", "")))
    usage_ranks = _competition_ranks(items, lambda item: _usage_bucket(_times_recalled(item)))
    for item in items:
        record_id = str(item.get("record_id", ""))
        fused = (
            _RECALL_RRF_WEIGHTS["relevance"] / (_RECALL_RRF_K + relevance[record_id])
            + recency_weight / (_RECALL_RRF_K + recency[record_id])
            + _RECALL_RRF_WEIGHTS["usage"] / (_RECALL_RRF_K + usage_ranks[record_id])
        )
        tier = _age_tier(str(item.get("approved_at", "")), now=now)
        veracity_pct = _ADMISSION_VERACITY_WEIGHT_PCT.get(str(item.get("admission_mode", "")), _ADMISSION_VERACITY_DEFAULT_PCT)
        # The attention rank is an ordering key, not a score input: it never
        # touches the fused score, so a tier change reorders records without
        # rewriting the relevance/recency/usage evidence that explains them.
        attention_rank = _MEMORY_ATTENTION_RANK.get(
            str(item.get("attention_tier", "")) or DEFAULT_MEMORY_ATTENTION_TIER,
            _MEMORY_ATTENTION_RANK[DEFAULT_MEMORY_ATTENTION_TIER],
        )
        item["ranking"] = {
            # rrf_score_micro is undecayed rank fusion under THIS pack's
            # weights (a temporal query raises the recency weight, so scores
            # compare within a pack, not across packs); age decay and
            # veracity land in decayed_score_micro, which is what the sort
            # uses.
            "rrf_score_micro": round(fused * 1_000_000),
            "decayed_score_micro": round(fused * _AGE_TIER_WEIGHTS[tier] * (veracity_pct / 100) * 1_000_000),
            "relevance_rank": relevance[record_id],
            "recency_rank": recency[record_id],
            "usage_rank": usage_ranks[record_id],
            "times_recalled": _times_recalled(item),
            "age_tier": tier,
            "attention_rank": attention_rank,
            "pinned": record_id in pins,
            "veracity_weight_pct": veracity_pct,
        }

def _usage_bucket(times_recalled: int) -> int:
    """Saturating ordinal for the usage signal: 0, 1-2, 3-9, 10+.

    Raw counts self-reinforce -- every delivery improves the rank that earns
    the next delivery -- so the signal saturates instead of compounding.
    """
    if times_recalled >= 10:
        return 3
    if times_recalled >= 3:
        return 2
    if times_recalled >= 1:
        return 1
    return 0

def _recall_query_intent(query: str) -> str:
    """Return "temporal" for an unambiguous time cue, else "default".

    Token cues use the recall tokenizer, so matching is exact token overlap,
    never substring guessing ("nowhere" and "knownHosts" stay default).
    Known limitation: hyphenated compounds tokenize whole ("recently-requested"),
    so a cue inside one does not fire. Phrase cues match on whitespace-
    normalized lowercase text.
    """
    if not query.strip():
        return "default"
    if _memory_tokens(query) & _TEMPORAL_QUERY_CUES:
        return "temporal"
    normalized = f" {' '.join(query.lower().split())} "
    return "temporal" if any(f" {phrase} " in normalized for phrase in _TEMPORAL_QUERY_PHRASES) else "default"

def _resolve_query_intent(query: str, supplied: str | None) -> str:
    """The caller's stated intent when there is one, else the English cues.

    The cue table is English-only and stays that way. Per the routing-language
    policy (`tests/test_routing_language_policy.py`, `src/routing/input_language.py`)
    per-language trigger tables do not scale to a global product, and non-English
    intent resolution belongs to model selection over supplied candidates rather
    than to more tokens. Measured before this existed: every Korean and Japanese
    phrasing of "recently" -- `어제 뭐 정했지`, `최근 배포 결정`, `3일 전에 정한 거`,
    `最近の変更` -- resolved to `default`, so recency weighting never engaged for
    them while `recent changes` got it.

    Adding those words to the table would have fixed five languages and left the
    rest, which is the habit the policy exists to stop. So the caller states it
    instead: Hermes read the message and already knows whether the user asked
    for the latest, in any language, and now has somewhere to say so.

    An unrecognized value is refused rather than ignored. A caller that
    misspells its intent should learn that, not silently get the default.
    """
    if supplied is None:
        return _recall_query_intent(query)
    normalized = str(supplied).strip().lower()
    if normalized in {"", "auto"}:
        return _recall_query_intent(query)
    if normalized not in {"default", "temporal"}:
        raise ValueError("query_intent must be one of auto, default, temporal")
    return normalized

def normalize_memory_attention_tier(value: Any) -> str:
    """The one place a tier name is accepted. An unknown tier is refused.

    Failing loudly here is deliberate: silently coercing an unrecognized tier
    to ``active`` would quietly undo an operator's archive request.
    """
    tier = str(value or "").strip().lower()
    if tier not in MEMORY_ATTENTION_TIERS:
        raise ValueError(f"unsupported memory attention tier: {value!r}; use one of {', '.join(MEMORY_ATTENTION_TIERS)}")
    return tier

def record_attention_tier(record: dict[str, Any]) -> str:
    """A stored record's attention tier; anything unreadable reads as active.

    Absence is the normal case for every record approved before tiers existed,
    and a corrupt tier value is indistinguishable from absence here. Defaulting
    to ``active`` is safe because attention is not a trust gate: expiry, scope,
    perspective, and review eligibility still decide what may be recalled.
    """
    attention = record.get("attention")
    tier = str(attention.get("tier", "")) if isinstance(attention, dict) else ""
    return tier if tier in MEMORY_ATTENTION_TIERS else DEFAULT_MEMORY_ATTENTION_TIER

def _record_attention_tier(record: dict[str, Any], *, override: dict[str, str] | None = None) -> str:
    """Stored tier, or the previewed tier when this pack is a projection.

    The override exists so a preview and the pack built after apply run the
    exact same ranking code on the exact same inputs. Recomputing the order a
    second way would make "what will remain in the working context" a guess.
    """
    if override:
        candidate = override.get(str(record.get("record_id", "")))
        if candidate is not None:
            return normalize_memory_attention_tier(candidate)
    return record_attention_tier(record)

def _attention_disclosure(
    included: list[dict[str, object]],
    archived_excluded: int,
    *,
    include_archived: bool,
) -> dict[str, object]:
    """Say out loud which tiers this pack is made of.

    The issue asks recall to disclose when reference records are included, and
    the same sentence is the only honest way to report an archived record that
    was held back: a smaller pack with no explanation is the failure this
    replaces.
    """
    counts = dict.fromkeys(MEMORY_ATTENTION_TIERS, 0)
    for item in included:
        tier = str(item.get("attention_tier", "")) or DEFAULT_MEMORY_ATTENTION_TIER
        counts[tier if tier in counts else DEFAULT_MEMORY_ATTENTION_TIER] += 1
    parts: list[str] = []
    if not included:
        parts.append("No reviewed records are in the working context.")
    else:
        parts.append(f"{counts['active']} active record(s) lead this working context.")
    if counts["reference"]:
        parts.append(f"{counts['reference']} reference-tier record(s) are included behind them.")
    if counts["archive"]:
        parts.append(f"{counts['archive']} archived record(s) are included because this query asked for the archive.")
    if archived_excluded:
        parts.append(
            f"{archived_excluded} archived record(s) stayed out of the working context; "
            "they remain in the store and are listed as archived_tier exclusions."
        )
    return {
        "active_included": counts["active"],
        "reference_included": counts["reference"],
        "archived_included": counts["archive"],
        "archived_excluded": int(archived_excluded),
        "include_archived": bool(include_archived),
        "detail": " ".join(parts),
    }

def _age_tier(approved_at: str, *, now: datetime) -> int:
    """0 for young, 1 for aging, 2 for old; unparseable timestamps stay 0 so
    a malformed record is never silently downweighted."""
    # Naive timestamps read as UTC, matching the expiry classifier: the
    # plain _parse_utc would read them as host-local and shift the tier by
    # up to +/-14 hours at the 30/180-day boundaries.
    approved = _parse_utc_naive_as_utc(str(approved_at or ""))
    if approved is None:
        return 0
    age_days = max((now - approved).total_seconds(), 0.0) / 86400.0
    if age_days >= _AGE_TIER_BOUNDS_DAYS[1]:
        return 2
    if age_days >= _AGE_TIER_BOUNDS_DAYS[0]:
        return 1
    return 0

def _perspective_projection(value: Any) -> dict[str, str]:
    """Projection of a stored perspective; {} when there is nothing observed.

    An empty observed actor is not a perspective -- the lens ignores it -- so
    projecting it would advertise scoping the filter does not apply.
    """
    perspective = value if isinstance(value, dict) else {}
    observed = str(perspective.get("observed", ""))
    if not observed:
        return {}
    return {"observer": str(perspective.get("observer", "") or _DEFAULT_PERSPECTIVE_OBSERVER), "observed": observed}

def _record_perspective_matches(record: dict[str, Any], *, observer: str | None, observed: str | None) -> bool:
    """Lens semantics: unscoped records always pass; scoped need a match.

    No lens (both None) also passes scoped records -- a plain recall is an
    inspection surface and hides nothing. Filtering both ways (a lens that
    excludes unscoped records) is deliberately not offered: unscoped is the
    compatibility default, not a perspective of its own.
    """
    perspective = record.get("perspective")
    if not isinstance(perspective, dict) or not str(perspective.get("observed", "")):
        return True
    if observer is None and observed is None:
        return True
    if observer is not None and str(perspective.get("observer", "")) != observer:
        return False
    if observed is not None and str(perspective.get("observed", "")) != observed:
        return False
    return True

def _empty_recall_pack(
    policy: dict[str, object],
    *,
    executor_target: str,
    session_id: str,
    task_ref: dict[str, object],
    scope_kind: str | None,
    scope_ref: str | None,
    reason: str,
) -> dict[str, object]:
    return {
        "schema_version": PROJECT_MEMORY_RECALL_PACK_SCHEMA_VERSION,
        "enabled": False,
        "executor_target": executor_target,
        "session_id": session_id,
        "task_ref": task_ref,
        "policy": policy,
        "scope": _scope(scope_kind or "project", scope_ref or "default"),
        "perspective": {"observer": "", "observed": ""},
        "query_intent": "default",
        "included_records": [],
        "excluded_records": [{"record_id": "", "reason": reason, "staleness": {"state": "not_checked"}}],
        "freshness_warnings": [],
        "attention": _attention_disclosure([], 0, include_archived=False),
        "record_count": 0,
        "unresolved_delivered": 0,
        "truncated": False,
        "redaction_policy": "metadata_only",
        "claim_boundary": "Memory recall is disabled or empty; no execution, review, CI, merge, or Hermes internal-memory evidence is produced.",
    }

def _recall_item(
    record: dict[str, Any],
    *,
    score: int,
    staleness: dict[str, object],
    evaluation: dict[str, object],
    attention_tier: str = DEFAULT_MEMORY_ATTENTION_TIER,
) -> dict[str, object]:
    evidence = _replay_evaluation(record, evaluation)
    resolution = str(staleness.get("resolution", "") or "")
    return {
        "record_id": _redacted_metadata_label(record.get("record_id", "")),
        "record_type": _redact_admitted_text(str(record.get("record_type", ""))),
        "summary": _redact_admitted_text(str(record.get("summary", "")))[:500],
        "scope": _normalize_scope(record.get("scope", _scope("project", "default"))),
        "tags": _normalize_tags(record.get("tags", [])),
        "source": str(record.get("source", "")),
        "approved_at": _redact_admitted_text(str(record.get("approved_at", ""))),
        "staleness": _redact_nested_metadata(staleness),
        # The delivery marker every consumer of an open record must show: an
        # unresolved record is never read as decided, so the item says so
        # beside its summary rather than only inside the staleness block.
        "resolution": resolution,
        "resolution_marker": open_resolution_marker(staleness),
        "score": int(score),
        "attention_tier": attention_tier,
        "derived_from": _string_list(record.get("derived_from", [])),
        "perspective": _perspective_projection(record.get("perspective")),
        **_recall_evidence_fields(evidence),
    }

def open_resolution_marker(staleness: Mapping[str, object]) -> str:
    """``open · N days unresolved`` for an open record's verdict; "" otherwise.

    One spelling, shared by the recall pack item, the provider's rendered
    record line, and the operator surfaces, so an open record looks the same
    everywhere it is delivered.
    """
    if str(staleness.get("resolution", "") or "") != "open":
        return ""
    days = staleness.get("open_days", 0)
    days = days if isinstance(days, int) and not isinstance(days, bool) and days >= 0 else 0
    return f"open · {days} days unresolved"

def _recall_exclusion(
    record: dict[str, Any],
    evaluation: dict[str, object],
    *,
    staleness: dict[str, object],
    reason: str | None = None,
) -> dict[str, object]:
    evidence = _replay_evaluation(record, evaluation)
    return {
        "record_id": _redacted_metadata_label(record.get("record_id", "")),
        "reason": reason or str(evidence["reason_code"]),
        "staleness": _redact_nested_metadata(staleness),
        **_recall_evidence_fields(evidence),
    }

def _freshness_warnings(
    included: list[dict[str, object]],
    excluded: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Name every record in this pack whose freshness is not confirmed.

    This is the surface acceptance criterion 1 asks for: before a stale,
    expired, superseded, or source-moved record can influence a plan or a
    handoff, the pack says which record it is, why revalidation is due, and
    that the operator must confirm, replace, or retire it. A held-back record
    warns exactly like a delivered one -- ``delivered`` says which happened --
    because a record silently missing from a pack is the failure this
    replaces, not the fix.

    Records excluded for reasons that are not about freshness
    (``no_query_overlap``, ``over_budget`` on a fresh record) never warn:
    a warning that fires for everything is read as noise and stops working.
    """
    blocking: list[dict[str, object]] = []
    advisory: list[dict[str, object]] = []
    seen: set[str] = set()
    for delivered, entry in [*((True, item) for item in included), *((False, item) for item in excluded)]:
        record_id = str(entry.get("record_id", ""))
        staleness = entry.get("staleness") if isinstance(entry.get("staleness"), dict) else {}
        state = str(staleness.get("state", ""))
        reason_code = str(entry.get("eligibility_reason", "") or "")
        known_reason = reason_code in _FRESHNESS_REASON_TEXT
        staleness_reason = str(staleness.get("reason", "") or "")
        if staleness_reason == "unresolved":
            # The verdict says `open/unresolved`; the pack's advisory
            # vocabulary names the same fact `unresolved_open`, beside the
            # blocking `unresolved_expired` it must never be confused with.
            staleness_reason = "unresolved_open"
        if not known_reason and delivered and staleness_reason in ADVISORY_FRESHNESS_REASONS:
            # A still-fresh record inside a pre-deadline window (review-due or
            # TTL expiry): eligibility has nothing to say about it, so the
            # advance notice comes from the staleness verdict instead. Only a
            # DELIVERED record earns it -- an advisory on every unrelated
            # record in the store would page the operator about records this
            # task never touched, and once the deadline actually passes the
            # ordinary blocking warning fires for held-back records as before.
            reason_code, known_reason = staleness_reason, True
        if not known_reason and state in {"", "fresh", "not_checked"}:
            continue
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        if not known_reason:
            reason_code = "freshness_unconfirmed"
        advisory_reason = reason_code in ADVISORY_FRESHNESS_REASONS
        # The limit truncates advisory notices first: an advance heads-up must
        # never displace the name of a record that actually left the pack --
        # naming those is the guarantee this list exists to keep.
        (advisory if advisory_reason else blocking).append(
            {
                "record_id": record_id,
                "state": state or "unknown",
                "reason_code": reason_code,
                "review_due_at": str(staleness.get("review_due_at", "") or ""),
                "expires_at": str(staleness.get("expires_at", "") or ""),
                "detail": _FRESHNESS_REASON_TEXT[reason_code],
                "delivered": bool(delivered),
                "next_action": _ADVISORY_NEXT_ACTIONS.get(reason_code, _FRESHNESS_NEXT_ACTION),
            }
        )
        if len(blocking) >= _FRESHNESS_WARNING_LIMIT:
            break
    return (blocking + advisory)[:_FRESHNESS_WARNING_LIMIT]

def freshness_reason_detail(reason_code: str) -> str:
    """Human text for one recall-pack freshness reason code, or "" when unknown.

    Public so other handoff surfaces can explain a record with the vocabulary
    the recall pack already emits instead of growing a parallel table that
    drifts from it. Read-only on purpose: adding a code to `_FRESHNESS_REASON_TEXT`
    also changes what `_freshness_warnings` treats as a freshness reason, so
    the table stays owned by recall. Membership no longer implies stale:
    codes in `ADVISORY_FRESHNESS_REASONS` describe a still-fresh record, and
    consumers that map "has a reason" to "is stale" must subtract that set.
    """
    return _FRESHNESS_REASON_TEXT.get(str(reason_code), "")

def _recall_evidence_fields(value: Any) -> dict[str, object]:
    evidence = value if isinstance(value, dict) else {}
    safe_evidence = _redact_nested_metadata(evidence)
    evidence = safe_evidence if isinstance(safe_evidence, dict) else {}
    return {
        "revision": int(evidence.get("revision", 0) or 0),
        "admission_mode": str(evidence.get("admission_mode") or ""),
        "source_class": str(evidence.get("source_class") or ""),
        "retention_class": str(evidence.get("retention_class") or ""),
        "evaluated_at": str(evidence.get("evaluated_at") or ""),
        "eligibility_reason": str(evidence.get("reason_code") or ""),
        "revalidation_evidence": evidence.get("revalidation_evidence", {}),
        "replay_evaluation": evidence,
    }

def _normalize_evaluator_timestamps(artifact: dict[str, Any]) -> dict[str, Any]:
    """Present legacy-naive ISO deadlines to the shared evaluator as UTC."""
    normalized = dict(artifact)
    for field, timestamp_key in (("retention", "expires_at"), ("revalidation", "deadline")):
        metadata = artifact.get(field)
        if not isinstance(metadata, dict) or not metadata.get(timestamp_key):
            continue
        parsed = _parse_utc_naive_as_utc(str(metadata[timestamp_key]))
        if parsed is None:
            continue
        normalized[field] = {**metadata, timestamp_key: parsed.isoformat().replace("+00:00", "Z")}
    return normalized

def _parse_utc_naive_as_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _replay_evaluation(artifact: dict[str, Any], result: dict[str, object]) -> dict[str, object]:
    try:
        identity = stable_artifact_identity(artifact)
    except ValueError:
        identity = {}
    revalidation = artifact.get("revalidation")
    return {
        "schema_version": "omh_memory_replay_evaluation/v1",
        "artifact_identity": identity,
        "revision": int(artifact.get("revision", 0) or 0),
        "admission_mode": str(result.get("admission_mode") or ""),
        "source_class": str(result.get("source_class") or ""),
        "retention_class": str(result.get("retention_class") or _retention_class(artifact)),
        "evaluated_at": str(result.get("evaluated_at", "")),
        "eligible": bool(result.get("eligible", False)),
        "reason_code": str(result.get("reason_code", "unknown")),
        "revalidation_evidence": {"deadline": str(revalidation.get("deadline", ""))} if isinstance(revalidation, dict) else {},
    }

def _retention_class(artifact: dict[str, Any]) -> str:
    retention = artifact.get("retention")
    return str(retention.get("class", "")) if isinstance(retention, dict) else ""

def normalized_summary_key(summary: str) -> str:
    """NFC, lowercase, collapsed-whitespace summary: the exact-duplicate identity.

    A copy of capture's `_normalized_summary_key` in `omh.workflows.memory`,
    because this bundle cannot import `omh`; a test pins the two together so
    capture's `duplicate_of` and recall's `duplicate_record` never disagree.
    """
    return " ".join(unicodedata.normalize("NFC", str(summary or "")).lower().split())

def _memory_recall_score(record: dict[str, Any], query: str) -> int:
    if not query.strip():
        return 1
    query_tokens = _memory_tokens(query)
    if not query_tokens:
        # The query carries no indexable tokens at all (emoji-only, an
        # unsupported script, or only sub-length words). Scoring it as zero
        # overlap used to exclude every record as no_query_overlap and hand
        # the executor an empty pack; fall back to unqueried recall so the
        # budget ladder still surfaces approved records.
        return 1
    record_tokens = _memory_tokens(
        " ".join(
            [
                str(record.get("summary", "")),
                str(record.get("record_type", "")),
                " ".join(_normalize_tags(record.get("tags", []))),
            ]
        )
    )
    overlap = query_tokens & record_tokens
    tag_overlap = query_tokens & set(_normalize_tags(record.get("tags", [])))
    return len(overlap) * 10 + len(tag_overlap) * 5

_MEMORY_ASCII_TOKEN = re.compile(r"[a-z0-9_/-]{3,}")

_MEMORY_SHORT_ASCII_TOKEN = re.compile(r"(?<![a-z0-9_/-])[a-z0-9]{2}(?![a-z0-9_/-])")

_MEMORY_SHORT_STOPWORDS = frozenset(
    {
        "am", "an", "as", "at", "be", "by", "do", "he", "if", "in", "is", "it",
        "me", "my", "no", "of", "oh", "on", "or", "so", "to", "up", "us", "we",
    }
)

_MEMORY_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3]+")

def _memory_tokens(value: str) -> set[str]:
    """Index tokens for recall scoring, covering ASCII and CJK text.

    ASCII words of three characters or more are indexed whole. Two-character
    words are indexed unless they are one of the named function words. The old
    >=3 floor made every two-letter technical term unreachable, and the two
    failure modes it produced were opposites: "CI" tokenized to nothing, so the
    no-indexable-tokens fallback handed back the whole store with no exclusion
    reason, while "ci failures" tokenized to {failures}, so every record came
    back no_query_overlap -- including one whose summary began with "CI" and
    carried `ci` as a tag, which the tag bonus could not rescue because the
    query token had already been dropped.

    CJK runs are indexed as the whole
    run plus its character bigrams: Korean particles glue to the noun
    ("배포는"), so whole-word overlap alone would miss "배포" in a query.
    The previous ASCII-only split tokenized any CJK query to the empty set,
    which excluded every record as no_query_overlap and silently emptied
    recall packs for projects that chat in Korean, Japanese, or Chinese.
    """
    lowered = unicodedata.normalize("NFC", value).lower()
    tokens = set(_MEMORY_ASCII_TOKEN.findall(lowered))
    tokens.update(token for token in _MEMORY_SHORT_ASCII_TOKEN.findall(lowered) if token not in _MEMORY_SHORT_STOPWORDS)
    for run in _MEMORY_CJK_RUN.findall(lowered):
        if len(run) >= 2:
            tokens.add(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens

def _record_staleness(
    record: dict[str, Any],
    *,
    now: datetime | None = None,
    due_soon_days: int | None = None,
) -> dict[str, object]:
    """The one freshness verdict: TTL, review-due date, and source evidence.

    The TTL half is decided by the bundle classifier, the single source of
    truth for what "expired" means: it reads naive timestamps as UTC, where
    the local ``_parse_utc`` would read them as host-local time and move the
    verdict by up to +/-14 hours depending on where the host happens to be.

    The review-due half reads ``review_due_at`` and the older ``stale_after``
    spelling of the same date, taking whichever comes first, so records
    written before the field was named keep their deadline and a record with
    only one spelling edited cannot read as fresh.

    The source half compares the digest recorded at capture against the cited
    local file as it reads now. It only ever makes a record less trusted: a
    moved source is ``stale``, an unreadable one is ``unknown``, and neither
    can turn into ``fresh``. Every input is stored metadata plus the caller's
    ``now`` plus locally observable bytes, so the verdict is reproducible.

    The fourth state is ``open``: a record a person marked unresolved
    (``staleness.resolution == "open"``) whose review deadline has passed.
    It is not ``stale`` -- nobody claimed it was decided, so there is nothing
    to re-confirm -- and it is delivered with its age (``open_days``) rather
    than held back. Only a confirm or a correct writes ``resolved``; no
    timeout ever promotes an open record to a fact. It still has a ceiling:
    ``open_expires_at`` (capture plus ``open_max_days``) makes it
    ``expired/unresolved_expired``, so "open" cannot become "forever". A
    changed or unreadable source still outranks ``open``, because evidence
    that moved is not the same as an answer that has not arrived. A record
    that is not open gets the verdict it always got; ``resolution`` is ""
    and ``open_days`` is 0 for it.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    ttl = record.get("ttl", {}) if isinstance(record.get("ttl"), dict) else {}
    staleness = record.get("staleness", {}) if isinstance(record.get("staleness"), dict) else {}
    expires_at = str(ttl.get("expires_at", ""))
    stale_after = str(staleness.get("stale_after", ""))
    review_due_at = _earliest_deadline(str(staleness.get("review_due_at", "") or ""), stale_after)
    source_state = _source_evidence_state(record)
    resolution = _record_resolution(staleness)
    is_open = resolution == "open"
    fields = {
        "stale_after": stale_after,
        "review_due_at": review_due_at,
        "expires_at": expires_at,
        "source_state": source_state,
        "resolution": resolution,
        "open_days": _open_days(staleness, now=now) if is_open else 0,
    }
    if _classify_record_expiry(record, now=now) == "expired":
        return {"state": "expired", "reason": "retention_expired", **fields}
    open_ceiling = _parse_utc_naive_as_utc(str(staleness.get("open_expires_at", "") or "")) if is_open else None
    if open_ceiling is not None and open_ceiling <= now:
        return {"state": "expired", "reason": "unresolved_expired", **fields}
    deadline = _parse_utc(review_due_at)
    if deadline and deadline <= now and not is_open:
        return {"state": "stale", "reason": "review_due", **fields}
    if source_state == "changed":
        return {"state": "stale", "reason": "source_changed", **fields}
    if source_state == "unreadable":
        return {"state": "unknown", "reason": "source_unreadable", **fields}
    if deadline and deadline <= now and is_open:
        return {"state": "open", "reason": "unresolved", **fields}
    # Still fresh and still eligible below here: the record delivers exactly
    # as before. The reason is the advance notice recall packs turn into a
    # warning, so a deadline stops being a surprise discovered only after the
    # record has already left every pack. Expiry outranks review-due when both
    # windows overlap: a passed TTL is terminal, a passed review date is not.
    # Naive TTL timestamps read as UTC, matching the expiry classifier that
    # decides the terminal verdict above; `_parse_utc` would read them as
    # host-local and move this window by up to +/-14 hours.
    expiry = _parse_utc_naive_as_utc(expires_at)
    expiry_window = due_soon_days if due_soon_days is not None else _EXPIRES_SOON_DAYS
    # A notice window longer than half the record's own life is not advance
    # notice, it is a permanent banner: a 7-day volatile record would warn
    # from the moment it was approved. Scale the window down to half the
    # record's lifespan (never below one day) so short-lived records warn
    # near the end, which is when the warning means something. The span
    # comes from ttl_days when there is one, else from the distance between
    # creation and the absolute expiry date -- an absolute two-day deadline
    # must not warn from birth either.
    ttl_days_value = ttl.get("ttl_days")
    span_days: int | None = None
    if isinstance(ttl_days_value, int) and not isinstance(ttl_days_value, bool) and ttl_days_value > 0:
        span_days = ttl_days_value
    elif expiry is not None:
        created = _parse_utc_naive_as_utc(str(record.get("created_at", "") or ""))
        if created is not None and expiry > created:
            span_days = max(int((expiry - created).total_seconds() // 86400), 1)
    if span_days is not None:
        expiry_window = min(expiry_window, max(span_days // 2, 1))
    if expiry and expiry - now <= timedelta(days=expiry_window):
        return {"state": "fresh", "reason": "expires_soon", **fields}
    if deadline and deadline - now <= timedelta(days=due_soon_days if due_soon_days is not None else _REVIEW_DUE_SOON_DAYS):
        return {"state": "fresh", "reason": "review_due_soon", **fields}
    return {"state": "fresh", "reason": "", **fields}

MEMORY_RESOLUTIONS = ("open", "resolved")

def _record_resolution(staleness: Mapping[str, object]) -> str:
    """``open`` | ``resolved`` | "" -- only the exact stored spellings count.

    Fail closed: any other value reads as an ordinary record, never as open,
    so a typo cannot exempt a record from its review deadline.
    """
    value = str(staleness.get("resolution", "") or "")
    return value if value in MEMORY_RESOLUTIONS else ""

def _open_days(staleness: Mapping[str, object], *, now: datetime) -> int:
    """Whole days since ``open_since``; 0 when absent, unreadable, or future."""
    since = _parse_utc_naive_as_utc(str(staleness.get("open_since", "") or ""))
    if since is None or since > now:
        return 0
    return int((now - since).total_seconds() // 86400)

def _earliest_deadline(*values: str) -> str:
    """The soonest parseable deadline among equivalent spellings, fail-closed.

    ``review_due_at`` and ``stale_after`` are two names for one date, so they
    normally agree. When something edits only one of them they must not cancel
    each other out: whichever deadline has already passed decides, so a
    half-updated record reads as due for review rather than as fresh.
    """
    best_value = ""
    best_time: datetime | None = None
    for value in values:
        if not value:
            continue
        if not best_value:
            best_value = value
        parsed = _parse_utc(value)
        if parsed is not None and (best_time is None or parsed < best_time):
            best_time, best_value = parsed, value
    return best_value

def _local_source_digest(source_ref: str) -> str:
    """SHA-256 of a local file, or "" when it cannot be digested here."""
    if not source_ref:
        return ""
    try:
        path = Path(source_ref)
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > _SOURCE_EVIDENCE_MAX_BYTES:
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        # Unreadable, gone, a directory, or a path this platform rejects. The
        # caller turns an empty digest into `unreadable`, never into `fresh`.
        return ""

def _source_evidence_state(record: dict[str, Any]) -> str:
    """"" (no evidence recorded) | unchanged | changed | unreadable."""
    evidence = record.get("source_evidence")
    if not isinstance(evidence, dict):
        return ""
    recorded = str(evidence.get("sha256", "") or "")
    if not recorded:
        return ""
    current = _local_source_digest(str(evidence.get("path", "") or ""))
    if not current:
        return "unreadable"
    return "unchanged" if current == recorded else "changed"

MAX_RETENTION_DAYS = 36500

_EPISODE_DEFAULT_TTL_DAYS = 30

_REVIEW_DEFAULT_DAYS = 90

# The open ceiling: an unresolved record is still delivered after its review
# deadline, but a question nobody answers for a year is not open, it is
# abandoned. Past capture + open_max_days it expires as `unresolved_expired`.
_OPEN_MAX_DAYS = 365

# How often the provider may ask about the same open record: once when the
# review deadline passes, then at most every open_ask_days. "Still open" is
# an answer that resets this clock; it never changes the record.
_OPEN_ASK_DAYS = 14

_MEMORY_CADENCE_DEFAULTS = {
    "stale_after_days_default": _REVIEW_DEFAULT_DAYS,
    "episode_ttl_days": _EPISODE_DEFAULT_TTL_DAYS,
    "due_soon_days": _REVIEW_DUE_SOON_DAYS,
    "open_max_days": _OPEN_MAX_DAYS,
    "open_ask_days": _OPEN_ASK_DAYS,
}

def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None

def _normalize_tags(values: Any, *, redact_sensitive: bool = True) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw_tag = unicodedata.normalize("NFC", str(value)).strip()
        if not raw_tag:
            continue
        if raw_tag == "[redacted]":
            tag = raw_tag
        elif redact_sensitive and contains_credential_like_material(raw_tag):
            tag = "[redacted]"
        else:
            tag = raw_tag.lower()
            if not _SAFE_TAG.match(tag):
                continue
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags[:12]

def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]

def _redacted_metadata_label(value: Any) -> str:
    text = str(value or "")
    return "redacted" if contains_credential_like_material(text) else text

def _redact_nested_metadata(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_admitted_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for index, (key, nested) in enumerate(value.items()):
            safe_key = _redact_admitted_text(str(key))
            if safe_key in sanitized:
                safe_key = f"redacted_{index}"
            sanitized[safe_key] = _redact_nested_metadata(nested)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_redact_nested_metadata(nested) for nested in value]
    return value

def _redact_admitted_text(value: str) -> str:
    if contains_credential_like_material(value):
        return "[redacted]"
    return value[:500]

def _normalize_scope(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        kind = str(value.get("kind", "project") or "project")
        ref = str(value.get("ref", "default") or "default")
        return _scope(kind, ref)
    if isinstance(value, str) and value:
        return _scope("project", value)
    return _scope("project", "default")

def _scope(kind: str, ref: str) -> dict[str, str]:
    return {"kind": kind, "ref": ref}

MEMORY_SCOPE_SCHEMA_VERSION = _V2_MEMORY_SCOPE_SCHEMA_VERSION

LEGACY_MEMORY_SCOPE_SCHEMA_VERSION = "omh_memory_scope/v1"

PROJECT_MEMORY_RECORD_SCHEMA_VERSION = _V2_PROJECT_MEMORY_RECORD_SCHEMA_VERSION

LEGACY_PROJECT_MEMORY_RECORD_SCHEMA_VERSION = "project_memory_record/v1"

_INSPECTABLE_STALE_REASONS = {"stale_review_required", "source_changed", "source_unverifiable"}


# Shared recall API also re-exported by the existing control-plane facade.
__all__ = [
    "LEGACY_MEMORY_SCOPE_SCHEMA_VERSION",
    "LEGACY_PROJECT_MEMORY_RECORD_SCHEMA_VERSION",
    "MEMORY_SCOPE_SCHEMA_VERSION",
    "PROJECT_MEMORY_RECORD_SCHEMA_VERSION",
    "_INSPECTABLE_STALE_REASONS",
    "ADVISORY_FRESHNESS_REASONS",
    "DEFAULT_MEMORY_ATTENTION_TIER",
    "MAX_RETENTION_DAYS",
    "MEMORY_ATTENTION_TIERS",
    "MEMORY_RESOLUTIONS",
    "PROJECT_MEMORY_RECALL_PACK_SCHEMA_VERSION",
    "_ADMISSION_VERACITY_DEFAULT_PCT",
    "_ADMISSION_VERACITY_WEIGHT_PCT",
    "_ADVISORY_NEXT_ACTIONS",
    "_AGE_TIER_BOUNDS_DAYS",
    "_AGE_TIER_WEIGHTS",
    "_DEFAULT_PERSPECTIVE_OBSERVER",
    "_DUE_SOON_NEXT_ACTION",
    "_EPISODE_DEFAULT_TTL_DAYS",
    "_EXPIRES_SOON_DAYS",
    "_EXPIRES_SOON_NEXT_ACTION",
    "_FRESHNESS_NEXT_ACTION",
    "_FRESHNESS_REASON_TEXT",
    "_FRESHNESS_WARNING_LIMIT",
    "_MEMORY_ASCII_TOKEN",
    "_MEMORY_ATTENTION_RANK",
    "_MEMORY_CADENCE_DEFAULTS",
    "_MEMORY_CADENCE_MAX",
    "_MEMORY_CJK_RUN",
    "_MEMORY_PINS_LIMIT",
    "_MEMORY_SHORT_ASCII_TOKEN",
    "_MEMORY_SHORT_STOPWORDS",
    "_OPEN_ASK_DAYS",
    "_OPEN_MAX_DAYS",
    "_RECALL_RRF_K",
    "_RECALL_RRF_WEIGHTS",
    "_REVIEW_DEFAULT_DAYS",
    "_REVIEW_DUE_SOON_DAYS",
    "_SAFE_TAG",
    "_SOURCE_EVIDENCE_MAX_BYTES",
    "_TEMPORAL_QUERY_CUES",
    "_TEMPORAL_QUERY_PHRASES",
    "_age_tier",
    "_attach_recall_ranking",
    "_attention_disclosure",
    "_cadence_value",
    "_competition_ranks",
    "_earliest_deadline",
    "_empty_recall_pack",
    "_freshness_warnings",
    "_local_source_digest",
    "_memory_recall_score",
    "_memory_tokens",
    "_normalize_evaluator_timestamps",
    "_normalize_scope",
    "_normalize_tags",
    "_open_days",
    "_parse_utc",
    "_parse_utc_naive_as_utc",
    "_perspective_projection",
    "_ranking_field",
    "_ranking_flag",
    "_recall_evidence_fields",
    "_recall_exclusion",
    "_recall_item",
    "_recall_query_intent",
    "_record_attention_tier",
    "_record_perspective_matches",
    "_record_resolution",
    "_record_staleness",
    "_redact_admitted_text",
    "_redact_nested_metadata",
    "_redacted_metadata_label",
    "_replay_evaluation",
    "_resolve_query_intent",
    "_retention_class",
    "_scope",
    "_source_evidence_state",
    "_string_list",
    "_usage_bucket",
    "freshness_reason_detail",
    "normalize_memory_attention_tier",
    "open_resolution_marker",
    "record_attention_tier",
]
