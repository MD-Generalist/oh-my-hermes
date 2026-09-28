from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.routing.policy import everyday_sense_phrase_unanchored
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "data-pipelines"
REFERENCE_PATH = "references/pipeline-method.md"


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


class DataPipelinesCatalogTests(unittest.TestCase):
    """#1565: idempotency and replay are outputs of their own, not advice appended to a plan."""

    def test_idempotency_and_replay_are_first_class_outputs(self) -> None:
        mine = _definition(SKILL)
        self.assertIn("idempotency_contract/v1", mine.expected_outputs)
        self.assertIn("replay_backfill_plan/v1", mine.expected_outputs)
        for schema in mine.expected_outputs:
            with self.subTest(schema=schema):
                self.assertEqual(len([line for line in mine.artifact_expectations if line.startswith(schema)]), 1)
        replay = next(line for line in mine.artifact_expectations if line.startswith("replay_backfill_plan/v1"))
        self.assertIn("writes through the idempotency contract", replay)

    def test_no_replay_without_an_idempotency_contract(self) -> None:
        mine = _definition(SKILL)
        self.assertTrue(mine.safety_rules[0].startswith("Never plan a replay or backfill without an idempotency contract"))
        self.assertTrue(mine.safety_rules[1].startswith("Bound every replay and backfill by window and target"))
        self.assertIn("Every replay or backfill is bounded by window and target.", mine.final_checklist)

    def test_the_reference_gives_every_idempotency_pattern_its_double_write(self) -> None:
        table = _reference().split("## 1. Idempotency", 1)[1].split("## 2.", 1)[0]
        rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Pattern") and "---" not in line]
        self.assertEqual(len(rows), 4)
        for row in rows:
            with self.subTest(row=row.split("|")[1]):
                self.assertTrue(all(cell.strip() for cell in row.strip("|").split("|")))
        self.assertIn("Rerun one partition and confirm nothing changes", _reference())
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)

    def test_backend_names_the_other_side(self) -> None:
        backend = [text for text in _definition("backend").do_not_use_when if f"`{SKILL}`" in text]
        self.assertEqual(len(backend), 1)
        mine = _definition(SKILL).do_not_use_when
        for sibling in ("backend", "data-analysis", "relational-db", "memory-sync"):
            with self.subTest(sibling=sibling):
                self.assertEqual(len([text for text in mine if f"`{sibling}`" in text]), 1)


class DataPipelinesRoutingTests(unittest.TestCase):
    def test_the_issue_row_and_the_pipeline_asks_dispatch_here(self) -> None:
        for message in (
            "our airflow etl backfill is producing duplicate events",
            "replay the last three days of kafka events into the warehouse without double counting",
            "the dbt model changed its schema, what breaks downstream",
            "make this spark job idempotent so a rerun does not duplicate rows",
            "which dashboards depend on this dbt model",
            "we need a data backfill for last month",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_the_same_words_elsewhere_stay_away(self) -> None:
        for message in (
            "my family lineage goes back to scotland",
            "we need to backfill the open position on the team",
            "I have duplicate events in my calendar",
            "sync my memory with the latest notes",
            "the pipeline for new sales leads is empty this quarter",
            "what is etl",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)

    def test_everyday_phrases_need_a_word_of_data_engineering(self) -> None:
        for message, withdrawn in (
            ("we need to backfill the open position on the team", True),
            ("my family lineage goes back to scotland", True),
            ("backfill the events table for march", False),
            ("we need a data backfill for last month", False),
            ("show me the lineage of the orders table", False),
        ):
            with self.subTest(message=message):
                self.assertIs(everyday_sense_phrase_unanchored(SKILL, normalized_phrase(message)), withdrawn)


if __name__ == "__main__":
    unittest.main()
