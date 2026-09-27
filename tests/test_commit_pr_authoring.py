from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "commit-pr-authoring"
REFERENCE_PATH = "references/commit-and-pr-conventions.md"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


def _route(message: str) -> dict[str, object]:
    return build_chat_interaction_payload(message, source="discord")["route"]


class CommitPrAuthoringCatalogTests(unittest.TestCase):
    def test_tested_lists_only_observed_commands(self) -> None:
        """#1711: the ledger decides the line, and wording cannot promote a row."""

        mine = _definition(SKILL)
        rule = next(rule for rule in mine.safety_rules if rule.startswith("List a command under `Tested:`"))
        self.assertIn("observed", rule)
        self.assertIn("`Not-tested:`", rule)
        self.assertIn("whatever the wording says", rule)
        self.assertIn("evidence_ledger/v1", mine.expected_outputs)
        self.assertTrue(any("observed ledger row" in line for line in mine.final_checklist))

    def test_it_reads_the_repo_convention_and_never_commits(self) -> None:
        mine = _definition(SKILL)
        self.assertIn("repo_convention_read/v1", mine.expected_outputs)
        self.assertTrue(any("template" in step and "log" in step for step in mine.opening_steps))
        self.assertTrue(any(rule.startswith("Never commit, amend, push, or open the PR") for rule in mine.safety_rules))
        self.assertTrue(any("Nothing was committed, pushed, or opened by OMH" in line for line in mine.final_checklist))

    def test_the_ledger_rules_live_in_the_reference(self) -> None:
        references = {
            template.relative_path: template.content
            for template in builtin_skill_reference_templates()
            if template.skill_name == SKILL
        }
        reference = references[REFERENCE_PATH]
        self.assertIn("Wording never promotes a row", reference)
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)


class CommitPrAuthoringRoutingTests(unittest.TestCase):
    def test_the_issue_rows_dispatch_here(self) -> None:
        for message in ("write the commit message", "draft the PR body"):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)
                self.assertEqual(awareness_route_hint(message).get("selected_workflow"), SKILL)

    def test_the_same_words_outside_a_repository_stay_away(self) -> None:
        for message in (
            "write a PR description of our product launch for the press release",
            "write a message to my landlord about the pull request for more time",
            "I need to commit to a message for my wedding speech",
            "draft the body of my cover letter",
            "the job description template for a nurse",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)

    def test_siblings_keep_their_lanes(self) -> None:
        self.assertEqual(_route("file this as a github issue")["selected_skill"], "github-issue-intake")
        self.assertEqual(_route("write the release notes for version 2.0")["selected_skill"], "content-operator")


if __name__ == "__main__":
    unittest.main()
