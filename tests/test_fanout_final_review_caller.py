"""Immutable post-integration final-review caller behavior."""

from __future__ import annotations

from collections.abc import Callable
import json
import re
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_artifacts import unit_result_path, write_fanout_contract  # noqa: E402
from omh.coding.fanout_dispatch import dispatch_fanout  # noqa: E402
from omh.coding.fanout_final_review_hook import run_final_review_after_integration  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402
from omh.coding.final_review_wave import (  # noqa: E402
    LANE_ORDER,
    ContextProvenance,
    FinalReviewWave,
    ImmutableRevision,
    IntegrationReceipt,
    LaneBudgetReservationInput,
    LaneObservation,
    LaneState,
    ReviewLens,
    prepare_final_review_wave,
)


_REVISION = "a" * 40


def _wave(
    *,
    revision: str = _REVISION,
    blocked: ReviewLens | None = None,
    inherited: ReviewLens | None = None,
) -> FinalReviewWave:
    wave = prepare_final_review_wave(
        "fanout-review",
        tuple(LaneBudgetReservationInput(lens, limit=1, reserved=0) for lens in LANE_ORDER),
    ).integrate(IntegrationReceipt(ImmutableRevision(revision), completed=True))
    for lens in LANE_ORDER:
        wave = wave.observe(
            LaneObservation(
                lens,
                LaneState.MISSING if lens is blocked else LaneState.COMPLETED,
                ImmutableRevision(revision),
                ContextProvenance.INHERITED_FROM_AUTHOR if lens is inherited else ContextProvenance.FRESH_FROM_DIFF,
            )
        )
    return wave


class _Engine:
    def __init__(self, wave: FinalReviewWave) -> None:
        self.wave = wave
        self.calls: list[ImmutableRevision] = []

    def execute(
        self,
        revision: ImmutableRevision,
        observe: Callable[[LaneObservation], None],
    ) -> FinalReviewWave:
        self.calls.append(revision)
        for lane in self.wave.lanes:
            if lane.observed_revision is not None:
                observe(
                    LaneObservation(lane.lens, lane.state, lane.observed_revision, lane.context_provenance)
                )
        return self.wave


class _SilentEngine:
    def __init__(self, wave: FinalReviewWave) -> None:
        self.wave = wave

    def execute(
        self,
        revision: ImmutableRevision,
        observe: Callable[[LaneObservation], None],
    ) -> FinalReviewWave:
        return self.wave


class FanoutFinalReviewCallerTests(unittest.TestCase):
    def test_runs_one_complete_same_revision_wave_and_attaches_only_review_metadata(self) -> None:
        engine = _Engine(_wave())

        result = run_final_review_after_integration(
            engine,
            integrated_revision=_REVISION,
            integration_green=True,
            producer_evidence=True,
            workspace_revision=lambda: _REVISION,
        )

        self.assertEqual(engine.calls, [ImmutableRevision(_REVISION)])
        self.assertEqual(result["final_review_status"], "PASS")
        self.assertEqual(result["final_review_aggregate"], {"revision": _REVISION, "verdict": "PASS"})
        self.assertEqual([record["lens"] for record in result["final_review_records"]], [lens.value for lens in LANE_ORDER])
        self.assertTrue(all(record["revision"] == _REVISION for record in result["final_review_records"]))
        self.assertTrue(all(record["execution_observed"] for record in result["final_review_records"]))
        self.assertEqual(
            {record["context_provenance"] for record in result["final_review_records"]}, {"fresh_from_diff"}
        )
        self.assertTrue(all(str(record["execution_ref"]).startswith("final-review:") for record in result["final_review_records"]))
        self.assertNotIn("integration_ready", result)
        self.assertNotIn("verification_status", result)

    def test_dispatch_runs_the_caller_engine_only_after_immutable_integration_green(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "seed"],
                cwd=repo,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(
                    "post-integration final review",
                    [{
                        "unit_id": "core",
                        "title": "Core",
                        "owner": "codex",
                        "file_scope": ["src/"],
                        "verification_checks": [
                            {"command": "python -c pass", "id": "unit", "safety": "read_only"},
                            {"command": "python -c pass", "id": "integration", "tier": "integration"},
                        ],
                    }],
                ),
            )
            legacy_sidecar = unit_result_path(paths, contract["fanout_id"], "core")

            class Completed:
                returncode = 0
                stdout = "done"
                stderr = ""

            def runner(argv: list[str], **kwargs: object) -> object:
                if argv[0] == "git":
                    return subprocess.run(argv, **kwargs)
                if argv[0] != "codex":
                    return subprocess.run(argv, **kwargs)
                # The dispatcher names an invocation-owned intake path in the
                # worker prompt; the durable per-unit path is no longer read.
                match = re.search(r"JSON sidecar to exactly (.+)\.", " ".join(argv))
                self.assertIsNotNone(match, "prompt names no sidecar path")
                assert match is not None
                sidecar = Path(match[1])
                self.assertNotEqual(sidecar, legacy_sidecar)
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(
                    json.dumps(
                        {
                            "schema_version": "fanout_unit_result/v1",
                            "unit_id": "core",
                            "run_id": contract["units"][0]["run_ref"],
                            "fanout_id": contract["fanout_id"],
                            "base_sha": sha,
                            "head_sha": sha,
                            "process_status": "process_succeeded",
                            "changed_paths": [],
                            "checks": [],
                            "findings": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return Completed()

            engine = _Engine(_wave(revision=revision))
            summary = dispatch_fanout(
                paths,
                contract,
                goal_text="post-integration final review",
                repo_root=repo,
                base_sha=sha,
                runner=runner,
                readiness=lambda _paths, profile: {"status": "ready", "profile": profile},
                run_verification=True,
                integrated_worktree=repo,
                integrated_revision=revision,
                final_review_engine=engine,
            )

            self.assertEqual(engine.calls, [ImmutableRevision(revision)])
            self.assertEqual(summary["final_review_status"], "PASS")
            self.assertEqual(summary["final_review_aggregate"]["revision"], revision)
            self.assertTrue(summary["units"][0]["integration_ready"])

    def test_incomplete_or_moving_integration_evidence_holds_without_running_reviewers(self) -> None:
        for values, expected in (
            ({"integrated_revision": "HEAD", "integration_green": True, "producer_evidence": True}, "BLOCK"),
            ({"integrated_revision": _REVISION, "integration_green": False, "producer_evidence": True}, "HOLD"),
            ({"integrated_revision": _REVISION, "integration_green": True, "producer_evidence": False}, "HOLD"),
        ):
            with self.subTest(values=values):
                engine = _Engine(_wave())
                result = run_final_review_after_integration(engine, **values)
                self.assertEqual(engine.calls, [])
                self.assertEqual(result["final_review_status"], expected)
                self.assertEqual(result["final_review_aggregate"]["verdict"], expected)

    def test_missing_lens_blocks_without_promoting_integration_readiness(self) -> None:
        result = run_final_review_after_integration(
            _Engine(_wave(blocked=ReviewLens.SAFETY)),
            integrated_revision=_REVISION,
            integration_green=True,
            producer_evidence=True,
            workspace_revision=lambda: _REVISION,
        )

        self.assertEqual(result["final_review_status"], "BLOCK")
        self.assertEqual(result["final_review_aggregate"], {
            "revision": _REVISION,
            "verdict": "BLOCK",
            "blocking_lens": "safety",
        })
        self.assertNotIn("integration_ready", result)

    def test_inherited_lens_blocks_and_the_aggregate_names_the_refused_field(self) -> None:
        result = run_final_review_after_integration(
            _Engine(_wave(inherited=ReviewLens.QUALITY)),
            integrated_revision=_REVISION,
            integration_green=True,
            producer_evidence=True,
            workspace_revision=lambda: _REVISION,
        )

        self.assertEqual(result["final_review_status"], "BLOCK")
        self.assertEqual(result["final_review_aggregate"], {
            "revision": _REVISION,
            "verdict": "BLOCK",
            "blocking_lens": "quality",
            "refused_field": "context_provenance",
        })
        self.assertEqual(
            [record["context_provenance"] for record in result["final_review_records"]],
            ["fresh_from_diff", "inherited_from_author", "fresh_from_diff", "fresh_from_diff"],
        )

    def test_observed_provenance_that_disagrees_with_the_wave_blocks(self) -> None:
        class _RelabelingEngine(_Engine):
            def execute(self, revision, observe):
                for lane in self.wave.lanes:
                    observe(LaneObservation(lane.lens, lane.state, lane.observed_revision, ContextProvenance.FRESH_FROM_DIFF))
                return self.wave

        result = run_final_review_after_integration(
            _RelabelingEngine(_wave(inherited=ReviewLens.SAFETY)),
            integrated_revision=_REVISION,
            integration_green=True,
            producer_evidence=True,
            workspace_revision=lambda: _REVISION,
        )

        self.assertEqual(result["final_review_status"], "BLOCK")
        self.assertNotIn("final_review_records", result)

    def test_workspace_mutation_after_review_blocks_a_reported_pass(self) -> None:
        revisions = iter((_REVISION, None))
        engine = _Engine(_wave())

        result = run_final_review_after_integration(
            engine,
            integrated_revision=_REVISION,
            integration_green=True,
            producer_evidence=True,
            workspace_revision=lambda: next(revisions),
        )

        self.assertEqual(engine.calls, [ImmutableRevision(_REVISION)])
        self.assertEqual(result["final_review_status"], "BLOCK")
        self.assertNotIn("final_review_records", result)

    def test_complete_wave_without_observed_lane_receipts_blocks(self) -> None:
        result = run_final_review_after_integration(
            _SilentEngine(_wave()),
            integrated_revision=_REVISION,
            integration_green=True,
            producer_evidence=True,
            workspace_revision=lambda: _REVISION,
        )

        self.assertEqual(result["final_review_status"], "BLOCK")
        self.assertNotIn("final_review_records", result)

    def test_absent_engine_preserves_existing_dispatch_shape(self) -> None:
        self.assertIsNone(
            run_final_review_after_integration(
                None,
                integrated_revision=_REVISION,
                integration_green=True,
                producer_evidence=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
