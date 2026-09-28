from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "agent-instructions"
DISTILL = "rules-distill"
REFERENCE_PATH = "references/instruction-file-method.md"
BEGIN = "<!-- omh:agent-instructions:begin -->"
END = "<!-- omh:agent-instructions:end -->"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


def _route(message: str) -> dict[str, object]:
    return build_chat_interaction_payload(message, source="discord")["route"]


def _reference() -> str:
    return next(
        template.content
        for template in builtin_skill_reference_templates()
        if template.skill_name == SKILL and template.relative_path == REFERENCE_PATH
    )


class AgentInstructionsCatalogTests(unittest.TestCase):
    def test_updates_stay_inside_the_marked_region(self) -> None:
        """#1713: hand-written sections are never rewritten."""

        mine = _definition(SKILL)
        rule = mine.safety_rules[0]
        self.assertTrue(rule.startswith("Never write outside the marker-delimited region"))
        for marker in (BEGIN, END):
            with self.subTest(marker=marker):
                self.assertIn(marker, rule)
                self.assertIn(marker, _reference())
        update = next(line for line in mine.artifact_expectations if line.startswith("instruction_region_update/v1"))
        self.assertIn("replaces only the text between", update)
        self.assertIn("byte-for-byte", update)

    def test_every_command_is_verified_or_marked_unverified(self) -> None:
        mine = _definition(SKILL)
        self.assertIn("Never write a command as verified without an observed run; mark it unverified instead.", mine.safety_rules)
        record = next(line for line in mine.artifact_expectations if line.startswith("command_verification_record/v1"))
        self.assertIn("verified, with the observed exit status", record)
        self.assertIn("or unverified", record)

    def test_counts_and_line_numbers_are_refused(self) -> None:
        mine = _definition(SKILL)
        refusal = next(rule for rule in mine.safety_rules if rule.startswith("Refuse to record counts, line numbers"))
        self.assertIn("they drift", refusal)
        self.assertIn("drift_refusal_note/v1 when a requested line would record a count or a line number", mine.expected_outputs)
        table = _reference().split("## 4. What never goes in", 1)[1]
        for row in ("the suite has", "the router is at line"):
            with self.subTest(row=row):
                self.assertIn(row, table)

    def test_the_method_lives_in_the_reference(self) -> None:
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)
        self.assertNotIn("## 4. What never goes in", body)

    def test_the_boundary_with_rules_distill_is_stated_from_both_sides(self) -> None:
        mine = _definition(SKILL)
        self.assertEqual(len([text for text in mine.do_not_use_when if f"`{DISTILL}`" in text]), 1)
        self.assertEqual(len([text for text in _definition(DISTILL).do_not_use_when if f"`{SKILL}`" in text]), 1)


class AgentInstructionsRoutingTests(unittest.TestCase):
    def test_the_issue_rows_dispatch_here(self) -> None:
        for message in ("set up AGENTS.md", "update our CLAUDE.md"):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_distilling_rule_candidates_stays_with_rules_distill(self) -> None:
        message = "Distill repeated lessons into AGENTS.md rule candidates about the four-fifths rule"
        self.assertEqual(_route(message)["selected_skill"], DISTILL)

    def test_the_same_words_outside_a_repository_stay_away(self) -> None:
        for message in (
            "my travel agent sent instructions for the trip",
            "write the instructions for the secret agent in our board game",
            "the mouse cursor rules the screen in this game",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)
                self.assertNotEqual(awareness_route_hint(message).get("selected_workflow"), SKILL)


if __name__ == "__main__":
    unittest.main()
