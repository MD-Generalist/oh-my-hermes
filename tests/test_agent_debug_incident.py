"""The agent-debug incident: selection, bounded reading, references, hypotheses, receipts, export.

Every fixture is a temp Hermes home (or a temp JSON Lines session record) shaped
like Hermes' own rows. Prompt, argument, and tool-output text carries
credentials, a raw prompt marker, private absolute paths, and another session's
text, so a report, incident, or export that kept any of it would show. Every
expected value is derived by hand from the fixture rows written here.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
import tracemalloc
import unittest

from _cli_harness import run_cli
from _credential_fixtures import AWS_ACCESS_KEY_ID
from omh.quality.agent_debug_incident import (
    AgentDebugIncidentError,
    agent_debug_export_leaks,
    agent_debug_incident_errors,
    agent_failure_capture_errors,
    agent_failure_pattern_hypothesis_errors,
    bind_receipts,
    build_agent_debug_export,
    build_agent_debug_incident,
    contained_recovery_action_errors,
    format_agent_debug_incident,
    write_agent_debug_export,
)
from omh.quality.agent_debug_report import (
    AgentDebugReportError,
    agent_debug_reference_errors,
    build_agent_debug_report,
    format_agent_debug_report,
    parse_turn_range,
)


SESSION = "20260930_091500_c0ffee"
SIBLING = "20260930_091500_c0dead"
STRANGER = "20260929_080000_000001"
SHARED_PREFIX = "20260930_091500_c0"
T0 = 1790758500.0
PRIVATE_PATH = "/Users/alice/private/payroll.csv"
RAW_PROMPT = "RAW-PROMPT-MARKER please fix payroll"
UNRELATED = "UNRELATED-SESSION-TEXT"
SECRET_VALUES = (AWS_ACCESS_KEY_ID, "sk-live-0123456789abcdefSECRET", PRIVATE_PATH, RAW_PROMPT, UNRELATED, "/Users/alice")


def _calls(call_id: str, name: str, arguments: dict) -> str:
    return json.dumps([{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}])


PYTEST = {"command": f"pytest {PRIVATE_PATH}", "env": {"AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID}}
PATCH = {"path": PRIVATE_PATH, "old_string": "sk-live-0123456789abcdefSECRET", "new_string": "x"}


def _session_rows() -> list[tuple]:
    """(id, session, role, content, tool_call_id, tool_name, timestamp, tool_calls, summary)."""
    failed = json.dumps({"exit_code": 1, "error": None, "output": f"FAILED {PRIVATE_PATH} {AWS_ACCESS_KEY_ID}"})
    return [
        (1, SESSION, "user", RAW_PROMPT, None, None, T0 + 1, None, 0),
        (2, SESSION, "assistant", "", None, None, T0 + 2, _calls("c1", "terminal", PYTEST), 0),
        (3, SESSION, "tool", failed, "c1", "terminal", T0 + 3, None, 0),
        (4, SESSION, "assistant", "", None, None, T0 + 4, _calls("c2", "terminal", PYTEST), 0),
        (5, SESSION, "tool", failed, "c2", "terminal", T0 + 5, None, 0),
        (6, SESSION, "user", f"{RAW_PROMPT} again", None, None, T0 + 6, None, 0),
        (7, SESSION, "assistant", "", None, None, T0 + 7, _calls("c3", "terminal", {"command": "pytest -x"}), 0),
        (8, SESSION, "tool", json.dumps({"exit_code": 0, "output": "ok"}), "c3", "terminal", T0 + 8, None, 0),
        (9, SESSION, "user", f"[summary] {RAW_PROMPT}", None, None, T0 + 9, None, 1),
        (10, SESSION, "user", f"{RAW_PROMPT} third", None, None, T0 + 10, None, 0),
        (11, SESSION, "assistant", "", None, None, T0 + 11, _calls("c4", "patch", PATCH), 0),
        (12, SESSION, "tool", json.dumps({"success": False, "error": f"no match in {PRIVATE_PATH}"}), "c4", "patch", T0 + 12, None, 0),
        (13, SIBLING, "user", UNRELATED, None, None, T0 - 50, None, 0),
        (14, SIBLING, "tool", json.dumps({"exit_code": 3, "output": UNRELATED}), "c1", "terminal", T0 - 40, None, 0),
        (15, STRANGER, "user", UNRELATED, None, None, T0 - 9000, None, 0),
    ]


def _write_db(home: Path, *, full_schema: bool = True, rows: list[tuple] | None = None) -> Path:
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
                    (SESSION, "tui", T0, T0 + 12, T0 + 20, "tui_shutdown"),
                    (SIBLING, "cli", T0 - 60, T0 - 40, None, None),
                    (STRANGER, "cli", T0 - 9100, T0 - 9000, None, None),
                ],
            )
            data = _session_rows() if rows is None else rows
            if full_schema:
                connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", data)
            else:
                connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", [row[:7] for row in data])
    finally:
        connection.close()
    return path


def _write_record(path: Path, rows: list[tuple], *, extra_lines: list[bytes] = ()) -> Path:
    with path.open("wb") as handle:
        for message_id, session_id, role, content, call_id, tool_name, stamp, tool_calls, summary in rows:
            message = {"session_id": session_id, "role": role, "content": content, "timestamp": stamp}
            if call_id is not None:
                message["tool_call_id"] = call_id
            if tool_name is not None:
                message["tool_name"] = tool_name
            if role == "assistant":
                message["tool_calls"] = json.loads(tool_calls) if tool_calls else []
            message["_compressed_summary"] = summary
            handle.write(json.dumps(message).encode("utf-8") + b"\n")
        for line in extra_lines:
            handle.write(line)
    return path


class SelectionTests(unittest.TestCase):
    def test_exact_unique_prefix_and_ambiguous_prefix_and_source_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_db(home)
            before = path.read_bytes()
            exact = build_agent_debug_report(home, SESSION)
            prefix = build_agent_debug_report(home, "20260930_091500_c0f")
            with self.assertRaises(AgentDebugReportError) as ambiguous:
                build_agent_debug_report(home, SHARED_PREFIX)
            with self.assertRaisesRegex(AgentDebugReportError, "no Hermes session 20991231"):
                build_agent_debug_report(home, "20991231")
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual((exact["source"]["selection"], prefix["source"]["selection"]), ("exact", "unique_prefix"))
        self.assertEqual(prefix["session"]["id"], SESSION)
        self.assertEqual(prefix["findings"], exact["findings"])
        message = str(ambiguous.exception)
        self.assertIn("is ambiguous", message)
        self.assertIn(SESSION, message)
        self.assertIn(SIBLING, message)

    def test_turn_range_reads_only_those_turns(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_db(home)
            first = build_agent_debug_report(home, SESSION, turns=parse_turn_range("1"))
            later = build_agent_debug_report(home, SESSION, turns=parse_turn_range("2:"))
            third = build_agent_debug_report(home, SESSION, turns=parse_turn_range("3:3"))
            with self.assertRaisesRegex(AgentDebugReportError, "turn 4 is past the session's last turn \\(3\\)"):
                build_agent_debug_report(home, SESSION, turns=(4, None))
        # Turn 1 is rows 1-5 (the summary row 9 is not a turn); turn 2 is 6-9; turn 3 is 10-12.
        self.assertEqual(
            [item["finding_id"] for item in first["findings"]],
            ["tool_error:3", "identical_retry_after_error:3", "tool_error:5"],
        )
        self.assertEqual([item["finding_id"] for item in later["findings"]], ["compaction_boundary:9", "tool_error:12"])
        self.assertEqual([item["finding_id"] for item in third["findings"]], ["tool_error:12"])
        self.assertEqual(first["turns"], {"start": 1, "end": 1, "turn_count": 3})
        self.assertEqual(later["turns"], {"start": 2, "end": None, "turn_count": 3})

    def test_turn_range_text_is_refused_when_malformed(self) -> None:
        for text in ("0", "3:2", "a", "1:b"):
            with self.subTest(text=text), self.assertRaises(AgentDebugReportError):
                parse_turn_range(text)

    def test_a_record_with_two_sessions_needs_a_selector(self) -> None:
        with TemporaryDirectory() as tmp:
            record = _write_record(Path(tmp) / "session.jsonl", _session_rows())
            with self.assertRaisesRegex(AgentDebugReportError, "holds more than one session"):
                build_agent_debug_report(None, None, session_record=record)
            with self.assertRaisesRegex(AgentDebugReportError, "is ambiguous"):
                build_agent_debug_report(None, SHARED_PREFIX, session_record=record)
            report = build_agent_debug_report(None, "20260930_091500_c0ff", session_record=record)
            single = _write_record(Path(tmp) / "one.jsonl", [row for row in _session_rows() if row[1] == SESSION])
            only = build_agent_debug_report(None, None, session_record=single)
        self.assertEqual(report["source"]["selection"], "unique_prefix")
        self.assertEqual(only["source"]["selection"], "only_session")
        self.assertEqual(only["findings"], report["findings"])


class RecordCitationTests(unittest.TestCase):
    def test_a_record_finding_cites_record_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            record = _write_record(Path(tmp) / "session.jsonl", _session_rows())
            report = build_agent_debug_report(None, SESSION, session_record=record)
            text = format_agent_debug_report(report)
            self.assertEqual(agent_debug_reference_errors(report, session_record=record), [])
        self.assertEqual(report["source"]["locator"], "record_line")
        # One line per fixture row, so line numbers equal the row ids.
        self.assertEqual(
            [item["finding_id"] for item in report["findings"]],
            ["tool_error:3", "identical_retry_after_error:3", "tool_error:5", "compaction_boundary:9", "tool_error:12"],
        )
        self.assertIn("identical_retry_after_error:3  at session.jsonl:3,session.jsonl:5", text)
        self.assertNotIn(tmp, text)


class BudgetTests(unittest.TestCase):
    def test_an_oversized_cell_is_listed_unread_and_the_reading_is_incomplete(self) -> None:
        rows = _session_rows()
        huge = json.dumps({"exit_code": 9, "output": "x" * (2 * 1024 * 1024)})
        rows.append((16, SESSION, "tool", huge, "c9", "terminal", T0 + 13, None, 0))
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_db(home, rows=rows)
            report = build_agent_debug_report(home, SESSION, max_row_bytes=4096)
        budget = report["budget"]
        self.assertEqual((budget["oversized_rows"], budget["oversized_refs"], budget["complete"]), (1, [16], False))
        self.assertNotIn("tool_error:16", [item["finding_id"] for item in report["findings"]])
        # Every row read was cut at the budget inside SQLite (content plus tool_calls cells).
        self.assertLessEqual(budget["bytes_read"], budget["rows_read"] * 2 * 4096)
        self.assertLess(len(json.dumps(report)), 16 * 1024)
        incident = build_agent_debug_incident(report, observable="unspecified")
        capture = incident["agent_failure_capture"]
        self.assertFalse(capture["reading_complete"])
        self.assertIn("oversized_rows", [item["evidence"] for item in capture["unavailable"]])
        # Nothing may be ruled out by absence when rows were not read.
        for item in incident["agent_failure_pattern_hypothesis"]["hypotheses"]:
            self.assertNotEqual(item["status"], "ruled_out", item)

    def test_a_long_session_stops_at_the_row_budget(self) -> None:
        rows = [(1, SESSION, "user", RAW_PROMPT, None, None, T0, None, 0)]
        for index in range(2, 3002):
            rows.append((index, SESSION, "tool", json.dumps({"exit_code": 1}), f"c{index}", "terminal", T0 + index, None, 0))
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_db(home, rows=rows)
            report = build_agent_debug_report(home, SESSION, max_rows=100)
        self.assertEqual((report["budget"]["rows_read"], report["budget"]["row_limit_reached"]), (100, True))
        self.assertFalse(report["budget"]["complete"])
        self.assertEqual(report["finding_counts"]["tool_error"], 99)
        self.assertIn("row limit reached", format_agent_debug_report(report))

    def test_an_oversized_record_line_is_skipped_without_being_held_in_memory(self) -> None:
        line_bytes = 4 * 1024 * 1024
        oversized = b'{"session_id": "' + SESSION.encode() + b'", "role": "tool", "content": "' + b"y" * line_bytes + b'"}\n'
        with TemporaryDirectory() as tmp:
            record = _write_record(Path(tmp) / "session.jsonl", [r for r in _session_rows() if r[1] == SESSION], extra_lines=[oversized])
            tracemalloc.start()
            try:
                report = build_agent_debug_report(None, SESSION, session_record=record, max_row_bytes=8192)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        budget = report["budget"]
        self.assertEqual((budget["oversized_rows"], budget["oversized_refs"]), (1, [13]))
        self.assertGreaterEqual(budget["bytes_skipped"], line_bytes)
        self.assertLessEqual(budget["bytes_read"], budget["rows_read"] * 8192)
        self.assertLess(peak, line_bytes // 4)
        self.assertFalse(budget["complete"])

    def test_a_long_record_stops_at_the_line_budget(self) -> None:
        rows = [(index, SESSION, "tool", json.dumps({"exit_code": 1}), f"c{index}", "terminal", T0 + index, None, 0) for index in range(1, 501)]
        with TemporaryDirectory() as tmp:
            record = _write_record(Path(tmp) / "session.jsonl", rows)
            report = build_agent_debug_report(None, SESSION, session_record=record, max_rows=50)
        self.assertEqual((report["budget"]["rows_read"], report["budget"]["row_limit_reached"]), (50, True))
        self.assertEqual(report["finding_counts"]["tool_error"], 50)


class ReferenceTests(unittest.TestCase):
    def _mutated(self, statement: str, params: tuple) -> list[str]:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_db(home)
            report = build_agent_debug_report(home, SESSION)
            self.assertEqual(agent_debug_reference_errors(report, hermes_home=home), [])
            connection = sqlite3.connect(path)
            with connection:
                connection.execute(statement, params)
            connection.close()
            return agent_debug_reference_errors(report, hermes_home=home)

    def test_missing_foreign_stale_and_mismatched_references_are_refused(self) -> None:
        self.assertEqual(self._mutated("DELETE FROM messages WHERE id = ?", (12,)), ["tool_error:12 reference 12 is missing from the source"])
        self.assertEqual(
            self._mutated("UPDATE messages SET session_id = ? WHERE id = ?", (SIBLING, 12)),
            ["tool_error:12 reference 12 is foreign: it belongs to another session"],
        )
        self.assertEqual(
            self._mutated("UPDATE messages SET timestamp = ? WHERE id = ?", (T0 + 99, 12)),
            ["tool_error:12 reference 12 is stale: the row's timestamp changed since the report"],
        )
        self.assertEqual(
            self._mutated("UPDATE messages SET tool_call_id = ? WHERE id = ?", ("c99", 12)),
            ["tool_error:12 reference 12 is mismatched: the row is not the cited tool call"],
        )
        self.assertEqual(
            self._mutated("UPDATE messages SET _compressed_summary = 0 WHERE id = ?", (9,)),
            ["compaction_boundary:9 reference 9 is mismatched: the row is not a compaction summary"],
        )

    def test_a_changed_record_makes_every_reference_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            record = _write_record(Path(tmp) / "session.jsonl", _session_rows())
            report = build_agent_debug_report(None, SESSION, session_record=record)
            with record.open("ab") as handle:
                handle.write(b"{}\n")
            errors = agent_debug_reference_errors(report, session_record=record)
        self.assertEqual(len(errors), 1)
        self.assertIn("stale", errors[0])


class HypothesisTests(unittest.TestCase):
    def _incident(self, observable: str = "looping", **kwargs) -> tuple[dict, dict]:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_db(home, **kwargs)
            report = build_agent_debug_report(home, SESSION)
        return report, build_agent_debug_incident(report, observable=observable)

    def test_competing_hypotheses_carry_evidence_for_against_and_stay_unresolved(self) -> None:
        report, incident = self._incident("looping")
        hypothesis = incident["agent_failure_pattern_hypothesis"]
        summary = [
            (item["hypothesis_id"], item["pattern"], item["status"], item["confidence"], item["evidence_for"], item["evidence_against"])
            for item in hypothesis["hypotheses"]
        ]
        self.assertEqual(
            summary,
            [
                ("H1", "tool_error_retry_loop", "supported", "low", ["finding:identical_retry_after_error:3"], []),
                ("H2", "context_loss_after_compaction", "supported", "low", ["finding:compaction_boundary:9"], []),
                ("H3", "outside_recorded_evidence", "unresolved", "low", [], []),
            ],
        )
        self.assertEqual(hypothesis["hypotheses"][2]["unavailable_evidence"], ["unavailable:host_runtime"])
        self.assertTrue(all(item["observed"] is False for item in hypothesis["hypotheses"]))
        self.assertEqual((hypothesis["resolution"], hypothesis["leading_hypothesis"]), ("unresolved", "H1"))
        recovery = incident["contained_recovery_action"]
        self.assertEqual(recovery["action"], "change_arguments_before_next_retry")
        self.assertEqual(recovery["targets"], ["finding:identical_retry_after_error:3"])
        self.assertEqual((recovery["executed"], recovery["requires_approval"], recovery["reversible"]), (False, True, True))
        self.assertEqual(agent_debug_incident_errors(report, incident), [])

    def test_a_retry_that_succeeded_is_evidence_against_a_loop(self) -> None:
        rows = _session_rows()
        rows[4] = (5, SESSION, "tool", json.dumps({"exit_code": 0}), "c2", "terminal", T0 + 5, None, 0)
        _, incident = self._incident("looping", rows=rows)
        loop = incident["agent_failure_pattern_hypothesis"]["hypotheses"][0]
        self.assertEqual((loop["status"], loop["evidence_for"], loop["evidence_against"]), ("unresolved", [], ["finding:identical_retry_after_error:3"]))

    def test_an_absence_in_a_complete_reading_rules_a_hypothesis_out(self) -> None:
        rows = [row for row in _session_rows() if row[0] != 9]
        _, incident = self._incident("context_loss", rows=rows)
        hypothesis = incident["agent_failure_pattern_hypothesis"]
        first, second = hypothesis["hypotheses"]
        self.assertEqual((first["status"], first["confidence"], first["evidence_against"]), ("ruled_out", "none", ["absence:compaction_boundary"]))
        self.assertEqual(second["pattern"], "outside_recorded_evidence")
        self.assertEqual((hypothesis["resolution"], hypothesis["leading_hypothesis"]), ("single_candidate", None))
        self.assertEqual(incident["contained_recovery_action"]["action"], "collect_discriminating_evidence")

    def test_an_unchecked_kind_leaves_its_hypothesis_unresolved_with_the_gap_named(self) -> None:
        report, incident = self._incident("context_loss", full_schema=False)
        first = incident["agent_failure_pattern_hypothesis"]["hypotheses"][0]
        self.assertEqual((first["status"], first["evidence_against"]), ("unresolved", []))
        self.assertEqual(first["unavailable_evidence"], ["unavailable:compaction_boundary"])
        self.assertEqual(incident["agent_failure_pattern_hypothesis"]["resolution"], "unresolved")
        self.assertIn("compaction_boundary", [item["evidence"] for item in incident["agent_failure_capture"]["unavailable"]])
        self.assertIn("host_runtime", [item["evidence"] for item in incident["agent_failure_capture"]["unavailable"]])

    def test_the_validators_refuse_unsupported_or_overclaimed_hypotheses(self) -> None:
        report, incident = self._incident("looping")
        capture = incident["agent_failure_capture"]
        base = incident["agent_failure_pattern_hypothesis"]

        def errors_after(mutate) -> list[str]:
            artifact = copy.deepcopy(base)
            mutate(artifact)
            return agent_failure_pattern_hypothesis_errors(artifact, capture)

        def single(artifact):
            artifact["hypotheses"] = artifact["hypotheses"][:1]

        def foreign(artifact):
            artifact["hypotheses"][0]["evidence_for"] = ["finding:tool_error:14"]

        def absence_of_observed(artifact):
            artifact["hypotheses"][1]["evidence_against"] = ["absence:compaction_boundary"]

        def high(artifact):
            artifact["hypotheses"][0]["confidence"] = "high"

        def rule_out_outside(artifact):
            artifact["hypotheses"][2].update(status="ruled_out", confidence="none", evidence_against=["absence:tool_error"])

        def unsupported(artifact):
            artifact["hypotheses"][0]["evidence_for"] = []

        def hidden_competitor(artifact):
            artifact["resolution"] = "single_candidate"

        def observed(artifact):
            artifact["hypotheses"][0]["observed"] = True

        self.assertEqual(errors_after(single), ["at least two competing hypotheses are required"])
        self.assertIn("H1 cites finding:tool_error:14, which the capture did not observe", errors_after(foreign))
        self.assertIn("H2 cites absence:compaction_boundary, but the capture observed that kind", errors_after(absence_of_observed))
        self.assertIn("H1 claims high confidence while a competing hypothesis is open", errors_after(high))
        self.assertIn("H3 rules out evidence outside the record, which records cannot do", errors_after(rule_out_outside))
        self.assertIn("H1 is supported with no evidence for it", errors_after(unsupported))
        self.assertIn("resolution must be unresolved with 3 open hypotheses", errors_after(hidden_competitor))
        self.assertIn("H1 must be marked observed: false; a hypothesis is inferred", errors_after(observed))

        stale_capture = copy.deepcopy(capture)
        stale_capture["identity"]["report_sha256"] = "0" * 64
        self.assertIn(
            "identity.report_sha256 does not match the report: the capture is stale or for another report",
            agent_failure_capture_errors(stale_capture, report),
        )
        self.assertIn("capture_sha256 does not match the capture", agent_failure_pattern_hypothesis_errors(base, stale_capture))
        executed = dict(incident["contained_recovery_action"], executed=True)
        self.assertIn(
            "a contained recovery action is never executed here and always requires approval",
            contained_recovery_action_errors(executed, base),
        )


class ReceiptBindingTests(unittest.TestCase):
    def _report(self) -> dict:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_db(home)
            return build_agent_debug_report(home, SESSION)

    def _summary(self, units: list[dict], **fields) -> dict:
        summary = {
            "schema_version": "fanout_dispatch_summary/v1",
            "fanout_id": "fix-payroll",
            "observed_at": "2026-09-30T09:20:00Z",
            "contract_digest": "a" * 64,
            "units": units,
        }
        summary.update(fields)
        return summary

    def test_only_units_binding_all_five_identities_are_admitted(self) -> None:
        report = self._report()
        bound = {"unit_id": "api", "run_ref": "run-1", "origin_session_id": SESSION, "status": "failed", "exit_code": 1}
        receipts = [
            (
                "dispatch_summary.json",
                self._summary(
                    [
                        bound,
                        {"unit_id": "web", "run_ref": "run-1", "origin_session_id": SIBLING},
                        {"unit_id": "docs", "origin_session_id": SESSION},
                        {"unit_id": "ops", "run_ref": "run-1"},
                    ]
                ),
            ),
            ("no_digest.json", self._summary([dict(bound, unit_id="cli")], contract_digest=None)),
            ("old.json", self._summary([dict(bound, unit_id="db")], observed_at="2026-09-01T00:00:00Z")),
            ("unstamped.json", self._summary([dict(bound, unit_id="ui")], observed_at=None)),
            ("other.json", {"schema_version": "session_activity_receipt/v1"}),
        ]
        bound_receipts = bind_receipts(report, receipts)
        self.assertEqual(
            bound_receipts["accepted"],
            [
                {
                    "ref": "receipt:fanout_dispatch_summary/v1#fix-payroll/api",
                    "receipt": "dispatch_summary.json",
                    "schema_version": "fanout_dispatch_summary/v1",
                    "session_id": SESSION,
                    "run_id": "run-1",
                    "unit_id": "api",
                    "configuration_digest": "a" * 64,
                    "observed_at": datetime(2026, 9, 30, 9, 20, tzinfo=timezone.utc).timestamp(),
                    "status": "failed",
                    "exit_code": 1,
                }
            ],
        )
        self.assertEqual(
            [(item["receipt"], item["unit_id"], item["reason"]) for item in bound_receipts["rejected"]],
            [
                ("dispatch_summary.json", "web", "foreign_session"),
                ("dispatch_summary.json", "docs", "run_unbound"),
                ("dispatch_summary.json", "ops", "session_unbound"),
                ("no_digest.json", "cli", "configuration_unbound"),
                ("old.json", "db", "stale"),
                ("unstamped.json", "ui", "freshness_unbound"),
                ("other.json", None, "ineligible_schema"),
            ],
        )

    def test_one_run_and_unit_under_two_configurations_is_refused(self) -> None:
        report = self._report()
        unit = {"unit_id": "api", "run_ref": "run-1", "origin_session_id": SESSION}
        bound = bind_receipts(
            report, [("a.json", self._summary([unit])), ("b.json", self._summary([unit], contract_digest="b" * 64))]
        )
        self.assertEqual(bound["accepted"], [])
        self.assertEqual([item["reason"] for item in bound["rejected"]], ["identity_conflict", "identity_conflict"])

    def test_a_capture_that_admitted_an_unbound_receipt_is_refused(self) -> None:
        report = self._report()
        unit = {"unit_id": "api", "run_ref": "run-1", "origin_session_id": SESSION}
        incident = build_agent_debug_incident(report, receipts=[("a.json", self._summary([unit]))])
        capture = copy.deepcopy(incident["agent_failure_capture"])
        self.assertEqual(len(capture["receipts"]["accepted"]), 1)
        capture["receipts"]["accepted"][0]["configuration_digest"] = None
        self.assertEqual(
            agent_failure_capture_errors(capture, report),
            ["receipt receipt:fanout_dispatch_summary/v1#fix-payroll/api was admitted without binding configuration"],
        )


class RedactionAndExportTests(unittest.TestCase):
    def _payload(self, home: Path) -> dict:
        status, stdout, stderr = run_cli(
            ["--omh-home", str(home.parent / ".omh"), "--hermes-home", str(home), "quality-evidence", "agent-debug",
             "--hermes-session", SESSION, "--observable", "looping", "--json"],
            output_json=False,
        )
        self.assertEqual(status, 0, stderr)
        return json.loads(stdout)

    def test_nothing_private_reaches_the_report_incident_or_export(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_db(home)
            payload = self._payload(home)
            report = build_agent_debug_report(home, SESSION)
            incident = build_agent_debug_incident(report, observable="looping")
            text = format_agent_debug_report(report) + "\n" + format_agent_debug_incident(incident)
            package = build_agent_debug_export(payload, hermes_home=home)
            output = Path(tmp) / "out" / "incident.json"
            output.parent.mkdir()
            write_agent_debug_export(package, output, hermes_home=home)
            written = output.read_text(encoding="utf-8")
            mode = stat.S_IMODE(output.stat().st_mode)
        persisted = json.dumps(package) + written + json.dumps(incident) + text
        for value in SECRET_VALUES:
            with self.subTest(value=value):
                self.assertNotIn(value, persisted)
        self.assertNotIn(tmp, persisted)
        self.assertNotIn("path", package["report"]["source"])
        self.assertEqual(agent_debug_export_leaks(package), [])
        if os.name == "posix":
            self.assertEqual(mode, 0o600)

    def test_the_leak_scan_refuses_planted_private_material(self) -> None:
        package = {"report": {"source": {"label": "state.db"}}, "note": "ok"}
        self.assertEqual(agent_debug_export_leaks(package), [])
        cases = {
            "path": {"note": "see /Users/alice/private/payroll.csv"},
            "credential": {"note": AWS_ACCESS_KEY_ID},
            "raw key": {"content": "x"},
            "email": {"note": "alice@example.com"},
            "body": {"note": "line one\nline two"},
        }
        for name, planted in cases.items():
            with self.subTest(name=name):
                self.assertTrue(agent_debug_export_leaks({**package, **planted}))

    def test_export_is_separate_never_overwrites_never_writes_into_the_source_and_refuses_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            db = _write_db(home)
            payload = self._payload(home)
            saved = Path(tmp) / "reviewed.json"
            saved.write_text(json.dumps(payload), encoding="utf-8")
            before = db.read_bytes()
            listing = sorted(str(item.relative_to(tmp)) for item in Path(tmp).rglob("*"))
            common = ["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(home), "quality-evidence", "agent-debug-export", "--report", str(saved)]
            output = Path(tmp) / "incident.json"
            preview = run_cli([*common, "--output", str(output)], output_json=False)
            self.assertFalse(output.exists())
            self.assertEqual(sorted(str(item.relative_to(tmp)) for item in Path(tmp).rglob("*")), listing)
            written = run_cli([*common, "--output", str(output), "--confirm-export"], output_json=False)
            again = run_cli([*common, "--output", str(output), "--confirm-export"], output_json=False)
            inside = run_cli([*common, "--output", str(home / "incident.json"), "--confirm-export"], output_json=False)
            self.assertEqual(db.read_bytes(), before)
            connection = sqlite3.connect(db)
            with connection:
                connection.execute("UPDATE messages SET timestamp = ? WHERE id = 12", (T0 + 99,))
            connection.close()
            stale = run_cli([*common, "--output", str(Path(tmp) / "late.json"), "--confirm-export"], output_json=False)
            self.assertFalse((Path(tmp) / "late.json").exists())
            self.assertFalse((home / "incident.json").exists())
        self.assertEqual(preview[0], 0, preview[2])
        self.assertIn("Not written", preview[1])
        self.assertEqual(written[0], 0, written[2])
        self.assertIn("Nothing was uploaded, filed, or posted", written[1])
        self.assertEqual(again[0], 2)
        self.assertIn("never overwrites", again[2])
        self.assertEqual(inside[0], 2)
        self.assertIn("inside the Hermes home", inside[2])
        self.assertEqual(stale[0], 2)
        self.assertIn("is stale", stale[2])

    def test_a_tampered_payload_is_refused_before_anything_is_written(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_db(home)
            payload = self._payload(home)
            tampered = copy.deepcopy(payload)
            tampered["incident"]["contained_recovery_action"]["executed"] = True
            with self.assertRaisesRegex(AgentDebugIncidentError, "never executed here"):
                build_agent_debug_export(tampered, hermes_home=home)
            narrative = copy.deepcopy(payload)
            narrative["findings"][0]["narrative"] = "the agent was confused"
            with self.assertRaisesRegex(AgentDebugIncidentError, "outside the finding shape"):
                build_agent_debug_export(narrative, hermes_home=home)


class DocumentedExampleTests(unittest.TestCase):
    """docs/HARNESS_QUALITY.md shows this fixture's output; the page may not drift from the reader."""

    def test_the_documented_examples_are_this_fixtures_output(self) -> None:
        doc = (Path(__file__).resolve().parents[1] / "docs" / "HARNESS_QUALITY.md").read_text(encoding="utf-8")
        for full_schema, observable, heading in (
            (True, "looping", "### Example: evidence-backed"),
            (False, "context_loss", "### Example: unavailable"),
        ):
            with self.subTest(observable=observable), TemporaryDirectory() as tmp:
                home = Path(tmp) / ".hermes"
                _write_db(home, full_schema=full_schema)
                report = build_agent_debug_report(home, SESSION)
                rendered = format_agent_debug_report(report) + "\n" + format_agent_debug_incident(
                    build_agent_debug_incident(report, observable=observable)
                )
                section = doc[doc.index(heading) :]
                self.assertIn("```text\n" + rendered + "\n```", section)


class DiagnosisSideEffectTests(unittest.TestCase):
    def test_diagnosis_writes_nothing_anywhere(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            db = _write_db(home)
            before = db.read_bytes()
            listing = sorted(str(item.relative_to(tmp)) for item in Path(tmp).rglob("*"))
            status, stdout, stderr = run_cli(
                ["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(home), "quality-evidence", "agent-debug",
                 "--hermes-session", SESSION, "--observable", "looping"],
                output_json=False,
            )
            after = sorted(str(item.relative_to(tmp)) for item in Path(tmp).rglob("*"))
            self.assertEqual(db.read_bytes(), before)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(after, listing)
        self.assertIn("Contained recovery (proposed, requires approval, not executed)", stdout)
        self.assertIn("not performed: recovery, executor_reset, session_mutation", stdout)


if __name__ == "__main__":
    unittest.main()
