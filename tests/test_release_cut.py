from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "release-cut"
WATCH = "deploy-and-monitor"
REFERENCE_PATH = "references/release-and-rollback-method.md"


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


class ReleaseCutCatalogTests(unittest.TestCase):
    def test_a_plan_is_not_ready_without_a_named_rollback_trigger_and_command(self) -> None:
        """#1693: readiness is a verdict over the rollback, decided before it is needed."""

        mine = _definition(SKILL)
        rule = mine.safety_rules[0]
        self.assertTrue(
            rule.startswith("A release plan cannot be ready without a named rollback trigger and the exact command that performs it")
        )
        self.assertIn("release_readiness_verdict/v1", mine.expected_outputs)
        verdict = next(line for line in mine.artifact_expectations if line.startswith("release_readiness_verdict/v1"))
        self.assertIn("named rollback trigger and its exact command", verdict)
        trigger = next(line for line in mine.artifact_expectations if line.startswith("rollback_trigger/v1"))
        for field in ("signal and threshold", "exact command", "who runs it"):
            with self.subTest(field=field):
                self.assertIn(field, trigger)

    def test_the_cut_freezes_the_branch_until_the_tag_is_pushed(self) -> None:
        """This repository's own failure: a merge during the cut broke the atomic push."""

        mine = _definition(SKILL)
        self.assertTrue(any("between starting a cut and its tag push" in rule for rule in mine.safety_rules))
        cut = _reference().split("## 2. The cut sequence", 1)[1].split("## 3.", 1)[0]
        self.assertLess(cut.index("| 2 | Freeze the release branch"), cut.index("| 3 | Run the cut"))
        self.assertLess(cut.index("| 3 | Run the cut"), cut.index("| 4 | Pass the approval gate"))
        self.assertLess(cut.index("| 5 | Publish"), cut.index("| 6 | Curate the notes"))

    def test_the_boundary_with_the_deploy_watch_is_stated_from_both_sides(self) -> None:
        mine = _definition(SKILL)
        self.assertEqual(len([text for text in mine.do_not_use_when if f"`{WATCH}`" in text]), 1)
        self.assertEqual(len([text for text in _definition(WATCH).do_not_use_when if f"`{SKILL}`" in text]), 1)

    def test_the_method_lives_in_the_reference(self) -> None:
        reference = _reference()
        for heading in ("## 2. The cut sequence", "## 3. Rollout stages", "## 4. Rollback trigger and command"):
            with self.subTest(heading=heading):
                self.assertIn(heading, reference)
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)
        self.assertNotIn("## 2. The cut sequence", body)


class ReleaseCutRoutingTests(unittest.TestCase):
    def test_the_issue_rows_dispatch_here(self) -> None:
        for message in (
            "cut a release and tag it",
            "roll back the last deploy",
            "set up a canary for this service",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_watching_a_deploy_stays_with_the_deploy_watch(self) -> None:
        self.assertEqual(_route("deploy this service to staging and keep an eye on release health")["selected_skill"], WATCH)
        route = _route("watch metrics after the deploy")
        self.assertNotEqual(route["action"], "dispatch")
        self.assertEqual(route.get("candidate_skill"), WATCH)

    def test_the_same_words_outside_shipping_software_stay_away(self) -> None:
        for message in (
            "the band cut a release of their new album",
            "my canary stopped singing",
            "the release candidate for mayor spoke tonight",
            "roll back the carpet in the hallway",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)
                self.assertNotEqual(awareness_route_hint(message).get("selected_workflow"), SKILL)


if __name__ == "__main__":
    unittest.main()
