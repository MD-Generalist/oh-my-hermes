"""Gate: a command's exit code never reports success over failed work.

On 2026-09-11 two real dispatches ended with "You've hit your session limit",
every unit failed, and `omh coding fanout dispatch` exited **0**. A wrapper
reading only the status was told the batch succeeded, the plan's checklist
stayed marked running, and the operator learned otherwise only by opening the
report. `_fanout_dispatch_exit_code` had a case for a refusal -- with a
docstring saying in as many words that "a shell that only checks the status
must not read 'nothing was dispatched' as success" -- and no case for a unit
that ran and failed. The sentence was right and it had only been applied to
half the ways work does not happen.

This module re-derives every exit-code mapper from source rather than naming
them, so the class cannot come back through a function nobody added here. It
is deliberately narrow: it does not say what a mapper must return for any
particular input, only that a summary carrying a failure signal must not map
to 0. A new command is free to define its own vocabulary, its own recoverable
lane and its own codes; it is not free to call failure success.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any, Callable

from _local_package import load_local_package

load_local_package()

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_ROOT = REPO_ROOT / "src" / "commands"

# A mapper is a module-level function whose name ends `_exit_code` and which
# takes exactly one positional argument: the result it is grading. The suffix
# is the convention in use; a mapper spelled differently should be renamed
# into it rather than exempted, so the next reader finds it here.
MAPPER_SUFFIX = "_exit_code"

# Summaries that mean "the work did not happen", each with the shape a real
# caller would see. Every mapper must refuse to call any of these a success.
FAILURE_SUMMARIES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("one unit failed", {"units": [{"unit_id": "a", "failure_kind": "crash"}]}),
    (
        "every unit failed",
        {
            "units": [
                {"unit_id": "a", "failure_kind": "limit_shaped"},
                {"unit_id": "b", "failure_kind": "crash"},
            ]
        },
    ),
    ("the batch was refused", {"refused": True, "refusal_reason": "spawn guard"}),
    ("the batch was cut short", {"interrupted": True}),
)

# The one shape that must still map to 0, so a mapper cannot pass this gate by
# refusing everything.
SUCCESS_SUMMARY: dict[str, Any] = {"units": [{"unit_id": "a"}, {"unit_id": "b"}]}


def _discovered_mappers() -> list[tuple[str, str]]:
    """`(module stem, function name)` for every exit-code mapper under src/commands."""
    found: list[tuple[str, str]] = []
    for path in sorted(COMMANDS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.endswith(MAPPER_SUFFIX):
                continue
            positional = len(node.args.args) + len(node.args.posonlyargs)
            if positional != 1:
                continue
            found.append((path.stem, node.name))
    return found


def _load(module_stem: str, function_name: str) -> Callable[[Any], int]:
    module = __import__(f"omh.commands.{module_stem}", fromlist=[function_name])
    return getattr(module, function_name)


class ExitCodeTruthfulnessPolicyTests(unittest.TestCase):
    def test_at_least_one_mapper_is_discovered(self) -> None:
        # A discovery gate that silently finds nothing is not a gate. If the
        # naming convention moves, this fails first and says so.
        self.assertTrue(
            _discovered_mappers(),
            f"no *{MAPPER_SUFFIX} function found under {COMMANDS_ROOT}; the convention moved",
        )

    def test_no_mapper_reports_success_over_failed_work(self) -> None:
        for module_stem, function_name in _discovered_mappers():
            mapper = _load(module_stem, function_name)
            for label, summary in FAILURE_SUMMARIES:
                with self.subTest(mapper=f"{module_stem}.{function_name}", case=label):
                    self.assertNotEqual(
                        mapper(summary),
                        0,
                        f"{module_stem}.{function_name} returned 0 when {label}; "
                        "a caller reading only the exit status would be told the work succeeded",
                    )

    def test_a_capability_gate_refusal_is_never_reported_as_success(self) -> None:
        """A unit the frozen capability evidence refused is re-derived, not restated.

        The row is built by the dispatcher's own refusal builder for every
        verdict the media gate can return plus the snapshot refusal, so a
        verdict added to the gate, or a builder that drops the failure
        signal, fails here rather than in a wrapper's shell.
        """
        from omh.coding.fanout_dispatch import _capability_refusal_entry, _capability_refusal_status
        from omh.coding.media_handoff_capabilities import FAIL_CLOSED_VERDICTS

        refusals = [f"{verdict}: the gate refused" for verdict in FAIL_CLOSED_VERDICTS]
        refusals.append("executor_capability_snapshot is required by this handoff")
        unit = {"unit_id": "media", "run_ref": "fanout-media"}
        for refusal in refusals:
            entry = _capability_refusal_entry(unit, "codex", [refusal])
            status = _capability_refusal_status([refusal])
            self.assertEqual(entry["status"], status)
            self.assertTrue(entry.get("failure_kind"), f"refusal row for {status} carries no failure signal")
            summary = {"units": [entry]}
            for module_stem, function_name in _discovered_mappers():
                with self.subTest(mapper=f"{module_stem}.{function_name}", status=status):
                    mapper = _load(module_stem, function_name)
                    self.assertNotEqual(
                        mapper(summary),
                        0,
                        f"{module_stem}.{function_name} reported success over a unit the {status} gate refused",
                    )

    def test_a_unit_skipped_while_another_dispatch_holds_it_is_never_success(self) -> None:
        """Every dispatch skip status is classified, and a deferred one is not 0.

        Re-derived from the dispatcher's own skip vocabulary, so a skip status
        added there fails here until someone says whether it means the work is
        done or merely not done yet. A deferred skip is busy, not failed: it
        must not read as success, and it must not read as the failure code
        either, or a wrapper cannot tell "retry later" from "fix something".
        Only the dispatch mapper grades these rows; other mappers keep their
        own vocabulary, as this module's docstring promises.
        """
        from omh.coding.fanout_dispatch import _DISPATCH_SKIP_STATUSES, _skipped
        from omh.coding.fanout_repair import REPAIR_IN_FLIGHT_STATUS
        from omh.commands.coding import FANOUT_DISPATCH_BUSY_EXIT_CODE, _fanout_dispatch_exit_code

        done_or_not_asked = {"already_completed", "not_selected"}
        deferred = {REPAIR_IN_FLIGHT_STATUS}
        self.assertEqual(
            set(_DISPATCH_SKIP_STATUSES),
            done_or_not_asked | deferred,
            "a dispatch skip status is unclassified; decide whether it is done or deferred",
        )
        unit = {"unit_id": "core", "run_ref": "run-core", "owner": "codex"}
        for status in sorted(_DISPATCH_SKIP_STATUSES):
            summary = {"units": [_skipped(unit, status)]}
            with self.subTest(status=status):
                if status in deferred:
                    self.assertEqual(_fanout_dispatch_exit_code(summary), FANOUT_DISPATCH_BUSY_EXIT_CODE)
                    self.assertNotIn(FANOUT_DISPATCH_BUSY_EXIT_CODE, {0, 1})
                else:
                    self.assertEqual(_fanout_dispatch_exit_code(summary), 0)

    def test_a_clean_summary_still_maps_to_zero(self) -> None:
        for module_stem, function_name in _discovered_mappers():
            mapper = _load(module_stem, function_name)
            with self.subTest(mapper=f"{module_stem}.{function_name}"):
                self.assertEqual(
                    mapper(SUCCESS_SUMMARY),
                    0,
                    f"{module_stem}.{function_name} refused a summary with no failure signal; "
                    "this gate must not be satisfiable by failing everything",
                )


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
