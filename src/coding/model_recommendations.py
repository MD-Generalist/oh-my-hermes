"""Editable editorial model recommendations and owner-specific projections.

This module is deliberately pure except for the explicit JSON override loader.
It records editorial candidate order and resolves it against caller-confirmed
active models. It does not discover providers, inspect credentials, invoke a
model, or own Hermes provider configuration.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Final, Iterable, Mapping

from .model_contracts import dated_snapshot_base
from .model_routing import MODEL_CATEGORIES, MODEL_ROLES


MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION: Final[str] = "model_recommendation_catalog/v2"
MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION: Final[str] = "model_recommendation_overrides/v2"
MODEL_RECOMMENDATION_RESOLUTION_SCHEMA_VERSION: Final[str] = "model_recommendation_resolution/v3"
_LEGACY_MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION: Final[str] = "model_recommendation_catalog/v1"
_LEGACY_MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION: Final[str] = "model_recommendation_overrides/v1"
MODEL_RECOMMENDATION_STATUSES: Final[tuple[str, ...]] = (
    "resolved",
    "choice_required",
    "owner_default",
)
MODEL_RECOMMENDATION_OWNERS: Final[tuple[str, ...]] = ("hermes", "maestro")
HERMES_MODEL_SETUP_ROLE_SLOTS: Final[tuple[str, ...]] = ("main",)
MODEL_RECOMMENDATION_DOMAINS: Final[tuple[str, ...]] = ("x_platform_data",)
MODEL_RECOMMENDATION_LAST_RESORT_SLOTS: Final[tuple[str, ...]] = ("any",)

_CANDIDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "model_alias",
        "model_family",
        "preferred_provider_families",
        "reasoning_effort",
        "reasoning",
        "recommendation_source",
    }
)
_REQUIRED_CANDIDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"model_alias", "model_family", "preferred_provider_families", "reasoning"}
)
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "credential",
    "password",
    "secret",
    "token",
)
_READ_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError)


def _candidate(
    model_alias: str,
    model_family: str,
    preferred_provider_families: tuple[str, ...],
    *,
    reasoning_effort: str = "",
    reasoning: str,
) -> dict[str, object]:
    return {
        "model_alias": model_alias,
        "model_family": model_family,
        "preferred_provider_families": list(preferred_provider_families),
        "reasoning_effort": reasoning_effort,
        "reasoning": reasoning,
        "recommendation_source": "shipped_editorial",
    }


_KIMI_K3 = _candidate(
    "kimi-k3",
    "kimi",
    ("apitopia", "kimi-coding", "openrouter", "opencode"),
    reasoning="Editorial general-work recommendation; not a benchmark claim.",
)
_OPUS_55 = _candidate(
    "claude-opus-5-5",
    "claude",
    ("ccapi", "anthropic", "openrouter"),
    reasoning="Editorial high-capability alternative; not a benchmark claim.",
)
# The Claude vendor order inside every chain is Fable 5.1 -> Opus (owner
# decision, 2026-09-06); Opus 5.5 took Opus 5's slots on 2026-09-23 at the
# same positions and efforts (editorial, unmeasured), and Opus 5 left the
# shipped chains under the superseded-generation rule below. Claude Mythos 5.1 is the same model as Fable 5.1
# served only to Project Glasswing-approved organizations, so a shipped chain
# naming it would read as a second model most accounts cannot reach; a user
# who explicitly names it is still recognized and routed. Fable 5 left the
# shipped chains with the other superseded generations (owner decision,
# 2026-09-11): a shipped chain names the current generation of each line, and
# a machine whose provider still serves only the older id keeps it through
# ~/.omh/routing/model-chains.json rather than through the public table.
_FABLE_51 = _candidate(
    "claude-fable-5-1",
    "claude",
    ("ccapi", "anthropic", "openrouter"),
    reasoning="Editorial most-capable Claude recommendation; not a benchmark claim.",
)
# GPT-6.1 Sol (2026-09-29) holds every slot GPT-6 Sol held (owner decision,
# 2026-10-01), each at the effort that slot already carried: medium in the
# shared last resort, high at the head of `deep` and in the main-role
# suggestions. Every one of those rungs is on its ladder, which unlike GPT-6
# Sol's has no `none`. GPT-6 Sol itself had taken the slots of GPT-5.6 Sol and
# GPT-5.6 Terra on 2026-09-23. Same list price as GPT-6 Sol with a cheaper
# cache read. Editorial and unmeasured; GPT-6 Sol left the shipped chains
# under the superseded-generation rule (owner decision, 2026-09-11).
_SOL = _candidate(
    "gpt-6.1-sol",
    "gpt",
    ("openai-codex", "openai"),
    reasoning_effort="medium",
    reasoning="Editorial reasoning recommendation; not a benchmark claim.",
)
# GPT-6 Astra (2026-09-03) heads the frontier GPT slots. The Luna lane is a
# cost-tier pick below it: Astra lists at 5x Sol and 100x Luna.
_ASTRA = _candidate(
    "gpt-6-astra",
    "gpt",
    ("openai-codex", "openai"),
    reasoning_effort="xhigh",
    reasoning="Editorial GPT frontier recommendation; staged rollout, not a benchmark claim.",
)
# DeepSeek V4.1 Flash (released 2026-09-10) takes the slots DeepSeek V3.2
# held: the reasoning-capable budget fall-through behind Sol on `deep` and
# the DeepSeek entry on `unspecified-low`. Its documented effort ladder is
# low/high/max with high as the default, so both placements name a
# documented rung. The chain names `deepseek-flash` — the id the vendor's
# own API serves and Hermes forwards — because the first-party endpoint
# rejects the versioned spelling `deepseek-v4.1-flash` with HTTP 400
# (observed in the Hermes DeepSeek profile, 2026-09-11). The exact contract
# stays versioned in `model_contracts.py`; `deepseek-flash` reaches it as a
# declared projection with a read date, and the next Flash release re-pins
# the pointer through the onboarding loop rather than by accident.
_DEEPSEEK_FLASH = _candidate(
    "deepseek-flash",
    "deepseek",
    ("deepseek", "openrouter", "opencode"),
    reasoning="Editorial budget reasoning recommendation; not a benchmark claim.",
)
_GLM_53 = _candidate(
    "glm-5.3",
    "glm",
    ("zai", "openrouter", "opencode"),
    reasoning="Editorial low-cost work recommendation; not a benchmark claim.",
)
_GLM_53_FLASH = _candidate(
    "glm-5.3-flash",
    "glm",
    ("zai", "openrouter", "opencode"),
    reasoning="Editorial fast alternative; not a benchmark claim.",
)
# Grok Build 0.1 holds the X-platform slot Grok Code Fast held (owner
# decision, 2026-10-01). xAI retired Grok Code Fast on 2026-05-15 and lists
# `grok-code-fast-1`, the bare `grok-code-fast`, and `grok-code-fast-1-0825`
# as aliases of `grok-build-0.1` (docs.x.ai model page and may-15-retirement
# guide, read 2026-10-01), so on xAI's API the old slot already ran this
# model; the chain now names the id xAI serves.
_GROK = _candidate(
    "grok-build-0.1",
    "grok",
    ("xai", "openrouter"),
    reasoning="Editorial X-platform domain affinity; not a benchmark claim.",
)
_GEMINI = _candidate(
    "gemini-3.1-pro",
    "gemini",
    ("google", "gemini", "openrouter"),
    reasoning="Editorial synthesis and visual-work alternative; not a benchmark claim.",
)
_QWEN = _candidate(
    "qwen3-coder",
    "qwen",
    ("qwen-oauth", "openrouter", "opencode"),
    reasoning="Editorial coding and structured-writing alternative; not a benchmark claim.",
)
# GPT-6 Luna took GPT-5.6 Luna's slots on 2026-09-23 at the same positions
# and effort (editorial, unmeasured); GPT-5.6 Luna left the shipped chains
# under the superseded-generation rule (owner decision, 2026-09-11).
_LUNA = _candidate(
    "gpt-6-luna",
    "gpt",
    ("openai-codex", "openai"),
    reasoning="Editorial fast GPT-tier quick candidate; not a benchmark claim.",
)
# Claude Haiku 4.5 was priced before it was ever recommended: the price table
# has carried a `claude-haiku-4-5` row for a user who names it, but no shipped
# chain recommended it, so it had no editorial candidate and no
# provider-families row. `simple-work` is the first chain to name it, and that
# is what pulls it onto the catalog side. Families match every other Claude
# entry, so the entitlement vocabulary does not widen.
_HAIKU_45 = _candidate(
    "claude-haiku-4-5",
    "claude",
    ("ccapi", "anthropic", "openrouter"),
    reasoning="Editorial low-cost Claude candidate; not a benchmark claim.",
)

# The category keys are exactly the existing closed vocabulary. Categories for
# which the approved profile defines no recommendation remain explicit empty
# chains rather than gaining invented defaults. `main` is separate because it
# is a Hermes setup slot, and `x_platform_data` is separate because it is a
# domain affinity.
def _with_effort(candidate: Mapping[str, object], effort: str) -> dict[str, object]:
    return dict(deepcopy(dict(candidate)), reasoning_effort=effort)


# Every category carries an explicit default reasoning effort (owner decision,
# 2026-08-18): before this, only ultrabrain/deep declared one and every other
# routed lane silently inherited the parent session's level — a wave looked
# like "everything runs medium" no matter which category chose the model. The
# category IS the model+effort pair; a per-lane override still wins.
SHIPPED_MODEL_RECOMMENDATIONS: Final[dict[str, object]] = {
    "schema_version": MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION,
    "categories": {
        "ultrabrain": [deepcopy(_ASTRA)],
        # DeepSeek gives deep a reasoning-capable budget fallback from a
        # fourth provider ecosystem — before it, deep sat entirely on GPT
        # and one rejected ecosystem exhausted the chain (owner rule below).
        "deep": [_with_effort(_SOL, "high"), _with_effort(_DEEPSEEK_FLASH, "high")],
        # Architecture and system-design lanes (owner request, 2026-08-19):
        # deepest declared effort across three provider ecosystems so a
        # rejected ecosystem cannot exhaust the chain. Efforts stay xhigh
        # end-to-end — the category IS "design at full depth".
        "architect": [
            _with_effort(_FABLE_51, "xhigh"),
            _with_effort(_ASTRA, "xhigh"),
            _with_effort(_KIMI_K3, "xhigh"),
        ],
        "unspecified-high": [_with_effort(_KIMI_K3, "medium"), _with_effort(_OPUS_55, "medium")],
        # Owner rule (2026-08-19): a chain that would otherwise sit in one
        # provider ecosystem ends with a comparable-tier candidate from
        # another, so one rejected ecosystem cannot exhaust the whole chain
        # (observed live: a Codex-billed account 400'd every GLM child and
        # quick fell straight to inherit). Tails are the owner's explicit
        # picks: Opus 5.5 at low closes unspecified-low, and quick runs the
        # owner-ordered Ultrafast -> Kimi -> Luna -> Fable sequence.
        # GLM 5.3 leads (owner decision, 2026-08-31). The 5.2 entries left
        # with the other superseded generations (owner decision, 2026-09-11).
        "unspecified-low": [
            _with_effort(_GLM_53, "low"),
            _with_effort(_DEEPSEEK_FLASH, "low"),
            _with_effort(_OPUS_55, "low"),
        ],
        "quick": [
            _with_effort(_GLM_53_FLASH, "low"),
            _with_effort(_KIMI_K3, "low"),
            _with_effort(_LUNA, "low"),
            _with_effort(_FABLE_51, "low"),
        ],
        "writing": [
            _with_effort(_KIMI_K3, "medium"),
            _with_effort(_QWEN, "medium"),
            _with_effort(_GEMINI, "medium"),
        ],
        "visual-engineering": [
            _with_effort(_FABLE_51, "high"),
            _with_effort(_KIMI_K3, "high"),
        ],
        "artistry": [
            _with_effort(_GEMINI, "high"),
            _with_effort(_FABLE_51, "high"),
            _with_effort(_KIMI_K3, "high"),
        ],
        # The three categories below are appended rather than interleaved
        # (owner request, 2026-09-11). Position is not cosmetic: canonical
        # order breaks ties in `mixture_category_for`, so appending is what
        # keeps `(claude-opus-5-5, medium)` labeled unspecified-high instead of
        # moving to capable on an equal-position tie.
        #
        # capable: the strong general-work lane, one rung under the frontier
        # categories, spanning four provider ecosystems. Effort is medium
        # because that is how opus-5-5 and kimi-k3 are already used in
        # unspecified-high, the nearest neighbour to what this is for. High
        # was rejected: `(claude-fable-5-1, high)` is visual-engineering's
        # chain head, so a capable chain at high would have taken that
        # category's projection label away from it.
        "capable": [
            _with_effort(_FABLE_51, "medium"),
            _with_effort(_OPUS_55, "medium"),
            _with_effort(_KIMI_K3, "medium"),
            _with_effort(_GLM_53, "medium"),
        ],
        # simple-work: cheap models for work that is small rather than fast.
        # It differs from quick in what leads — quick leads with the
        # cheapest-available GLM tier, simple-work leads with a GPT-family
        # small model — so a user who wants GPT behavior on a trivial task
        # has somewhere to ask for it. Low throughout, matching quick.
        "simple-work": [
            _with_effort(_LUNA, "low"),
            _with_effort(_DEEPSEEK_FLASH, "low"),
            _with_effort(_HAIKU_45, "low"),
        ],
        # deep-work: the GPT frontier model on a long task. Effort is high,
        # NOT xhigh: at xhigh this chain's head would be ultrabrain's head,
        # and since head-match resolves in canonical order the earlier
        # category would take every projection and leave deep-work a label
        # nothing could ever carry. High also mirrors `deep`, the same shape
        # one price rung down. The original request named Sol behind Astra;
        # the owner dropped it (2026-09-11, after that request) for
        # consistency with the same-day superseded-generation retirement, so
        # the single entry is deliberate.
        "deep-work": [_with_effort(_ASTRA, "high")],
    },
    "role_suggestions": {
        "main": [
            deepcopy(_KIMI_K3),
            deepcopy(_FABLE_51),
            deepcopy(_OPUS_55),
            deepcopy(_ASTRA),
            _with_effort(_SOL, "high"),
        ],
    },
    "domain_affinities": {
        "x_platform_data": [deepcopy(_GROK), deepcopy(_KIMI_K3), deepcopy(_GEMINI)],
    },
    # One shared final attempt, consulted only after every candidate of the
    # selected chain is unavailable. A profile whose chains concentrate in one
    # provider ecosystem otherwise drops straight to the owner default when
    # that ecosystem is not confirmed active. This is not a per-category
    # anchor: it never outranks a category, role, or domain candidate, and it
    # never rescues an unavailable explicit model.
    #
    # Opus 5.5 names `medium` here rather than no effort (2026-09-24,
    # editorial, unmeasured). An entry with no effort leaves the level to
    # whatever runs it: fanout dispatch passes no effort flag and the executor
    # CLI's own default applies, and a prepared route carries none. Anthropic's
    # Opus 5.5 guidance is to set effort explicitly, `medium` is the rung it
    # already carries in `unspecified-high` and `capable`, and Sol's entry beside it already
    # names `medium`, so the row is now uniform. Named follow-up: bench arm O4
    # (Opus 5.5 at `medium`, live-model-tools) in MODEL_OPTI.md.
    "last_resort": {
        "any": [_with_effort(_OPUS_55, "medium"), deepcopy(_SOL)],
    },
}


def serialize_recommendation_payload(payload: Mapping[str, object]) -> str:
    """Return stable compact JSON for review, fingerprints, and tests."""
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def load_recommendation_overrides(
    source: Path | str | Mapping[str, object],
) -> dict[str, object]:
    """Load and normalize one secret-free user override document.

    Override documents replace only the chains they name. They cannot add a
    category, turn a Hermes role into a category, add a role slot/domain, or
    carry provider configuration. Candidate provider families are preference
    metadata only; actual provider availability remains caller-supplied truth.
    """
    if isinstance(source, Mapping):
        raw: object = deepcopy(dict(source))
    else:
        path = Path(source)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except _READ_ERRORS as exc:
            raise ValueError("model recommendation override is not readable JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("model recommendation override must be a JSON object")
    _reject_secret_keys(raw)
    schema_version = str(raw.get("schema_version", ""))
    supported_versions = (
        _LEGACY_MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION,
        MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION,
    )
    if schema_version not in supported_versions:
        raise ValueError(
            f"model recommendation override must use one of {supported_versions}"
        )
    allowed_top = {
        "schema_version",
        "categories",
        "role_suggestions",
        "domain_affinities",
    }
    if schema_version == MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION:
        allowed_top.add("last_resort")
    unknown_top = set(raw) - allowed_top
    if unknown_top:
        raise ValueError(f"unsupported model recommendation override fields: {sorted(unknown_top)}")

    normalized: dict[str, object] = {
        "schema_version": schema_version,
        "categories": _normalize_section(
            raw.get("categories", {}), allowed=MODEL_CATEGORIES, section="categories"
        ),
        "role_suggestions": _normalize_section(
            raw.get("role_suggestions", {}),
            allowed=HERMES_MODEL_SETUP_ROLE_SLOTS,
            section="role_suggestions",
        ),
        "domain_affinities": _normalize_section(
            raw.get("domain_affinities", {}),
            allowed=MODEL_RECOMMENDATION_DOMAINS,
            section="domain_affinities",
        ),
    }
    if schema_version == MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION:
        normalized["last_resort"] = _normalize_section(
            raw.get("last_resort", {}),
            allowed=MODEL_RECOMMENDATION_LAST_RESORT_SLOTS,
            section="last_resort",
        )
    return normalized


def merge_recommendation_catalog(
    catalog: Mapping[str, object],
    overrides: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return a new catalog with named user chains replacing editorial ones."""
    schema_version = str(catalog.get("schema_version", ""))
    supported_versions = (
        _LEGACY_MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION,
        MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION,
    )
    if schema_version not in supported_versions:
        raise ValueError("unsupported model recommendation catalog schema")
    if (
        schema_version == _LEGACY_MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION
        and "last_resort" in catalog
    ):
        raise ValueError("model_recommendation_catalog/v1 cannot define last_resort")
    merged = deepcopy(dict(catalog))
    if overrides is None:
        return merged
    # Revalidate even a payload returned by the loader. Callers may mutate a
    # normal-looking dict before reuse; no alternate path may bypass the same
    # closed-surface and secret-field checks as a file-loaded override.
    normalized = load_recommendation_overrides(overrides)
    for section in ("categories", "role_suggestions", "domain_affinities", "last_resort"):
        destination = merged.get(section)
        replacements = normalized.get(section)
        if replacements is None:
            continue
        if section == "last_resort" and destination is None:
            if not replacements:
                continue
            merged[section] = {}
            destination = merged[section]
        if not isinstance(destination, dict) or not isinstance(replacements, Mapping):
            raise ValueError(f"malformed model recommendation catalog section: {section}")
        for name, chain in replacements.items():
            destination[str(name)] = deepcopy(chain)
    if (
        schema_version == _LEGACY_MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION
        and normalized.get("last_resort")
    ):
        merged["schema_version"] = MODEL_RECOMMENDATION_CATALOG_SCHEMA_VERSION
    return merged


def resolve_model_recommendation(
    *,
    owner: str,
    active_models: Iterable[str | Mapping[str, object]],
    category: str = "",
    role_slot: str = "",
    domain: str = "",
    explicit_model: str = "",
    overrides: Mapping[str, object] | None = None,
    catalog: Mapping[str, object] = SHIPPED_MODEL_RECOMMENDATIONS,
) -> dict[str, object]:
    """Resolve one selector to confirmed-active models and project by owner.

    Explicit models are fail-closed: an unavailable explicit request returns
    ``choice_required`` and never falls through to an editorial candidate.
    Recommendation chains skip unavailable heads and select the next confirmed,
    owner-compatible candidate. When the whole selected chain is unavailable,
    the shared ``last_resort`` chain is tried as one final attempt and, when it
    resolves, is reported with source ``last_resort_chain``. With no eligible
    candidate anywhere, ``owner_default`` keeps the selected owner's native
    default model in effect without blocking the surrounding installation.
    """
    normalized_owner = str(owner or "").strip().casefold()
    if normalized_owner not in MODEL_RECOMMENDATION_OWNERS:
        raise ValueError(f"owner must be one of {MODEL_RECOMMENDATION_OWNERS}")
    selector_section, selector_name = _selector(category, role_slot, domain, bool(explicit_model))
    merged = merge_recommendation_catalog(catalog, overrides)
    normalized_active = _normalized_active_models(active_models, normalized_owner)
    chain = _catalog_chain(merged, selector_section, selector_name) if selector_section else []
    eligible_chain: list[dict[str, object]] = []
    inactive_candidates: list[str] = []
    for candidate in chain:
        active = _active_for_candidate(candidate, normalized_active)
        if active is None:
            inactive_candidates.append(str(candidate["model_alias"]))
            continue
        eligible_chain.append(_resolved_candidate(candidate, active))

    requested = str(explicit_model or "").strip()
    if requested:
        explicit = _active_for_explicit(requested, normalized_active)
        if explicit is None:
            return _resolution_payload(
                owner=normalized_owner,
                selector_section=selector_section,
                selector_name=selector_name,
                status="choice_required",
                source="explicit_model",
                selected=None,
                projection=None,
                requested_model=requested,
                available_chain=[str(entry["model_alias"]) for entry in eligible_chain],
                inactive_candidates=inactive_candidates,
            )
        selected = _resolved_explicit(explicit)
        projection = _projection(normalized_owner, selector_name or requested, [selected])
        return _resolution_payload(
            owner=normalized_owner,
            selector_section=selector_section,
            selector_name=selector_name,
            status="resolved",
            source="explicit_model",
            selected=selected,
            projection=projection,
            requested_model=requested,
            available_chain=[str(selected["model_alias"])],
            inactive_candidates=inactive_candidates,
        )

    if not eligible_chain:
        for candidate in _last_resort_chain(merged):
            active = _active_for_candidate(candidate, normalized_active)
            if active is None:
                alias = str(candidate["model_alias"])
                if alias not in inactive_candidates:
                    inactive_candidates.append(alias)
                continue
            eligible_chain.append(_resolved_candidate(candidate, active))
        if eligible_chain:
            selected = eligible_chain[0]
            return _resolution_payload(
                owner=normalized_owner,
                selector_section=selector_section,
                selector_name=selector_name,
                status="resolved",
                source="last_resort_chain",
                selected=selected,
                projection=_projection(normalized_owner, selector_name, eligible_chain),
                requested_model="",
                available_chain=[str(entry["model_alias"]) for entry in eligible_chain],
                inactive_candidates=inactive_candidates,
            )
        return _resolution_payload(
            owner=normalized_owner,
            selector_section=selector_section,
            selector_name=selector_name,
            status="owner_default",
            source="owner_default",
            selected=None,
            projection=None,
            requested_model="",
            available_chain=[],
            inactive_candidates=inactive_candidates,
        )
    selected = eligible_chain[0]
    return _resolution_payload(
        owner=normalized_owner,
        selector_section=selector_section,
        selector_name=selector_name,
        status="resolved",
        source="recommendation_chain",
        selected=selected,
        projection=_projection(normalized_owner, selector_name, eligible_chain),
        requested_model="",
        available_chain=[str(entry["model_alias"]) for entry in eligible_chain],
        inactive_candidates=inactive_candidates,
    )


def _normalize_section(
    value: object,
    *,
    allowed: tuple[str, ...],
    section: str,
) -> dict[str, list[dict[str, object]]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{section} override must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"unsupported {section} names: {sorted(unknown)}")
    normalized: dict[str, list[dict[str, object]]] = {}
    for name in allowed:
        if name not in value:
            continue
        raw_chain = value[name]
        if not isinstance(raw_chain, (list, tuple)):
            raise ValueError(f"{section}.{name} must be an ordered candidate list")
        normalized[name] = [_normalize_candidate(item) for item in raw_chain]
    return normalized


def _normalize_candidate(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("model recommendation candidate must be an object")
    unknown = set(value) - _CANDIDATE_FIELDS
    missing = _REQUIRED_CANDIDATE_FIELDS - set(value)
    if unknown or missing:
        raise ValueError(
            f"model recommendation candidate fields invalid; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    model_alias = _identifier(value.get("model_alias"), "model_alias")
    model_family = _identifier(value.get("model_family"), "model_family")
    raw_providers = value.get("preferred_provider_families")
    if not isinstance(raw_providers, (list, tuple)) or not raw_providers:
        raise ValueError("preferred_provider_families must be a non-empty ordered list")
    providers = [_identifier(item, "preferred_provider_family") for item in raw_providers]
    if len(set(providers)) != len(providers):
        raise ValueError("preferred_provider_families must not contain duplicates")
    reasoning = value.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip() or len(reasoning) > 500 or "\n" in reasoning:
        raise ValueError("reasoning must be one non-empty bounded line")
    effort = value.get("reasoning_effort", "")
    if not isinstance(effort, str) or (effort and not _IDENTIFIER_RE.fullmatch(effort)):
        raise ValueError("reasoning_effort must be an opaque identifier")
    return {
        "model_alias": model_alias,
        "model_family": model_family,
        "preferred_provider_families": providers,
        "reasoning_effort": effort,
        "reasoning": reasoning.strip(),
        "recommendation_source": "user_override",
    }


def _reject_secret_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(marker in normalized for marker in _SECRET_KEY_MARKERS):
                raise ValueError("model recommendation overrides cannot contain secret fields")
            _reject_secret_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_keys(item)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded opaque identifier")
    return value


def _selector(
    category: str,
    role_slot: str,
    domain: str,
    has_explicit: bool,
) -> tuple[str, str]:
    values = (
        ("categories", str(category or "").strip()),
        ("role_suggestions", str(role_slot or "").strip()),
        ("domain_affinities", str(domain or "").strip()),
    )
    selected = [(section, name) for section, name in values if name]
    if len(selected) > 1:
        raise ValueError("category, role_slot, and domain are mutually exclusive")
    if not selected:
        if has_explicit:
            return "", ""
        raise ValueError("one recommendation selector is required")
    section, name = selected[0]
    allowed = {
        "categories": MODEL_CATEGORIES,
        "role_suggestions": HERMES_MODEL_SETUP_ROLE_SLOTS,
        "domain_affinities": MODEL_RECOMMENDATION_DOMAINS,
    }[section]
    if name not in allowed:
        raise ValueError(f"unsupported {section} selector: {name}")
    return section, name


def _catalog_chain(
    catalog: Mapping[str, object],
    section: str,
    name: str,
) -> list[Mapping[str, object]]:
    table = catalog.get(section)
    if not isinstance(table, Mapping):
        raise ValueError(f"malformed model recommendation catalog section: {section}")
    chain = table.get(name)
    if not isinstance(chain, list):
        raise ValueError(f"malformed model recommendation chain: {section}.{name}")
    if not all(isinstance(candidate, Mapping) for candidate in chain):
        raise ValueError(f"malformed model recommendation candidate: {section}.{name}")
    return list(chain)


def _last_resort_chain(catalog: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return the shared final-attempt chain, or empty when none is configured."""
    table = catalog.get("last_resort")
    if not isinstance(table, Mapping) or "any" not in table:
        return []
    return _catalog_chain(catalog, "last_resort", "any")


def _normalized_active_models(
    models: Iterable[str | Mapping[str, object]],
    owner: str,
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for value in models:
        if isinstance(value, str):
            alias = value.strip()
            if alias:
                normalized.append(
                    {"model_alias": alias, "model_id": alias, "provider": "", "provider_family": "", "model_family": ""}
                )
            continue
        if not isinstance(value, Mapping):
            continue
        status = str(value.get("status", "")).strip().casefold()
        if status != "confirmed_active" and value.get("active") is not True:
            continue
        owners = value.get("compatible_owners", value.get("owners", MODEL_RECOMMENDATION_OWNERS))
        if isinstance(owners, str):
            compatible = {owners.strip().casefold()}
        elif isinstance(owners, (list, tuple, set, frozenset)):
            compatible = {str(item).strip().casefold() for item in owners}
        else:
            compatible = set(MODEL_RECOMMENDATION_OWNERS)
        if owner not in compatible:
            continue
        alias = str(value.get("model_alias", value.get("alias", ""))).strip()
        model_id = str(value.get("model_id", value.get("model", alias))).strip()
        provider = str(value.get("provider", "")).strip()
        if not alias:
            alias = model_id.rsplit("/", 1)[-1]
        if not alias or not model_id:
            continue
        normalized.append(
            {
                "model_alias": alias,
                "model_id": model_id,
                "provider": provider,
                "provider_family": str(value.get("provider_family", provider)).strip(),
                "model_family": str(value.get("model_family", value.get("family", ""))).strip(),
            }
        )
    return normalized


def _active_spellings(active: Mapping[str, str]) -> set[str]:
    """Every spelling a confirmed-active model answers to.

    As recorded, unqualified, and — when the provider serves a dated snapshot
    (`gpt-6.1-sol-YYYY-MM-DD`) — the base alias it is a snapshot of, so the
    chain entry `gpt-6.1-sol` still counts the model as available and the
    route keeps the id as served.
    """
    model_id = active["model_id"]
    spellings = {active["model_alias"], model_id, model_id.rsplit("/", 1)[-1]}
    for spelling in tuple(spellings):
        base = dated_snapshot_base(spelling)
        if base:
            spellings.add(base)
    return spellings


def _active_for_candidate(
    candidate: Mapping[str, object],
    active_models: list[dict[str, str]],
) -> dict[str, str] | None:
    alias = str(candidate["model_alias"])
    matches = [active for active in active_models if alias in _active_spellings(active)]
    if not matches:
        return None
    preferred = [str(item) for item in candidate.get("preferred_provider_families", [])]
    ranks = {provider: index for index, provider in enumerate(preferred)}
    return min(
        matches,
        key=lambda item: (
            ranks.get(item["provider_family"], len(ranks)),
            item["provider_family"],
            item["provider"],
            item["model_id"],
        ),
    )


def _active_for_explicit(
    requested: str,
    active_models: list[dict[str, str]],
) -> dict[str, str] | None:
    # An explicit request for the base alias is met by a confirmed dated
    # snapshot of it; the reverse stays fail-closed — a request pinned to a
    # date is not met by an unpinned base the provider may serve differently.
    matches = [
        active
        for active in active_models
        if requested
        in _active_spellings(active)
        | {f"{active['provider']}/{active['model_id']}" if active["provider"] else ""}
    ]
    return min(matches, key=lambda item: (item["provider"], item["model_id"], item["model_alias"])) if matches else None


def _resolved_candidate(
    candidate: Mapping[str, object],
    active: Mapping[str, str],
) -> dict[str, object]:
    return {
        "model_alias": str(candidate["model_alias"]),
        "model_family": str(candidate["model_family"]),
        "provider": active["provider"],
        "provider_family": active["provider_family"],
        "model_id": active["model_id"],
        "reasoning_effort": str(candidate.get("reasoning_effort", "")),
        "reasoning": str(candidate["reasoning"]),
        "recommendation_source": str(candidate["recommendation_source"]),
    }


def _resolved_explicit(active: Mapping[str, str]) -> dict[str, object]:
    return {
        "model_alias": active["model_alias"],
        "model_family": active["model_family"],
        "provider": active["provider"],
        "provider_family": active["provider_family"],
        "model_id": active["model_id"],
        "reasoning_effort": "",
        "reasoning": "Explicit user model selection.",
        "recommendation_source": "explicit_model",
    }


def _projection(owner: str, alias: str, chain: list[dict[str, object]]) -> dict[str, object]:
    if owner == "maestro":
        return {
            "kind": "maestro_ordered_chain",
            "chain": [deepcopy(entry) for entry in chain],
        }
    selected = chain[0]
    provider = str(selected["provider"])
    model_id = str(selected["model_id"])
    binding = model_id if "/" in model_id else f"{provider}/{model_id}" if provider else model_id
    return {
        "kind": "hermes_native_binding",
        "alias": alias,
        "provider": provider,
        "model_id": model_id,
        "binding": binding,
        "apply_state": "approval_required",
    }


def _resolution_payload(
    *,
    owner: str,
    selector_section: str,
    selector_name: str,
    status: str,
    source: str,
    selected: dict[str, object] | None,
    projection: dict[str, object] | None,
    requested_model: str,
    available_chain: list[str],
    inactive_candidates: list[str],
) -> dict[str, object]:
    return {
        "schema_version": MODEL_RECOMMENDATION_RESOLUTION_SCHEMA_VERSION,
        "status": status,
        "owner": owner,
        "selector": {"surface": selector_section, "name": selector_name},
        "source": source,
        "requested_model": requested_model,
        "selected": selected,
        "projection": projection,
        "available_chain": available_chain,
        "inactive_candidates": inactive_candidates,
        "setup_can_continue": True,
        "claim_boundary": (
            "Recommendation resolution uses caller-confirmed local metadata only. It is not provider "
            "availability, entitlement, credential, dispatch, or execution evidence."
        ),
    }


# Importing the established vocabularies is intentional: this foundation may
# consume them but cannot extend them. Keep the name live for static checkers.
assert MODEL_ROLES
