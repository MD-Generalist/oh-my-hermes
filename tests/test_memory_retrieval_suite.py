"""Retrieval-quality regression suite for project memory (issue #1427).

These tests hold three lines:

- the suite is a gate, not a snapshot: a seeded ranking, lens, freshness,
  archive, or budget regression has to make it fail, and each has to fail under
  its own name rather than as one undifferentiated count;
- the report is deterministic and identity-bound: the same inputs produce the
  same bytes, and two reports built from different fixtures, configuration,
  clock, or revision refuse to be compared at all;
- the corpus stays offline and self-contained: no clock read, no network or
  model import, and no write outside each case's own temporary store.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.workflows.memory_evaluation import (
    RETRIEVAL_EVALUATION_SCHEMA_VERSION,
    RETRIEVAL_IDENTITY_FIELDS,
    compare_retrieval_reports,
    retrieval_report_identity,
    run_memory_evaluation,
    run_memory_retrieval_evaluation,
)
from omh.workflows.memory_retrieval_fixtures import (
    CONTAMINATION_CLASSES,
    RETRIEVAL_CASES,
    fixture_digest,
    materialize_fixture_record,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "workflows"


def _case(case_id: str) -> dict:
    return next(case for case in RETRIEVAL_CASES if case["case_id"] == case_id)


def _serialize(report: dict) -> str:
    return json.dumps(report, sort_keys=True, separators=(",", ":"))


def _case_result(report: dict, case_id: str) -> dict:
    return next(case for case in report["cases"] if case["case_id"] == case_id)


def _leak_builder(record_id: str, source_case_id: str):
    """A recall builder that leaks one fixture record back into its pack.

    The leaked entry is built from the fixture record itself, so the seeded
    fault is exactly "this record reached the pack", which is what a broken
    lens, a broken expiry check, or a broken tier gate would produce.
    """
    from omh.memory import build_project_memory_recall_pack

    spec = next(item for item in _case(source_case_id)["records"] if item["record_id"] == record_id)
    record, _review = materialize_fixture_record(spec)

    def _builder(paths, query="", **kwargs):
        pack = build_project_memory_recall_pack(paths, query, **kwargs)
        leaked = {
            "record_id": record_id,
            "record_type": record["record_type"],
            "summary": record["summary"],
            "scope": record["scope"],
            "tags": record["tags"],
            "source": record["source"],
            "approved_at": record["approved_at"],
            "staleness": {"state": "not_checked"},
            "score": 0,
            "attention_tier": "active",
            "derived_from": [],
            "perspective": {},
            "eligibility_reason": "eligible",
            "replay_evaluation": {"eligible": True, "reason_code": "eligible"},
        }
        included = [item for item in pack["included_records"] if str(item["record_id"]) != record_id]
        excluded = [item for item in pack["excluded_records"] if str(item["record_id"]) != record_id]
        return {**pack, "included_records": [*included, leaked], "excluded_records": excluded}

    return _builder


class RetrievalSuiteDeterminismTests(unittest.TestCase):
    def test_identical_inputs_produce_identical_report_bytes(self) -> None:
        first = run_memory_retrieval_evaluation(target_revision="rev-a")
        second = run_memory_retrieval_evaluation(target_revision="rev-a")

        self.assertEqual(_serialize(first), _serialize(second))
        self.assertEqual(first["schema_version"], RETRIEVAL_EVALUATION_SCHEMA_VERSION)

    def test_the_retained_corpus_passes_against_the_production_recall_builder(self) -> None:
        report = run_memory_retrieval_evaluation()

        self.assertTrue(report["passed"], report["summary"])
        self.assertEqual(report["summary"]["failed_cases"], 0)
        self.assertEqual(report["summary"]["exact_order_cases"], report["case_count"])
        self.assertEqual(report["summary"]["expected_hit_count"], report["summary"]["expected_included_count"])

    def test_a_corpus_edit_moves_the_fixture_digest(self) -> None:
        changed = copy.deepcopy(list(RETRIEVAL_CASES))
        changed[0]["records"][0]["summary"] = "a different synthetic fixture summary"

        self.assertNotEqual(fixture_digest(RETRIEVAL_CASES), fixture_digest(tuple(changed)))

    def test_the_report_is_bound_to_fixture_configuration_clock_and_revision(self) -> None:
        report = run_memory_retrieval_evaluation(target_revision="rev-a")
        identity = retrieval_report_identity(report)

        self.assertEqual(set(identity), set(RETRIEVAL_IDENTITY_FIELDS))
        self.assertEqual(identity["fixture_digest"], fixture_digest())
        self.assertEqual(identity["clock"], "2031-01-02T03:04:05Z")
        self.assertEqual(identity["target_revision"], "rev-a")
        self.assertEqual(len(str(identity["retrieval_config_digest"])), 64)


class RetrievalCorpusCoverageTests(unittest.TestCase):
    def test_case_ids_and_record_ids_are_unique_within_the_corpus(self) -> None:
        case_ids = [case["case_id"] for case in RETRIEVAL_CASES]
        self.assertEqual(len(case_ids), len(set(case_ids)))
        for case in RETRIEVAL_CASES:
            record_ids = [record["record_id"] for record in case["records"]]
            self.assertEqual(len(record_ids), len(set(record_ids)), case["case_id"])

    def test_every_fixture_record_has_exactly_one_expected_disposition(self) -> None:
        """A record with no declared disposition would score as a silent pass."""
        for case in RETRIEVAL_CASES:
            expected = case["expected"]
            buckets = {
                "included": set(expected["included_order"]),
                "excluded": set(expected["excluded_reasons"]),
                "absent": set(expected["absent"]),
            }
            for record in case["records"]:
                record_id = record["record_id"]
                declared = [name for name, ids in buckets.items() if record_id in ids]
                self.assertEqual(len(declared), 1, f"{case['case_id']}/{record_id}: {declared}")

    def test_the_corpus_covers_every_named_retrieval_interaction(self) -> None:
        required = {
            "exact_and_partial_relevance",
            "no_match_falls_back_to_the_active_tier",
            "no_match_fallback_leaves_reference_keyword_gated",
            "partial_match_never_falls_back",
            "duplicate_records_collapse_to_the_newest",
            "same_topic_disagreement_names_the_cut_sibling",
            "supersession_removes_the_corrected_revision",
            "expired_volatile_record_leaves_the_pack",
            "stale_review_is_excluded_by_default",
            "include_stale_surfaces_the_record_carrying_ineligible_evidence",
            "scope_lens_isolates_a_foreign_scope",
            "perspective_lens_isolates_another_actors_record",
            "pins_lead_without_owning_the_whole_budget",
            "attention_tier_outranks_relevance",
            "archive_tier_leaves_the_default_pack_named",
            "archive_tier_returns_on_an_explicit_archived_query",
            "age_tie_breaks_deterministically_by_record_id",
            "record_limit_truncation_reports_over_budget",
            "character_budget_truncation_is_reported_separately",
        }

        self.assertEqual(required - {case["case_id"] for case in RETRIEVAL_CASES}, set())

    def test_every_contamination_class_is_exercised_by_at_least_one_record(self) -> None:
        labelled = {
            str(record["contamination_class"])
            for case in RETRIEVAL_CASES
            for record in case["records"]
            if record["contamination_class"]
        }

        self.assertEqual(set(CONTAMINATION_CLASSES) - labelled, set())


class SeededRegressionTests(unittest.TestCase):
    def test_a_seeded_ranking_regression_breaks_order_and_fails_the_suite(self) -> None:
        from omh.memory import build_project_memory_recall_pack

        def _reversed(paths, query="", **kwargs):
            pack = build_project_memory_recall_pack(paths, query, **kwargs)
            return {**pack, "included_records": list(reversed(pack["included_records"]))}

        report = run_memory_retrieval_evaluation(pack_builder=_reversed)

        self.assertFalse(report["passed"])
        self.assertLess(report["summary"]["exact_order_cases"], report["case_count"])
        self.assertGreater(report["summary"]["failed_cases"], 0)

    def test_a_seeded_expected_hit_regression_fails_the_suite(self) -> None:
        from omh.memory import build_project_memory_recall_pack

        def _drops_the_leader(paths, query="", **kwargs):
            pack = build_project_memory_recall_pack(paths, query, **kwargs)
            return {**pack, "included_records": pack["included_records"][1:]}

        report = run_memory_retrieval_evaluation(pack_builder=_drops_the_leader)

        self.assertFalse(report["passed"])
        self.assertGreater(report["summary"]["missing_inclusion_count"], 0)
        self.assertLess(report["summary"]["expected_hit_count"], report["summary"]["expected_included_count"])

    def test_a_seeded_scope_leak_increments_the_scope_counter_and_fails(self) -> None:
        builder = _leak_builder("mem_r51_other_scope", "scope_lens_isolates_a_foreign_scope")

        report = run_memory_retrieval_evaluation(pack_builder=builder)
        case = _case_result(report, "scope_lens_isolates_a_foreign_scope")

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["foreign_scope_contamination"], 1)
        self.assertEqual(case["contamination"]["foreign_scope_contamination"], 1)
        self.assertEqual(
            [(row["record_id"], row["expected_disposition"], row["observed_disposition"]) for row in case["unexpected_inclusions"]],
            [("mem_r51_other_scope", "absent", "included")],
        )

    def test_a_seeded_perspective_leak_increments_the_perspective_counter_and_fails(self) -> None:
        builder = _leak_builder("mem_r62_theirs", "perspective_lens_isolates_another_actors_record")

        report = run_memory_retrieval_evaluation(pack_builder=builder)

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["foreign_perspective_contamination"], 1)
        self.assertEqual(report["summary"]["foreign_scope_contamination"], 0)

    def test_a_seeded_expiry_leak_increments_the_expired_counter_and_fails(self) -> None:
        builder = _leak_builder("mem_r31_expired", "expired_volatile_record_leaves_the_pack")

        report = run_memory_retrieval_evaluation(pack_builder=builder)
        case = _case_result(report, "expired_volatile_record_leaves_the_pack")

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["expired_contamination"], 1)
        self.assertEqual(
            [(row["record_id"], row["expected_disposition"]) for row in case["exclusion_reason_drift"]],
            [("mem_r31_expired", "excluded:expired_volatile")],
        )

    def test_a_seeded_archive_leak_increments_the_archive_counter_and_fails(self) -> None:
        builder = _leak_builder("mem_r91_archived", "archive_tier_leaves_the_default_pack_named")

        report = run_memory_retrieval_evaluation(pack_builder=builder)

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["archived_contamination"], 1)

    def test_a_seeded_stale_leak_increments_the_stale_counter_and_fails(self) -> None:
        builder = _leak_builder("mem_r41_stale", "stale_review_is_excluded_by_default")

        report = run_memory_retrieval_evaluation(pack_builder=builder)

        self.assertFalse(report["passed"])
        self.assertEqual(report["summary"]["stale_contamination"], 1)

    def test_a_seeded_budget_regression_is_reported_apart_from_a_relevance_miss(self) -> None:
        """A pack that keeps every expected record but overruns its budget must
        fail as a budget violation, with the relevance counters left clean."""
        from omh.memory import build_project_memory_recall_pack

        def _ignores_the_budget(paths, query="", **kwargs):
            pack = build_project_memory_recall_pack(paths, query, **{**kwargs, "limit": 99, "max_chars": None})
            return {**pack, "truncated": bool(kwargs.get("limit", 6) < len(pack["included_records"]))}

        report = run_memory_retrieval_evaluation(pack_builder=_ignores_the_budget)
        case = _case_result(report, "record_limit_truncation_reports_over_budget")

        self.assertFalse(report["passed"])
        self.assertGreater(report["summary"]["budget_violation_count"], 0)
        self.assertEqual(
            [(row["violation"], row["limit"], row["observed"]) for row in case["budget_violations"]],
            [("record_limit_exceeded", 2, 4)],
        )
        self.assertEqual(report["summary"]["missing_inclusion_count"], 0)

    def test_a_seeded_ineligible_evidence_regression_fails_the_stale_inspection_case(self) -> None:
        """--include-stale must deliver the stale record still marked ineligible."""
        from omh.memory import build_project_memory_recall_pack

        def _blesses_the_stale_record(paths, query="", **kwargs):
            pack = build_project_memory_recall_pack(paths, query, **kwargs)
            included = [
                {**item, "replay_evaluation": {**item.get("replay_evaluation", {}), "eligible": True}}
                for item in pack["included_records"]
            ]
            return {**pack, "included_records": included}

        report = run_memory_retrieval_evaluation(pack_builder=_blesses_the_stale_record)
        case = _case_result(report, "include_stale_surfaces_the_record_carrying_ineligible_evidence")

        self.assertFalse(case["passed"])
        self.assertEqual([row["record_id"] for row in case["ineligible_evidence_drift"]], ["mem_r41_stale"])

    def test_every_finding_names_the_fixture_record_and_both_dispositions(self) -> None:
        builder = _leak_builder("mem_r51_other_scope", "scope_lens_isolates_a_foreign_scope")
        report = run_memory_retrieval_evaluation(pack_builder=builder)

        findings = [
            row
            for case in report["cases"]
            for key in ("unexpected_inclusions", "missing_inclusions", "exclusion_reason_drift")
            for row in case[key]
        ]
        self.assertTrue(findings)
        for row in findings:
            self.assertEqual(
                set(row),
                {"case_id", "record_id", "expected_disposition", "observed_disposition", "reason_code", "contamination_class"},
            )
            self.assertTrue(row["case_id"] and row["record_id"])


class RetrievalReportComparisonTests(unittest.TestCase):
    def test_reports_with_different_revisions_refuse_comparison(self) -> None:
        left = run_memory_retrieval_evaluation(target_revision="rev-a")
        right = run_memory_retrieval_evaluation(target_revision="rev-b")

        comparison = compare_retrieval_reports(left, right)

        self.assertFalse(comparison["comparable"])
        self.assertEqual(comparison["mismatched_identity_fields"], ["target_revision"])

    def test_reports_with_a_different_fixture_digest_refuse_comparison(self) -> None:
        left = run_memory_retrieval_evaluation(target_revision="rev-a")
        right = {**copy.deepcopy(left), "fixture_digest": "0" * 64}

        comparison = compare_retrieval_reports(left, right)

        self.assertFalse(comparison["comparable"])
        self.assertEqual(comparison["mismatched_identity_fields"], ["fixture_digest"])

    def test_matching_identities_compare_and_name_the_regressed_case(self) -> None:
        from omh.memory import build_project_memory_recall_pack

        def _reversed(paths, query="", **kwargs):
            pack = build_project_memory_recall_pack(paths, query, **kwargs)
            return {**pack, "included_records": list(reversed(pack["included_records"]))}

        left = run_memory_retrieval_evaluation(target_revision="rev-a")
        right = run_memory_retrieval_evaluation(target_revision="rev-a", pack_builder=_reversed)

        comparison = compare_retrieval_reports(left, right)

        self.assertTrue(comparison["comparable"])
        self.assertFalse(comparison["identical_results"])
        self.assertIn("exact_and_partial_relevance", comparison["regressed_cases"])


class RetrievalExecutionBoundaryTests(unittest.TestCase):
    def test_the_fixture_corpus_reads_no_clock_and_imports_nothing_remote(self) -> None:
        source = (_SOURCE_ROOT / "memory_retrieval_fixtures.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])

        self.assertFalse(imported & {"http", "httpx", "requests", "socket", "urllib", "openai", "anthropic", "subprocess"})
        # Call sites, not prose: the module docstring names ``utc_now`` to
        # explain why the corpus does not use it, and a text scan would read
        # that explanation as the very thing it forbids.
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        self.assertFalse(
            called & {"now", "today", "utc_now", "time", "monotonic", "perf_counter", "perf_counter_ns"},
            "a fixture that reads the wall clock goes stale on its own",
        )

    def test_the_suite_writes_nothing_into_an_existing_memory_store(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / ".omh" / "memory" / "records"
            store.mkdir(parents=True)
            record, _review = materialize_fixture_record(RETRIEVAL_CASES[0]["records"][0])
            (store / "existing.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            before = hashlib.sha256(b"".join(sorted(path.read_bytes() for path in (root / ".omh").rglob("*") if path.is_file()))).hexdigest()

            report = run_memory_retrieval_evaluation()

            after = hashlib.sha256(b"".join(sorted(path.read_bytes() for path in (root / ".omh").rglob("*") if path.is_file()))).hexdigest()
            self.assertEqual(before, after)
            self.assertNotIn(str(root), _serialize(report))

    def test_the_report_states_its_offline_execution_and_claim_boundary(self) -> None:
        report = run_memory_retrieval_evaluation()

        self.assertIn("no model call", report["execution_boundary"])
        self.assertIn("never execution, review, CI, merge", report["claim_boundary"])
        self.assertIn("separate observed-evidence contract", report["claim_boundary"])


class RetrievalSchemaCompatibilityTests(unittest.TestCase):
    def test_the_existing_evaluation_report_schema_is_untouched(self) -> None:
        """Old omh_memory_evaluation/v1 reports stay readable: the retrieval
        result is a sibling schema, never a widening of the existing one."""
        report = run_memory_evaluation("small", repetitions=1)

        self.assertEqual(report["schema_version"], "omh_memory_evaluation/v1")
        self.assertEqual(set(report) & set(RETRIEVAL_IDENTITY_FIELDS), {"schema_version"})
        self.assertNotIn("cases", report)


class RetrievalSuiteCliTests(unittest.TestCase):
    def test_the_cli_writes_the_report_and_exits_zero_when_the_corpus_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "retrieval.json"
            status, stdout, stderr = run_cli(
                ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes"), "memory", "recall-suite", "--revision", "rev-a", "--output", str(output)]
            )

            self.assertEqual((status, stderr), (0, ""))
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], RETRIEVAL_EVALUATION_SCHEMA_VERSION)
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["target_revision"], "rev-a")
            self.assertEqual(payload, json.loads(output.read_text(encoding="utf-8")))

    def test_the_cli_exits_non_zero_when_the_report_fails(self) -> None:
        failing = {**run_memory_retrieval_evaluation(), "passed": False}
        with mock.patch("omh.commands.memory.run_memory_retrieval_evaluation", return_value=failing):
            status, stdout, _stderr = run_cli(["memory", "recall-suite"])

        self.assertEqual(status, 1)
        self.assertFalse(json.loads(stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
