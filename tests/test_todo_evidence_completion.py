"""Contracts for closing a plan item by evidence rather than by a done mark.

The continuation rule stops a plan when every item is done or an item is
recorded blocked with its reason. A done mark is a declaration, so a run that
marked its items done in words -- with no command behind them -- ended its own
loop. These tests pin the record-backed half of the stop criterion:

* a done item whose evidence reference resolves to a recorded success closes;
* a done item with no reference, in a session that recorded commands, stays
  open as ``done_unverified`` and the turn-end directive names it;
* a forged reference -- an unknown id, a nonzero exit, a kind pointing at the
  wrong tool, a kind nothing resolves yet -- does not close;
* a store that exists and cannot be read is said, never taken as evidence;
* a blocked reason still stops the loop, on an open item or on a done one;
* records and sessions from before evidence existed keep their behaviour;
* the host's nudge budget still bounds the continuation.

Every fixture is a Hermes ``state.db`` built here with the columns the reader
queries; nothing reads the item text or a command's output.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import todo_evidence
from omh.plugin_bundle.omh.hooks import nudge_budget, verify_hooks
from omh.plugin_bundle.omh.runtime_reader import read_omh_todo
from omh.plugin_bundle.omh.todo_reconciliation import (
    DONE_UNVERIFIED,
    EVIDENCE_REASON_FAILED,
    EVIDENCE_REASON_NONE,
    EVIDENCE_REASON_UNREADABLE,
    EVIDENCE_REASON_UNRESOLVED,
    TODO_CONTINUATION_RULE,
    TODO_EVIDENCE_RULE,
    open_plan_position,
    open_todo_reminder,
    unverified_done_items,
)
from omh.plugin_bundle.omh.todo_store import (
    TodoValidationError,
    attach_done_evidence,
    build_todo_record,
    todo_path,
    write_todo,
)
from omh.plugin_bundle.omh.tools.todo_tool import omh_todo_handler

SESSION = "20260928_101500_abc123"
PARENT = "20260928_090000_parent"
T0 = 1_790_000_000.0


def _terminal(exit_code, *, error=None):
    result = {"output": "", "exit_code": exit_code}
    if error:
        result["error"] = error
    return json.dumps(result)


def build_state_db(
    hermes: Path, rows: list[tuple], *, sessions: tuple[tuple[str, str | None], ...] = ((SESSION, None),)
) -> Path:
    """``rows`` are ``(session_id, tool_name, tool_call_id, content, disposition, timestamp)``."""
    hermes.mkdir(parents=True, exist_ok=True)
    path = hermes / "state.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, "
            "effect_disposition TEXT, timestamp REAL)"
        )
        connection.executemany("INSERT INTO sessions VALUES (?, ?)", sessions)
        connection.executemany(
            "INSERT INTO messages (session_id, role, tool_name, tool_call_id, content, "
            "effect_disposition, timestamp) VALUES (?, 'tool', ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return path


def add_rows(hermes: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(hermes / "state.db")
    try:
        connection.executemany(
            "INSERT INTO messages (session_id, role, tool_name, tool_call_id, content, "
            "effect_disposition, timestamp) VALUES (?, 'tool', ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def evidence(kind: str, ref: str) -> dict:
    return {"kind": kind, "ref": ref}


class _PlanHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "omh"
        self.hermes = root / "hermes"
        self.hermes.mkdir(parents=True, exist_ok=True)
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)

    def write_plan(self, items: list[dict], *, session_ref: str = SESSION) -> dict:
        record = build_todo_record("plan", items, source="test", session_ref=session_ref)
        _ = write_todo(self.home, record)
        return record

    def todo(self) -> dict:
        return read_omh_todo(self.home, self.hermes, session_ref=SESSION)

    def unverified(self) -> list[dict]:
        return unverified_done_items(self.todo(), hermes_home=str(self.hermes), session_ref=SESSION)

    def fire(self, attempt: int = 0):
        return verify_hooks.pre_verify(
            session_id=SESSION,
            coding=True,
            attempt=attempt,
            changed_paths=["src/example.py"],
            omh_home=str(self.home),
            hermes_home=str(self.hermes),
        )


class EvidenceClosesItemsTest(_PlanHomeTest):
    def test_done_with_an_exit_zero_command_closes_the_item(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_ok", _terminal(0), None, T0)])
        self.write_plan(
            [
                {"text": "land the fix", "state": "done", "evidence": evidence("tool_call", "toolu_ok")},
            ]
        )

        self.assertEqual(self.unverified(), [])
        self.assertIsNone(open_plan_position(self.todo(), self.unverified()))
        self.assertIsNone(self.fire())

    def test_a_landed_write_and_a_successful_patch_close_as_file_writes(self):
        build_state_db(
            self.hermes,
            [
                (SESSION, "write_file", "toolu_w", json.dumps({"bytes_written": 12}), None, T0),
                (SESSION, "patch", "toolu_p", json.dumps({"success": True, "diff": ""}), None, T0 + 1),
            ],
        )
        self.write_plan(
            [
                {"text": "write it", "state": "done", "evidence": evidence("file_write", "toolu_w")},
                {"text": "patch it", "state": "done", "evidence": evidence("file_write", "toolu_p")},
            ]
        )

        self.assertEqual(self.unverified(), [])
        self.assertIsNone(self.fire())

    def test_evidence_recorded_before_a_compaction_resolves_for_the_continued_session(self):
        build_state_db(
            self.hermes,
            [(PARENT, "terminal", "toolu_before", _terminal(0), None, T0)],
            sessions=((PARENT, None), (SESSION, PARENT)),
        )
        self.write_plan(
            [{"text": "land the fix", "state": "done", "evidence": evidence("tool_call", "toolu_before")}]
        )

        self.assertEqual(self.unverified(), [])


class DoneWithoutEvidenceTest(_PlanHomeTest):
    def test_done_without_evidence_stays_open_and_the_directive_names_it(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_any", _terminal(0), None, T0)])
        self.write_plan(
            [
                {"text": "land the fix", "state": "done", "evidence": evidence("tool_call", "toolu_any")},
                {"text": "run the suite", "state": "done"},
            ]
        )

        unverified = self.unverified()
        self.assertEqual(
            unverified,
            [{"item": 2, "state": DONE_UNVERIFIED, "text": "run the suite", "reason": EVIDENCE_REASON_NONE}],
        )
        self.assertEqual(open_plan_position(self.todo(), unverified), (1, 2))
        result = self.fire()
        self.assertEqual(result["action"], "continue")
        message = result["message"]
        self.assertIn("[OMH plan todo] 1/2 done · next: run the suite", message)
        self.assertIn(f"{DONE_UNVERIFIED}: item 2 ({EVIDENCE_REASON_NONE})", message)
        self.assertIn(TODO_CONTINUATION_RULE, message)
        self.assertIn(TODO_EVIDENCE_RULE, message)

    def test_the_per_turn_line_names_it_too(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_any", _terminal(0), None, T0)])
        self.write_plan([{"text": "land the fix", "state": "done"}, {"text": "report", "state": "active"}])

        line = open_todo_reminder(omh_home=str(self.home), hermes_home=str(self.hermes), session_ref=SESSION)

        self.assertIn("[OMH plan todo] 0/2 done · active: report", line)
        self.assertIn(f"{DONE_UNVERIFIED}: item 1 ({EVIDENCE_REASON_NONE})", line)
        self.assertIn(TODO_EVIDENCE_RULE, line)

    def test_a_conversational_session_keeps_the_done_mark(self):
        # A store exists but this session recorded no evidence-capable call:
        # nothing a command could close, so the done mark is not second-guessed.
        build_state_db(self.hermes, [("another-session", "terminal", "toolu_x", _terminal(0), None, T0)])
        self.write_plan([{"text": "explain the design", "state": "done"}])

        self.assertEqual(self.unverified(), [])
        self.assertIsNone(self.fire())

    def test_an_unreadable_store_is_said_and_not_taken_as_evidence(self):
        (self.hermes / "state.db").write_bytes(b"this is not a sqlite database at all" * 8)
        self.write_plan([{"text": "land the fix", "state": "done"}])

        self.assertEqual(
            [entry["reason"] for entry in self.unverified()], [EVIDENCE_REASON_UNREADABLE]
        )
        self.assertIn(f"({EVIDENCE_REASON_UNREADABLE})", self.fire()["message"])


class ForgedEvidenceTest(_PlanHomeTest):
    def setUp(self) -> None:
        super().setUp()
        build_state_db(
            self.hermes,
            [
                (SESSION, "terminal", "toolu_fail", _terminal(1), None, T0),
                (SESSION, "terminal", "toolu_ok", _terminal(0), None, T0 + 1),
                (SESSION, "terminal", "toolu_errored", _terminal(0, error="denied"), None, T0 + 2),
                (SESSION, "terminal", "toolu_no_effect", _terminal(0), "none", T0 + 3),
                (SESSION, "terminal", "toolu_yielded", _terminal(None), None, T0 + 4),
                ("someone-else", "terminal", "toolu_foreign", _terminal(0), None, T0 + 5),
            ],
        )

    def reason_for(self, ref: dict) -> str:
        self.write_plan([{"text": "land the fix", "state": "done", "evidence": ref}])
        unverified = self.unverified()
        self.assertEqual(len(unverified), 1, unverified)
        return unverified[0]["reason"]

    def test_an_unknown_id_does_not_close(self):
        self.assertEqual(self.reason_for(evidence("tool_call", "toolu_invented")), EVIDENCE_REASON_UNRESOLVED)

    def test_a_nonzero_exit_does_not_close(self):
        self.assertEqual(self.reason_for(evidence("tool_call", "toolu_fail")), EVIDENCE_REASON_FAILED)

    def test_an_error_field_or_a_no_effect_disposition_does_not_close(self):
        self.assertEqual(self.reason_for(evidence("tool_call", "toolu_errored")), EVIDENCE_REASON_FAILED)
        self.assertEqual(self.reason_for(evidence("tool_call", "toolu_no_effect")), EVIDENCE_REASON_FAILED)

    def test_a_command_never_observed_finishing_does_not_close(self):
        self.assertEqual(self.reason_for(evidence("tool_call", "toolu_yielded")), EVIDENCE_REASON_UNRESOLVED)

    def test_another_sessions_call_does_not_close(self):
        self.assertEqual(self.reason_for(evidence("tool_call", "toolu_foreign")), EVIDENCE_REASON_UNRESOLVED)

    def test_a_kind_pointing_at_the_wrong_tool_does_not_close(self):
        self.assertEqual(self.reason_for(evidence("file_write", "toolu_ok")), EVIDENCE_REASON_UNRESOLVED)

    def test_kinds_nothing_resolves_yet_keep_the_item_open(self):
        for ref in (
            evidence("pr", "rlaope/oh-my-hermes#1930"),
            evidence("ci_run", "18123456789"),
            evidence("team_check", "team-7/unit-2/attempt-3/check"),
        ):
            with self.subTest(kind=ref["kind"]):
                self.assertEqual(self.reason_for(ref), EVIDENCE_REASON_UNRESOLVED)


class BlockedReasonStopsTheLoopTest(_PlanHomeTest):
    def setUp(self) -> None:
        super().setUp()
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_ok", _terminal(0), None, T0)])

    def test_a_blocked_next_item_stops_the_plan_line_despite_unverified_done_items(self):
        self.write_plan(
            [
                {"text": "land the fix", "state": "done"},
                {"text": "merge", "state": "pending", "blocked_reason": "waiting on the owner's review"},
            ]
        )

        self.assertTrue(self.unverified())
        self.assertIsNone(self.fire())

    def test_a_done_item_carrying_a_reason_is_closed_as_skipped(self):
        self.write_plan(
            [
                {"text": "land the fix", "state": "done", "evidence": evidence("tool_call", "toolu_ok")},
                {"text": "demo video", "state": "done", "blocked_reason": "no recorder on this host"},
            ]
        )

        self.assertEqual(self.unverified(), [])
        self.assertIsNone(self.fire())


class LegacyCompatibilityTest(_PlanHomeTest):
    def test_a_record_written_before_evidence_existed_loads_and_projects_unchanged(self):
        legacy = {
            "schema_version": "omh_todo/v1",
            "title": "plan",
            "source": "omh_todo",
            "updated_at": build_todo_record("t", [{"text": "x"}], source="s")["updated_at"],
            "session_ref": SESSION,
            "items": [
                {"text": "land the fix", "state": "done"},
                {"text": "report", "state": "active"},
            ],
            "claim_boundary": "legacy",
        }
        destination = todo_path(self.home, SESSION)
        destination.parent.mkdir(parents=True)
        destination.write_text(json.dumps(legacy), encoding="utf-8", newline="\n")

        todo = self.todo()

        self.assertEqual(todo["status"], "established")
        self.assertEqual(
            todo["items"], [{"text": "land the fix", "state": "done"}, {"text": "report", "state": "active"}]
        )
        # No session store: the done mark counts exactly as it always did.
        self.assertEqual(self.unverified(), [])
        self.assertIn("[OMH plan todo] 1/2 done · next: report", self.fire()["message"])

    def test_a_record_without_evidence_is_byte_identical_to_before(self):
        record = build_todo_record("plan", [{"text": "a", "state": "done"}], source="s", session_ref=SESSION)

        self.assertEqual(record["items"], [{"text": "a", "state": "done"}])

    def test_a_finished_plan_without_a_store_stays_finished(self):
        self.write_plan([{"text": "a", "state": "done"}, {"text": "b", "state": "done"}])

        self.assertIsNone(self.fire())
        self.assertIsNone(open_plan_position(self.todo(), self.unverified()))


class NudgeBudgetBoundsTheLoopTest(_PlanHomeTest):
    def test_a_plan_that_does_not_move_gets_one_nudge_per_turn(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_ok", _terminal(0), None, T0)])
        self.write_plan([{"text": "a", "state": "done"}, {"text": "b", "state": "done"}])

        self.assertEqual(self.fire(attempt=0)["action"], "continue")
        # Nothing was written since: the host's later attempts are refused.
        self.assertIsNone(self.fire(attempt=1))
        self.assertIsNone(self.fire(attempt=2))


class StoreContractTest(unittest.TestCase):
    def test_evidence_is_refused_on_an_open_item(self):
        with self.assertRaisesRegex(TodoValidationError, "only on a done item"):
            build_todo_record(
                "plan", [{"text": "a", "state": "active", "evidence": evidence("tool_call", "toolu_ok")}],
                source="s",
            )

    def test_evidence_must_be_a_known_kind_in_its_shape(self):
        for bad in (
            "toolu_ok",
            evidence("screenshot", "toolu_ok"),
            evidence("tool_call", "has spaces"),
            evidence("team_check", "team-7/unit-2/check"),
            evidence("tool_call", "x" * 129),
        ):
            with self.subTest(bad=bad), self.assertRaises(TodoValidationError):
                build_todo_record("plan", [{"text": "a", "state": "done", "evidence": bad}], source="s")

    def test_set_attaches_the_observed_call_only_to_newly_done_items(self):
        observed = evidence("tool_call", "toolu_new")
        prior = [
            {"text": "verified", "state": "done", "evidence": evidence("tool_call", "toolu_old")},
            {"text": "claimed", "state": "done"},
            {"text": "next", "state": "active"},
        ]
        sent = [
            {"text": "verified", "state": "done"},
            {"text": "claimed", "state": "done"},
            {"text": "next", "state": "done"},
            {"text": "explicit", "state": "done", "evidence": evidence("file_write", "toolu_w")},
        ]

        attached = attach_done_evidence(sent, prior_items=prior, observed=observed)

        self.assertEqual(
            [item.get("evidence") for item in attached],
            [evidence("tool_call", "toolu_old"), None, observed, evidence("file_write", "toolu_w")],
        )


class ToolAttachesEvidenceTest(_PlanHomeTest):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"OMH_HOME": str(self.home), "HERMES_HOME": str(self.hermes)})
        env.start()
        self.addCleanup(env.stop)
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_setup", _terminal(0), None, T0)])

    def call(self, args: dict) -> dict:
        return json.loads(omh_todo_handler(args, session_id=SESSION))

    def stored_items(self) -> list[dict]:
        return json.loads(todo_path(self.home, SESSION).read_text(encoding="utf-8"))["items"]

    def test_advance_records_the_command_that_ran_since_the_last_plan_write(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}, {"text": "ship"}]})
        add_rows(self.hermes, [(SESSION, "terminal", "toolu_suite", _terminal(0), None, 4_000_000_000.0)])

        result = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertEqual(result["status"], "written")
        self.assertEqual(self.stored_items()[0]["evidence"], evidence("tool_call", "toolu_suite"))
        self.assertNotIn("done_unverified", result)

    def test_a_done_mark_with_no_command_since_the_last_write_is_reported_unverified(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}, {"text": "ship"}]})

        result = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertNotIn("evidence", self.stored_items()[0])
        self.assertEqual(
            result["done_unverified"],
            [{"item": 1, "state": DONE_UNVERIFIED, "reason": EVIDENCE_REASON_NONE}],
        )

    def test_a_failed_command_is_recorded_and_a_passing_rerun_closes_a_finished_plan(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}]})
        add_rows(self.hermes, [(SESSION, "terminal", "toolu_red", _terminal(1), None, 4_000_000_000.0)])
        first = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})
        self.assertEqual(first["done_unverified"][0]["reason"], EVIDENCE_REASON_FAILED)

        add_rows(self.hermes, [(SESSION, "terminal", "toolu_green", _terminal(0), None, 4_000_000_001.0)])
        # Every item says done, and the plan still takes the done write that closes it.
        second = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertEqual(second["status"], "written")
        self.assertEqual(self.stored_items()[0]["evidence"], evidence("tool_call", "toolu_green"))
        self.assertNotIn("done_unverified", second)

    def test_moving_an_item_out_of_done_drops_its_evidence(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}, {"text": "ship"}]})
        add_rows(self.hermes, [(SESSION, "terminal", "toolu_suite", _terminal(0), None, 4_000_000_000.0)])
        self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "active"})

        self.assertNotIn("evidence", self.stored_items()[0])

    def test_a_finished_plan_still_refuses_a_move_out_of_done(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "done"}]})

        result = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "active"})

        self.assertEqual(result["status"], "invalid_todo")
        self.assertIn("this plan is finished", result["error"])


class ReaderNeverRaisesTest(unittest.TestCase):
    def test_a_store_without_the_expected_tables_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp)
            sqlite3.connect(hermes / "state.db").close()

            reading = todo_evidence.evidence_reading(hermes, SESSION, [evidence("tool_call", "toolu_ok")])

        self.assertEqual(reading["store"], todo_evidence.STORE_UNREADABLE)

    def test_no_store_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            reading = todo_evidence.evidence_reading(tmp, SESSION, [])

        self.assertEqual(reading["store"], todo_evidence.STORE_ABSENT)


if __name__ == "__main__":
    unittest.main()
