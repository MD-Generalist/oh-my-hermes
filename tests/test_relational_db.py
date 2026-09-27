from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.awareness import awareness_route_hint
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "relational-db"
BACKEND = "backend"
REFERENCE_PATH = "references/engine-lock-tables.md"


def _definition(name: str):
    return next(definition for definition in builtin_definitions() if definition.name == name)


def _route(message: str) -> dict[str, object]:
    return build_chat_interaction_payload(message, source="discord")["route"]


class RelationalDbCatalogTests(unittest.TestCase):
    def test_a_migration_is_not_ready_without_lock_behaviour_and_rollback(self) -> None:
        """#1692: readiness is a verdict over every step, not a summary."""

        mine = _definition(SKILL)
        rule = mine.safety_rules[0]
        self.assertTrue(rule.startswith("A migration plan cannot be ready while any step lacks a stated lock behaviour or a rollback"))
        self.assertIn("migration_readiness_verdict/v1", mine.expected_outputs)
        verdict = next(line for line in mine.artifact_expectations if line.startswith("migration_readiness_verdict/v1"))
        self.assertIn("lock behaviour", verdict)
        self.assertIn("rollback", verdict)
        plan = next(line for line in mine.artifact_expectations if line.startswith("online_migration_plan/v1"))
        for field in ("lock mode", "`lock_timeout`", "rollback"):
            with self.subTest(field=field):
                self.assertIn(field, plan)

    def test_backend_keeps_only_the_schema_migration_plan(self) -> None:
        """The boundary from both sides: backend owns `schema_migration_plan/v1`, this skill the database work."""

        mine = _definition(SKILL)
        backend = _definition(BACKEND)
        self.assertNotIn("schema_migration_plan/v1", " ".join(mine.expected_outputs))
        self.assertTrue(any(output.startswith("schema_migration_plan/v1") for output in backend.expected_outputs))
        to_backend = [text for text in mine.do_not_use_when if f"`{BACKEND}`" in text]
        self.assertEqual(len(to_backend), 1)
        self.assertIn("owns schema_migration_plan/v1", to_backend[0])
        back = [text for text in backend.do_not_use_when if f"`{SKILL}`" in text]
        self.assertEqual(len(back), 1)

    def test_the_per_engine_lock_tables_live_in_the_reference(self) -> None:
        reference = next(
            template.content
            for template in builtin_skill_reference_templates()
            if template.skill_name == SKILL and template.relative_path == REFERENCE_PATH
        )
        for heading in ("PostgreSQL lock modes by statement", "MySQL (InnoDB) online DDL", "Index selection and sizing"):
            with self.subTest(heading=heading):
                self.assertIn(heading, reference)
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)
        self.assertNotIn("PostgreSQL lock modes by statement", body)


class RelationalDbRoutingTests(unittest.TestCase):
    def test_the_issue_rows_dispatch_here(self) -> None:
        for message in (
            "write an online migration for a 200M row table",
            "this query seq-scans 40M rows, what index",
            "N+1 in the orders endpoint",
            "when do we need to shard",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_the_settled_lock_report_is_hinted_but_not_dispatched(self) -> None:
        """A report with no ask stays a direct answer (narration guard); live Hermes still gets the hint."""

        message = "ALTER TABLE took a lock during deploy"
        self.assertEqual(awareness_route_hint(message).get("selected_workflow"), SKILL)
        self.assertEqual(
            _route("ALTER TABLE took a lock during deploy, help me make the migration lock-safe")["selected_skill"], SKILL
        )

    def test_the_same_words_outside_a_database_stay_away(self) -> None:
        for message in (
            "what index fund should I buy",
            "the table in the kitchen needs a new partition",
            "alter the table setting for the dinner party",
            "the lock on the front door is broken",
            "the bird migration season starts in october",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)

    def test_backend_keeps_the_service_contract(self) -> None:
        self.assertEqual(_route("design a rest api with postgres schema and migrations")["selected_skill"], BACKEND)


if __name__ == "__main__":
    unittest.main()
