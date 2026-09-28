"""agent_debug_report/v1: cited findings for one Hermes session, read from state.db.

The fixture is a temp Hermes home whose ``state.db`` carries the columns the
reader uses, shaped like Hermes' own rows: assistant rows with a ``tool_calls``
JSON array, tool results in the JSON shapes the host persists, a compaction
summary row, a tool row a compaction re-persisted, a tool row with an empty
``tool_call_id``, and a second session whose rows must not leak in. Prompt and
tool-output text carries ``SECRET`` so a report that quoted any of it would
show. Every expected finding below is a hand derivation from those rows.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _cli_harness import run_cli
from omh.quality import agent_debug_report
from omh.quality.agent_debug_report import (
    AGENT_DEBUG_REPORT_SCHEMA_VERSION,
    CITATION_KEYS,
    FINDING_KINDS,
    AgentDebugReportError,
    agent_debug_report_errors,
    build_agent_debug_report,
    format_agent_debug_report,
)


SESSION = "20260928_101500_ab12cd"
OTHER = "20260928_090000_ffffff"


def _calls(call_id: str, name: str, arguments: dict) -> str:
    return json.dumps(
        [{"id": call_id, "call_id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
    )


PYTEST = {"command": "pytest tests/SECRET_test.py", "timeout": 60}
# The same arguments with keys in another order: the digest is over canonical JSON.
PYTEST_REORDERED = {"timeout": 60, "command": "pytest tests/SECRET_test.py"}
PATCH = {"path": "src/app.py", "old_string": "SECRET old", "new_string": "SECRET new"}


def _write_state_db(home: Path, *, full_schema: bool = True) -> Path:
    """Two sessions; ``full_schema=False`` drops ``tool_calls`` and ``_compressed_summary``,
    the two columns an older Hermes build may not have."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state.db"
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL, last_activity_at REAL, "
                "ended_at REAL, end_reason TEXT)"
            )
            extra = ", tool_calls TEXT, _compressed_summary INTEGER NOT NULL DEFAULT 0" if full_schema else ""
            connection.execute(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                f"tool_call_id TEXT, tool_name TEXT, timestamp REAL{extra})"
            )
            connection.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (SESSION, "tui", 1000.0, 1100.0, 1100.0, "tui_shutdown"),
                    (OTHER, "cli", 900.0, 950.0, None, None),
                ],
            )
            rows = [
                # (id, session, role, content, tool_call_id, tool_name, timestamp, tool_calls, summary)
                (1, SESSION, "user", "fix the SECRET prompt", None, None, 1001.0, None, 0),
                (2, SESSION, "assistant", "", None, None, 1002.0, _calls("c1", "terminal", PYTEST), 0),
                (3, SESSION, "tool", json.dumps({"exit_code": 1, "error": None, "output": "SECRET output"}), "c1", "terminal", 1003.0, None, 0),
                (4, SESSION, "assistant", "", None, None, 1004.0, _calls("c2", "terminal", PYTEST_REORDERED), 0),
                (5, SESSION, "tool", json.dumps({"exit_code": 1, "error": None, "output": "SECRET output"}), "c2", "terminal", 1005.0, None, 0),
                (6, SESSION, "assistant", "", None, None, 1006.0, _calls("c3", "terminal", {"command": "pytest -x"}), 0),
                (7, SESSION, "tool", json.dumps({"exit_code": 0, "error": None, "output": "ok"}), "c3", "terminal", 1007.0, None, 0),
                (8, SESSION, "assistant", "", None, None, 1008.0, _calls("c4", "patch", PATCH), 0),
                (9, SESSION, "tool", json.dumps({"success": False, "error": "SECRET no match"}), "c4", "patch", 1009.0, None, 0),
                (10, SESSION, "assistant", "", None, None, 1010.0, _calls("c5", "read_file", {"path": "src/app.py"}), 0),
                (11, SESSION, "tool", "SECRET plain text result", "c5", "read_file", 1011.0, None, 0),
                (12, SESSION, "assistant", "", None, None, 1012.0, _calls("c6", "patch", PATCH), 0),
                # The next patch call repeats c4's arguments after c4 failed; this one succeeded.
                (13, SESSION, "tool", json.dumps({"success": True, "diff": "SECRET diff"}), "c6", "patch", 1013.0, None, 0),
                (14, SESSION, "user", "[summary of SECRET earlier turns]", None, None, 1014.0, None, 1),
                # A compaction re-persists c1's result under a new id: still one call.
                (15, SESSION, "tool", json.dumps({"exit_code": 1, "error": None, "output": "SECRET output"}), "c1", "terminal", 1015.0, None, 0),
                (16, SESSION, "assistant", "", None, None, 1016.0, _calls("c7", "terminal", {"command": "serve", "background": True}), 0),
                (17, SESSION, "tool", json.dumps({"exit_code": 0, "pid": 4242, "notify_on_complete": False, "session_id": "proc_1"}), "c7", "terminal", 1017.0, None, 0),
                (18, SESSION, "assistant", "", None, None, 1018.0, _calls("c8", "terminal", {"command": "watch", "background": True}), 0),
                (19, SESSION, "tool", json.dumps({"exit_code": 0, "pid": 4243, "notify_on_complete": True, "session_id": "proc_2"}), "c8", "terminal", 1019.0, None, 0),
                # No assistant row carries c9's arguments; a bool exit_code is not an exit code.
                (20, SESSION, "tool", json.dumps({"error": "SECRET bad regex"}), "c9", "search_files", 1020.0, None, 0),
                (21, SESSION, "tool", json.dumps({"exit_code": True}), "c10", "terminal", 1021.0, None, 0),
                # An empty tool_call_id cannot be cited as a call.
                (22, SESSION, "tool", json.dumps({"exit_code": 2}), "", "terminal", 1022.0, None, 0),
                (23, OTHER, "tool", json.dumps({"exit_code": 1}), "c1", "terminal", 951.0, None, 0),
            ]
            if full_schema:
                connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            else:
                connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", [row[:7] for row in rows])
    finally:
        connection.close()
    return path


def _citation(message_ids, timestamps, tool_call_ids=(), tool_name=None, error_class=None, exit_code=None, digest=None):
    return {
        "session_id": SESSION,
        "message_ids": list(message_ids),
        "timestamps": list(timestamps),
        "tool_call_ids": list(tool_call_ids),
        "tool_name": tool_name,
        "error_class": error_class,
        "exit_code": exit_code,
        "arguments_sha256": digest,
    }


class AgentDebugReportTests(unittest.TestCase):
    def _report(self, **kwargs) -> dict:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home, **kwargs)
            return build_agent_debug_report(home, SESSION)

    def test_findings_are_derived_from_record_fields_and_cited(self) -> None:
        report = self._report()
        pytest_digest = report["findings"][1]["citation"]["arguments_sha256"]
        patch_digest = report["findings"][4]["citation"]["arguments_sha256"]
        self.assertEqual(len(pytest_digest), 16)
        self.assertNotEqual(pytest_digest, patch_digest)
        expected = [
            ("tool_error:3", "tool_error", _citation([3], [1003.0], ["c1"], "terminal", "nonzero_exit", 1)),
            (
                "identical_retry_after_error:3",
                "identical_retry_after_error",
                _citation([3, 5], [1003.0, 1005.0], ["c1", "c2"], "terminal", "nonzero_exit", 1, pytest_digest),
            ),
            ("tool_error:5", "tool_error", _citation([5], [1005.0], ["c2"], "terminal", "nonzero_exit", 1)),
            ("tool_error:9", "tool_error", _citation([9], [1009.0], ["c4"], "patch", "success_false")),
            (
                "identical_retry_after_error:9",
                "identical_retry_after_error",
                _citation([9, 13], [1009.0, 1013.0], ["c4", "c6"], "patch", "success_false", None, patch_digest),
            ),
            ("compaction_boundary:14", "compaction_boundary", _citation([14], [1014.0])),
            ("background_without_notify:17", "background_without_notify", _citation([17], [1017.0], ["c7"], "terminal")),
            ("tool_error:20", "tool_error", _citation([20], [1020.0], ["c9"], "search_files", "error_field")),
        ]
        self.assertEqual(
            [(item["finding_id"], item["kind"], item["citation"]) for item in report["findings"]], expected
        )
        self.assertEqual(report["schema_version"], AGENT_DEBUG_REPORT_SCHEMA_VERSION)
        self.assertEqual(
            report["finding_counts"],
            {"tool_error": 4, "identical_retry_after_error": 2, "background_without_notify": 1, "compaction_boundary": 1},
        )
        # c1..c10 plus the empty-id row; c1's re-persisted row is the same call.
        self.assertEqual(report["counts"], {"tool_calls": 11, "tool_calls_without_id": 1, "tool_calls_with_arguments": 8})
        self.assertEqual(
            report["session"],
            {"id": SESSION, "source": "tui", "started_at": 1000.0, "ended_at": 1100.0, "end_reason": "tui_shutdown"},
        )
        self.assertEqual(report["checked_kinds"], list(FINDING_KINDS))
        self.assertEqual(report["unavailable"], [])
        self.assertTrue(report["observed"])
        self.assertEqual(agent_debug_report_errors(report), [])

    def test_no_prompt_argument_or_tool_output_text_reaches_the_report(self) -> None:
        report = self._report()
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("SECRET", format_agent_debug_report(report))

    def test_a_missing_column_leaves_its_kind_unavailable_not_clean(self) -> None:
        report = self._report(full_schema=False)
        self.assertEqual(report["checked_kinds"], ["tool_error", "background_without_notify"])
        self.assertEqual(
            [item["kind"] for item in report["unavailable"]], ["identical_retry_after_error", "compaction_boundary"]
        )
        self.assertEqual(report["finding_counts"]["identical_retry_after_error"], 0)
        self.assertEqual(report["counts"]["tool_calls_with_arguments"], 0)
        self.assertIn("Unavailable\n  identical_retry_after_error: messages.tool_calls column not present", format_agent_debug_report(report))
        self.assertEqual(agent_debug_report_errors(report), [])

    def test_latest_resolves_and_unknown_or_missing_sources_are_named_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            with self.assertRaisesRegex(AgentDebugReportError, "no Hermes state database"):
                build_agent_debug_report(home, "latest")
            path = _write_state_db(home)
            before = path.read_bytes()
            latest = build_agent_debug_report(home, "latest")
            other = build_agent_debug_report(home, OTHER)
            with self.assertRaisesRegex(AgentDebugReportError, "no Hermes session nope"):
                build_agent_debug_report(home, "nope")
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(latest["session"]["id"], SESSION)
        self.assertEqual(latest["source"]["requested_session"], "latest")
        # The other session's c1 failure is its own; nothing of SESSION's leaks in.
        self.assertEqual([item["finding_id"] for item in other["findings"]], ["tool_error:23"])
        self.assertEqual(other["findings"][0]["citation"]["session_id"], OTHER)


class AgentDebugReportValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)
            cls.report = build_agent_debug_report(home, SESSION)

    def _errors_after(self, mutate) -> list[str]:
        report = copy.deepcopy(self.report)
        mutate(report)
        return agent_debug_report_errors(report)

    def test_the_built_report_is_valid(self) -> None:
        self.assertEqual(agent_debug_report_errors(self.report), [])

    def test_a_finding_without_a_citation_is_refused(self) -> None:
        errors = self._errors_after(lambda report: report["findings"][0].pop("citation"))
        self.assertEqual(errors, ["finding 0 (tool_error) has no citation"])

    def test_a_citation_missing_what_its_kind_requires_is_refused(self) -> None:
        def drop_calls(report):
            report["findings"][1]["citation"]["tool_call_ids"] = ["c1"]

        def drop_digest(report):
            report["findings"][1]["citation"]["arguments_sha256"] = None

        def drop_timestamp(report):
            report["findings"][0]["citation"]["timestamps"] = []

        def drop_message(report):
            report["findings"][5]["citation"]["message_ids"] = []

        def drop_error_class(report):
            report["findings"][0]["citation"]["error_class"] = "looked bad"

        def drop_key(report):
            report["findings"][6]["citation"].pop("tool_name")

        self.assertIn("finding 1 (identical_retry_after_error) citation must cite 2 tool_call ids", self._errors_after(drop_calls))
        self.assertIn("finding 1 (identical_retry_after_error) citation must carry the arguments sha256 prefix", self._errors_after(drop_digest))
        self.assertIn("finding 0 (tool_error) citation must carry 1 timestamp", self._errors_after(drop_timestamp))
        self.assertIn("finding 5 (compaction_boundary) citation must cite 1 message id", self._errors_after(drop_message))
        self.assertIn(
            "finding 0 (tool_error) citation must carry an error_class from nonzero_exit, success_false, error_field",
            self._errors_after(drop_error_class),
        )
        self.assertIn("finding 6 (background_without_notify) citation is missing tool_name", self._errors_after(drop_key))

    def test_a_foreign_session_text_field_unknown_kind_or_bad_id_is_refused(self) -> None:
        def foreign(report):
            report["findings"][0]["citation"]["session_id"] = OTHER

        def text(report):
            report["findings"][0]["citation"]["output"] = "tool output"

        def finding_text(report):
            report["findings"][0]["narrative"] = "the agent was confused"

        def unknown(report):
            report["findings"][0]["kind"] = "goal_drift"

        def bad_id(report):
            report["findings"][0]["finding_id"] = "F1"

        def duplicate(report):
            report["findings"].append(copy.deepcopy(report["findings"][0]))

        self.assertEqual(self._errors_after(foreign), ["finding 0 (tool_error) citation names another session"])
        self.assertEqual(
            self._errors_after(text), ["finding 0 (tool_error) citation carries keys outside the citation shape: output"]
        )
        self.assertEqual(
            self._errors_after(finding_text), ["finding 0 carries keys outside the finding shape: narrative"]
        )
        self.assertEqual(self._errors_after(unknown), ["finding 0 has unknown kind 'goal_drift'"])
        self.assertEqual(
            self._errors_after(bad_id), ["finding 0 (tool_error) finding_id must be tool_error:<first cited message id>"]
        )
        self.assertEqual(self._errors_after(duplicate), ["finding 8 (tool_error) repeats finding_id tool_error:3"])

    def test_the_builder_refuses_to_return_a_report_the_validator_rejects(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)
            with mock.patch.object(agent_debug_report, "agent_debug_report_errors", return_value=["finding 0 (tool_error) has no citation"]):
                with self.assertRaisesRegex(AgentDebugReportError, "failed validation: finding 0 \\(tool_error\\) has no citation"):
                    build_agent_debug_report(home, SESSION)

    def test_the_citation_shape_is_the_closed_key_set(self) -> None:
        for finding in self.report["findings"]:
            self.assertEqual(tuple(finding["citation"]), CITATION_KEYS)


class AgentDebugCliTests(unittest.TestCase):
    def _common(self, tmp: str) -> list[str]:
        return ["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(Path(tmp) / ".hermes")]

    def test_plain_text_by_default_json_by_flag_and_read_only(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _write_state_db(Path(tmp) / ".hermes")
            before = path.read_bytes()
            args = [*self._common(tmp), "quality-evidence", "agent-debug", "--hermes-session", "latest"]
            text_status, text_stdout, text_stderr = run_cli(args, output_json=False)
            flag_status, flag_stdout, flag_stderr = run_cli([*args, "--json"], output_json=False)
            self.assertEqual(path.read_bytes(), before)

        self.assertEqual((text_status, flag_status), (0, 0), (text_stderr, flag_stderr))
        self.assertTrue(text_stdout.startswith(f"OMH agent debug report: session {SESSION} (source tui)\n"))
        self.assertIn(
            "  identical_retry_after_error:3  messages 3,5  at 1970-01-01T00:16:43Z,1970-01-01T00:16:45Z  "
            "calls c1,c2  tool terminal  nonzero_exit  exit 1  args sha256 ",
            text_stdout,
        )
        self.assertIn("Boundary\n", text_stdout)
        payload = json.loads(flag_stdout)
        self.assertEqual(payload["schema_version"], AGENT_DEBUG_REPORT_SCHEMA_VERSION)
        self.assertEqual(len(payload["findings"]), 8)

    def test_an_unknown_session_or_missing_database_exits_two(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = run_cli([*self._common(tmp), "quality-evidence", "agent-debug", "--hermes-session", "latest"])
            _write_state_db(Path(tmp) / ".hermes")
            unknown = run_cli([*self._common(tmp), "quality-evidence", "agent-debug", "--hermes-session", "nope"])

        self.assertEqual(missing[0], 2)
        self.assertIn("no Hermes state database", missing[2])
        self.assertEqual(unknown[0], 2)
        self.assertIn("no Hermes session nope", unknown[2])
        self.assertEqual((missing[1].strip(), unknown[1].strip()), ("", ""))


if __name__ == "__main__":
    unittest.main()
