from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.skills.context_cost import skill_context_cost_payload, skill_context_cost_profile


class SkillContextCostTests(unittest.TestCase):
    def test_full_profile_moves_common_rails_out_of_skill_bodies(self) -> None:
        profile = skill_context_cost_profile("full")
        headings = {row["heading"]: row for row in profile["headings"]}

        self.assertEqual(headings.get("OMH Context Rail", {"duplicate_bytes": 0})["duplicate_bytes"], 0)
        self.assertEqual(
            headings.get("Hermes Compatibility Contract", {"duplicate_bytes": 0})["duplicate_bytes"],
            0,
        )
        # 100,000 -> 104,000: the reply rule (the user's words, the host's
        # voice, record terms stay in records, a stop offers the next action)
        # is read at the moment the reply is written, so it rides every body's
        # tail on purpose; the substitution table stays in the rail. Repeated
        # bytes measure 101,024 with it. Warranted always-loaded growth.
        # 104,000 -> 107,000: the six `jev-*` bodies each carry the lines
        # every workflow body must (the reply rule, completion checklist,
        # fallback contract, lane footer), taking repeated bytes to 104,214;
        # their shared Jev rules live once in `jev-rail.md` instead.
        # 107,000 -> 111,000: `app-debugging`, `commit-pr-authoring`,
        # `git-workflow`, and `relational-db` (#1709, #1711, #1695, #1692)
        # each join the coding_handoff lane, whose `Workflow Lane` line is
        # stamped into every lane body, and the two without a declared harness
        # carry the shared `Runtime Evidence` section verbatim, as 14 skills
        # already do. Repeated bytes measure 107,916 with the first three and
        # 108,400 with all four; ~2.4% headroom kept.
        self.assertLess(profile["repeated"]["bytes"], 111_000)

    def test_ulw_context_reports_bounded_static_body_and_progressive_references(self) -> None:
        payload = skill_context_cost_payload()

        self.assertEqual(payload["schema_version"], "omh_skill_context_cost/v1")
        context = payload["catalog_increment"]["ulw-context"]
        self.assertGreater(context["skill_body_bytes"], 0)
        self.assertGreater(context["reference_bytes"], 0)
        self.assertEqual(context["reference_file_count"], 2)
        self.assertEqual(context["project_specific_bytes"], 0)
        self.assertTrue(context["ceilings_pass"])
        self.assertLessEqual(context["skill_body_bytes"], context["ceilings"]["skill_body_bytes"])
        self.assertLessEqual(context["reference_bytes"], context["ceilings"]["reference_bytes"])

        serialized = str(payload).casefold()
        self.assertNotIn("dispatch packet", serialized)
        self.assertNotIn("핸드오프", serialized)


if __name__ == "__main__":
    unittest.main()
