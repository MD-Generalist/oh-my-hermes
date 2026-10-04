from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.coding_contracts import (  # noqa: E402
    STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE,
    STRUCTURAL_SEARCH_GUIDANCE,
)
from omh.coding.coding_delegation import (  # noqa: E402
    _executor_local_capability_strategy,
    _local_capability_prompt_block,
    _runtime_local_capability_prompt_block,
)
from omh.coding.executor_guidance_compatibility import (  # noqa: E402
    build_executor_guidance_compatibility,
    guidance_leakage_findings,
)
from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_dispatch import build_unit_prompt  # noqa: E402

# Pre-existing host-vocabulary leakage in two capability block shapes, measured on
# this tree and deliberately NOT fixed by this PR. Source: the "Before acting,
# inspect project instructions and executor-local capabilities such as AGENTS.md,
# CLAUDE.md, ..." discovery bullet in _local_capability_prompt_block
# (src/coding/coding_delegation.py) — the claude-code block and the generic
# fall-through block each carry their own copy of that sentence; the codex and
# runtime shapes do not.
#
# MAINTENANCE: update this set when that leakage is fixed — tracked as a follow-up
# issue (see the PR's linked issue trail). Do not widen it to absorb a NEW leak; a
# new token here means this PR's guidance introduced one, which is exactly what
# the test must catch.
PRE_EXISTING_HOST_LEAKAGE = {
    "codex": frozenset(),
    "claude-code": frozenset({"CLAUDE.md"}),
    "generic": frozenset({"CLAUDE.md"}),
    "runtime": frozenset(),
}


def _lane_a_shapes() -> dict[str, str]:
    return {
        "codex": _local_capability_prompt_block("codex", "Codex"),
        "claude-code": _local_capability_prompt_block("claude-code", "Claude Code"),
        "generic": _local_capability_prompt_block("opencode", "OpenCode"),
        "runtime": _runtime_local_capability_prompt_block("hermes", "Hermes"),
    }


def _lane_b_prompt() -> str:
    contract = build_fanout_contract(
        "add the bounded exporter flag",
        [
            {"unit_id": "impl", "title": "Impl", "owner": "codex", "file_scope": ["src/core/"]},
            {"unit_id": "aux", "title": "Aux", "owner": "claude-code", "file_scope": ["docs/"]},
        ],
    )
    unit = {unit["unit_id"]: unit for unit in contract["units"]}["impl"]
    return build_unit_prompt(unit, "add the bounded exporter flag")


class StructuralSearchGuidanceNeutralityTests(unittest.TestCase):
    def test_constant_is_neutral(self) -> None:
        self.assertEqual(guidance_leakage_findings(STRUCTURAL_SEARCH_GUIDANCE), ())

    def test_capped_search_discipline_constant_is_neutral(self) -> None:
        self.assertEqual(guidance_leakage_findings(STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE), ())
        guidance = STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE.lower()
        self.assertIn("capped", guidance)
        self.assertIn("stop", guidance)

    def test_lane_b_carries_capped_search_discipline(self) -> None:
        self.assertIn(STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE, _lane_b_prompt())

    def test_shapes_carry_only_pre_existing_leakage(self) -> None:
        for shape, text in _lane_a_shapes().items():
            with self.subTest(shape=shape):
                tokens = {entry.token for entry in guidance_leakage_findings(text)}
                self.assertEqual(tokens, set(PRE_EXISTING_HOST_LEAKAGE[shape]))

    def test_lane_b_stays_fully_clean(self) -> None:
        self.assertEqual(guidance_leakage_findings(_lane_b_prompt()), ())

    def test_public_compatibility_entry_point_reports_clean(self) -> None:
        payload = build_executor_guidance_compatibility(
            guidance_ref="structural_search_guidance/v1",
            guidance_text=STRUCTURAL_SEARCH_GUIDANCE,
        )
        self.assertEqual(payload["leakage"]["status"], "clean")
        self.assertEqual(payload["summary"]["leakage_finding_count"], 0)

    def test_guidance_is_present_in_every_lane(self) -> None:
        for shape, text in _lane_a_shapes().items():
            with self.subTest(shape=shape):
                self.assertIn(f"- {STRUCTURAL_SEARCH_GUIDANCE}\n", text)
        # Lane B states the preference once, in the shared head's capped-search
        # block ("structural search when available, grep otherwise"); the
        # tool-preference sentence is not appended a second time.
        prompt = _lane_b_prompt()
        self.assertIn("structural search when available, grep otherwise", prompt)
        self.assertNotIn(STRUCTURAL_SEARCH_GUIDANCE, prompt)

    def test_guidance_stays_before_the_closing_claim_boundary(self) -> None:
        for shape, text in _lane_a_shapes().items():
            with self.subTest(shape=shape):
                guidance_at = text.index(STRUCTURAL_SEARCH_GUIDANCE)
                boundary_at = text.index("- Do not claim OMH observed")
                self.assertLess(guidance_at, boundary_at)

    def test_strategy_record_names_structural_search(self) -> None:
        strategy = _executor_local_capability_strategy("codex")
        self.assertTrue(
            any("structural search" in source for source in strategy["preferred_sources"])
        )
        self.assertIn("code_exploration", strategy["stage_guidance"])
        self.assertIn("structural search", strategy["stage_guidance"]["code_exploration"])


if __name__ == "__main__":
    unittest.main()
