from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.routing.policy import everyday_sense_phrase_unanchored
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "mobile-release"
REFERENCE_PATH = "references/mobile-release-method.md"


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


class MobileReleaseCatalogTests(unittest.TestCase):
    """#1568: signing, privacy, beta, staged rollout and hotfix are named outputs."""

    def test_the_five_store_gates_are_named_outputs(self) -> None:
        mine = _definition(SKILL)
        self.assertEqual(
            mine.expected_outputs,
            (
                "signing_plan/v1",
                "privacy_declaration_check/v1",
                "beta_channel_plan/v1",
                "staged_rollout_plan/v1",
                "hotfix_plan/v1",
            ),
        )
        for schema in mine.expected_outputs:
            with self.subTest(schema=schema):
                self.assertEqual(len([line for line in mine.artifact_expectations if line.startswith(schema)]), 1)

    def test_the_halt_and_the_next_build_come_before_the_rollout(self) -> None:
        mine = _definition(SKILL)
        self.assertTrue(mine.safety_rules[0].startswith("A store release cannot be rolled back"))
        rollout = next(line for line in mine.artifact_expectations if line.startswith("staged_rollout_plan/v1"))
        self.assertIn("thresholds that halt each step", rollout)
        hotfix = next(line for line in mine.artifact_expectations if line.startswith("hotfix_plan/v1"))
        self.assertIn("higher build number", hotfix)
        signing = next(line for line in mine.artifact_expectations if line.startswith("signing_plan/v1"))
        self.assertIn("never contains the secret itself", signing)

    def test_the_reference_gives_both_stores_every_gate(self) -> None:
        table = _reference().split("## 1. Per-platform gates", 1)[1].split("## 2.", 1)[0]
        rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Gate") and "---" not in line]
        self.assertEqual([row.split("|")[1].strip() for row in rows], ["signing", "privacy", "beta", "rollout", "no rollback"])
        for row in rows:
            with self.subTest(gate=row.split("|")[1].strip()):
                self.assertTrue(all(cell.strip() for cell in row.strip("|").split("|")))
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)

    def test_release_cut_names_the_other_side(self) -> None:
        sibling = [text for text in _definition("release-cut").do_not_use_when if f"`{SKILL}`" in text]
        self.assertEqual(len(sibling), 1)
        mine = _definition(SKILL).do_not_use_when
        for other in ("release-cut", "deploy-and-monitor", "production-audit", "app-debugging"):
            with self.subTest(sibling=other):
                self.assertEqual(len([text for text in mine if f"`{other}`" in text]), 1)


class MobileReleaseRoutingTests(unittest.TestCase):
    def test_the_store_release_asks_dispatch_here(self) -> None:
        for message in (
            "we are shipping 3.2 to the app store and google play next week",
            "our testflight build is stuck in beta review",
            "the play store staged rollout is at 20% and crashes went up, halt it and plan the fix",
            "set up code signing and provisioning profiles for the ios release",
            "update the privacy manifest for the required reason apis before we submit",
            "push a hotfix for the android release, the login crashes",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_generic_releases_and_deploy_watching_keep_their_owners(self) -> None:
        for message, owner in (
            ("set up a staged rollout for the api release", "release-cut"),
            ("cut a release and tag it", "release-cut"),
            ("deploy the api that the ios app calls and keep an eye on release health", "deploy-and-monitor"),
        ):
            with self.subTest(message=message):
                self.assertEqual(_route(message)["selected_skill"], owner)

    def test_the_same_words_elsewhere_stay_away(self) -> None:
        for message in (
            "the play store on my phone will not open",
            "my google play gift card did not work",
            "the app store keeps asking for my password",
            "rotate the keystore for the kafka brokers",
            "my android phone battery drains fast",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)

    def test_everyday_phrases_need_a_word_of_release_work(self) -> None:
        for message, withdrawn in (
            ("the play store on my phone will not open", True),
            ("my google play gift card did not work", True),
            ("apple rejected our app store submission", False),
            ("we are shipping 3.2 to the app store and google play next week", False),
        ):
            with self.subTest(message=message):
                self.assertIs(everyday_sense_phrase_unanchored(SKILL, normalized_phrase(message)), withdrawn)


if __name__ == "__main__":
    unittest.main()
