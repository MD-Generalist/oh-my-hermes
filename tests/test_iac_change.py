from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.routing.policy import everyday_sense_phrase_unanchored
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "iac-change"
REFERENCE_PATH = "references/iac-change-method.md"


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


class IacChangeCatalogTests(unittest.TestCase):
    """#1566: the six named outputs, and a stage never promotes on a prepared gate."""

    def test_the_six_named_outputs_are_first_class(self) -> None:
        mine = _definition(SKILL)
        self.assertEqual(
            mine.expected_outputs,
            (
                "drift_assessment/v1",
                "blast_radius/v1",
                "cost_delta/v1",
                "staged_apply_plan/v1",
                "health_gate/v1",
                "rollback_plan/v1",
            ),
        )
        for schema in mine.expected_outputs:
            with self.subTest(schema=schema):
                self.assertEqual(len([line for line in mine.artifact_expectations if line.startswith(schema)]), 1)

    def test_every_stage_has_a_gate_and_a_rollback(self) -> None:
        mine = _definition(SKILL)
        self.assertIn("per stage", next(line for line in mine.artifact_expectations if line.startswith("health_gate/v1")))
        self.assertIn("per stage", next(line for line in mine.artifact_expectations if line.startswith("rollback_plan/v1")))
        self.assertTrue(mine.safety_rules[0].startswith("Never promote a stage without its health gate observed"))
        self.assertIn("Every stage names its health gate and its rollback.", mine.final_checklist)

    def test_the_reference_gives_every_tool_a_health_gate_and_a_rollback(self) -> None:
        table = _reference().split("## 1. Per tool", 1)[1].split("## 2.", 1)[0]
        rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Tool") and "---" not in line]
        self.assertEqual(len(rows), 5)
        for row in rows:
            with self.subTest(row=row.split("|")[1]):
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                self.assertEqual(len(cells), 5)
                self.assertTrue(all(cells), row)
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)

    def test_application_releases_name_the_other_side(self) -> None:
        deploy = [text for text in _definition("deploy-and-monitor").do_not_use_when if f"`{SKILL}`" in text]
        self.assertEqual(len(deploy), 1)
        mine = _definition(SKILL).do_not_use_when
        for sibling in ("deploy-and-monitor", "release-cut", "inference-serving", "live-incident-response"):
            with self.subTest(sibling=sibling):
                self.assertEqual(len([text for text in mine if f"`{sibling}`" in text]), 1)


class IacChangeRoutingTests(unittest.TestCase):
    def test_the_issue_row_and_the_tools_dispatch_here(self) -> None:
        for message in (
            "terraform plan shows drift in the kubernetes cluster, stage the apply",
            "review this terraform plan before we apply it",
            "estimate the cost delta of this terraform change",
            "we are changing the helm chart values for the payments service, what could break",
            "pulumi preview shows 14 resources replaced",
            "kubectl apply this manifest change safely",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_application_releases_stay_with_their_owners(self) -> None:
        for message, owner in (
            ("deploy this service to staging and keep an eye on release health", "deploy-and-monitor"),
            ("deploy the new app version to the kubernetes cluster and watch the health checks", "deploy-and-monitor"),
            ("roll back the last deploy", "release-cut"),
            ("cut a release and tag it", "release-cut"),
            ("serve llama on kubernetes with vllm", "inference-serving"),
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], owner)

    def test_the_same_words_outside_infrastructure_stay_away(self) -> None:
        for message in (
            "we need to terraform mars in my sci-fi novel",
            "the boat drifted so take the helm",
            "the cost delta between the two flights is 40 dollars",
            "pods stuck in CrashLoopBackOff after helm upgrade",
            "what is terraform",
        ):
            with self.subTest(message=message):
                self.assertNotEqual(_route(message)["action"], "dispatch")

    def test_everyday_phrases_need_a_word_of_infrastructure(self) -> None:
        for message, withdrawn in (
            ("the cost delta between the two flights is 40 dollars", True),
            ("a helm chart for the sailing class", True),
            ("estimate the cost delta of this terraform change", False),
            ("update the helm chart values for the ingress", False),
        ):
            with self.subTest(message=message):
                self.assertIs(everyday_sense_phrase_unanchored(SKILL, normalized_phrase(message)), withdrawn)


if __name__ == "__main__":
    unittest.main()
