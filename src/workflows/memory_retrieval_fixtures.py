"""Retained retrieval fixtures for the project-memory recall regression suite.

This module is data plus one materializer. It holds no ranking, eligibility, or
budget logic: every expectation below is a recorded verdict about what the
production recall-pack builder must return, never a second implementation of
how it decides.

Two properties make the corpus a regression gate rather than a snapshot:

- The clock is part of the fixture. Every timestamp is absolute and the case
  clock is ``FIXTURE_CLOCK``, so a case that depends on expiry, review
  deadlines, or age tiers reads the same on any host on any day. No fixture
  here reads the wall clock, directly or through a default argument.
- Record order is the order written here, and the store reader sorts by file
  name, so neither dict iteration nor filesystem order can reorder a pack.

Each case declares the records, the pins, the delivery counters, the query,
the scope and perspective lens, the budgets, and the expected disposition of
every record: included in this order, excluded for this named reason, or
absent because a lens filtered it before the pack could name it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..plugin_bundle.omh.memory_governance import (
    MEMORY_GOVERNANCE_POLICY_VERSION,
    PROJECT_MEMORY_RECORD_SCHEMA_VERSION,
    PROJECT_MEMORY_REVIEW_RECORD_SCHEMA_VERSION,
    canonical_payload_digest,
    stable_artifact_identity,
)

FIXTURE_CORPUS_VERSION = "omh-memory-retrieval-fixtures/v3"
FIXTURE_PROJECT_IDENTITY = "repo:" + hashlib.sha256(b"example.invalid/memory-fixture").hexdigest()[:32]
# The corpus clock. Absolute, declared once, never derived from the host.
FIXTURE_CLOCK = datetime(2031, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
FIXTURE_CLOCK_ISO = "2031-01-02T03:04:05Z"

_RECORD_CLAIM_BOUNDARY = (
    "Reviewed OMH project memory is prepared context only; it is not execution, "
    "review, CI, merge, or Hermes internal-memory evidence."
)
_REVIEW_CLAIM_BOUNDARY = (
    "Project memory review decisions are prepared governance only, never executor-use evidence."
)
# Contamination classes label a record that a lens, a deadline, or a tier is
# supposed to keep out of the pack. A labelled record appearing in a pack that
# did not expect it increments the counter named here, so a leak is reported
# under its own name instead of as a generic relevance miss.
CONTAMINATION_CLASSES = ("foreign_scope", "foreign_perspective", "stale", "expired", "archived")


def _record(
    record_id: str,
    summary: str,
    *,
    approved_at: str,
    tags: tuple[str, ...] = (),
    record_type: str = "fact",
    scope_kind: str = "project",
    scope_ref: str = FIXTURE_PROJECT_IDENTITY,
    observer: str = "",
    observed: str = "",
    retention_class: str = "standard",
    expires_at: str = "",
    review_due_at: str = "",
    superseded_by: str = "",
    attention_tier: str = "active",
    admission_state: str = "approved_manual",
    contamination_class: str = "",
    source: str = "",
) -> dict[str, object]:
    """One fixture record specification, in the retained corpus's own shape.

    ``source`` names the admission channel the stored record carries; a case
    about recovering from one wrong source needs records that differ on it. It
    is emitted only when set, so every spec that does not care about the source
    stays byte-identical and the corpus digest does not move.
    """
    if contamination_class and contamination_class not in CONTAMINATION_CLASSES:
        raise ValueError(f"unsupported contamination class: {contamination_class}")
    return {
        **({"source": source} if source else {}),
        "record_id": record_id,
        "summary": summary,
        "approved_at": approved_at,
        "tags": list(tags),
        "record_type": record_type,
        "scope_kind": scope_kind,
        "scope_ref": scope_ref,
        "observer": observer,
        "observed": observed,
        "retention_class": retention_class,
        "expires_at": expires_at,
        "review_due_at": review_due_at,
        "superseded_by": superseded_by,
        "attention_tier": attention_tier,
        "admission_state": admission_state,
        "contamination_class": contamination_class,
    }


def materialize_fixture_record(spec: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """Turn one fixture specification into a stored record and its review.

    The pair is written straight into an isolated fixture store rather than
    produced through capture and approval, because approval stamps the record
    with ``utc_now()`` and mints a random record id. Both would make the pack
    depend on the day the suite ran and on the bytes of ``os.urandom``.
    """
    approved_at = str(spec["approved_at"])
    record_id = str(spec["record_id"])
    review_id = f"review_{record_id}"
    retention: dict[str, object] = {"class": str(spec["retention_class"]), "admitted_at": approved_at}
    expires_at = str(spec["expires_at"])
    if expires_at:
        retention["expires_at"] = expires_at
    review_due_at = str(spec["review_due_at"])
    tags = spec["tags"] if isinstance(spec.get("tags"), list) else []
    record: dict[str, object] = {
        "schema_version": PROJECT_MEMORY_RECORD_SCHEMA_VERSION,
        "record_id": record_id,
        "candidate_id": f"cand_{record_id}",
        "revision": 1,
        "record_type": str(spec["record_type"]),
        "summary": str(spec["summary"]),
        "scope": {"kind": str(spec["scope_kind"]), "ref": str(spec["scope_ref"])},
        "tags": [str(tag) for tag in tags],
        "source": str(spec.get("source") or "retrieval_fixture"),
        "source_class": "omh_local",
        "source_ref": "",
        "derived_from": [],
        "admission": {
            "state": str(spec["admission_state"]),
            "review_id": review_id,
            "reviewer_claim": "operator",
            "admitted_at": approved_at,
            "policy_version": MEMORY_GOVERNANCE_POLICY_VERSION,
        },
        "retention": retention,
        "revalidation": {"deadline": review_due_at} if review_due_at else {},
        "approved_at": approved_at,
        "created_at": approved_at,
        "updated_at": approved_at,
        "ttl": {"expires_at": expires_at, "ttl_days": None},
        "staleness": {"stale_after": review_due_at, "review_due_at": review_due_at, "stale_after_days": None},
        "attention": {
            "schema_version": "omh_memory_attention/v1",
            "tier": str(spec["attention_tier"]),
            "reason": "retrieval_fixture",
            "previous_tier": "",
            "changed_at": approved_at,
        },
        "safety": {},
        "redaction_policy": "metadata_only",
        "claim_boundary": _RECORD_CLAIM_BOUNDARY,
    }
    if str(spec["observed"]):
        record["perspective"] = {"observer": str(spec["observer"]), "observed": str(spec["observed"])}
    if str(spec["superseded_by"]):
        record["superseded_by"] = str(spec["superseded_by"])
    digest = canonical_payload_digest(record)
    admission = record["admission"]
    if isinstance(admission, dict):
        admission["payload_digest"] = digest
    review = {
        "schema_version": PROJECT_MEMORY_REVIEW_RECORD_SCHEMA_VERSION,
        "review_id": review_id,
        "artifact_identity": stable_artifact_identity(record),
        "decision": str(spec["admission_state"]),
        "reviewer_claim": "operator",
        "payload_digest": digest,
        "policy_version": MEMORY_GOVERNANCE_POLICY_VERSION,
        "reviewed_at": approved_at,
        "claim_boundary": _REVIEW_CLAIM_BOUNDARY,
    }
    return record, review


def _case(
    case_id: str,
    intent: str,
    *,
    records: tuple[dict[str, object], ...],
    query: str = "",
    scope_kind: str | None = None,
    scope_ref: str | None = None,
    observer: str | None = None,
    observed: str | None = None,
    limit: int = 6,
    max_chars: int | None = None,
    include_stale: bool = False,
    include_archived: bool = False,
    pins: tuple[str, ...] = (),
    usage: tuple[tuple[str, int], ...] = (),
    included_order: tuple[str, ...] = (),
    excluded_reasons: tuple[tuple[str, str], ...] = (),
    absent: tuple[str, ...] = (),
    truncated: bool = False,
    sibling_hints: tuple[tuple[str, str], ...] = (),
    ineligible_included: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "intent": intent,
        "clock": FIXTURE_CLOCK_ISO,
        "query": query,
        "lens": {"scope_kind": scope_kind, "scope_ref": scope_ref, "observer": observer, "observed": observed},
        "budget": {"limit": limit, "max_chars": max_chars},
        "flags": {"include_stale": include_stale, "include_archived": include_archived},
        "pins": list(pins),
        "usage": {record_id: times for record_id, times in usage},
        "records": list(records),
        "expected": {
            "included_order": list(included_order),
            "excluded_reasons": dict(excluded_reasons),
            "absent": list(absent),
            "truncated": truncated,
            "sibling_hints": dict(sibling_hints),
            "ineligible_included": list(ineligible_included),
        },
    }


# Timestamps used across the corpus. Named so a case reads as a statement about
# freshness rather than as a date arithmetic puzzle.
_YOUNG = "2030-12-20T00:00:00Z"
_YOUNGER = "2030-12-28T00:00:00Z"
_AGING = "2030-10-01T00:00:00Z"
_OLD = "2029-06-01T00:00:00Z"
_PAST_DEADLINE = "2030-11-15T00:00:00Z"
_FUTURE_DEADLINE = "2032-06-01T00:00:00Z"


RETRIEVAL_CASES: tuple[dict[str, object], ...] = (
    _case(
        "exact_and_partial_relevance",
        "An exact multi-token match leads a partial one, and a record with no overlap is named, not dropped.",
        records=(
            _record("mem_r01_exact", "Staging deploys run canary batches before promotion", approved_at=_YOUNG, tags=("deploy", "staging")),
            _record("mem_r02_partial", "Deploys are announced in the release channel", approved_at=_YOUNG, tags=("deploy",)),
            _record("mem_r03_unrelated", "The parser owner is the platform team", approved_at=_YOUNG, tags=("parser",)),
        ),
        query="staging deploys canary",
        included_order=("mem_r01_exact", "mem_r02_partial"),
        excluded_reasons=(("mem_r03_unrelated", "no_query_overlap"),),
    ),
    _case(
        "no_match_falls_back_to_the_active_tier",
        "A query nothing answers serves the active records as an unqueried pack would, instead of an empty pack.",
        records=(
            _record("mem_r01_exact", "Staging deploys run canary batches before promotion", approved_at=_YOUNG, tags=("deploy", "staging")),
            _record("mem_r02_partial", "Deploys are announced in the release channel", approved_at=_YOUNG, tags=("deploy",)),
            _record("mem_r03_unrelated", "The parser owner is the platform team", approved_at=_YOUNG, tags=("parser",)),
        ),
        query="quantum ledger reconciliation",
        included_order=("mem_r01_exact", "mem_r02_partial", "mem_r03_unrelated"),
    ),
    _case(
        "no_match_fallback_leaves_reference_keyword_gated",
        "The no-match fallback re-admits only the active tier; a reference record still needs a keyword.",
        records=(
            _record("mem_r04_active", "Deploys wait for a green nightly run", approved_at=_YOUNG, tags=("deploy",)),
            _record(
                "mem_r05_reference",
                "The parser owner is the platform team",
                approved_at=_YOUNG,
                tags=("parser",),
                attention_tier="reference",
            ),
        ),
        query="quantum ledger reconciliation",
        included_order=("mem_r04_active",),
        excluded_reasons=(("mem_r05_reference", "no_query_overlap"),),
    ),
    _case(
        "partial_match_never_falls_back",
        "Once any record overlaps the query, an active record that does not stays out as no_query_overlap.",
        records=(
            _record("mem_r06_match", "Staging deploys run canary batches before promotion", approved_at=_YOUNG, tags=("staging",)),
            _record("mem_r07_miss", "The parser owner is the platform team", approved_at=_YOUNGER, tags=("parser",)),
        ),
        query="staging quantum",
        included_order=("mem_r06_match",),
        excluded_reasons=(("mem_r07_miss", "no_query_overlap"),),
    ),
    _case(
        "duplicate_records_collapse_to_the_newest",
        "Two approved records with the same normalized summary take one slot; the older one names why it is out.",
        records=(
            _record("mem_r08_older", "Hotfixes go through the release branch", approved_at=_AGING, tags=("hotfix",)),
            _record("mem_r09_newer", "  hotfixes GO through   the release branch ", approved_at=_YOUNGER, tags=("hotfix",)),
        ),
        query="hotfixes release branch",
        included_order=("mem_r09_newer",),
        excluded_reasons=(("mem_r08_older", "duplicate_record"),),
    ),
    _case(
        "same_topic_disagreement_names_the_cut_sibling",
        "Two records disagree on one topic and the budget keeps one: the cut side names the sibling that survived.",
        records=(
            _record("mem_r10_keeps", "Deploys wait for a green nightly run", approved_at=_YOUNGER, tags=("deploy",)),
            _record("mem_r11_cut", "Deploys ship without waiting for the nightly run", approved_at=_OLD, tags=("deploy",)),
        ),
        query="deploys nightly",
        limit=1,
        included_order=("mem_r10_keeps",),
        excluded_reasons=(("mem_r11_cut", "over_budget"),),
        truncated=True,
        sibling_hints=(("mem_r11_cut", "mem_r10_keeps"),),
    ),
    _case(
        "supersession_removes_the_corrected_revision",
        "A corrected record points at its replacement and leaves the pack as superseded, not as a relevance miss.",
        records=(
            _record("mem_r20_current", "Release notes are written before the tag is pushed", approved_at=_YOUNG, tags=("release",)),
            _record(
                "mem_r21_superseded",
                "Release notes are written after the tag is pushed",
                approved_at=_AGING,
                tags=("release",),
                superseded_by="mem_r20_current",
            ),
        ),
        query="release notes tag",
        included_order=("mem_r20_current",),
        excluded_reasons=(("mem_r21_superseded", "superseded"),),
    ),
    _case(
        "expired_volatile_record_leaves_the_pack",
        "A volatile record whose retention deadline has passed is excluded under its retention class, not silently.",
        records=(
            _record("mem_r30_live", "The staging database runs postgres 17", approved_at=_YOUNG, tags=("staging",)),
            _record(
                "mem_r31_expired",
                "The staging database runs postgres 16",
                approved_at=_AGING,
                tags=("staging",),
                retention_class="volatile",
                expires_at=_PAST_DEADLINE,
                contamination_class="expired",
            ),
        ),
        query="staging database postgres",
        included_order=("mem_r30_live",),
        excluded_reasons=(("mem_r31_expired", "expired_volatile"),),
    ),
    _case(
        "stale_review_is_excluded_by_default",
        "A passed revalidation deadline holds the record out of the default pack under its own reason code.",
        records=(
            _record("mem_r40_fresh", "The nightly job runs on the ubuntu runner", approved_at=_YOUNG, tags=("nightly",), review_due_at=_FUTURE_DEADLINE),
            _record(
                "mem_r41_stale",
                "The nightly job runs on the macos runner",
                approved_at=_AGING,
                tags=("nightly",),
                review_due_at=_PAST_DEADLINE,
                contamination_class="stale",
            ),
        ),
        query="nightly job runner",
        included_order=("mem_r40_fresh",),
        excluded_reasons=(("mem_r41_stale", "stale_review_required"),),
    ),
    _case(
        "include_stale_surfaces_the_record_carrying_ineligible_evidence",
        "The inspection affordance returns the stale record, and it arrives still marked ineligible.",
        records=(
            _record("mem_r40_fresh", "The nightly job runs on the ubuntu runner", approved_at=_YOUNG, tags=("nightly",), review_due_at=_FUTURE_DEADLINE),
            _record(
                "mem_r41_stale",
                "The nightly job runs on the macos runner",
                approved_at=_AGING,
                tags=("nightly",),
                review_due_at=_PAST_DEADLINE,
                contamination_class="stale",
            ),
        ),
        query="nightly job runner",
        include_stale=True,
        included_order=("mem_r40_fresh", "mem_r41_stale"),
        ineligible_included=("mem_r41_stale",),
    ),
    _case(
        "scope_lens_isolates_a_foreign_scope",
        "A record filed under another scope never reaches the pack the lens asked for.",
        records=(
            _record("mem_r50_in_scope", "The default scope owns the release checklist", approved_at=_YOUNG, tags=("release",)),
            _record(
                "mem_r51_other_scope",
                "The other scope owns the release checklist",
                approved_at=_YOUNG,
                tags=("release",),
                scope_ref="other",
                contamination_class="foreign_scope",
            ),
        ),
        query="release checklist",
        scope_kind="project",
        scope_ref=FIXTURE_PROJECT_IDENTITY,
        included_order=("mem_r50_in_scope",),
        absent=("mem_r51_other_scope",),
    ),
    _case(
        "perspective_lens_isolates_another_actors_record",
        "An unscoped record and this actor's record pass; a record observed for another actor does not.",
        records=(
            _record("mem_r60_unscoped", "Handoffs cite the branch name", approved_at=_YOUNG, tags=("handoff",)),
            _record("mem_r61_mine", "Handoffs cite the branch name and the base revision", approved_at=_YOUNG, tags=("handoff",), observer="omh", observed="claude-code"),
            _record(
                "mem_r62_theirs",
                "Handoffs cite the branch name and the ticket id",
                approved_at=_YOUNG,
                tags=("handoff",),
                observer="omh",
                observed="codex",
                contamination_class="foreign_perspective",
            ),
        ),
        query="handoffs branch name",
        observer="omh",
        observed="claude-code",
        included_order=("mem_r60_unscoped", "mem_r61_mine"),
        absent=("mem_r62_theirs",),
    ),
    _case(
        "pins_lead_without_owning_the_whole_budget",
        "A pinned anchor with no query overlap leads the pack, and the strongest match still takes a slot.",
        records=(
            _record("mem_r70_pinned", "The team reviews architecture decisions on friday", approved_at=_OLD, tags=("process",)),
            _record("mem_r71_strong", "Cache invalidation runs on every deploy", approved_at=_YOUNG, tags=("cache", "deploy")),
            _record("mem_r72_weak", "Cache warmers are optional", approved_at=_YOUNG, tags=("cache",)),
        ),
        query="cache invalidation deploy",
        limit=2,
        pins=("mem_r70_pinned",),
        included_order=("mem_r70_pinned", "mem_r71_strong"),
        excluded_reasons=(("mem_r72_weak", "over_budget"),),
        truncated=True,
    ),
    _case(
        "attention_tier_outranks_relevance",
        "An active record leads a reference record that matches the query more strongly.",
        records=(
            _record("mem_r80_active", "Migrations are applied by the release job", approved_at=_YOUNG, tags=("migration",)),
            _record(
                "mem_r81_reference",
                "Migrations are applied by the release job during the release window",
                approved_at=_YOUNG,
                tags=("migration", "release"),
                attention_tier="reference",
            ),
        ),
        query="migrations release job window",
        included_order=("mem_r80_active", "mem_r81_reference"),
    ),
    _case(
        "archive_tier_leaves_the_default_pack_named",
        "An archived record is held back under its own reason code while it stays in the store.",
        records=(
            _record("mem_r90_active", "Feature flags are removed after two releases", approved_at=_YOUNG, tags=("flags",)),
            _record(
                "mem_r91_archived",
                "Feature flags are removed after five releases",
                approved_at=_AGING,
                tags=("flags",),
                attention_tier="archive",
                contamination_class="archived",
            ),
        ),
        query="feature flags releases",
        included_order=("mem_r90_active",),
        excluded_reasons=(("mem_r91_archived", "archived_tier"),),
    ),
    _case(
        "archive_tier_returns_on_an_explicit_archived_query",
        "The archived record comes back when the operator asks for it, behind its active peer.",
        records=(
            _record("mem_r90_active", "Feature flags are removed after two releases", approved_at=_YOUNG, tags=("flags",)),
            _record(
                "mem_r91_archived",
                "Feature flags are removed after five releases",
                approved_at=_AGING,
                tags=("flags",),
                attention_tier="archive",
                contamination_class="archived",
            ),
        ),
        query="feature flags releases",
        include_archived=True,
        included_order=("mem_r90_active", "mem_r91_archived"),
    ),
    _case(
        "age_tie_breaks_deterministically_by_record_id",
        "Two records of equal relevance, equal age, and equal usage order by record id, never by store order.",
        records=(
            _record("mem_ra1_first", "Backups run every night", approved_at=_YOUNG, tags=("backup",)),
            _record("mem_ra2_second", "Backups run each night", approved_at=_YOUNG, tags=("backup",)),
        ),
        query="backups night",
        included_order=("mem_ra1_first", "mem_ra2_second"),
    ),
    _case(
        "record_limit_truncation_reports_over_budget",
        "A record-count budget cuts the tail and says so, rather than returning a short pack silently.",
        records=(
            _record("mem_rb1", "Queue workers retry failed jobs twice", approved_at=_YOUNGER, tags=("queue",)),
            _record("mem_rb2", "Queue workers log every retry", approved_at=_YOUNG, tags=("queue",)),
            _record("mem_rb3", "Queue depth alerts fire at one thousand", approved_at=_AGING, tags=("queue",)),
            _record("mem_rb4", "Queue names are prefixed by the service", approved_at=_OLD, tags=("queue",)),
        ),
        query="queue workers retry",
        limit=2,
        included_order=("mem_rb1", "mem_rb2"),
        excluded_reasons=(("mem_rb3", "over_budget"), ("mem_rb4", "over_budget")),
        truncated=True,
    ),
    _case(
        "character_budget_truncation_is_reported_separately",
        "A character budget cuts the pack under the same over_budget reason with the record limit left slack.",
        records=(
            _record("mem_rc1", "Queue workers retry failed jobs twice", approved_at=_YOUNGER, tags=("queue",)),
            _record("mem_rc2", "Queue workers log every retry attempt for audit", approved_at=_YOUNG, tags=("queue",)),
            _record("mem_rc3", "Queue depth alerts fire at one thousand messages", approved_at=_AGING, tags=("queue",)),
        ),
        query="queue workers retry",
        limit=6,
        max_chars=40,
        included_order=("mem_rc1",),
        excluded_reasons=(("mem_rc2", "over_budget"), ("mem_rc3", "over_budget")),
        truncated=True,
    ),
)


def fixture_digest(cases: tuple[dict[str, object], ...] = RETRIEVAL_CASES) -> str:
    """One digest over the retained corpus, keys sorted, separators fixed."""
    return hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
