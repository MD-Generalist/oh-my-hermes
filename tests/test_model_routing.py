from __future__ import annotations

import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _local_package import load_local_package

load_local_package()

from _cli_harness import run_cli  # noqa: E402

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_dispatch import build_dispatch_argv  # noqa: E402
from omh.coding.model_routing import (  # noqa: E402
    CODING_MODEL_ROUTE_SCHEMA_VERSION,
    EXECUTOR_MODEL_OPTIONS,
    GENERATIVE_MODEL_CLASS,
    MODEL_CLASSES,
    MODEL_ROLES,
    MODEL_ROUTE_PROVENANCES,
    MODEL_ROUTE_STATUSES,
    NON_GENERATIVE_MODEL_CLASS,
    REASONING_EFFORT_LADDER,
    ROLE_MODEL_CHAINS,
    model_class,
    model_family,
    model_route_for_unit,
    resolve_model_route,
    route_provenance,
)


class ResearchRoleTests(unittest.TestCase):
    def test_research_defaults_to_standard_depth(self) -> None:
        """Autorouting survey consensus: standard is the default; shallow is
        the declared saving, deep the declared escalation — never inferred."""
        codex = resolve_model_route("codex", role="research")
        self.assertEqual(codex["selected_model"], "gpt-6.1-sol")
        # Standard research takes the effort its category declares: `research`
        # reads unspecified-low/quick, and since 2026-10-01 the codex
        # unspecified-low row names `medium` rather than leaving it to the CLI,
        # because GPT-6.1 Sol's Codex default is `low` where GPT-6 Sol's was
        # `medium`. Depth is the dial that escalates.
        self.assertEqual(codex["selected_reasoning_effort"], "medium")
        self.assertEqual(codex["provenance"], "role_chain_head")
        self.assertNotIn("depth", codex)
        claude = resolve_model_route("claude-code", role="research")
        self.assertEqual(claude["selected_model"], "sonnet")
        self.assertEqual(claude["selected_reasoning_effort"], "")

    def test_declared_depth_swaps_the_chain(self) -> None:
        shallow = resolve_model_route("claude-code", role="research", requested_depth="shallow")
        self.assertEqual(shallow["selected_model"], "haiku")
        self.assertEqual(shallow["depth"], "shallow")
        deep = resolve_model_route("claude-code", role="research", requested_depth="deep")
        self.assertEqual(deep["selected_model"], "claude-fable-5-1")
        # `deep` reads the `ultrabrain` category, which is the deepest rung and
        # is reachable only by DECLARING it -- so the escalation to xhigh costs
        # nothing until someone asks for it.
        self.assertEqual(deep["selected_reasoning_effort"], "xhigh")
        deep_codex = resolve_model_route("codex", role="research", requested_depth="deep")
        self.assertEqual(deep_codex["selected_model"], "gpt-6-astra")
        self.assertEqual(deep_codex["selected_reasoning_effort"], "xhigh")
        outcomes = {entry["stage"]: entry["outcome"] for entry in deep["attempted"]}
        self.assertEqual(outcomes["research_depth"], "applied")

    def test_standard_and_unknown_depths_keep_the_role_chain(self) -> None:
        standard = resolve_model_route("codex", role="research", requested_depth="standard")
        self.assertEqual(standard["selected_model"], "gpt-6.1-sol")
        self.assertEqual(standard["depth"], "standard")
        unknown = resolve_model_route("codex", role="research", requested_depth="bottomless")
        self.assertEqual(unknown["selected_model"], "gpt-6.1-sol")
        outcomes = {entry["stage"]: entry["outcome"] for entry in unknown["attempted"]}
        self.assertEqual(outcomes["research_depth"], "unknown_depth")

    def test_depth_on_non_research_role_is_recorded_and_skipped(self) -> None:
        baseline = resolve_model_route("codex", role="brain")
        route = resolve_model_route("codex", role="brain", requested_depth="deep")
        self.assertEqual(route["selected_model"], baseline["selected_model"])
        self.assertEqual(route["chain"], baseline["chain"])
        self.assertEqual(route["depth"], "deep")
        outcomes = {entry["stage"]: entry["outcome"] for entry in route["attempted"]}
        self.assertEqual(outcomes["research_depth"], "skipped")

    def test_depth_chain_models_stay_inside_the_catalog(self) -> None:
        from omh.coding.model_routing import RESEARCH_DEPTH_CHAINS, RESEARCH_DEPTHS

        self.assertEqual(RESEARCH_DEPTHS, ("shallow", "standard", "deep"))
        for profile, depths in RESEARCH_DEPTH_CHAINS.items():
            catalog_ids = {str(o["model_id"]) for o in EXECUTOR_MODEL_OPTIONS[profile]}
            self.assertEqual(set(depths), {"shallow", "deep"}, profile)
            for depth, chain in depths.items():
                for entry in chain:
                    self.assertIn(entry["model_id"], catalog_ids, f"{profile}:{depth}")

    def test_deep_research_escalates_only_explicitly(self) -> None:
        route = resolve_model_route(
            "claude-code", role="research", requested_model="opus", requested_effort="high"
        )
        self.assertEqual(route["provenance"], "request_named_model")
        self.assertEqual(route["selected_model"], "opus")
        self.assertEqual(route["selected_reasoning_effort"], "high")


class FamilyPrefixParityTests(unittest.TestCase):
    def test_family_prefixes_match_dynamic_workflow_target_prefixes(self) -> None:
        """The two prefix lists must name families the same way; drift between
        them was previously silent (no gate) and is a live risk each time a
        family is added. The family label is the prefix minus its dash."""
        from omh.coding.dynamic_workflow_specs import _MODEL_TARGET_PREFIXES
        from omh.coding.model_routing import _MODEL_FAMILY_ALIASES, _MODEL_FAMILY_PREFIXES

        routing_prefixes = tuple(prefix for prefix, _family in _MODEL_FAMILY_PREFIXES)
        routing_aliases = dict(_MODEL_FAMILY_ALIASES)
        canonical_targets = tuple(
            prefix
            for prefix in _MODEL_TARGET_PREFIXES
            if not any(alias.startswith(prefix) for alias in routing_aliases)
        )
        self.assertEqual(routing_prefixes, canonical_targets)
        self.assertTrue(
            all(
                prefix in routing_prefixes or any(alias.startswith(prefix) for alias in routing_aliases)
                for prefix in _MODEL_TARGET_PREFIXES
            )
        )
        for prefix, family in _MODEL_FAMILY_PREFIXES:
            self.assertEqual(family, prefix.rstrip("-"), prefix)

    def test_every_bare_alias_is_a_declared_family_label(self) -> None:
        """A bare alias returns its own spelling as the family, so an entry
        whose spelling is not a family label would invent one. `opus` is the
        shape that must not land here -- a tier word for the `claude` family,
        which `_CLAUDE_TIER_ALIASES` maps explicitly. This asserts the
        invariant the set's comment states, so it is not only stated."""
        from omh.coding.model_routing import _BARE_MODEL_ALIASES, _MODEL_FAMILY_PREFIXES

        families = {family for _prefix, family in _MODEL_FAMILY_PREFIXES}
        for alias in _BARE_MODEL_ALIASES:
            with self.subTest(alias=alias):
                self.assertIn(alias, families)
                self.assertEqual(model_family(alias), alias)

    def test_grok_family_is_recognized(self) -> None:
        self.assertEqual(model_family("grok-code-fast-1"), "grok")

    def test_solar_family_is_recognized(self) -> None:
        # Upstage Solar ids previously fell to "unknown" (#1052); the family
        # label is what routes them to their calibration instead of generic.
        self.assertEqual(model_family("solar-pro2"), "solar")
        self.assertEqual(model_family("upstage/solar-pro2"), "solar")

    def test_minimax_family_is_recognized(self) -> None:
        # MiniMax ids as the vendor serves them (MiniMax-M3, 2026-05-31;
        # MiniMax-M2.7, 2026-03-18) and as gateways spell them.
        self.assertEqual(model_family("MiniMax-M3"), "minimax")
        self.assertEqual(model_family("MiniMax-M2.7"), "minimax")
        self.assertEqual(model_family("minimax/minimax-m2.7"), "minimax")
        self.assertEqual(model_family("MiniMaxAI/MiniMax-M2.7-highspeed"), "minimax")
        # Negative controls: the bare vendor name and a near-miss prefix stay
        # unknown, and a provider segment alone never classifies.
        self.assertEqual(model_family("minimax"), "unknown")
        self.assertEqual(model_family("minimaxi-m3"), "unknown")
        self.assertEqual(model_family("minimax/abab6.5s-chat"), "unknown")

    def test_qwen_dotted_minor_versions_are_bounded_family_aliases(self) -> None:
        from omh.coding.unit_prompt_protocol import (
            MAIN_AGENT_COMPOSITION_CALIBRATIONS,
            composition_calibration_for_model,
        )

        for model in ("qwen/qwen3.8-max-0902", "qwen/qwen3.8-flash"):
            self.assertEqual(model_family(model), "qwen")
            self.assertEqual(
                composition_calibration_for_model(model),
                MAIN_AGENT_COMPOSITION_CALIBRATIONS["qwen"],
            )
        for model in ("qwen3", "qwen3x-max", "qwen/abab-style"):
            self.assertEqual(model_family(model), "unknown")

    def test_vendor_prefixed_model_ids_alias_to_design_families(self) -> None:
        self.assertEqual(model_family("digitalocean/openai-gpt-5.6-sol"), "gpt")
        self.assertEqual(model_family("digitalocean/anthropic-claude-opus-5"), "claude")

    def test_vendor_prefixed_non_design_models_remain_unknown(self) -> None:
        self.assertEqual(model_family("digitalocean/openai-o3"), "unknown")
        self.assertEqual(model_family("digitalocean/openai-gpt-image-2"), "unknown")

    def test_provider_prefixed_ids_classify_by_model_segment(self) -> None:
        self.assertEqual(model_family("opencode/kimi-k3"), "kimi")
        self.assertEqual(model_family("anthropic/claude-opus-5"), "claude")
        self.assertEqual(model_family("openai/gpt-5.6-sol"), "gpt")
        self.assertEqual(model_family("qwen/qwen3-coder-next"), "qwen")
        self.assertEqual(model_family("deepseek/deepseek-v4-pro"), "deepseek")
        # DeepSeek V4.1 Flash as served: the versioned gateway id, the
        # first-party pointer id, and the Hugging Face spelling all classify;
        # the bare vendor word stays unknown like `minimax` above.
        self.assertEqual(model_family("deepseek/deepseek-v4.1-flash"), "deepseek")
        self.assertEqual(model_family("deepseek-flash"), "deepseek")
        self.assertEqual(model_family("DeepSeek-V4.1-Flash"), "deepseek")
        self.assertEqual(model_family("deepseek"), "unknown")
        self.assertEqual(model_family("zai/glm-5"), "glm")
        self.assertEqual(model_family("opencode/big-pickle"), "unknown")


_JEV_ROUTE_SPELLINGS = (
    "jev",
    "jev-latest",
    "jev-1.13.0",
    "typesafe/jev",
    # A vendor's dated snapshot of an id the catalog knows resolves to its
    # base, so the refusal has to survive that suffix too.
    "jev-1.13.0-2026-09-15",
)


class NonGenerativeModelTests(unittest.TestCase):
    """The first model class that cannot own a coding handoff.

    Jev answers a typed Choice, Score, or Noul over options the caller
    supplies and is not trained to generate text
    (docs.typesafe.ai/model-jaggedness/jev-1.13, read 2026-09-21). OMH
    recognizes it, contracts it, prices it, and refuses it everywhere a model
    would be handed work to write.
    """

    def test_jev_family_is_recognized_on_every_served_spelling(self) -> None:
        for model in ("jev-1.13.0", "jev-latest", "jev-preview", "JEV-1.13.0"):
            with self.subTest(model=model):
                self.assertEqual(model_family(model), "jev")
        # The bare id is a served id, not a vendor word: TypeSafe's own docs
        # send `jev-latest` in the `model` field and a gateway spells the
        # bare form `typesafe/jev`, which strips to the same token.
        self.assertEqual(model_family("jev"), "jev")
        self.assertEqual(model_family("typesafe/jev"), "jev")

    def test_near_misses_and_a_tool_name_stay_unknown(self) -> None:
        # The prefix is `jev-` and the bare set holds exactly `jev`, so a
        # word that merely begins with the letters does not classify, and
        # neither does the plugin TOOL name a Hermes catalog entry declares
        # (`jev_evaluate`) -- a tool is not a model.
        for model in ("jevon", "jevons", "jev_evaluate", "jevity-7b", "typesafe/jevon"):
            with self.subTest(model=model):
                self.assertEqual(model_family(model), "unknown")
                self.assertEqual(model_class(model), GENERATIVE_MODEL_CLASS)

    def test_model_class_is_generative_by_default(self) -> None:
        # The default is what keeps every model the catalog has never met
        # routing exactly as it routes today.
        for model in ("gpt-6-astra", "claude-opus-5", "opencode/big-pickle", "", "deepseek"):
            with self.subTest(model=model):
                self.assertEqual(model_class(model), GENERATIVE_MODEL_CLASS)
        for model in _JEV_ROUTE_SPELLINGS:
            with self.subTest(model=model):
                self.assertEqual(model_class(model), NON_GENERATIVE_MODEL_CLASS)
        self.assertEqual(MODEL_CLASSES, (GENERATIVE_MODEL_CLASS, NON_GENERATIVE_MODEL_CLASS))

    def test_a_requested_non_generative_model_is_refused_not_routed(self) -> None:
        for profile in ("codex", "claude-code", "hermes", "generic"):
            for model in _JEV_ROUTE_SPELLINGS:
                route = resolve_model_route(profile, requested_model=model, requested_effort="high")
                with self.subTest(profile=profile, model=model):
                    self.assertEqual(route["status"], "model_refused")
                    self.assertIn(route["status"], MODEL_ROUTE_STATUSES)
                    self.assertEqual(route["provenance"], "request_named_model")
                    # Nothing was prepared, so nothing is reported: no model,
                    # no effort, no chain, and no effort_change for a
                    # requested effort to have changed into.
                    self.assertEqual(route["selected_model"], "")
                    self.assertEqual(route["selected_reasoning_effort"], "")
                    self.assertEqual(route["chain"], [])
                    self.assertNotIn("effort_change", route)
                    refusal = route["refusal"]
                    self.assertEqual(refusal["kind"], "non_generative_model")
                    # The id is a field, so a JSON consumer never parses the
                    # sentence to learn what was refused.
                    self.assertEqual(refusal["requested_model"], model)
                    self.assertEqual(route["attempted"][-1]["outcome"], "refused")
                    # `model_family` reads off `selected_model`, and nothing
                    # was selected, so it is empty here exactly as it is on a
                    # `model_unrouted` route. The family that IS the basis of
                    # this decision stays recoverable from the refusal record,
                    # which is why the record carries the id.
                    self.assertEqual(route["model_family"], "")
                    self.assertEqual(model_family(refusal["requested_model"]), "jev")

    def test_a_refused_route_reports_the_default_catalog_kind_not_an_adjudicator(self) -> None:
        # The refusal is decided before any catalog is read, so `catalog_kind`
        # on a refused route is the default constant and names no adjudicating
        # basis. Pinned because the comment at the branch says so, and a later
        # move of the refusal past the catalog-kind resolution would silently
        # turn the field into a claim about a table nothing consulted.
        from omh.coding.model_routing import MODEL_CATALOG_KIND

        category_config = {
            "schema_version": "category_maestro/v1",
            "path": "/operator/category-maestro.json",
            "profiles": {
                "codex": {
                    "quick": {
                        "chain": [{"model_id": "gpt-5.6-sol", "reasoning_effort": "low"}],
                        "source": "operator",
                    }
                }
            },
        }
        routed = resolve_model_route("codex", requested_category="quick", category_config=category_config)
        self.assertEqual(routed["catalog_kind"], "operator_category_config")
        refused = resolve_model_route(
            "codex",
            requested_model="jev-1.13.0",
            requested_category="quick",
            category_config=category_config,
        )
        self.assertEqual(refused["status"], "model_refused")
        self.assertEqual(refused["catalog_kind"], MODEL_CATALOG_KIND)
        self.assertNotIn("catalog_fingerprint", refused)

    def test_the_hermes_recommendation_path_cannot_route_around_the_refusal(self) -> None:
        # `resolve_model_route` hands Hermes to a separate resolver whenever
        # a confirmed-active set is supplied; the refusal is decided from the
        # id before that hand-off, so both paths answer the same way.
        route = resolve_model_route(
            "hermes", requested_model="jev-latest", active_models=("jev-latest", "kimi-k3")
        )
        self.assertEqual(route["status"], "model_refused")
        self.assertEqual(route["selected_model"], "")
        self.assertEqual(route["catalog_kind"], "editorial_recommendations")

    def test_an_operator_recommendation_naming_one_at_the_head_is_skipped(self) -> None:
        # The other half of the Hermes lane. With no requested model the
        # refusal above never fires, and the chain is built from an operator
        # recommendation document -- the hand-edited path `omh coding
        # model-route --from-inventory --recommendations <file>` reads. The
        # head must not be prepared, and the next generative entry takes it.
        from omh.coding.model_recommendations import (
            MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION,
        )

        def active(alias: str, provider: str, family: str) -> dict[str, object]:
            return {
                "model_alias": alias,
                "model_id": alias,
                "provider": provider,
                "provider_family": provider,
                "model_family": family,
                "compatible_owners": ["hermes"],
                "status": "confirmed_active",
            }

        overrides = {
            "schema_version": MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION,
            "categories": {
                "quick": [
                    {
                        "model_alias": "jev-1.13.0",
                        "model_family": "jev",
                        "preferred_provider_families": ["typesafe"],
                        "reasoning_effort": "low",
                        "reasoning": "Operator-selected head.",
                    },
                    {
                        "model_alias": "kimi-k3",
                        "model_family": "kimi",
                        "preferred_provider_families": ["apitopia"],
                        "reasoning_effort": "low",
                        "reasoning": "Operator-selected runner-up.",
                    },
                ]
            },
        }
        route = resolve_model_route(
            "hermes",
            role="implementation",
            requested_category="quick",
            active_models=[
                active("jev-1.13.0", "typesafe", "jev"),
                active("kimi-k3", "apitopia", "kimi"),
            ],
            recommendation_overrides=overrides,
        )
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "apitopia/kimi-k3")
        self.assertEqual([entry["model_id"] for entry in route["chain"]], ["apitopia/kimi-k3"])
        skipped = [entry for entry in route["attempted"] if entry["stage"] == "chain_entry"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["outcome"], "skipped")
        # The provider-prefixed spelling the recommendation lane builds still
        # classifies: the provider names where it runs, the family what it is.
        self.assertIn("typesafe/jev-1.13.0", skipped[0]["reason"])
        self.assertIn(NON_GENERATIVE_MODEL_CLASS, skipped[0]["reason"])

    def test_an_operator_recommendation_of_only_non_generative_models_falls_to_the_owner_default(
        self,
    ) -> None:
        # Filtering the whole chain away must not leave an empty head: the
        # existing owner-default branch answers, which is the same shape the
        # lane already gives when nothing is confirmed active.
        from omh.coding.model_recommendations import (
            MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION,
        )

        overrides = {
            "schema_version": MODEL_RECOMMENDATION_OVERRIDE_SCHEMA_VERSION,
            "categories": {
                "quick": [
                    {
                        "model_alias": "jev-1.13.0",
                        "model_family": "jev",
                        "preferred_provider_families": ["typesafe"],
                        "reasoning_effort": "low",
                        "reasoning": "Operator-selected head.",
                    }
                ]
            },
        }
        route = resolve_model_route(
            "hermes",
            role="implementation",
            requested_category="quick",
            active_models=[
                {
                    "model_alias": "jev-1.13.0",
                    "model_id": "jev-1.13.0",
                    "provider": "typesafe",
                    "provider_family": "typesafe",
                    "model_family": "jev",
                    "compatible_owners": ["hermes"],
                    "status": "confirmed_active",
                }
            ],
            recommendation_overrides=overrides,
        )
        self.assertEqual(route["status"], "model_unrouted")
        self.assertEqual(route["selected_model"], "")
        self.assertEqual(route["chain"], [])
        stages = [entry["stage"] for entry in route["attempted"]]
        self.assertIn("chain_entry", stages)
        self.assertIn("executor_default", stages)
        # The reason must name the condition that actually emptied the chain.
        # A candidate WAS confirmed active here and was dropped for its class,
        # so the pre-existing "none is confirmed active" wording would send a
        # reader to their active set for a fault in their recommendation file.
        owner_default = next(
            entry for entry in route["attempted"] if entry["outcome"] == "owner_default"
        )
        self.assertIn(NON_GENERATIVE_MODEL_CLASS, owner_default["reason"])
        self.assertNotIn("no editorial candidate is confirmed active", owner_default["reason"])
        self.assertIn(NON_GENERATIVE_MODEL_CLASS, " ".join(route["reasons"]))
        # The resolution itself did resolve; only the class filter emptied it.
        self.assertEqual(route["recommendation"]["status"], "resolved")

    def test_an_empty_active_set_keeps_the_original_owner_default_reason(self) -> None:
        # The other branch of the same record, so the new wording cannot leak
        # onto the case it does not describe: with nothing confirmed active
        # there is no candidate to have been dropped, and the lane's existing
        # sentence is the true one.
        route = resolve_model_route("hermes", role="implementation", active_models=())
        self.assertEqual(route["status"], "model_unrouted")
        owner_default = next(
            entry for entry in route["attempted"] if entry["outcome"] == "owner_default"
        )
        self.assertEqual(
            owner_default["reason"], "no editorial candidate is confirmed active for Hermes"
        )
        self.assertNotIn(NON_GENERATIVE_MODEL_CLASS, owner_default["reason"])

    def test_a_generative_request_still_wins_over_the_catalog(self) -> None:
        # The invariant the refusal narrows and must not replace: an
        # explicitly requested generative model is still never adjudicated.
        route = resolve_model_route("codex", requested_model="custom-model-1", requested_effort="high")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "custom-model-1")

    def test_a_non_generative_chain_entry_is_skipped_on_record(self) -> None:
        # Defensive: no shipped chain names one, `omh model-chains set`
        # refuses to write one, and the operator category config rejects one
        # on read. A hand-edited document is the path that still reaches
        # here, and the next generative entry must take the head rather than
        # a model that answers with a Choice.
        chains = {
            "codex": {
                "brain": (
                    {"model_id": "jev-1.13.0", "reasoning_effort": "low"},
                    {"model_id": "gpt-5.6-sol", "reasoning_effort": "high"},
                )
            }
        }
        route = resolve_model_route("codex", role="brain", chains=chains)
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["selected_model"], "gpt-5.6-sol")
        self.assertEqual([entry["model_id"] for entry in route["chain"]], ["gpt-5.6-sol"])
        skipped = [entry for entry in route["attempted"] if entry["stage"] == "chain_entry"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["outcome"], "skipped")
        self.assertIn("jev-1.13.0", skipped[0]["reason"])
        self.assertIn(NON_GENERATIVE_MODEL_CLASS, skipped[0]["reason"])

    def test_no_shipped_chain_names_a_non_generative_model(self) -> None:
        # The claim the skip above is defensive about, asserted rather than
        # assumed, over every profile and role the catalog ships.
        for profile, chains in ROLE_MODEL_CHAINS.items():
            for role, entries in chains.items():
                for entry in entries:
                    with self.subTest(profile=profile, role=role, model=entry["model_id"]):
                        self.assertEqual(model_class(entry["model_id"]), GENERATIVE_MODEL_CLASS)


_LOCAL_CATALOG = {
    "schema_version": "local_model_catalog/v1",
    "executor_profile": "omo-runtime",
    "catalog_kind": "local_inventory",
    "options": (
        {
            "model_id": "opencode/kimi-k3",
            "label": "kimi family via opencode",
            "tier": "",
            "recommended_roles": (),
            "reasoning_efforts": (),
        },
        {
            "model_id": "opencode/gemini-3.1-pro",
            "label": "gemini family via opencode",
            "tier": "",
            "recommended_roles": (),
            "reasoning_efforts": (),
        },
    ),
    "chains": {
        "brain": (
            {"model_id": "opencode/kimi-k3", "reasoning_effort": "high"},
            {"model_id": "opencode/gemini-3.1-pro", "reasoning_effort": ""},
            {"model_id": "opencode/grok-code-fast", "reasoning_effort": ""},
        ),
    },
    "fingerprint": {"digest": "cafe1234beef5678", "sources": {"omo_agent_config": "present"}, "observed_at": "t"},
    "domain_affinities": {"x_platform_data": ("grok",), "multimodal_vision": ("gemini", "gpt", "claude")},
}


class LocalCatalogRouteTests(unittest.TestCase):
    def test_catalogless_profile_routes_from_local_catalog_with_fingerprint(self) -> None:
        route = resolve_model_route("omo-runtime", role="brain", local_catalog=_LOCAL_CATALOG)
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["provenance"], "role_chain_head")
        self.assertEqual(route["selected_model"], "opencode/kimi-k3")
        self.assertEqual(route["selected_reasoning_effort"], "high")
        self.assertEqual(route["model_family"], "kimi")
        self.assertEqual(route["catalog_kind"], "local_inventory")
        self.assertEqual(route["catalog_fingerprint"]["digest"], "cafe1234beef5678")
        self.assertEqual(len(route["chain"]), 3)

    def test_local_catalog_never_applies_to_built_in_profiles(self) -> None:
        """Built-in catalogs always win: a local catalog naming codex (or
        handed to a codex resolution) must change nothing, byte for byte."""
        hostile = dict(_LOCAL_CATALOG)
        hostile["executor_profile"] = "codex"
        baseline = resolve_model_route("codex", role="brain")
        self.assertEqual(resolve_model_route("codex", role="brain", local_catalog=hostile), baseline)
        self.assertEqual(resolve_model_route("codex", role="brain", local_catalog=_LOCAL_CATALOG), baseline)

    def test_local_catalog_for_other_profile_is_ignored(self) -> None:
        route = resolve_model_route("hermes", role="brain", local_catalog=_LOCAL_CATALOG)
        self.assertEqual(route["status"], "no_model_catalog")
        self.assertEqual(route["catalog_kind"], "built_in_defaults")
        self.assertNotIn("catalog_fingerprint", route)

    def test_local_catalog_never_gains_effort_authority(self) -> None:
        """Observed config variants are evidence of use, not vocabulary: a
        requested ladder effort passes through untouched, never downgraded."""
        route = resolve_model_route(
            "omo-runtime", role="brain", requested_effort="max", local_catalog=_LOCAL_CATALOG
        )
        self.assertEqual(route["selected_reasoning_effort"], "max")
        self.assertEqual(route["effort_change"]["kind"], "catalog_no_authority_passthrough")

    def test_requested_model_still_wins_over_local_chain(self) -> None:
        route = resolve_model_route(
            "omo-runtime",
            role="brain",
            requested_model="opencode/glm-5",
            local_catalog=_LOCAL_CATALOG,
        )
        self.assertEqual(route["provenance"], "request_named_model")
        self.assertEqual(route["selected_model"], "opencode/glm-5")
        self.assertEqual(route["model_family"], "glm")
        self.assertEqual(route["catalog_kind"], "local_inventory")

    def test_unchained_role_on_local_catalog_is_explicit_choice(self) -> None:
        route = resolve_model_route("omo-runtime", role="docs", local_catalog=_LOCAL_CATALOG)
        self.assertEqual(route["status"], "choice_required")
        self.assertEqual(route["provenance"], "role_unchained")
        self.assertEqual(route["catalog_kind"], "local_inventory")

    def test_declared_domain_reorders_local_chain_without_removal(self) -> None:
        """The user's example made concrete: X-platform data work declared on
        the unit moves the grok-family entry to the chain head — a stable
        reorder recorded in attempted[], with every entry kept."""
        route = resolve_model_route(
            "omo-runtime", role="brain", requested_domain="x_platform_data", local_catalog=_LOCAL_CATALOG
        )
        self.assertEqual(route["selected_model"], "opencode/grok-code-fast")
        self.assertEqual(route["domain"], "x_platform_data")
        self.assertEqual(
            [entry["model_id"] for entry in route["chain"]],
            ["opencode/grok-code-fast", "opencode/kimi-k3", "opencode/gemini-3.1-pro"],
        )
        outcomes = {entry["stage"]: entry["outcome"] for entry in route["attempted"]}
        self.assertEqual(outcomes["domain_affinity"], "reordered")

    def test_unknown_domain_is_recorded_and_ignored(self) -> None:
        route = resolve_model_route(
            "omo-runtime", role="brain", requested_domain="nonsense", local_catalog=_LOCAL_CATALOG
        )
        self.assertEqual(route["selected_model"], "opencode/kimi-k3")
        self.assertEqual(route["domain"], "nonsense")
        outcomes = {entry["stage"]: entry["outcome"] for entry in route["attempted"]}
        self.assertEqual(outcomes["domain_affinity"], "unknown_domain")

    def test_no_domain_means_no_domain_key(self) -> None:
        route = resolve_model_route("omo-runtime", role="brain", local_catalog=_LOCAL_CATALOG)
        self.assertNotIn("domain", route)
        self.assertNotIn("domain_affinity", [entry["stage"] for entry in route["attempted"]])

    def test_domain_on_built_in_profile_is_recorded_and_skipped(self) -> None:
        """Built-in chains stay curated: a declared domain on codex changes
        no selection, only records itself and the explicit skip."""
        baseline = resolve_model_route("codex", role="brain")
        route = resolve_model_route("codex", role="brain", requested_domain="x_platform_data")
        self.assertEqual(route["selected_model"], baseline["selected_model"])
        self.assertEqual(route["chain"], baseline["chain"])
        self.assertEqual(route["domain"], "x_platform_data")
        outcomes = {entry["stage"]: entry["outcome"] for entry in route["attempted"]}
        self.assertEqual(outcomes["domain_affinity"], "skipped")

    def test_requested_model_wins_over_domain(self) -> None:
        route = resolve_model_route(
            "omo-runtime",
            role="brain",
            requested_model="opencode/glm-5",
            requested_domain="x_platform_data",
            local_catalog=_LOCAL_CATALOG,
        )
        self.assertEqual(route["provenance"], "request_named_model")
        self.assertEqual(route["selected_model"], "opencode/glm-5")

    def test_empty_or_corrupt_local_catalog_claims_no_basis(self) -> None:
        """A frozen record must never name a basis that adjudicated nothing:
        an offered catalog with no usable options falls through to the
        executor default WITHOUT catalog_kind/fingerprint, and says so."""
        for hostile_options in ([], ["not-a-mapping", 42]):
            hostile = dict(_LOCAL_CATALOG)
            hostile["options"] = hostile_options
            route = resolve_model_route("omo-runtime", role="brain", local_catalog=hostile)
            self.assertEqual(route["status"], "no_model_catalog")
            self.assertEqual(route["catalog_kind"], "built_in_defaults")
            self.assertNotIn("catalog_fingerprint", route)
            self.assertTrue(
                any("no usable options" in reason for reason in route["reasons"]),
                route["reasons"],
            )


class ModelRouteResolverTests(unittest.TestCase):
    def test_requested_model_always_wins_and_passes_through_unvalidated(self) -> None:
        route = resolve_model_route("codex", requested_model="gpt-6-terra", requested_effort="xhigh", role="brain")
        self.assertEqual(route["schema_version"], CODING_MODEL_ROUTE_SCHEMA_VERSION)
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["provenance"], "request_named_model")
        self.assertEqual(route["selected_model"], "gpt-6-terra")
        self.assertEqual(route["selected_reasoning_effort"], "xhigh")
        self.assertEqual(route["model_family"], "gpt")

    def test_unknown_family_is_named_not_rejected(self) -> None:
        route = resolve_model_route("claude-code", requested_model="luna-5.5")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["model_family"], "unknown")

    def test_claude_tier_aliases_fold_into_claude_family(self) -> None:
        self.assertEqual(model_family("opus"), "claude")
        self.assertEqual(model_family("claude-opus-5"), "claude")
        # The bare Fable-tier names users type in chat ("use fable") and the
        # concrete 5.1 ids both land on the claude calibration, never generic.
        for model_id in ("fable", "mythos", "claude-fable-5-1", "claude-mythos-5-1", "anthropic/claude-fable-5-1"):
            self.assertEqual(model_family(model_id), "claude", model_id)

    def test_every_role_routes_to_its_chain_head_on_both_profiles(self) -> None:
        for profile, chains in ROLE_MODEL_CHAINS.items():
            for role in MODEL_ROLES:
                with self.subTest(profile=profile, role=role):
                    route = resolve_model_route(profile, role=role)
                    self.assertEqual(route["status"], "routed")
                    self.assertEqual(route["provenance"], "role_chain_head")
                    self.assertEqual(route["selected_model"], chains[role][0]["model_id"])

    def test_review_role_on_codex_routes_to_chain_head_with_named_alternative(self) -> None:
        # THE behavior change of this PR: review-on-codex previously resolved
        # choice_required with no selected model; it now routes to the chain
        # head. Both review categories now head on the same served model, so
        # the chain de-duplicates to the one entry the CLI can actually run.
        route = resolve_model_route("codex", role="review")
        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["provenance"], "role_chain_head")
        self.assertEqual(route["selected_model"], "gpt-6.1-sol")
        chain_models = [entry["model_id"] for entry in route["chain"]]
        self.assertEqual(chain_models, ["gpt-6.1-sol"])
        selected_flags = [entry["selected"] for entry in route["chain"]]
        self.assertEqual(selected_flags, [True])

    def test_brain_role_gets_high_effort_from_chain_entry(self) -> None:
        # brain's high default moved from _HIGH_EFFORT_ROLES into chain data;
        # behaviour is preserved and effort_change stays absent because the
        # user requested nothing (chain efforts never appear in effort_change).
        route = resolve_model_route("codex", role="brain")
        self.assertEqual(route["selected_reasoning_effort"], "high")
        self.assertNotIn("effort_change", route)

    def test_requested_effort_outranks_chain_entry_effort(self) -> None:
        route = resolve_model_route("codex", role="brain", requested_effort="medium")
        self.assertEqual(route["selected_reasoning_effort"], "medium")
        self.assertEqual(route["effort_change"]["requested"], "medium")
        self.assertEqual(route["effort_change"]["kind"], "unchanged")

    def test_no_request_is_an_explicit_executor_default_outcome(self) -> None:
        route = resolve_model_route("codex")
        self.assertEqual(route["status"], "model_unrouted")
        self.assertEqual(route["provenance"], "executor_default")
        self.assertEqual(route["selected_model"], "")

    def test_profile_without_catalog_is_named(self) -> None:
        route = resolve_model_route("hermes")
        self.assertEqual(route["status"], "no_model_catalog")
        self.assertEqual(route["provenance"], "no_catalog")

    def test_unknown_role_is_named_in_reasons_not_erased(self) -> None:
        route = resolve_model_route("codex", role="tester")
        self.assertEqual(route["status"], "model_unrouted")
        self.assertTrue(any("tester" in reason for reason in route["reasons"]))
        self.assertFalse(any("No model or role was requested" in reason for reason in route["reasons"]))

    def test_attempted_is_nonempty_and_staged_for_every_outcome(self) -> None:
        cases = (
            resolve_model_route("codex", requested_model="gpt-5"),
            resolve_model_route("codex", role="brain"),
            resolve_model_route("codex"),
            resolve_model_route("hermes"),
            resolve_model_route("codex", role="review", chains={"codex": {}}),
        )
        for route in cases:
            with self.subTest(provenance=route["provenance"]):
                attempted = route["attempted"]
                self.assertTrue(attempted)
                for record in attempted:
                    self.assertIn("stage", record)
                    self.assertIn("outcome", record)
                    self.assertIn("reason", record)
                expected_last = "unavailable" if route["status"] == "choice_required" else "selected"
                self.assertEqual(attempted[-1]["outcome"], expected_last)

    def test_chain_gap_is_an_explicit_choice_structural_net(self) -> None:
        # role_unchained/choice_required are unreachable from the shipped
        # catalog under the bidirectional parity gate; the branch is exercised
        # with a constructed chain-gap dict, never production data.
        route = resolve_model_route("codex", role="review", chains={"codex": {}})
        self.assertEqual(route["status"], "choice_required")
        self.assertEqual(route["provenance"], "role_unchained")
        self.assertEqual(route["selected_model"], "")
        self.assertTrue(any("no declared chain" in reason for reason in route["reasons"]))

    def test_claim_boundary_disclaims_provider_truth_and_retries(self) -> None:
        route = resolve_model_route("codex", requested_model="gpt-5")
        self.assertIn("provider truth", route["claim_boundary"])
        self.assertIn("never retries or switches", route["claim_boundary"])

    def test_model_roles_vocabulary_is_stable(self) -> None:
        self.assertEqual(
            MODEL_ROLES,
            ("brain", "implementation", "design_visual", "review", "docs", "research"),
        )


class RouteVocabularyPolicyTests(unittest.TestCase):
    """Every resolver-emitted enum value stays inside the declared vocabulary."""

    def _all_routes(self) -> list[dict[str, object]]:
        routes = []
        for profile in (*EXECUTOR_MODEL_OPTIONS, "hermes", "generic"):
            routes.append(resolve_model_route(profile))
            routes.append(resolve_model_route(profile, requested_model="custom-model-1"))
            for role in (*MODEL_ROLES, "tester"):
                routes.append(resolve_model_route(profile, role=role))
        routes.append(resolve_model_route("codex", role="review", chains={"codex": {}}))
        # The refusal path joins the enumerating gate: a status emitted by
        # the resolver and absent from `MODEL_ROUTE_STATUSES` is exactly what
        # this test exists to catch, and a branch it never reaches is not
        # covered by it.
        routes.append(resolve_model_route("codex", requested_model="jev-1.13.0"))
        routes.append(resolve_model_route("hermes", requested_model="jev", active_models=()))
        return routes

    def test_emitted_statuses_and_provenances_are_declared(self) -> None:
        for route in self._all_routes():
            with self.subTest(profile=route["executor_profile"], provenance=route["provenance"]):
                self.assertIn(route["status"], MODEL_ROUTE_STATUSES)
                self.assertIn(route["provenance"], MODEL_ROUTE_PROVENANCES)

    def test_bidirectional_chain_catalog_parity(self) -> None:
        # chains ⊆ catalog AND every catalog profile has a chain for every
        # role — both directions fail loudly so neither table drifts ahead.
        self.assertEqual(set(ROLE_MODEL_CHAINS), set(EXECUTOR_MODEL_OPTIONS))
        for profile, chains in ROLE_MODEL_CHAINS.items():
            catalog_models = {str(option["model_id"]) for option in EXECUTOR_MODEL_OPTIONS[profile]}
            self.assertEqual(set(chains), set(MODEL_ROLES), profile)
            for role, entries in chains.items():
                self.assertTrue(entries, (profile, role))
                for entry in entries:
                    self.assertIn(entry["model_id"], catalog_models, (profile, role))


class EffortLadderTests(unittest.TestCase):
    def test_canonical_ladder_is_weakest_to_strongest(self) -> None:
        self.assertEqual(
            REASONING_EFFORT_LADDER,
            ("off", "minimal", "low", "medium", "high", "xhigh", "max"),
        )

    def test_legacy_none_normalizes_to_off_and_keeps_requested_provenance(self) -> None:
        route = resolve_model_route("codex", requested_model="gpt-6-terra", requested_effort="none")
        self.assertEqual(route["selected_reasoning_effort"], "off")
        self.assertEqual(route["effort_change"]["requested"], "none")
        self.assertEqual(route["effort_change"]["selected"], "off")
        self.assertEqual(route["effort_change"]["kind"], "legacy_alias_normalized")

    def test_auto_passes_through_when_catalog_has_no_contract_for_it(self) -> None:
        route = resolve_model_route("codex", requested_model="gpt-5.6-sol", requested_effort="auto")
        self.assertEqual(route["selected_reasoning_effort"], "auto")
        self.assertEqual(route["effort_change"]["kind"], "automatic_passthrough")

    def test_legacy_none_normalizes_on_catalogless_hermes_projection(self) -> None:
        route = resolve_model_route(
            "hermes",
            role="implementation",
            requested_effort="none",
            active_models=("kimi-k3",),
        )
        self.assertEqual(route["selected_reasoning_effort"], "off")

    # The four-quadrant grid: (ladder-vocab?, exact-model authority?).
    def test_quadrant_ladder_vocab_exact_model_downgrades(self) -> None:
        # THE second behavior change of this PR (the live-bug fix): `max` on a
        # codex catalog model previously passed through unsupported; it now
        # steps down the ladder to the strongest supported rung.
        route = resolve_model_route("codex", requested_model="gpt-5.6-sol", requested_effort="max")
        self.assertEqual(route["selected_reasoning_effort"], "xhigh")
        change = route["effort_change"]
        self.assertEqual(change["kind"], "ladder_downgrade")
        self.assertEqual(change["requested"], "max")
        self.assertEqual(change["selected"], "xhigh")
        self.assertIn("max", change["reason"])
        self.assertIn("xhigh", change["reason"])

    def test_quadrant_ladder_vocab_unknown_model_passes_through(self) -> None:
        # Never-edit guard (b): the catalog disclaims authority over models it
        # has not met, so an in-vocabulary effort on an unknown model is NOT
        # downgraded. If this test needs editing to make another pass, the
        # ladder has widened its claim — change the ladder instead.
        route = resolve_model_route("codex", requested_model="gpt-6-terra", requested_effort="max")
        self.assertEqual(route["selected_reasoning_effort"], "max")
        self.assertEqual(route["effort_change"]["kind"], "catalog_no_authority_passthrough")

    def test_quadrant_off_vocab_exact_model_passes_through(self) -> None:
        # Never-edit guard (a): effort-shaped off-vocabulary values pass
        # through byte-identically so a newer CLI vocabulary is never blocked
        # by a stale catalog. Same never-edit rule as guard (b).
        route = resolve_model_route("codex", requested_model="gpt-6.1-sol", requested_effort="turbo-9")
        self.assertEqual(route["selected_reasoning_effort"], "turbo-9")
        self.assertEqual(route["effort_change"]["kind"], "unknown_vocabulary_passthrough")

    def test_quadrant_off_vocab_unknown_model_passes_through(self) -> None:
        route = resolve_model_route("codex", requested_model="gpt-6-terra", requested_effort="turbo-9")
        self.assertEqual(route["selected_reasoning_effort"], "turbo-9")
        self.assertEqual(route["effort_change"]["kind"], "unknown_vocabulary_passthrough")

    def test_absent_model_keeps_requested_ladder_effort(self) -> None:
        route = resolve_model_route("codex", requested_effort="max")
        self.assertEqual(route["status"], "model_unrouted")
        self.assertEqual(route["selected_reasoning_effort"], "max")
        self.assertEqual(route["effort_change"]["kind"], "catalog_no_authority_passthrough")

    def test_max_on_claude_catalog_model_is_unchanged(self) -> None:
        route = resolve_model_route("claude-code", requested_model="opus", requested_effort="max")
        self.assertEqual(route["selected_reasoning_effort"], "max")
        self.assertEqual(route["effort_change"]["kind"], "unchanged")

    def test_noncontiguous_supported_set_steps_to_next_lower_supported_rung(self) -> None:
        """Ladder order is the authority, not adjacency in the supported list.

        The catalog patched here is SYNTHETIC — it describes no real executor;
        it exists only to produce a supported set with a hole in it.
        """
        from unittest import mock

        synthetic = {
            "codex": (
                {
                    "model_id": "synthetic-model",
                    "label": "synthetic",
                    "tier": "frontier",
                    "recommended_roles": ("brain",),
                    "reasoning_efforts": ("low", "xhigh"),
                },
            ),
        }
        with mock.patch.dict(EXECUTOR_MODEL_OPTIONS, synthetic, clear=True):
            route = resolve_model_route("codex", requested_model="synthetic-model", requested_effort="max")
            self.assertEqual(route["selected_reasoning_effort"], "xhigh")
            route = resolve_model_route("codex", requested_model="synthetic-model", requested_effort="high")
            # high and medium are unsupported; low is the next supported rung
            # DOWN the ladder from high.
            self.assertEqual(route["selected_reasoning_effort"], "low")

    def test_canonical_lower_rungs_clamp_across_holes(self) -> None:
        synthetic = {
            "codex": (
                {
                    "model_id": "synthetic-model",
                    "label": "synthetic",
                    "tier": "frontier",
                    "recommended_roles": ("brain",),
                    "reasoning_efforts": ("off", "low", "high"),
                },
            ),
        }
        with mock.patch.dict(EXECUTOR_MODEL_OPTIONS, synthetic, clear=True):
            medium = resolve_model_route(
                "codex", requested_model="synthetic-model", requested_effort="medium"
            )
            minimal = resolve_model_route(
                "codex", requested_model="synthetic-model", requested_effort="minimal"
            )
        self.assertEqual(medium["selected_reasoning_effort"], "low")
        self.assertEqual(minimal["selected_reasoning_effort"], "off")

    def test_ladder_with_no_supported_rung_terminates_empty(self) -> None:
        from unittest import mock

        synthetic = {
            "codex": (
                {
                    "model_id": "synthetic-model",
                    "label": "synthetic",
                    "tier": "frontier",
                    "recommended_roles": ("brain",),
                    "reasoning_efforts": (),
                },
            ),
        }
        with mock.patch.dict(EXECUTOR_MODEL_OPTIONS, synthetic, clear=True):
            route = resolve_model_route("codex", requested_model="synthetic-model", requested_effort="max")
            self.assertEqual(route["selected_reasoning_effort"], "")
            self.assertEqual(route["effort_change"]["kind"], "ladder_downgrade")

    def test_unsafe_effort_shape_falls_back_to_cli_default(self) -> None:
        route = resolve_model_route("codex", requested_model="gpt-5", requested_effort='high\nsandbox_mode = "danger"')
        self.assertEqual(route["selected_reasoning_effort"], "")
        self.assertEqual(route["effort_change"]["kind"], "rejected_unsafe_shape")

    def test_effort_survives_profiles_without_catalog(self) -> None:
        route = resolve_model_route("gemini-runtime", requested_effort="high")
        self.assertEqual(route["status"], "no_model_catalog")
        self.assertEqual(route["selected_reasoning_effort"], "high")
        self.assertEqual(route["effort_change"]["kind"], "catalog_no_authority_passthrough")

    def test_effort_change_absent_when_no_effort_requested(self) -> None:
        for route in (
            resolve_model_route("codex", role="implementation"),
            resolve_model_route("codex", requested_model="gpt-5"),
            resolve_model_route("hermes"),
        ):
            self.assertNotIn("effort_change", route)


class RouteProvenanceCompatTests(unittest.TestCase):
    def test_v2_route_reads_its_provenance(self) -> None:
        route = resolve_model_route("codex", role="brain")
        self.assertEqual(route_provenance(route), ("role_chain_head", "v2"))

    def test_v1_route_reads_source_verbatim_never_translated(self) -> None:
        # A frozen contract's record must keep saying exactly what was
        # written: a chainless resolver never produced chain vocabulary.
        v1_route = {
            "schema_version": "coding_model_route/v1",
            "source": "role_catalog_default",
            "selected_model": "gpt-5",
        }
        self.assertEqual(route_provenance(v1_route), ("role_catalog_default", "v1"))

    def test_v1_route_missing_or_offvocab_source_is_unknown(self) -> None:
        self.assertEqual(
            route_provenance({"schema_version": "coding_model_route/v1"}), ("unknown", "unknown")
        )
        self.assertEqual(
            route_provenance({"schema_version": "coding_model_route/v1", "source": "made-up"}),
            ("unknown", "unknown"),
        )

    def test_unknown_version_and_malformed_payloads_are_unknown(self) -> None:
        self.assertEqual(route_provenance({"schema_version": "coding_model_route/v9"}), ("unknown", "unknown"))
        self.assertEqual(route_provenance({}), ("unknown", "unknown"))
        self.assertEqual(route_provenance(None), ("unknown", "unknown"))
        self.assertEqual(
            route_provenance({"schema_version": "coding_model_route/v2"}), ("unknown", "unknown")
        )


class ProvenanceSoleAccessorPolicyTests(unittest.TestCase):
    """`route_provenance` is the only sanctioned reader of route provenance.

    Source-derived gate: no dict-access read of the `provenance` key may
    appear outside src/coding/model_routing.py. The scope (src/coding/ and
    src/commands/) is a deliberate floor tied to today's layout — routing
    code landing in a new directory must widen it. Attribute-style
    `provenance` usages elsewhere (src/commands/ops.py research provenance,
    src/quality/ evidence records) are an unrelated vocabulary and are not
    matched by the dict-access anchors.

    `_UNRELATED_VOCABULARY` is the same "unrelated vocabulary" carve-out for a
    file that does use dict access, and it is deliberately per-file rather than
    a pattern: `handoff_input_manifest/v1` gives every manifest item a
    `provenance` object naming the local source it came from, which is not a
    `coding_model_route/*` provenance and reaches no resolver. Widening this
    tuple is how a second such contract is admitted — one file at a time, with
    a reason — so the gate keeps its teeth on everything that really is a route.
    """

    _ACCESS_RE = re.compile(r"""(?:\[\s*["']provenance["']\s*\]|\.get\(\s*["']provenance["'])""")
    _UNRELATED_VOCABULARY = (("coding", "handoff_input_manifest.py"),)

    def test_provenance_key_reads_stay_in_model_routing(self) -> None:
        repo_src = Path(__file__).resolve().parent.parent / "src"
        offenders: list[str] = []
        for directory in ("coding", "commands"):
            for path in sorted((repo_src / directory).rglob("*.py")):
                if path.name == "model_routing.py" and directory == "coding":
                    continue
                if (directory, path.name) in self._UNRELATED_VOCABULARY:
                    continue
                text = path.read_text(encoding="utf-8")
                for match in self._ACCESS_RE.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(repo_src)}:{line}")
        self.assertEqual(offenders, [], "read provenance via route_provenance() instead")

    def test_unrelated_vocabulary_carve_outs_never_touch_model_routes(self) -> None:
        """The carve-out's premise, checked rather than asserted in prose.

        An exempted file that started reading model routes would still be
        exempt, and the gate would report nothing. Proving it imports no
        resolver is what keeps the exemption honest, and a stale entry for a
        renamed file fails here instead of rotting into a silent hole.
        """
        repo_src = Path(__file__).resolve().parent.parent / "src"
        for directory, name in ProvenanceSoleAccessorPolicyTests._UNRELATED_VOCABULARY:
            path = repo_src / directory / name
            self.assertTrue(path.is_file(), f"stale provenance carve-out: {directory}/{name}")
            self.assertNotIn("model_routing", path.read_text(encoding="utf-8"), f"{directory}/{name}")


class DispatchArgvTests(unittest.TestCase):
    def test_no_route_keeps_argv_byte_identical(self) -> None:
        argv = build_dispatch_argv("codex", "do the work")
        self.assertEqual(argv, ["codex", "exec", "do the work"])
        claude = build_dispatch_argv("claude-code", "do the work")
        self.assertEqual(claude[0:3], ["claude", "-p", "do the work"])
        self.assertNotIn("--model", claude)
        with mock.patch("omh.coding.fanout_dispatch.shutil.which", lambda name: "/x/senpi" if name == "senpi" else None):
            senpi = build_dispatch_argv("omo-runtime", "do the work")
        self.assertEqual(
            senpi,
            ["senpi", "--print", "--no-session", "--permission-preset", "workspace", "do the work"],
        )

    def test_omo_runtime_model_and_thinking_insert_before_prompt(self) -> None:
        """The senpi host treats trailing tokens as message positionals, so
        routed options must land before the prompt — validated live: --print
        completes non-interactively and the workspace preset allowed file
        edits plus git add/commit."""
        route = {
            "schema_version": CODING_MODEL_ROUTE_SCHEMA_VERSION,
            "selected_model": "kimi-coding/k3",
            "selected_reasoning_effort": "high",
        }
        with mock.patch("omh.coding.fanout_dispatch.shutil.which", lambda name: "/x/senpi" if name == "senpi" else None):
            argv = build_dispatch_argv("omo-runtime", "do the work", route)
        self.assertEqual(
            argv,
            [
                "senpi",
                "--print",
                "--no-session",
                "--permission-preset",
                "workspace",
                "--model",
                "kimi-coding/k3",
                "--thinking",
                "high",
                "do the work",
            ],
        )

    def test_codex_model_and_effort_insert_before_prompt(self) -> None:
        route = resolve_model_route("codex", requested_model="gpt-5.6-sol", requested_effort="xhigh")
        argv = build_dispatch_argv("codex", "do the work", route)
        self.assertEqual(
            argv,
            [
                "codex",
                "exec",
                "--model",
                "gpt-5.6-sol",
                "--config",
                "model_reasoning_effort=xhigh",
                "do the work",
            ],
        )

    def test_claude_model_options_append_after_base_argv(self) -> None:
        route = resolve_model_route("claude-code", requested_model="opus", requested_effort="high")
        argv = build_dispatch_argv("claude-code", "do the work", route)
        self.assertEqual(argv[-4:], ["--model", "opus", "--effort", "high"])
        self.assertEqual(argv[1], "-p")

    def test_unknown_owner_has_no_argv(self) -> None:
        self.assertIsNone(build_dispatch_argv("hermes", "do the work"))

    def test_argv_unchanged_for_identical_route_dict(self) -> None:
        # 5a: the argv builder never learns about chains, provenance, or the
        # ladder — an identical route dict yields an identical argv.
        route = {"selected_model": "gpt-5", "selected_reasoning_effort": "high"}
        self.assertEqual(
            build_dispatch_argv("codex", "p", route),
            ["codex", "exec", "--model", "gpt-5", "--config", "model_reasoning_effort=high", "p"],
        )

    def test_behavior_change_review_on_codex_now_emits_model(self) -> None:
        # 5b(i) before/after: the old resolver returned choice_required with
        # no selected_model for review-on-codex, so dispatch emitted the bare
        # template argv; the chain head now emits --model gpt-6.1-sol, and
        # since 2026-10-01 the named `medium` its category row carries.
        old_shape_route = {"selected_model": "", "selected_reasoning_effort": ""}
        self.assertEqual(build_dispatch_argv("codex", "p", old_shape_route), ["codex", "exec", "p"])
        new_route = resolve_model_route("codex", role="review")
        self.assertEqual(
            build_dispatch_argv("codex", "p", new_route),
            ["codex", "exec", "--model", "gpt-6.1-sol", "--config", "model_reasoning_effort=medium", "p"],
        )

    def test_behavior_change_effort_max_on_codex_catalog_model(self) -> None:
        # 5b(ii) before/after: `max` previously reached the CLI unsupported;
        # the ladder now emits the downgraded rung in the argv.
        old_shape_route = {"selected_model": "gpt-5.6-sol", "selected_reasoning_effort": "max"}
        self.assertIn("model_reasoning_effort=max", build_dispatch_argv("codex", "p", old_shape_route))
        new_route = resolve_model_route("codex", requested_model="gpt-5.6-sol", requested_effort="max")
        self.assertIn("model_reasoning_effort=xhigh", build_dispatch_argv("codex", "p", new_route))


class OmoHostDetectionTests(unittest.TestCase):
    def test_detection_order_prefers_pi_then_senpi_then_opencode(self) -> None:
        """The usual host is `pi`; senpi is its distribution; opencode hosts
        omo as a plugin. First on PATH wins, deterministically."""
        from omh.coding.fanout_dispatch import OMO_RUNTIME_HOST_CANDIDATES, omo_runtime_host

        self.assertEqual(OMO_RUNTIME_HOST_CANDIDATES, ("pi", "senpi", "opencode"))
        both = lambda name: f"/x/{name}" if name in ("pi", "senpi") else None
        self.assertEqual(omo_runtime_host(both), "pi")
        self.assertEqual(omo_runtime_host(lambda name: "/x/senpi" if name == "senpi" else None), "senpi")
        self.assertEqual(omo_runtime_host(lambda name: "/x/opencode" if name == "opencode" else None), "opencode")
        self.assertIsNone(omo_runtime_host(lambda name: None))

    def test_pi_and_opencode_hosts_shape_the_argv(self) -> None:
        route = {
            "schema_version": CODING_MODEL_ROUTE_SCHEMA_VERSION,
            "selected_model": "zai/glm-5.2",
            "selected_reasoning_effort": "high",
        }
        with mock.patch("omh.coding.fanout_dispatch.shutil.which", lambda name: "/x/pi" if name == "pi" else None):
            pi_argv = build_dispatch_argv("omo-runtime", "work", route)
        self.assertEqual(pi_argv[0], "pi")
        self.assertEqual(pi_argv[5:9], ["--model", "zai/glm-5.2", "--thinking", "high"])
        with mock.patch("omh.coding.fanout_dispatch.shutil.which", lambda name: "/x/opencode" if name == "opencode" else None):
            oc_argv = build_dispatch_argv("omo-runtime", "work", route)
        self.assertEqual(oc_argv, ["opencode", "run", "--model", "zai/glm-5.2", "--variant", "high", "work"])

    def test_no_host_on_path_means_no_argv(self) -> None:
        with mock.patch("omh.coding.fanout_dispatch.shutil.which", lambda name: None):
            self.assertIsNone(build_dispatch_argv("omo-runtime", "work"))


class UnitModelRouteTests(unittest.TestCase):
    def test_unit_without_model_fields_stays_unrouted(self) -> None:
        self.assertIsNone(model_route_for_unit({"unit_id": "core"}, "codex"))

    def test_contract_embeds_model_route_when_unit_names_one(self) -> None:
        contract = build_fanout_contract(
            "split the sample feature across agents",
            [
                {"unit_id": "brain", "owner": "codex", "file_scope": ["src/a/"], "role": "brain"},
                {"unit_id": "ui", "owner": "claude-code", "file_scope": ["src/b/"], "model": "opus"},
                {"unit_id": "plain", "owner": "codex", "file_scope": ["src/c/"]},
            ],
        )
        units = {unit["unit_id"]: unit for unit in contract["units"]}
        brain_route = units["brain"]["handoff"]["model_route"]
        self.assertEqual(brain_route["schema_version"], CODING_MODEL_ROUTE_SCHEMA_VERSION)
        self.assertEqual(brain_route["selected_reasoning_effort"], "high")
        self.assertEqual(brain_route["provenance"], "role_chain_head")
        ui_route = units["ui"]["handoff"]["model_route"]
        self.assertEqual(ui_route["selected_model"], "opus")
        self.assertNotIn("model_route", units["plain"]["handoff"])


class ModelRouteCliTests(unittest.TestCase):
    def _base(self, tmp: str) -> list[str]:
        root = Path(tmp)
        return ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

    def test_cli_resolves_route_json(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(
                self._base(tmp) + ["coding", "model-route", "--executor", "codex", "--role", "brain"]
            )
            self.assertEqual(status, 0, stderr)
            route = json.loads(stdout)
            self.assertEqual(route["schema_version"], CODING_MODEL_ROUTE_SCHEMA_VERSION)
            self.assertEqual(route["selected_reasoning_effort"], "high")
            self.assertEqual(route["provenance"], "role_chain_head")

    def test_cli_plain_text_is_default_and_names_provenance(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(
                self._base(tmp) + ["coding", "model-route", "--executor", "codex", "--role", "brain"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            self.assertIn("role_chain_head", stdout)
            self.assertIn("chain:", stdout)
            self.assertNotIn('{"', stdout)

    def test_explain_matrix_covers_every_profile_role_cell(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(self._base(tmp) + ["coding", "model-route", "--explain"])
            self.assertEqual(status, 0, stderr)
            matrix = json.loads(stdout)
            self.assertEqual(matrix["schema_version"], "coding_model_route_matrix/v1")
            cells = matrix["cells"]
            self.assertEqual(len(cells), len(EXECUTOR_MODEL_OPTIONS) * len(MODEL_ROLES))
            for cell in cells:
                direct = resolve_model_route(cell["executor_profile"], role=cell["role"])
                self.assertEqual(cell["selected_model"], direct["selected_model"], cell)
                self.assertEqual(cell["provenance"], direct["provenance"], cell)
                self.assertTrue(cell["chain"], cell)
            self.assertIn("hermes", matrix["catalogless_profiles"])

    def test_explain_narrows_with_executor_and_renders_full_chains_in_text(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(
                self._base(tmp) + ["coding", "model-route", "--explain", "--executor", "codex"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            lines = [line for line in stdout.splitlines() if line.startswith("- ")]
            self.assertEqual(len(lines), len(MODEL_ROLES))
            self.assertIn("gpt-6.1-sol high*", stdout)
            self.assertNotIn("gpt-5.6", stdout)
            self.assertNotIn("claude-code", stdout)

    def test_model_route_without_executor_or_explain_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            status, _stdout, stderr = run_cli(self._base(tmp) + ["coding", "model-route"])
            self.assertNotEqual(status, 0)
            self.assertIn("--executor", stderr)


if __name__ == "__main__":
    unittest.main()
