from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "security-event-response"
REVIEW = "security-safety-review"
EVENTS = "github-event-ops"
UPGRADE = "refactor-plan"
REFERENCE_PATH = "references/event-containment-order.md"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


def _route(message: str) -> dict[str, object]:
    return build_chat_interaction_payload(message, source="discord")["route"]


class SecurityEventResponseCatalogTests(unittest.TestCase):
    def test_a_leaked_secret_cannot_close_on_a_prepared_rotation(self) -> None:
        """#1694: closure is a verdict over observed steps, not over a plan."""

        mine = _definition(SKILL)
        rule = mine.safety_rules[0]
        self.assertTrue(rule.startswith("A leaked-secret event cannot close while its rotation is prepared rather than observed"))
        self.assertIn("event_closure_verdict/v1", mine.expected_outputs)
        verdict = next(line for line in mine.artifact_expectations if line.startswith("event_closure_verdict/v1"))
        self.assertIn("observed", verdict)
        self.assertIn("prepared step keeps the event open", verdict)

    def test_rotation_is_ordered_before_any_history_rewrite(self) -> None:
        mine = _definition(SKILL)
        plan = next(line for line in mine.artifact_expectations if line.startswith("containment_plan/v1"))
        self.assertLess(plan.index("rotation"), plan.index("history rewrite"))
        order_rule = next(rule for rule in mine.safety_rules if rule.startswith("Order rotation before history rewriting"))
        self.assertLess(order_rule.index("observe the old one rejected"), order_rule.index("then rewrite history"))
        reference = next(
            template.content
            for template in builtin_skill_reference_templates()
            if template.skill_name == SKILL and template.relative_path == REFERENCE_PATH
        )
        table = reference.split("## 1. Leaked secret", 1)[1].split("## 2.", 1)[0]
        self.assertLess(table.index("| 3 | Revoke the leaked credential"), table.index("| 5 | Rewrite history"))

    def test_the_boundary_is_stated_from_both_sides(self) -> None:
        mine = _definition(SKILL)
        for sibling in (REVIEW, UPGRADE):
            with self.subTest(sibling=sibling):
                self.assertEqual(len([text for text in mine.do_not_use_when if f"`{sibling}`" in text]), 1)
                back = [text for text in _definition(sibling).do_not_use_when if f"`{SKILL}`" in text]
                self.assertEqual(len(back), 1)
        # The PR card hands an advisory here; a bump with no advisory is the
        # upgrade lane's (#1712), so this skill names `refactor-plan` for it
        # rather than handing it back to the PR card.
        self.assertEqual(len([text for text in _definition(EVENTS).do_not_use_when if f"`{SKILL}`" in text]), 1)
        self.assertEqual([text for text in mine.do_not_use_when if f"`{EVENTS}`" in text], [])

    def test_the_per_event_order_lives_in_the_reference(self) -> None:
        reference = next(
            template.content
            for template in builtin_skill_reference_templates()
            if template.skill_name == SKILL and template.relative_path == REFERENCE_PATH
        )
        for heading in ("## 1. Leaked secret", "## 2. CVE or advisory in a dependency", "## 3. License question"):
            with self.subTest(heading=heading):
                self.assertIn(heading, reference)
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)
        self.assertNotIn("## 1. Leaked secret", body)


class SecurityEventResponseRoutingTests(unittest.TestCase):
    def test_the_issue_rows_dispatch_here(self) -> None:
        for message in (
            "triage this CVE in our dependency tree",
            "we committed a secret, what now",
            "is this dependency's license OK for us",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_a_dependabot_security_advisory_is_an_event_not_a_pr_card(self) -> None:
        route = _route("dependabot opened a security advisory PR for lodash")
        self.assertEqual(route["selected_skill"], SKILL)
        self.assertEqual(_route("PR opened with failing CI, triage it")["selected_skill"], EVENTS)

    def test_a_planned_rotation_stays_with_the_safety_review(self) -> None:
        self.assertEqual(_route("rotate this api key without an outage")["selected_skill"], REVIEW)

    def test_the_same_words_outside_code_stay_away(self) -> None:
        for message in (
            "the travel security advisory for mexico",
            "the leaked secret ending of the film spoiled it",
            "is my driver's license ok to use abroad",
            "my secret recipe for pancakes",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)
                self.assertNotEqual(awareness_route_hint(message).get("selected_workflow"), SKILL)


if __name__ == "__main__":
    unittest.main()
