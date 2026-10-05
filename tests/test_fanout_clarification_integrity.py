from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_fanout_clarification import GOAL, LocalFanout, _prompted_sidecar, _ready
from omh.coding.fanout_clarification import clarification_path, read_clarification
from omh.coding.fanout_clarification_dispatch import claim_answered_worktree, record_answer_redispatch
from omh.coding.fanout_clarification_records import ClarificationRecord
from omh.coding.fanout_status import project_fanout_status
from omh.coding.fanout_dispatch import dispatch_fanout
from omh.coding.unit_prompt_assembly import assemble_unit_prompt, recorded_model_route
from omh.system.paths import OmhPaths


class ClarificationIntegrityTests(unittest.TestCase):
    def test_changed_answer_cannot_borrow_the_original_observation(self) -> None:
        with TemporaryDirectory(prefix="omh-decision-integrity-") as directory:
            # Given: the real parent CLI observed one allowed answer.
            root = Path(directory)
            with patch.dict(os.environ, {
                "HOME": directory, "HERMES_HOME": directory,
                "XDG_CONFIG_HOME": directory, "OMH_FANOUT_DEPTH": "0",
            }):
                fixture = LocalFanout(root)
                first = fixture.dispatch()
                request = fixture.question()
                code, _stdout, stderr = fixture.answer(request, "--answer", "json")
                self.assertEqual(code, 0, stderr)
                original = project_fanout_status(
                    fixture.paths, fixture.fanout_id, unit_id="unit-b"
                )
                self.assertEqual(original["units"][0]["clarification"]["answer"], "observed")
                path = clarification_path(fixture.paths, fixture.fanout_id, "unit-b")
                record = json.loads(path.read_text())
                record["answer"]["answer"] = "text"
                path.write_text(json.dumps(record))
                calls_before = len(fixture.calls)

                # When: status and explicit redispatch consume the altered record.
                changed = project_fanout_status(
                    fixture.paths, fixture.fanout_id, unit_id="unit-b"
                )
                fixture.ask = False
                fixture.dispatch(fixture.journal(first))

                # Then: a valid-shaped but unobserved answer has no execution authority.
                self.assertEqual(
                    (changed["units"][0]["clarification"]["answer"], len(fixture.calls)),
                    ("none", calls_before),
                )

    def test_redispatch_serializes_the_validated_answer_snapshot(self) -> None:
        with TemporaryDirectory(prefix="omh-decision-snapshot-") as directory:
            with patch.dict(os.environ, {
                "HOME": directory, "HERMES_HOME": directory,
                "XDG_CONFIG_HOME": directory, "OMH_FANOUT_DEPTH": "0",
            }):
                # Given: the parent recorded json; an earlier file read sees stale text.
                fixture = LocalFanout(Path(directory))
                first = fixture.dispatch()
                code, _stdout, stderr = fixture.answer(fixture.question(), "--answer", "json")
                self.assertEqual(code, 0, stderr)

                def stale_snapshot(path: Path) -> Mapping[str, object] | None:
                    original = path.read_bytes()
                    changed = json.loads(original)
                    changed["answer"]["answer"] = "text"
                    path.write_text(json.dumps(changed))
                    try:
                        return read_clarification(path)
                    finally:
                        path.write_bytes(original)

                fixture.ask = False
                # When: the real claim validates current disk state before the spawn.
                with patch(
                    "omh.coding.fanout_dispatch.read_clarification",
                    side_effect=stale_snapshot,
                ):
                    fixture.dispatch(fixture.journal(first))

                # Then: parse the actual delivered machine payload, not its prose heading.
                prompt = fixture.calls[-1][1]
                decoder = json.JSONDecoder()
                answers = []
                for offset, character in enumerate(prompt):
                    if character != "{":
                        continue
                    try:
                        value, _end = decoder.raw_decode(prompt[offset:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict) and value.get("schema_version") == "fanout_clarification_answer/v1":
                        answers.append(value["answer"])
                self.assertEqual(answers, ["json"])

    def test_redispatch_observation_describes_the_sent_snapshot(self) -> None:
        with TemporaryDirectory(prefix="omh-decision-receipt-") as directory:
            with patch.dict(os.environ, {
                "HOME": directory, "HERMES_HOME": directory,
                "XDG_CONFIG_HOME": directory, "OMH_FANOUT_DEPTH": "0",
            }):
                # Given: json was answered and selected for the actual child prompt.
                fixture = LocalFanout(Path(directory))
                first = fixture.dispatch()
                code, _stdout, stderr = fixture.answer(fixture.question(), "--answer", "json")
                self.assertEqual(code, 0, stderr)

                def changed_before_receipt(paths: OmhPaths, record: ClarificationRecord) -> None:
                    path = clarification_path(paths, record["fanout_id"], record["unit_id"])
                    changed = json.loads(path.read_text())
                    changed["answer"]["answer"] = "text"
                    path.write_text(json.dumps(changed))
                    record_answer_redispatch(paths, record)

                fixture.ask = False
                # When: the persisted record changes only at the spawn observation boundary.
                with patch(
                    "omh.coding.fanout_dispatch.record_answer_redispatch",
                    side_effect=changed_before_receipt,
                ):
                    fixture.dispatch(fixture.journal(first))

                # Then: a receipt for sent json cannot claim that altered text was dispatched.
                status = project_fanout_status(
                    fixture.paths, fixture.fanout_id, unit_id="unit-b"
                )
                self.assertEqual(status["units"][0]["clarification"]["redispatch"], "none")

    def test_answered_unit_keeps_its_allowed_transport_retry(self) -> None:
        with TemporaryDirectory(prefix="omh-decision-retry-") as directory:
            with patch.dict(os.environ, {
                "HOME": directory, "HERMES_HOME": directory,
                "XDG_CONFIG_HOME": directory, "OMH_FANOUT_DEPTH": "0",
            }):
                # Given: one answer and a replay-safe transport failure on its first spawn.
                fixture = LocalFanout(Path(directory))
                first = fixture.dispatch()
                code, _stdout, stderr = fixture.answer(fixture.question(), "--answer", "json")
                self.assertEqual(code, 0, stderr)
                fixture.ask = False
                failed_once = False
                calls_before = len(fixture.calls)
                delays: list[float] = []

                def transient_runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
                    nonlocal failed_once
                    if argv[0] not in ("git", sys.executable) and not failed_once:
                        failed_once = True
                        fixture.calls.append(("unit-b", argv[-1]))
                        return subprocess.run(
                            [sys.executable, "-c",
                             "import sys; sys.stderr.write('connection reset by peer'); sys.exit(1)"],
                            cwd=kwargs["cwd"], timeout=kwargs["timeout"],
                            capture_output=True, text=True,
                        )
                    return fixture.runner(argv, **kwargs)

                # When: explicit redispatch permits one retry, with no real backoff wait.
                result = dispatch_fanout(
                    fixture.paths, fixture.contract, goal_text=GOAL, repo_root=fixture.repo,
                    base_sha=fixture.sha, runner=transient_runner, readiness=_ready,
                    only_units=["unit-b"], resume_journal=fixture.journal(first),
                    goal_attempt_id="attempt-1", max_retries=1, run_verification=True,
                    sleep=delays.append, rng=lambda: 0.0,
                )

                # Then: each process attempt is observed without reclaiming the same answer.
                unit = next(row for row in result["units"] if row["unit_id"] == "unit-b")
                self.assertEqual(unit["status"], "completed")
                self.assertEqual(len(fixture.calls) - calls_before, 2)
                self.assertEqual(len(delays), 1)

    def test_a_retry_after_a_claimed_decision_resends_that_decision(self) -> None:
        with TemporaryDirectory(prefix="omh-decision-retry-prompt-") as directory:
            with patch.dict(os.environ, {
                "HOME": directory, "HERMES_HOME": directory,
                "XDG_CONFIG_HOME": directory, "OMH_FANOUT_DEPTH": "0",
            }):
                # Given: an observed answer, and a transport failure on the first redispatch spawn.
                fixture = LocalFanout(Path(directory))
                first = fixture.dispatch()
                code, _stdout, stderr = fixture.answer(fixture.question(), "--answer", "json")
                self.assertEqual(code, 0, stderr)
                fixture.ask = False
                claimed: list[ClarificationRecord] = []
                spawned: list[list[str]] = []

                def capture_claim(*args, **kwargs) -> ClarificationRecord:
                    claimed.append(claim_answered_worktree(*args, **kwargs))
                    return claimed[-1]

                def transient_runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
                    if argv[0] in ("git", sys.executable):
                        return fixture.runner(argv, **kwargs)
                    spawned.append(argv)
                    if len(spawned) == 1:
                        return subprocess.run(
                            [sys.executable, "-c",
                             "import sys; sys.stderr.write('connection reset by peer'); sys.exit(1)"],
                            cwd=kwargs["cwd"], timeout=kwargs["timeout"],
                            capture_output=True, text=True,
                        )
                    return fixture.runner(argv, **kwargs)

                # When: the redispatch claims the answer once and retries the spawn.
                with patch("omh.coding.fanout_dispatch.claim_answered_worktree", side_effect=capture_claim):
                    dispatch_fanout(
                        fixture.paths, fixture.contract, goal_text=GOAL, repo_root=fixture.repo,
                        base_sha=fixture.sha, runner=transient_runner, readiness=_ready,
                        only_units=["unit-b"], resume_journal=fixture.journal(first),
                        goal_attempt_id="attempt-1", max_retries=1, run_verification=True,
                        sleep=lambda _delay: None, rng=lambda: 0.0,
                    )

                # Then: the retry's prompt is the assembler's, with the claimed decision appended.
                self.assertEqual((len(claimed), len(spawned)), (1, 2))
                unit = next(row for row in fixture.units if row["unit_id"] == "unit-b")
                retry_argv = spawned[1]
                expected = assemble_unit_prompt(
                    unit, GOAL, route=recorded_model_route(unit),
                    binding={"path": str(_prompted_sidecar(retry_argv)), "unit_id": "unit-b",
                             "run_id": unit["run_ref"], "fanout_id": fixture.fanout_id,
                             "base_sha": fixture.sha},
                    parent_decision=claimed[0],
                )
                self.assertEqual(expected.names()[-1], "append.parent_decision")
                self.assertEqual(retry_argv[-1], expected.text)
