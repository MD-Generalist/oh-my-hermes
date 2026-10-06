"""The code-review reference files carry clauses that prevent a wrong answer.

These are not style assertions. Each locked string below is the one sentence in
its section that stops a specific failure: a review that covered a fraction of
the change, a fix that was attempted rather than made, or a partial
implementation of a finding set that was not understood. A rewrite that drops
one of them should fail here rather than ship quietly.
"""

from __future__ import annotations

import unittest

from omh.skills.packaging import builtin_skill_reference_templates
from omh.skills.render import code_review_reference_templates


class CodeReviewReferenceTests(unittest.TestCase):
    def _content(self, relative_path: str) -> str:
        for template in code_review_reference_templates():
            if template.relative_path == relative_path:
                return template.content
        self.fail(f"no code-review reference at {relative_path}")

    def test_both_references_ship_under_the_code_review_skill(self) -> None:
        paths = {template.relative_path for template in code_review_reference_templates()}
        self.assertEqual(
            paths,
            {
                "references/review-dispatch.md",
                "references/review-response.md",
                "references/smell-baseline.md",
            },
        )
        for template in code_review_reference_templates():
            self.assertEqual(template.skill_name, "code-review")

    def test_the_packaged_set_includes_them(self) -> None:
        # skill_record.REFERENCE_PRODUCERS lists producers by hand; a new one is a one-line
        # edit and an omitted one is silent until the byte gate runs.
        packaged = {(t.skill_name, t.relative_path) for t in builtin_skill_reference_templates()}
        self.assertIn(("code-review", "references/review-dispatch.md"), packaged)
        self.assertIn(("code-review", "references/review-response.md"), packaged)
        self.assertIn(("code-review", "references/smell-baseline.md"), packaged)

    def test_the_base_sha_rule_states_the_defect_not_just_the_preference(self) -> None:
        content = self._content("references/review-dispatch.md")
        # Handing a reviewer HEAD~1 on a multi-commit task yields a clean
        # verdict on code nobody read - a manufactured pass.
        self.assertIn("Never `HEAD~1`", content)
        self.assertIn("BASE_SHA", content)
        self.assertIn("HEAD_SHA", content)

    def test_the_four_implementer_statuses_are_all_present(self) -> None:
        content = self._content("references/review-dispatch.md")
        for status in ("DONE", "DONE_WITH_CONCERNS", "NEEDS_CONTEXT", "BLOCKED"):
            with self.subTest(status=status):
                self.assertIn(status, content)

    def test_attempted_is_not_addressed_survives(self) -> None:
        self.assertIn('"Attempted" is not "addressed."', self._content("references/review-dispatch.md"))

    def test_the_smell_baseline_names_all_twelve_and_stays_a_judgement_call(self) -> None:
        content = self._content("references/smell-baseline.md")
        for smell in (
            "Mysterious name",
            "Duplicated code",
            "Feature envy",
            "Data clumps",
            "Primitive obsession",
            "Repeated switches",
            "Shotgun surgery",
            "Divergent change",
            "Speculative generality",
            "Message chains",
            "Middle man",
            "Refused bequest",
        ):
            with self.subTest(smell=smell):
                self.assertIn(smell, content)
        # The two rules that keep the baseline from becoming a linter: repo
        # standards override it, and a smell is argued, never auto-reported.
        self.assertIn("override this baseline", content)
        self.assertIn("never an automatic finding", content)

    def test_the_spec_axis_is_locked_into_the_skill_body(self) -> None:
        from omh.skills.catalog import builtin_definitions

        definition = next(d for d in builtin_definitions() if d.name == "code-review")
        joined = "\n".join(definition.quality_bar)
        self.assertIn("never re-ranked against each other", joined)
        self.assertIn("`not_assessed`", joined)
        checklist = "\n".join(definition.final_checklist)
        self.assertIn("spec-axis verdict", checklist)
        self.assertIn("could-not-assess list", checklist)

    def test_the_clarification_gate_is_all_or_nothing(self) -> None:
        content = self._content("references/review-response.md")
        self.assertIn("before implementing any of them", content)

    def test_both_references_end_at_a_boundary(self) -> None:
        for relative_path in (
            "references/review-dispatch.md",
            "references/review-response.md",
            "references/smell-baseline.md",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertIn("## Boundary", self._content(relative_path))


if __name__ == "__main__":
    unittest.main()
