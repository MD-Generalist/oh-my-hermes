"""handoff_contract/v1 on prepared coding handoffs (#1715).

Three claims, each with its negative case:

(a) a declared input no handoff template uses, or a template variable nothing
    declares, refuses the handoff and names the offender;
(b) only a recorded integer exit status per declared postcondition moves the
    receipt from prepared_not_observed to observed -- a completion status, a
    wrapper flag, or the word "passed" cannot;
(c) forbidden actions land in the executor-facing Don't section of the
    prompt, not in the routing metadata around it.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.coding.handoff_contract import (
    HandoffContractError,
    build_handoff_contract,
    build_handoff_contract_receipt,
    contract_verification_observed,
    handoff_contract_errors,
)
from omh.coding_delegation import build_coding_delegation_payload
from omh.coding_lifecycle import (
    CodingLifecycleError,
    record_codex_dispatch,
    record_codex_result,
    record_codex_verification,
    start_codex_delegation_lifecycle,
)
from omh.commands.coding import _postcondition_exit_statuses
from omh.paths import resolve_paths
from omh.runtime.artifacts import summarize_delegated_coding_status, write_wrapper_contract

CODING_TASK = "Fix the failing parser test in src/parser.py"
HANDOFF_KEYS = ("executor_handoff", "prompt_handoff", "runtime_handoff")
# One executor per handoff schema: codex -> coding_executor_handoff/v1,
# claude-code -> coding_prompt_handoff/v1, hermes -> coding_runtime_handoff/v1.
EXECUTORS = ("codex", "claude-code", "hermes")
FORBIDDEN = "Push to the default branch or force-push any shared branch."


def declaration(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "inputs": [{"name": "test_path", "input_type": "file", "requirement": "required"}],
        "postconditions": [
            {"id": "parser-tests", "command": "PYTHONPATH=tests python -m unittest {test_path}"},
            {"id": "compile", "command": "python -m compileall -q src"},
        ],
        "forbidden_actions": [FORBIDDEN],
        "output_shape": {"format": "json", "required_fields": ["status", "changed_files"]},
    }
    base.update(overrides)
    return base


def handoff_of(payload: dict[str, object]) -> tuple[str, dict[str, object]]:
    for key in HANDOFF_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            return key, value
    raise AssertionError("payload carries no prepared handoff")


def section(prompt: str, heading: str) -> str:
    start = prompt.index(f"\n{heading}\n")
    end = prompt.index("\n\n", start + len(heading) + 2)
    return prompt[start:end]


class InputValidationTests(unittest.TestCase):
    """Criterion (a): unused inputs and undeclared variables are refused by name."""

    def test_declared_contract_attaches_to_every_prepared_handoff_schema(self) -> None:
        for executor in EXECUTORS:
            with self.subTest(executor=executor):
                _, handoff = handoff_of(
                    build_coding_delegation_payload(CODING_TASK, executor_target=executor, handoff_contract=declaration())
                )
                contract = handoff["handoff_contract"]
                self.assertEqual(contract["schema_version"], "handoff_contract/v1")
                self.assertEqual(contract["status"], "prepared_not_observed")
                self.assertEqual([entry["name"] for entry in contract["inputs"]], ["message", "test_path"])
                self.assertEqual([entry["id"] for entry in contract["postconditions"]], ["parser-tests", "compile"])
                self.assertEqual(handoff_contract_errors(handoff), [])

    def test_declared_but_unused_input_refuses_the_handoff_and_names_it(self) -> None:
        unused = declaration(
            inputs=[
                {"name": "test_path", "input_type": "file", "requirement": "required"},
                {"name": "ticket_id", "input_type": "string", "requirement": "required"},
            ]
        )
        for executor in EXECUTORS:
            with self.subTest(executor=executor):
                with self.assertRaises(HandoffContractError) as caught:
                    build_coding_delegation_payload(CODING_TASK, executor_target=executor, handoff_contract=unused)
                self.assertIn("'ticket_id' is declared but no handoff template uses it", str(caught.exception))
                self.assertNotIn("test_path", str(caught.exception))

    def test_template_variable_without_declaration_refuses_the_handoff_and_names_it(self) -> None:
        undeclared = declaration(inputs=[])
        for executor in EXECUTORS:
            with self.subTest(executor=executor):
                with self.assertRaises(HandoffContractError) as caught:
                    build_coding_delegation_payload(CODING_TASK, executor_target=executor, handoff_contract=undeclared)
                self.assertIn("template variable 'test_path' has no input declaration", str(caught.exception))

    def test_a_variable_added_to_a_rendered_template_is_named_by_the_validator(self) -> None:
        _, handoff = handoff_of(
            build_coding_delegation_payload(CODING_TASK, executor_target="claude-code", handoff_contract=declaration())
        )
        tampered = dict(handoff)
        tampered["invocation"] = {**handoff["invocation"], "dispatch_text_template": "On {branch}: {message}"}
        self.assertEqual(
            handoff_contract_errors(tampered),
            ["handoff_contract template variable 'branch' has no input declaration"],
        )

    def test_the_task_input_is_supplied_by_omh_and_cannot_be_redeclared(self) -> None:
        with self.assertRaises(HandoffContractError) as caught:
            build_handoff_contract(declaration(inputs=[{"name": "message", "input_type": "string"}]))
        self.assertIn("'message' is supplied by OMH", str(caught.exception))

    def test_a_contract_without_a_postcondition_is_refused(self) -> None:
        with self.assertRaises(HandoffContractError) as caught:
            build_handoff_contract(declaration(postconditions=[]))
        self.assertIn("postconditions", str(caught.exception))

    def test_optional_input_requires_a_default_and_required_input_refuses_one(self) -> None:
        with self.assertRaises(HandoffContractError):
            build_handoff_contract(declaration(inputs=[{"name": "test_path", "requirement": "optional"}]))
        with self.assertRaises(HandoffContractError):
            build_handoff_contract(declaration(inputs=[{"name": "test_path", "requirement": "required", "default": "x"}]))


class ReceiptTests(unittest.TestCase):
    """Criterion (b) at the record: exit statuses decide, words are refused."""

    def setUp(self) -> None:
        self.contract = build_handoff_contract(declaration())

    def test_every_zero_exit_status_is_observed_and_passed(self) -> None:
        receipt = build_handoff_contract_receipt(self.contract, {"parser-tests": 0, "compile": 0})
        self.assertEqual((receipt["status"], receipt["verdict"]), ("observed", "passed"))
        self.assertTrue(contract_verification_observed(self.contract, receipt))

    def test_a_nonzero_exit_status_is_observed_and_failed(self) -> None:
        receipt = build_handoff_contract_receipt(self.contract, {"parser-tests": 1, "compile": 0})
        self.assertEqual((receipt["status"], receipt["verdict"]), ("observed", "failed"))
        self.assertFalse(contract_verification_observed(self.contract, receipt))

    def test_a_contract_never_run_stays_prepared_not_observed_and_names_what_is_missing(self) -> None:
        receipt = build_handoff_contract_receipt(self.contract, {})
        self.assertEqual((receipt["status"], receipt["verdict"]), ("prepared_not_observed", "not_observed"))
        self.assertEqual(receipt["unobserved_postconditions"], ["parser-tests", "compile"])
        partial = build_handoff_contract_receipt(self.contract, {"parser-tests": 0})
        self.assertEqual((partial["status"], partial["verdict"]), ("prepared_not_observed", "not_observed"))
        self.assertEqual(partial["unobserved_postconditions"], ["compile"])

    def test_wording_is_refused_as_an_exit_status(self) -> None:
        for claim in ("passed", "0", True, "all tests passed"):
            with self.subTest(claim=claim):
                with self.assertRaises(HandoffContractError) as caught:
                    build_handoff_contract_receipt(self.contract, {"parser-tests": claim, "compile": 0})
                self.assertIn("'parser-tests' exit status must be an integer", str(caught.exception))

    def test_an_undeclared_postcondition_is_refused(self) -> None:
        with self.assertRaises(HandoffContractError) as caught:
            build_handoff_contract_receipt(self.contract, {"lint": 0})
        self.assertIn("lint", str(caught.exception))

    def test_a_receipt_for_another_contract_does_not_verify_this_one(self) -> None:
        other = build_handoff_contract(declaration(forbidden_actions=[]))
        receipt = build_handoff_contract_receipt(other, {"parser-tests": 0, "compile": 0})
        self.assertFalse(contract_verification_observed(self.contract, receipt))

    def test_cli_exit_status_parser_refuses_words(self) -> None:
        self.assertEqual(_postcondition_exit_statuses(["parser-tests=0", "compile=2"]), {"parser-tests": 0, "compile": 2})
        for entry in ("parser-tests=passed", "parser-tests=ok", "parser-tests", "=0"):
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    _postcondition_exit_statuses([entry])


class LifecycleReceiptTests(unittest.TestCase):
    """Criterion (b) on the run-backed receipt path: `coding lifecycle verify`."""

    def setUp(self) -> None:
        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        self.paths = resolve_paths(root / "omh", root / "hermes")

    def completed_run(self, contract: dict[str, object] | None) -> str:
        started = start_codex_delegation_lifecycle(self.paths, CODING_TASK, handoff_contract=contract)
        run_id = str(started["run"]["run_id"])
        record_codex_dispatch(self.paths, run_id)
        record_codex_result(self.paths, run_id, result="completed", evidence_refs=["fixture:result"])
        return run_id

    def verification(self, run_id: str) -> dict[str, object]:
        return summarize_delegated_coding_status(self.paths, run_id)["verification"]

    def test_completion_wording_without_exit_statuses_does_not_promote(self) -> None:
        run_id = self.completed_run(declaration())
        result = record_codex_verification(self.paths, run_id, completion_status="completed")
        self.assertEqual(result["handoff_contract_receipt"]["status"], "prepared_not_observed")
        self.assertNotEqual(result["status"]["next_action"], "report_completion_with_evidence")
        # The stored wrapper record must not carry the flag either: a reader of
        # wrapper.json alone would otherwise see verification the receipt denies.
        self.assertFalse(result["wrapper"]["verification_observed"])
        verification = self.verification(run_id)
        self.assertFalse(verification["observed"])
        self.assertEqual(verification["handoff_contract"]["status"], "prepared_not_observed")
        self.assertEqual(verification["handoff_contract"]["unobserved_postconditions"], ["parser-tests", "compile"])

    def test_a_wrapper_flag_cannot_promote_a_contracted_run(self) -> None:
        run_id = self.completed_run(declaration())
        # The `omh runtime wrapper --verification-observed` path: a boolean
        # someone asserts, with no exit status behind it.
        write_wrapper_contract(
            self.paths.runtime_runs_dir / run_id,
            {
                "prompt_dispatched": True,
                "hermes_response_observed": True,
                "verification_observed": True,
                "completion_status": "completed",
            },
        )
        verification = self.verification(run_id)
        self.assertFalse(verification["observed"])
        self.assertEqual(verification["handoff_contract"]["reason"], "no_exit_status_recorded")

    def test_recorded_zero_exit_statuses_promote_the_receipt_to_observed(self) -> None:
        run_id = self.completed_run(declaration())
        result = record_codex_verification(
            self.paths, run_id, postcondition_exit_statuses={"parser-tests": 0, "compile": 0}
        )
        verification = self.verification(run_id)
        self.assertTrue(verification["observed"])
        self.assertEqual(
            (verification["handoff_contract"]["status"], verification["handoff_contract"]["verdict"]),
            ("observed", "passed"),
        )
        self.assertEqual(result["status"]["next_action"], "report_completion_with_evidence")
        self.assertTrue(result["status"]["runtime_validation"]["ok"])

    def test_a_failing_exit_status_is_observed_but_does_not_verify(self) -> None:
        run_id = self.completed_run(declaration())
        record_codex_verification(self.paths, run_id, postcondition_exit_statuses={"parser-tests": 1, "compile": 0})
        verification = self.verification(run_id)
        self.assertFalse(verification["observed"])
        self.assertEqual(verification["handoff_contract"]["verdict"], "failed")

    def test_a_run_without_a_contract_keeps_the_existing_verification_record(self) -> None:
        run_id = self.completed_run(None)
        result = record_codex_verification(self.paths, run_id)
        self.assertNotIn("handoff_contract_receipt", result)
        verification = self.verification(run_id)
        self.assertTrue(verification["observed"])
        self.assertNotIn("handoff_contract", verification)

    def test_exit_statuses_for_a_run_without_a_contract_are_refused(self) -> None:
        run_id = self.completed_run(None)
        with self.assertRaises(CodingLifecycleError):
            record_codex_verification(self.paths, run_id, postcondition_exit_statuses={"parser-tests": 0})


class ForbiddenActionPlacementTests(unittest.TestCase):
    """Criterion (c): forbidden actions reach the executor, not the routing metadata."""

    def test_forbidden_actions_render_in_the_executor_dont_section(self) -> None:
        for executor in EXECUTORS:
            with self.subTest(executor=executor):
                _, handoff = handoff_of(
                    build_coding_delegation_payload(CODING_TASK, executor_target=executor, handoff_contract=declaration())
                )
                dont = section(str(handoff["prompt_template"]), "Don't")
                self.assertIn(f"- Forbidden: {FORBIDDEN}", dont)

    def test_forbidden_actions_are_absent_from_routing_metadata(self) -> None:
        for executor in EXECUTORS:
            with self.subTest(executor=executor):
                payload = build_coding_delegation_payload(
                    CODING_TASK, executor_target=executor, handoff_contract=declaration()
                )
                key, _ = handoff_of(payload)
                routing = {name: value for name, value in payload.items() if name != key}
                self.assertNotIn(FORBIDDEN, json.dumps(routing))

    def test_postconditions_render_in_the_test_section_as_exit_status_verdicts(self) -> None:
        _, handoff = handoff_of(
            build_coding_delegation_payload(CODING_TASK, executor_target="generic", handoff_contract=declaration())
        )
        test_section = section(str(handoff["prompt_template"]), "Test")
        self.assertIn("Postcondition `parser-tests`: run `PYTHONPATH=tests python -m unittest {test_path}`", test_section)
        self.assertIn("Postcondition `compile`: run `python -m compileall -q src`", test_section)


class CliTests(unittest.TestCase):
    """The operator surface: declare with --handoff-contract, record with --postcondition-exit."""

    def test_lifecycle_verify_needs_integer_exit_statuses_to_observe_a_declared_contract(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(declaration()), encoding="utf-8", newline="\n")
            status, stdout, stderr = run_cli(
                base
                + [
                    "coding", "lifecycle", "start", "--record",
                    "--handoff-contract", str(contract_path), *CODING_TASK.split(),
                ]
            )
            self.assertEqual((status, stderr), (0, ""))
            run_id = json.loads(stdout)["run"]["run_id"]
            self.assertEqual(run_cli(base + ["coding", "lifecycle", "dispatch", "--run", run_id])[0], 0)
            self.assertEqual(
                run_cli(base + ["coding", "lifecycle", "result", "--run", run_id, "--result", "completed"])[0], 0
            )

            status, _, stderr = run_cli(
                base + ["coding", "lifecycle", "verify", "--run", run_id, "--postcondition-exit", "parser-tests=passed"]
            )
            self.assertNotEqual(status, 0)
            self.assertIn("integer exit status", stderr)

            status, stdout, _ = run_cli(base + ["coding", "lifecycle", "verify", "--run", run_id])
            self.assertEqual(status, 0)
            self.assertFalse(json.loads(stdout)["status"]["verification"]["observed"])

            status, stdout, _ = run_cli(
                base
                + [
                    "coding", "lifecycle", "verify", "--run", run_id,
                    "--postcondition-exit", "parser-tests=0", "--postcondition-exit", "compile=0",
                ]
            )
            self.assertEqual(status, 0)
            verification = json.loads(stdout)["status"]["verification"]
            self.assertTrue(verification["observed"])
            self.assertEqual(verification["handoff_contract"]["status"], "observed")

    def test_delegate_refuses_a_contract_with_an_unused_input_by_name(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
            contract_path = root / "contract.json"
            unused = declaration(
                inputs=[
                    {"name": "test_path", "input_type": "file"},
                    {"name": "ticket_id", "input_type": "string"},
                ]
            )
            contract_path.write_text(json.dumps(unused), encoding="utf-8", newline="\n")
            status, _, stderr = run_cli(
                base
                + [
                    "coding", "delegate", "--executor", "claude-code",
                    "--handoff-contract", str(contract_path), *CODING_TASK.split(),
                ]
            )
            self.assertNotEqual(status, 0)
            self.assertIn("'ticket_id' is declared but no handoff template uses it", stderr)


class UnaffectedHandoffTests(unittest.TestCase):
    """A handoff that declares no contract, and a non-coding request, are unchanged."""

    def test_a_handoff_without_a_declaration_carries_no_contract_and_no_contract_text(self) -> None:
        for executor in EXECUTORS:
            with self.subTest(executor=executor):
                key, handoff = handoff_of(build_coding_delegation_payload(CODING_TASK, executor_target=executor))
                self.assertNotIn("handoff_contract", handoff)
                prompt = str(handoff["prompt_template"])
                self.assertNotIn("Forbidden:", prompt)
                self.assertNotIn("Postcondition `", prompt)

    def test_a_non_coding_request_is_unchanged_and_says_the_declaration_was_not_attached(self) -> None:
        message = "what is the capital of France"
        plain = build_coding_delegation_payload(message)
        declared = build_coding_delegation_payload(message, handoff_contract=declaration())
        self.assertNotEqual(plain["delegation"]["action"], "delegate")
        self.assertEqual(
            declared.pop("handoff_contract_not_attached"),
            {"schema_version": "handoff_contract/v1", "status": "not_attached", "reason": "no_prepared_handoff"},
        )
        self.assertEqual(json.dumps(declared, sort_keys=True), json.dumps(plain, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
