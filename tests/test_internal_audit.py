from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.routing.policy import everyday_sense_phrase_unanchored
from omh.skills.catalog import builtin_definitions
from omh.skills.packaging import builtin_skill_reference_templates, builtin_skill_templates
from omh.wrapper.contract import build_chat_interaction_payload

SKILL = "internal-audit"
REFERENCE_PATH = "references/control-audit-method.md"


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


class InternalAuditCatalogTests(unittest.TestCase):
    """#1569: sample, evidence, re-performance and a severity grade derived from stated criteria."""

    def test_sample_evidence_reperformance_and_severity_are_named_outputs(self) -> None:
        mine = _definition(SKILL)
        for schema in ("sample_design/v1", "evidence_request/v1", "reperformance_record/v1", "deficiency_severity_grade/v1"):
            with self.subTest(schema=schema):
                self.assertIn(schema, mine.expected_outputs)
        for schema in mine.expected_outputs:
            with self.subTest(expectation=schema):
                self.assertEqual(len([line for line in mine.artifact_expectations if line.startswith(schema)]), 1)

    def test_the_grade_is_derived_from_stated_criteria_or_withheld(self) -> None:
        mine = _definition(SKILL)
        grade = next(line for line in mine.artifact_expectations if line.startswith("deficiency_severity_grade/v1"))
        for criterion in ("likelihood", "magnitude against stated materiality", "compensating controls"):
            with self.subTest(criterion=criterion):
                self.assertIn(criterion, grade)
        self.assertIn("withholds the grade when a criterion is missing", grade)
        self.assertTrue(mine.safety_rules[0].startswith("Derive every severity grade from stated criteria"))

    def test_the_reference_severity_table_has_every_grade_and_its_three_inputs(self) -> None:
        table = _reference().split("## 4. Severity", 1)[1]
        rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Reasonable") and "---" not in line]
        grades = {row.strip("|").split("|")[-1].strip() for row in rows}
        self.assertTrue({"material weakness", "significant deficiency", "control deficiency"} <= grades)
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(len(row.strip("|").split("|")), 4)
                self.assertTrue(all(cell.strip() for cell in row.strip("|").split("|")))
        self.assertIn("withhold the grade", table)
        body = next(template.content for template in builtin_skill_templates() if template.name == SKILL)
        self.assertIn(REFERENCE_PATH, body)

    def test_finance_analysis_names_the_other_side(self) -> None:
        sibling = [text for text in _definition("finance-analysis").do_not_use_when if f"`{SKILL}`" in text]
        self.assertEqual(len(sibling), 1)
        mine = _definition(SKILL).do_not_use_when
        for other in ("finance-analysis", "legal-compliance-review", "production-audit", "tech-debt-audit"):
            with self.subTest(sibling=other):
                self.assertEqual(len([text for text in mine if f"`{other}`" in text]), 1)


class InternalAuditRoutingTests(unittest.TestCase):
    def test_the_control_testing_asks_dispatch_here(self) -> None:
        for message in INTERVENTIONS:
            with self.subTest(message=message):
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], SKILL)

    def test_the_same_words_elsewhere_stay_away(self) -> None:
        for message in (
            "the red sox won last night",
            "i have no internal control over my snacking",
            "quality control testing on the assembly line found two defects",
            "the material weakness in this bridge design is the cable anchor",
            "this is a significant improvement in performance",
            "the test showed a weakness in my left knee",
        ):
            with self.subTest(message=message):
                route = _route(message)
                self.assertNotEqual(route["action"], "dispatch")
                self.assertNotEqual(route.get("candidate_skill"), SKILL)

    def test_other_audits_keep_their_owners(self) -> None:
        for message, owner in OWNERS:
            with self.subTest(message=message):
                self.assertEqual(_route(message)["selected_skill"], owner)

    def test_everyday_phrases_need_a_word_of_auditing(self) -> None:
        for message, withdrawn in (
            ("the red sox won last night", True),
            ("i have no internal control over my snacking", True),
            ("the material weakness in this bridge design is the cable anchor", True),
            ("we need to test the quarterly access review control for sox and grade what we find", False),
            ("the auditors found a material weakness in revenue recognition", False),
        ):
            with self.subTest(message=message):
                self.assertIs(everyday_sense_phrase_unanchored(SKILL, normalized_phrase(message)), withdrawn)


INTERVENTIONS = (
    "we need to do sox testing on the quarterly access review control and grade what we find",
    "how many samples do we need to test a daily control for icfr",
    "is this a significant deficiency or a material weakness",
    "reperform the bank reconciliation control for march",
    "the auditors want evidence the itgc change management control operated all year",
    "the same clerk sets up suppliers and approves their payments, is that a segregation of duties deficiency",
)
OWNERS = (
    ("what was our budget variance last month", "finance-analysis"),
    ("run a production readiness audit before launch", "production-audit"),
    ("audit my codebase for tech debt", "tech-debt-audit"),
)


if __name__ == "__main__":
    unittest.main()
