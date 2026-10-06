"""Gate: a run-record file name or schema version is spelled once, in `run_records`.

The plugin bundle READS the run, dispatch, and receipt files the control plane
WRITES. The bundle cannot import `omh.*`, so for a long time each side spelled
the format itself and five parity tests compared the copies after the fact --
which is how `omo_runtime` bindings were rejected at the read boundary long
after the lane accepted them. `src/plugin_bundle/omh/run_records.py` now owns
the format and the writers import it.

This module re-derives the owned names from `run_records` itself and walks
every module under `src/` with `ast`, so a second spelling fails here the day
it is written instead of the day the two drift apart.

Scope: it matches string constants (`ast.Constant`) equal to an owned value,
including implicit concatenation. A spelling assembled at runtime --
`"events" + ".jsonl"`, an f-string, a decoded bytes literal -- is not seen,
and the vocabularies (`EXECUTOR_PROGRESS_PROFILES`, `OBSERVED_RESULTS`, ...)
are not gated: a hand copy of a tuple is caught only when a reader imports
the one definition, which every reader now does. This is a lint against the
plain second spelling, not a proof that none exists.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import run_records

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"
OWNER = "src/plugin_bundle/omh/run_records.py"
# The one module that spells every schema id on purpose: `reply_lint` is the
# registry of words a reply must not leak, so a run-record schema version
# belongs there as a word, not as a format. Allowlisted whole.
ALLOWED_PATHS = frozenset({"src/quality/reply_lint.py"})
# One (module, value) pair, not a module: `memory_evaluation` writes the memory
# store's own `journal/events.jsonl`, a basename it shares with the run journal
# and nothing else. Importing the run-record name there would couple the
# memory store to a format it never reads or writes; exempting the whole file
# would let a real run-record literal added there later pass unseen.
ALLOWED_SPELLINGS = frozenset({("src/workflows/memory_evaluation.py", "events.jsonl")})
_OWNED_SUFFIXES = ("_FILE", "_STORE_NAME", "_SCHEMA_VERSION")


def _owned_literals() -> dict[str, str]:
    """value -> constant name, for every file name and schema version `run_records` owns."""
    owned: dict[str, str] = {}
    for name, value in vars(run_records).items():
        if name.isupper() and name.endswith(_OWNED_SUFFIXES) and isinstance(value, str):
            owned[value] = name
    return owned


def _second_spellings(source: str, relative: str, owned: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in owned:
            if (relative, node.value) in ALLOWED_SPELLINGS:
                continue
            findings.append(
                f"{relative}:{node.lineno}: {node.value!r} -- import "
                f"`{owned[node.value]}` from `omh.plugin_bundle.omh.run_records` "
                f"(in the bundle: `from .run_records import {owned[node.value]}`)"
            )
    return findings


class RunRecordFormatPolicyTests(unittest.TestCase):
    def test_owned_names_are_derived_not_listed(self) -> None:
        owned = _owned_literals()
        # Not vacuous: sixteen file names and eleven schema versions moved
        # into `run_records`. Fewer means the derivation broke.
        self.assertGreaterEqual(len(owned), 27, sorted(owned))
        self.assertEqual(owned["run.json"], "RUN_FILE")
        self.assertEqual(owned["omh_inflight_marker/v1"], "INFLIGHT_MARKER_SCHEMA_VERSION")

    def test_no_module_outside_run_records_spells_a_run_record_name(self) -> None:
        owned = _owned_literals()
        findings: list[str] = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative == OWNER or relative in ALLOWED_PATHS:
                continue
            findings.extend(_second_spellings(path.read_text(encoding="utf-8"), relative, owned))
        self.assertEqual(
            findings,
            [],
            "a run-record file name or schema version is spelled outside run_records; "
            "a second spelling is a second definition:\n" + "\n".join(findings),
        )

    def test_allowlist_names_files_that_exist(self) -> None:
        for relative in sorted(ALLOWED_PATHS | {OWNER} | {path for path, _ in ALLOWED_SPELLINGS}):
            with self.subTest(path=relative):
                self.assertTrue((REPO_ROOT / relative).is_file(), relative)

    def test_a_pair_exemption_covers_one_value_in_one_module_only(self) -> None:
        # The memory store may spell its own `events.jsonl`; a run-record name
        # added to the same module later is still a second spelling.
        owned = _owned_literals()
        module, value = next(iter(sorted(ALLOWED_SPELLINGS)))
        self.assertEqual(_second_spellings(f"p = d / {value!r}\n", module, owned), [])
        self.assertEqual(len(_second_spellings("p = d / 'run.json'\n", module, owned)), 1)
        self.assertEqual(len(_second_spellings(f"p = d / {value!r}\n", "src/runtime/other.py", owned)), 1)

    def test_scan_flags_a_literal_and_names_the_constant(self) -> None:
        # The derivation itself is a contract: a bare literal is found with its
        # line and the constant to import, and a reference to the name is not.
        source = (
            "from .run_records import RUN_FILE\n"
            "a = run_dir / RUN_FILE\n"
            "b = run_dir / 'run.json'\n"
            "c = {'schema_version': \"omh_progress_event/v1\"}\n"
            "d = 'a sentence that mentions run.json is prose'\n"
        )
        findings = _second_spellings(source, "src/example.py", _owned_literals())
        self.assertEqual(len(findings), 2, findings)
        self.assertTrue(findings[0].startswith("src/example.py:3: 'run.json' -- import `RUN_FILE`"), findings)
        self.assertIn("EXECUTOR_PROGRESS_EVENT_SCHEMA_VERSION", findings[1])


if __name__ == "__main__":
    unittest.main()
