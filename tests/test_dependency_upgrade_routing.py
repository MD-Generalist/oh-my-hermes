from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.routing.policy import dependency_bump_spoken, version_jump_spoken
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "refactor-plan"
EVENT = "security-event-response"
GITHUB = "github-event-ops"
REFERENCE_PATH = "references/dependency-upgrade.md"


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


class CallSiteReadinessGateTests(unittest.TestCase):
    """#1712: an upgrade plan is ready only over a filled call-site table."""

    def test_the_plan_is_not_ready_while_a_breaking_change_has_no_call_sites(self) -> None:
        mine = _definition(SKILL)
        rule = next(line for line in mine.safety_rules if line.startswith("An upgrade plan is not ready"))
        self.assertIn("lacks its call sites or an observed empty search", rule)
        self.assertIn("any stage lacks its rollback", rule)
        self.assertEqual(
            len([line for line in mine.expected_outputs if line.startswith("for an upgrade, the call-site readiness gate")]), 1
        )
        self.assertEqual(
            len([line for line in mine.final_checklist if "every breaking change row names its call sites" in line]), 1
        )

    def test_the_reference_table_has_a_call_site_and_a_rollback_column(self) -> None:
        gate = _reference().split("## 5. Call-site readiness gate", 1)[1].split("## Phase order for an upgrade", 1)[0]
        self.assertIn("| Breaking change | Call sites in this repo | Stage | Rollback |", gate)
        self.assertIn("A search that was prepared\nbut not run is an empty cell.", gate)

    def test_every_upgrade_stage_states_its_rollback(self) -> None:
        stages = _reference().split("## Phase order for an upgrade", 1)[1].split("## Boundary", 1)[0]
        self.assertIn("| Stage | What it does | Rollback |", stages)
        for stage in ("| Prepare |", "| Bump |", "| Adapt |", "| Remove shims |"):
            with self.subTest(stage=stage):
                row = next(line for line in stages.splitlines() if line.startswith(stage))
                self.assertEqual(row.count("|"), 4)
                self.assertTrue(row.rsplit("|", 2)[1].strip(), f"{stage} has no rollback")

    def test_the_dependabot_split_is_stated_in_all_three_skills(self) -> None:
        """An advisory is an event; a bump with no advisory is an upgrade; the PR card takes neither."""

        mine = [text for text in _definition(SKILL).do_not_use_when if f"`{EVENT}`" in text]
        self.assertEqual(len(mine), 1)
        self.assertIn("advisory", mine[0])
        event = [text for text in _definition(EVENT).do_not_use_when if "dependabot bump" in text]
        self.assertEqual(len(event), 1)
        self.assertIn(f"`{SKILL}`", event[0])
        github = [text for text in _definition(GITHUB).do_not_use_when if "dependabot" in text]
        self.assertEqual(len(github), 1)
        self.assertIn(f"`{SKILL}`", github[0])


class DependencyBumpRoutingTests(unittest.TestCase):
    def test_the_issue_rows_dispatch_to_the_upgrade_plan(self) -> None:
        for message in (
            "dependabot bumped express 4.18 to 5.0, safe to merge",
            "dependabot opened a PR bumping express from 4.18 to 5.0",
            "renovate bumped django 4.2 to 5.1, is it safe to merge",
            "upgrade react from 18 to 19",
            "upgrade python from 3.11 to 3.13",
            "we need to upgrade next.js from 14 to 15",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_an_advisory_stays_an_event_and_a_ci_failure_stays_a_pr_card(self) -> None:
        self.assertEqual(_route("dependabot opened a security advisory PR for lodash")["selected_skill"], EVENT)
        self.assertEqual(_route("PR opened with failing CI, triage it")["selected_skill"], GITHUB)
        cve_bump = _route("dependabot bumped lodash from 4.17.20 to 4.17.21 to fix CVE-2021-23337")
        self.assertNotEqual(cve_bump["selected_skill"], SKILL)
        self.assertNotEqual(cve_bump.get("candidate_skill"), SKILL)

    def test_a_version_jump_needs_two_versions_and_a_word_of_software(self) -> None:
        for message, expected in (
            ("upgrade react from 18 to 19", True),
            ("bumped express 4.18 to 5.0 via dependabot", True),
            ("upgrade flask from 2.3 to 3.0", True),
            ("upgrade react to the latest", False),
            ("upgrade the python course to 3 credits", False),
            ("upgrade my iphone from 14 to 16", False),
            ("upgrade to express shipping from 5 to 2 days", False),
            ("move the react meeting from 3 to 4", False),
        ):
            with self.subTest(message=message):
                self.assertIs(version_jump_spoken(normalized_phrase(message)), expected)

    def test_a_bump_that_carries_a_security_event_is_not_an_upgrade(self) -> None:
        for message in (
            "dependabot bumped lodash from 4.17.20 to 4.17.21 to fix CVE-2021-23337",
            "dependabot bumped lodash for the security advisory",
        ):
            with self.subTest(message=message):
                self.assertFalse(dependency_bump_spoken(normalized_phrase(message)))

    def test_the_same_shape_outside_software_stays_away(self) -> None:
        for message in (
            "upgrade my iphone from 14 to 16",
            "the price bumped from 10 to 12 dollars",
            "upgrade to express shipping from 5 to 2 days",
            "my kid bumped his grade from 3 to 4",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)


if __name__ == "__main__":
    unittest.main()
