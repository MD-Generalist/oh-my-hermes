"""Contracts for `src/plugin_bundle/omh/turn_budget.py`, the per-turn slot table.

Each per-turn limit moved there from `src/maintenance/release.py` with its
history. These pin that the move kept every value, that the bundle table and
the drift ledger cannot disagree, and that the hooks' primer is the text the
slot measures.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()
from omh.maintenance import per_turn_context, release
from omh.maintenance.drift import budget_metrics, limits
from omh.plugin_bundle.omh import turn_budget
from omh.plugin_bundle.omh.awareness import awareness_primer_context, awareness_primer_markdown

REPO_ROOT = Path(__file__).resolve().parents[1]
TURN_BUDGET_SOURCE = REPO_ROOT / "src" / "plugin_bundle" / "omh" / "turn_budget.py"
RELEASE_SOURCE = REPO_ROOT / "src" / "maintenance" / "release.py"

# Slot -> (constant name, the drift ledger entry that measures it).
SLOTS = {
    "primer": ("AWARENESS_PRIMER_CONTEXT_CHAR_LIMIT", "awareness_primer_context_chars"),
    "primer_markdown": ("AWARENESS_PRIMER_MARKDOWN_CHAR_LIMIT", "awareness_primer_markdown_chars"),
    "workflow_context": ("AWARENESS_WORKFLOW_CONTEXT_CHAR_LIMIT", "awareness_workflow_context_chars_max"),
    "role_context": ("ROLE_CONTEXT_CHAR_LIMIT", "role_context_chars_max"),
    "pre_llm_call": ("PRE_LLM_CALL_CONTEXT_CHAR_LIMIT", "pre_llm_call_context_chars_max"),
    "pre_llm_call_fallback": ("PRE_LLM_CALL_CONTEXT_FALLBACK_CHAR_LIMIT", "pre_llm_call_context_fallback_chars_max"),
}

# Every `# A -> B:` history entry the moved limits carried in release.py
# before the move: three for the primer pair, four for each pre_llm_call limit.
MOVED_HISTORY = (
    "900 -> 1050",
    "1050 -> 1260",
    "1260 -> 1440",
    "6260 -> 5214",
    "5214 -> 5544",
    "5544 -> 5647",
    "5647 -> 5662",
    "6260 -> 6799",
    "6799 -> 6902",
    "6902 -> 6917",
    "6917 -> 7098",
)
_HISTORY_LINE = re.compile(r"^# (\d+ -> \d+)\b", re.MULTILINE)


class TurnBudgetTests(unittest.TestCase):
    def test_the_slot_table_names_every_slot(self) -> None:
        self.assertEqual(set(turn_budget.SLOT_LIMITS), set(SLOTS))
        self.assertEqual(set(per_turn_context._SLOT_METRICS), set(SLOTS))

    def test_each_slot_limit_is_the_ledger_value(self) -> None:
        ledger = limits()
        for slot, (constant, metric) in SLOTS.items():
            with self.subTest(slot=slot):
                self.assertEqual(turn_budget.SLOT_LIMITS[slot], ledger[metric].value)
                self.assertEqual(getattr(turn_budget, constant), ledger[metric].value)
                # The old import path still reads the same constant.
                self.assertEqual(getattr(release, constant), getattr(turn_budget, constant))

    def test_the_ledger_points_at_the_new_home(self) -> None:
        sites = {metric.name: metric.limit_site for metric in budget_metrics()}
        for slot, (_constant, metric) in SLOTS.items():
            with self.subTest(slot=slot):
                self.assertEqual(sites[metric], "src/plugin_bundle/omh/turn_budget.py")

    def test_headroom_is_limit_minus_live(self) -> None:
        report = per_turn_context.budget_report()
        ledger = limits()
        live = {metric.name: metric for metric in budget_metrics()}
        for slot, (_constant, metric) in SLOTS.items():
            with self.subTest(slot=slot):
                row = report[slot]
                self.assertEqual(row["limit"], turn_budget.SLOT_LIMITS[slot])
                self.assertEqual(row["live"], live[metric].live())
                self.assertEqual(row["headroom"], row["limit"] - row["live"])
                self.assertEqual(row["kind"], ledger[metric].kind)
                self.assertEqual(per_turn_context.headroom(slot), row["headroom"])

    def test_render_is_the_awareness_text(self) -> None:
        self.assertEqual(turn_budget.render("primer"), awareness_primer_context())
        self.assertEqual(turn_budget.render("primer_markdown"), awareness_primer_markdown())

    def test_render_refuses_a_slot_without_fixed_text(self) -> None:
        with self.assertRaises(KeyError):
            turn_budget.render("pre_llm_call")

    def test_the_moved_history_moved_whole(self) -> None:
        moved = _HISTORY_LINE.findall(TURN_BUDGET_SOURCE.read_text(encoding="utf-8"))
        remaining = set(_HISTORY_LINE.findall(RELEASE_SOURCE.read_text(encoding="utf-8")))
        self.assertEqual(tuple(moved), MOVED_HISTORY)
        self.assertEqual(remaining & set(MOVED_HISTORY), set())


if __name__ == "__main__":
    unittest.main()
