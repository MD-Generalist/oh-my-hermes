from __future__ import annotations

import re
import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "git-workflow"
REFERENCE_PATH = "references/git-repair-method.md"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


def _route(message: str) -> dict[str, object]:
    return build_chat_interaction_payload(message, source="discord")["route"]


def _skill_texts() -> list[str]:
    body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
    references = [
        template.content for template in builtin_skill_reference_templates() if template.skill_name == SKILL
    ]
    return [body, *references]


class GitWorkflowCatalogTests(unittest.TestCase):
    def test_what_is_pushed_is_named_before_any_rewrite(self) -> None:
        """#1695: the inventory comes first, in the outputs and in the opening steps."""

        mine = _definition(SKILL)
        self.assertEqual(mine.expected_outputs[0], "pushed_state_inventory/v1")
        self.assertIn("pushed", mine.opening_steps[0])
        self.assertTrue(any(rule.startswith("Name what is already pushed before planning any rewrite") for rule in mine.safety_rules))

    def test_every_force_push_is_leased(self) -> None:
        mine = _definition(SKILL)
        self.assertTrue(any("`--force-with-lease`" in rule and "never part of the plan" in rule for rule in mine.safety_rules))
        # No shipped text for this skill offers an unleased force-push: the
        # only `--force` it may spell is the bare form it names to forbid.
        bare = re.compile(r"--force(?![-\w])")
        for text in _skill_texts():
            with self.subTest(text=text[:40]):
                offered = [m.start() for m in bare.finditer(text) if not text[: m.start()].endswith("bare `")]
                self.assertEqual(offered, [])

    def test_the_procedures_live_in_the_reference(self) -> None:
        reference = next(
            template.content
            for template in builtin_skill_reference_templates()
            if template.skill_name == SKILL and template.relative_path == REFERENCE_PATH
        )
        for heading in ("Inventory what is pushed", "Conflicts", "Bisect", "History rewrite", "Stacked branches"):
            with self.subTest(heading=heading):
                self.assertIn(heading, reference)
        self.assertIn("never picked", reference)


class GitWorkflowRoutingTests(unittest.TestCase):
    def test_the_issue_rows_dispatch_here(self) -> None:
        for message in (
            "resolve this merge conflict",
            "bisect to find which commit broke it",
            "clean up this branch's history before review",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)
                self.assertEqual(awareness_route_hint(message).get("selected_workflow"), SKILL)

    def test_the_same_words_outside_a_repository_stay_away(self) -> None:
        for message in (
            "how do I bisect an angle with a compass",
            "force push the car out of the snow",
            "we need to resolve the conflict between the two teams",
            "cherry-pick the best apples at the orchard",
            "clean up my browser history",
            "the reflog of my feelings",
        ):
            with self.subTest(message=message):
                self.assertNotEqual(_route(message)["action"], "dispatch")
                self.assertNotEqual(_route(message).get("selected_skill"), SKILL)

    def test_siblings_keep_their_lanes(self) -> None:
        self.assertEqual(_route("write the commit message")["selected_skill"], "commit-pr-authoring")
        self.assertEqual(_route("the build is failing with a compile error")["selected_skill"], "build-failure-triage")


if __name__ == "__main__":
    unittest.main()
