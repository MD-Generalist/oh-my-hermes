from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "lib"))

from common import CONDITIONS, artifact_is_safe  # noqa: E402
from common import append_jsonl, tree_digest, write_json  # noqa: E402
from runner import doctor, execute_one  # noqa: E402
from statistics import analyze  # noqa: E402
from auditing import audit  # noqa: E402


class OmhBenchmarkFrameworkTests(unittest.TestCase):
    def test_doctor_reports_omh_hermes_child_contract(self) -> None:
        result = doctor(BASE, BASE / "manifest.json")
        self.assertTrue(result["ok"])
        self.assertEqual(result["capabilities"]["live_harness"], "omh_hermes_child")
        self.assertEqual(result["capabilities"]["observation_schema"], "routing_observation/v1")

    def test_fake_smoke_is_offline_and_passes(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        with TemporaryDirectory() as root:
            output = Path(root) / "runs.jsonl"
            result = execute_one(
                BASE,
                manifest,
                "development",
                "D-RENAME",
                "edit",
                7919,
                "baseline",
                output,
            )
            self.assertTrue(result["grade"]["pass"])
            self.assertEqual(result["harness"], "fake")
            self.assertTrue(artifact_is_safe(result))

    def test_artifact_gate_rejects_prompt_secret_and_absolute_path(self) -> None:
        self.assertFalse(
            artifact_is_safe(
                {
                    "schema_version": "omh_live_model_tool_run/v1",
                    "prompt": "SECRET USER PROMPT BODY",
                    "api_key": "sk-abcdefghijklmnopqrst",
                    "workspace": "/Users/victim/private/repo",
                }
            )
        )

    def test_output_symlink_and_workspace_symlinks_are_rejected(self) -> None:
        with TemporaryDirectory() as root:
            root_path = Path(root)
            target = root_path / "target.jsonl"
            target.write_text("sentinel", encoding="utf-8")
            link = root_path / "runs.jsonl"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                append_jsonl(link, {"safe": True})
            with self.assertRaises(OSError):
                write_json(link, {"safe": True})
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")
            outside = root_path / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            workspace = root_path / "workspace"
            workspace.mkdir()
            (workspace / "escape").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                tree_digest(workspace)

    def test_live_harness_is_blocked_without_explicit_flag(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(BASE / "bench.py"), "smoke", "--harness", "omh"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires --allow-paid-live", completed.stderr)

    def test_live_harness_requires_explicit_call_budget(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(BASE / "bench.py"),
                "smoke",
                "--harness",
                "omh",
                "--allow-paid-live",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires --max-paid-calls", completed.stderr)

    def test_offline_receipt_counts_zero_paid_calls(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        with TemporaryDirectory() as root:
            from runner import run_matrix

            receipt = run_matrix(
                BASE,
                manifest,
                "development",
                "baseline",
                Path(root) / "runs.jsonl",
            )
            self.assertEqual(receipt["paid_calls_launched"], 0)
            self.assertIs(type(receipt["paid_calls_launched"]), int)

    def test_help_names_omh_and_never_omo(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(BASE / "bench.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("OMH-native Hermes Agent", completed.stdout)
        self.assertNotIn("OMO", completed.stdout)

    def test_run_record_schema_matches_observation_summary(self) -> None:
        schema = json.loads((BASE / "schemas" / "run-record.schema.json").read_text())
        self.assertEqual(schema["$id"], "omh_live_model_tool_run/v1")
        self.assertEqual(
            set(schema["properties"]["observation"]["required"]),
            {"status", "tools", "tokens", "cost_usd"},
        )
        answer_schema = json.loads((BASE / "schemas" / "final-answer.schema.json").read_text())
        self.assertEqual(answer_schema["$id"], "omh_live_model_tool_answer/v1")
        self.assertTrue(answer_schema["additionalProperties"])

    def test_analysis_compares_equal_task_digests(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        with TemporaryDirectory() as root:
            root_path = Path(root)
            baseline = root_path / "baseline.jsonl"
            optimized = root_path / "optimized.jsonl"
            for condition, output in (("baseline", baseline), ("optimized", optimized)):
                execute_one(
                    BASE,
                    manifest,
                    "development",
                    "D-RENAME",
                    "edit",
                    7919,
                    condition,
                    output,
                )
            result = analyze(baseline, optimized, 100, 7, manifest)
            self.assertEqual(result["models"]["offline/fake-model"]["n"], 1)
            self.assertEqual(result["models"]["offline/fake-model"]["delta"], 0)
            report = root_path / "analysis.json"
            write_json(report, result)
            audited = audit(BASE / "manifest.json", report)
            self.assertTrue(audited["ok"])
            self.assertFalse(audited["claim_permitted"])

    def test_analysis_pairs_the_family_arm_against_the_override(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        with TemporaryDirectory() as root:
            root_path = Path(root)
            family = root_path / "family.jsonl"
            optimized = root_path / "optimized.jsonl"
            for condition, output in (("family", family), ("optimized", optimized)):
                record = execute_one(BASE, manifest, "development", "D-RENAME", "edit", 7919, condition, output)
                self.assertEqual(record["condition"], condition)
            result = analyze(family, optimized, 100, 7, manifest, baseline_condition="family")
            self.assertEqual(result["conditions"], {"baseline": "family", "optimized": "optimized"})
            self.assertEqual(result["models"]["offline/fake-model"]["n"], 1)
            with self.assertRaisesRegex(ValueError, "baseline file contains wrong condition"):
                analyze(family, optimized, 100, 7, manifest)
            default = analyze(optimized, optimized, 100, 7, manifest, baseline_condition="optimized")
            self.assertEqual(default["conditions"], {"baseline": "optimized", "optimized": "optimized"})

    def test_bench_cli_schedules_the_family_condition_offline(self) -> None:
        with TemporaryDirectory() as root:
            output = Path(root) / "family-runs.jsonl"
            completed = subprocess.run(
                [sys.executable, str(BASE / "bench.py"), "run", "--condition", "family", "--output", str(output)],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertEqual((receipt["condition"], receipt["scheduled"], receipt["paid_calls_launched"]), ("family", 10, 0))
            conditions = {json.loads(line)["condition"] for line in output.read_text().splitlines()}
            self.assertEqual(conditions, {"family"})

    def test_every_condition_enumeration_matches_the_declared_arms(self) -> None:
        self.assertEqual(CONDITIONS, ("baseline", "optimized", "family", "unit", "unit_lean"))
        schema = json.loads((BASE / "schemas" / "run-record.schema.json").read_text())
        self.assertEqual(schema["properties"]["condition"]["enum"], list(CONDITIONS))
        self.assertIn("prompt_digest", schema["properties"])
        self.assertIn("head_omitted_blocks", schema["properties"])
        for script in ("bench.py", "analyze.py"):
            argv = [sys.executable, str(BASE / script)]
            argv += ["run", "--condition", "bogus"] if script == "bench.py" else [
                "--baseline", "a", "--optimized", "b", "--output", "c", "--baseline-condition", "bogus",
            ]
            completed = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 2, script)
            for condition in CONDITIONS:
                self.assertIn(f"'{condition}'", completed.stderr, script)

    def test_bench_cli_runs_the_unit_arms_offline_and_records_their_prompts(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        with TemporaryDirectory() as root:
            root_path = Path(root)
            records = {}
            for condition in ("unit", "unit_lean"):
                output = root_path / f"{condition}-runs.jsonl"
                for command in ("smoke", "run"):
                    completed = subprocess.run(
                        [sys.executable, str(BASE / "bench.py"), command, "--harness", "fake",
                         "--condition", condition, "--output", str(output)],
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                receipt = json.loads(completed.stdout)
                self.assertEqual(
                    (receipt["condition"], receipt["scheduled"], receipt["passed"], receipt["paid_calls_launched"]),
                    (condition, 10, 10, 0),
                )
                rows = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual(len(rows), 11)
                self.assertEqual({row["condition"] for row in rows}, {condition})
                for row in rows:
                    self.assertRegex(row["prompt_digest"], r"^[0-9a-f]{64}$")
                    self.assertTrue(artifact_is_safe(row))
                records[condition] = rows[1:]
            self.assertEqual([row["head_omitted_blocks"] for row in records["unit"]], [[]] * 10)
            self.assertEqual(
                [row["head_omitted_blocks"] for row in records["unit_lean"]], [["head.parent_clarification"]] * 10
            )
            for unit, lean in zip(records["unit"], records["unit_lean"], strict=True):
                self.assertEqual(unit["task_digest"], lean["task_digest"])
                self.assertNotEqual(unit["prompt_digest"], lean["prompt_digest"])
            baseline = execute_one(
                BASE, manifest, "development", "D-RENAME", "edit", 7919, "baseline", root_path / "baseline.jsonl",
            )
            self.assertIsNone(baseline["head_omitted_blocks"])
            self.assertNotIn(baseline["prompt_digest"], {row["prompt_digest"] for row in records["unit"]})
            unit_path, lean_path = root_path / "unit.jsonl", root_path / "lean.jsonl"
            for path, rows in ((unit_path, records["unit"]), (lean_path, records["unit_lean"])):
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            result = analyze(
                unit_path, lean_path, 100, 7, manifest, baseline_condition="unit", optimized_condition="unit_lean",
            )
            self.assertEqual(result["conditions"], {"baseline": "unit", "optimized": "unit_lean"})
            self.assertEqual(result["models"]["offline/fake-model"]["n"], 10)
            report = root_path / "analysis.json"
            write_json(report, result)
            audited = audit(BASE / "manifest.json", report)
            self.assertTrue(audited["ok"])
            self.assertTrue(audited["checks"]["conditions_declared"])
            write_json(report, {**result, "conditions": {"baseline": "unit", "optimized": "bogus"}})
            self.assertFalse(audit(BASE / "manifest.json", report)["checks"]["conditions_declared"])

    def test_analysis_rejects_unscheduled_model_claim_matrix(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        with TemporaryDirectory() as root:
            root_path = Path(root)
            baseline = root_path / "baseline.jsonl"
            optimized = root_path / "optimized.jsonl"
            row = {
                "schema_version": "omh_live_model_tool_run/v1",
                "created_at": "2026-08-13T00:00:00+00:00",
                "instance_id": "fabricated-1",
                "split": "development",
                "harness": "omh",
                "model": {"provider": "not-in-manifest", "id": "fabricated-model"},
                "task_digest": "0" * 64,
                "route": {},
                "observation": {
                    "status": "completed",
                    "tools": 1,
                    "tokens": 1,
                    "cost_usd": 0.0,
                },
                "grade": {"pass": True},
                "final_tree_digest": "1" * 64,
            }
            for condition, path in (("baseline", baseline), ("optimized", optimized)):
                current = {**row, "condition": condition}
                path.write_text(json.dumps(current) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "live manifest matrix"):
                analyze(baseline, optimized, 10, 7, manifest)

    def test_analysis_rejects_optimized_development_split(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        records = {}
        for model in manifest["models"]:
            if not model.get("live"):
                continue
            for template, _task_class in __import__("corpus").TEMPLATES:
                for seed in __import__("corpus").EVALUATION_SEEDS:
                    instance_id = f"E-{template}-{seed}"
                    records[(model["provider"], model["id"], instance_id)] = {
                        "schema_version": "omh_live_model_tool_run/v1",
                        "created_at": "2026-08-13T00:00:00+00:00",
                        "instance_id": instance_id,
                        "split": "evaluation",
                        "harness": "omh",
                        "model": model,
                        "task_digest": "0" * 64,
                        "route": {},
                        "observation": {
                            "status": "completed",
                            "tools": 1,
                            "tokens": 1,
                            "cost_usd": 0.0,
                        },
                        "grade": {"pass": True},
                        "final_tree_digest": "1" * 64,
                    }
        with TemporaryDirectory() as root:
            root_path = Path(root)
            baseline = root_path / "baseline.jsonl"
            optimized = root_path / "optimized.jsonl"
            baseline.write_text(
                "".join(
                    json.dumps({**row, "condition": "baseline"}) + "\n"
                    for row in records.values()
                ),
                encoding="utf-8",
            )
            optimized.write_text(
                "".join(
                    json.dumps(
                        {**row, "condition": "optimized", "split": "development"}
                    )
                    + "\n"
                    for row in records.values()
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "evaluation split"):
                analyze(baseline, optimized, 10, 7, manifest)


class OmhTargetedManifestAnalysisTests(unittest.TestCase):
    """Issue #1056 step-0: targeted family manifests must flow end-to-end.

    bench.py run accepts --manifest, but the analyze.py CLI used to reload the
    canonical manifest directly, so a targeted manifest (for example, one live
    solar family entry) could run but never pass the analysis coverage check.
    These tests pin the CLI contract: --manifest selects the manifest used for
    analysis coverage, defaults stay canonical, and mismatched manifests still
    fail loudly.
    """

    def _run_cli(self, args):
        return subprocess.run(
            [sys.executable, str(BASE / "analyze.py"), *args],
            capture_output=True,
            text=True,
        )

    def _live_manifest(self):
        manifest = json.loads((BASE / "manifest.json").read_text())
        manifest["models"] = [m for m in manifest["models"] if m.get("live")][:1]
        return manifest

    def _evaluation_rows(self, manifest, condition):
        import corpus

        model = next(m for m in manifest["models"] if m.get("live"))
        rows = []
        for template, _task_class in corpus.TEMPLATES:
            for seed in corpus.EVALUATION_SEEDS:
                rows.append(
                    {
                        "schema_version": "omh_live_model_tool_run/v1",
                        "created_at": "2026-08-13T00:00:00+00:00",
                        "instance_id": f"E-{template}-{seed}",
                        "split": "evaluation",
                        "harness": "omh",
                        "model": {"provider": model["provider"], "id": model["id"]},
                        "task_digest": "a" * 64,
                        "route": {},
                        "observation": {
                            "status": "completed",
                            "tools": 1,
                            "tokens": 1,
                            "cost_usd": 0.0,
                        },
                        "grade": {"pass": True},
                        "final_tree_digest": "b" * 64,
                        "condition": condition,
                    }
                )
        return rows

    def _write_conditions(self, root_path, manifest):
        """Write a baseline and an optimized run file for the manifest's live model.

        Both rejection tests need a real optimized file too: passing the
        baseline twice fails on "wrong condition" before the manifest check
        is ever reached, which would let any non-zero exit pass for the
        wrong reason.
        """
        paths = []
        for condition in ("baseline", "optimized"):
            output = root_path / f"{condition}.jsonl"
            output.write_text(
                "".join(json.dumps(row) + chr(10) for row in self._evaluation_rows(manifest, condition)),
                encoding="utf-8",
            )
            paths.append(output)
        return tuple(paths)

    def test_targeted_manifest_cli_analyze_end_to_end(self) -> None:
        manifest = self._live_manifest()
        with TemporaryDirectory() as root:
            root_path = Path(root)
            manifest_path = root_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            baseline, optimized = self._write_conditions(root_path, manifest)
            result = self._run_cli(
                [
                    "--baseline", str(baseline),
                    "--optimized", str(optimized),
                    "--manifest", str(manifest_path),
                    "--bootstrap-repetitions", "100",
                    "--seed", "7",
                    "--output", str(root_path / "report.json"),
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root_path / "report.json").read_text())
            model = next(m for m in manifest["models"] if m.get("live"))
            label = f"{model['provider']}/{model['id']}"
            self.assertIn(label, report["models"])
            self.assertEqual(
                report["models"][label]["n"],
                len(self._evaluation_rows(manifest, "baseline")),
            )

    def test_targeted_manifest_cli_rejects_mismatched_manifest(self) -> None:
        manifest = self._live_manifest()
        excluded = manifest["models"][0]
        # A populated matrix of a DIFFERENT live model, not an empty one:
        # the run rows name `excluded`, the manifest under analysis does not.
        other = json.loads((BASE / "manifest.json").read_text())
        other["models"] = [
            m
            for m in other["models"]
            if m.get("live") and (m["provider"], m["id"]) != (excluded["provider"], excluded["id"])
        ][:1]
        self.assertEqual(len(other["models"]), 1)
        with TemporaryDirectory() as root:
            root_path = Path(root)
            other_path = root_path / "other.json"
            other_path.write_text(json.dumps(other), encoding="utf-8")
            baseline, optimized = self._write_conditions(root_path, manifest)
            result = self._run_cli(
                [
                    "--baseline", str(baseline),
                    "--optimized", str(optimized),
                    "--manifest", str(other_path),
                    "--bootstrap-repetitions", "100",
                    "--output", str(root_path / "report.json"),
                ]
            )
            self.assertNotEqual(result.returncode, 0)
            # The analysis contract rejected it, not argparse.
            self.assertIn("does not match the live manifest matrix", result.stderr)

    def test_cli_analyze_defaults_to_canonical_manifest(self) -> None:
        manifest = self._live_manifest()
        with TemporaryDirectory() as root:
            root_path = Path(root)
            baseline, optimized = self._write_conditions(root_path, manifest)
            result = self._run_cli(
                [
                    "--baseline", str(baseline),
                    "--optimized", str(optimized),
                    "--bootstrap-repetitions", "100",
                    "--output", str(root_path / "report.json"),
                ]
            )
            # One targeted live model against the canonical manifest violates
            # the canonical matrix check: the old failure mode, still intact.
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match the live manifest matrix", result.stderr)


if __name__ == "__main__":
    unittest.main()
