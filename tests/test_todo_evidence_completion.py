"""Contracts for closing a plan item by evidence rather than by a done mark.

The continuation rule stops a plan when every item is done or an item is
recorded blocked with its reason. A done mark is a declaration, so a run could
mark an item done over a failed command, or tick several items off one
command, and end its own loop. These tests pin the record-backed half of the
stop criterion:

* a done item whose bound call resolves to a recorded success closes;
* OMH binds evidence itself: a reference the writer sends is ignored, a
  reference two items share closes one, and a call from before the item's
  window closes nothing;
* one recorded call closes at most one item, and a plan's first declaration
  cannot be closed by anything the session ran before it;
* a done item whose window holds commands but no call of its own stays open
  as ``done_unverified`` and the turn-end directive names it in plain words;
  a done item whose window holds no command at all is conversational and
  closes;
* a forged reference -- an unknown id, a nonzero exit, a kind pointing at the
  wrong tool, a kind nothing resolves yet, a rewound row -- does not close;
* a store that exists and cannot be read is reported and never taken as
  evidence, and it does not drive the loop;
* a blocked reason still stops the loop, on an open item or on a done one;
* items done before evidence existed count as done;
* the host's nudge budget still bounds the continuation, and a no-op
  re-advance neither restamps the plan nor buys another nudge.

Every fixture is a Hermes ``state.db`` built here with the columns the reader
queries; nothing reads the item text or a command's output.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
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
    bind_done_items,
    build_todo_record,
    todo_path,
    write_todo,
)
from omh.plugin_bundle.omh.tools.todo_tool import omh_todo_handler

SESSION = "20260928_101500_abc123"
PARENT = "20260928_090000_parent"
T0 = 1_790_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


WINDOW = {"window_start": iso(T0 - 60), "done_at": iso(T0 + 60)}


def _terminal(exit_code, *, error=None):
    result = {"output": "", "exit_code": exit_code}
    if error:
        result["error"] = error
    return json.dumps(result)


def build_state_db(
    hermes: Path, rows: list[tuple], *, sessions: tuple[tuple[str, str | None], ...] = ((SESSION, None),)
) -> Path:
    """``rows`` are ``(session_id, tool_name, tool_call_id, content, disposition, timestamp[, active, compacted])``."""
    hermes.mkdir(parents=True, exist_ok=True)
    path = hermes / "state.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, "
            "effect_disposition TEXT, timestamp REAL, active INTEGER NOT NULL DEFAULT 1, "
            "compacted INTEGER NOT NULL DEFAULT 0)"
        )
        connection.executemany("INSERT INTO sessions VALUES (?, ?)", sessions)
        connection.commit()
    finally:
        connection.close()
    add_rows(hermes, rows)
    return path


def add_rows(hermes: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(hermes / "state.db")
    try:
        for row in rows:
            active, compacted = (row[6], row[7]) if len(row) > 6 else (1, 0)
            connection.execute(
                "INSERT INTO messages (session_id, role, tool_name, tool_call_id, content, "
                "effect_disposition, timestamp, active, compacted) VALUES (?, 'tool', ?, ?, ?, ?, ?, ?, ?)",
                (*row[:6], active, compacted),
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

    def reasons(self) -> list[tuple[int, str]]:
        return [(entry["item"], entry["reason"]) for entry in self.unverified()]

    def fire(self, attempt: int = 0):
        return verify_hooks.pre_verify(
            session_id=SESSION,
            coding=True,
            attempt=attempt,
            changed_paths=["src/example.py"],
            omh_home=str(self.home),
            hermes_home=str(self.hermes),
        )


def done(text: str, ref: dict | None = None, **window) -> dict:
    item = {"text": text, "state": "done", **(window or WINDOW)}
    if ref is not None:
        item["evidence"] = ref
    return item


class EvidenceClosesItemsTest(_PlanHomeTest):
    def test_done_with_an_exit_zero_command_closes_the_item(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_ok", _terminal(0), None, T0)])
        self.write_plan([done("land the fix", evidence("tool_call", "toolu_ok"))])

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
            [done("write it", evidence("file_write", "toolu_w")), done("patch it", evidence("file_write", "toolu_p"))]
        )

        self.assertEqual(self.unverified(), [])
        self.assertIsNone(self.fire())

    def test_evidence_recorded_before_a_compaction_resolves_for_the_continued_session(self):
        build_state_db(
            self.hermes,
            [(PARENT, "terminal", "toolu_before", _terminal(0), None, T0)],
            sessions=((PARENT, None), (SESSION, PARENT)),
        )
        self.write_plan([done("land the fix", evidence("tool_call", "toolu_before"))])

        self.assertEqual(self.unverified(), [])

    def test_a_row_a_compaction_folded_still_resolves(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_folded", _terminal(0), None, T0, 0, 1)])
        self.write_plan([done("land the fix", evidence("tool_call", "toolu_folded"))])

        self.assertEqual(self.unverified(), [])


class WindowTest(_PlanHomeTest):
    def test_commands_in_the_window_and_none_bound_leave_the_item_open_and_named(self):
        build_state_db(
            self.hermes,
            [
                (SESSION, "terminal", "toolu_a", _terminal(0), None, T0),
                (SESSION, "terminal", "toolu_b", _terminal(0), None, T0 + 1),
            ],
        )
        self.write_plan([done("land the fix", evidence("tool_call", "toolu_a")), done("run the suite")])

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
        self.assertIn("item 2 is marked done, but commands ran but none was recorded for it", message)
        self.assertIn(TODO_CONTINUATION_RULE, message)
        self.assertIn(TODO_EVIDENCE_RULE, message)

    def test_the_line_uses_plain_words_not_record_codes(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_red", _terminal(2), None, T0)])
        self.write_plan([done("land the fix", evidence("tool_call", "toolu_red"))])

        message = self.fire()["message"]

        self.assertIn("the command recorded for it failed", message)
        for code in (DONE_UNVERIFIED, EVIDENCE_REASON_FAILED, EVIDENCE_REASON_NONE):
            self.assertNotIn(code, message)

    def test_the_per_turn_line_names_it_too(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_any", _terminal(1), None, T0)])
        self.write_plan(
            [done("land the fix", evidence("tool_call", "toolu_any")), {"text": "report", "state": "active"}]
        )

        line = open_todo_reminder(omh_home=str(self.home), hermes_home=str(self.hermes), session_ref=SESSION)

        self.assertIn("[OMH plan todo] 0/2 done · active: report", line)
        self.assertIn("item 1 is marked done, but the command recorded for it failed", line)
        self.assertIn(TODO_EVIDENCE_RULE, line)

    def test_an_item_whose_own_window_holds_no_command_is_conversational_and_closes(self):
        # Commands ran for the first item; the second item's window is later
        # and empty, so it is not held open by the session's earlier work.
        # Commands after the window closed belong to later work, not to it.
        build_state_db(
            self.hermes,
            [
                (SESSION, "terminal", "toolu_a", _terminal(0), None, T0),
                (SESSION, "terminal", "toolu_later", _terminal(0), None, T0 + 300),
            ],
        )
        self.write_plan(
            [
                done("land the fix", evidence("tool_call", "toolu_a")),
                done("explain the design", window_start=iso(T0 + 100), done_at=iso(T0 + 200)),
            ]
        )

        self.assertEqual(self.unverified(), [])
        self.assertIsNone(self.fire())

    def test_a_call_from_before_the_items_window_does_not_close(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_old", _terminal(0), None, T0)])
        self.write_plan(
            [done("land the fix", evidence("tool_call", "toolu_old"), window_start=iso(T0 + 1), done_at=iso(T0 + 9))]
        )

        self.assertEqual(self.reasons(), [(1, EVIDENCE_REASON_UNRESOLVED)])

    def test_a_reference_two_items_share_closes_only_the_first(self):
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_one", _terminal(0), None, T0)])
        record = self.write_plan([done("a"), done("b")])
        # The writer refuses a shared reference, so the shape is reached by a
        # hand edit; the reader must still close only one item with it.
        record["items"][0]["evidence"] = evidence("tool_call", "toolu_one")
        record["items"][1]["evidence"] = evidence("tool_call", "toolu_one")
        todo_path(self.home, SESSION).write_text(json.dumps(record), encoding="utf-8", newline="\n")

        self.assertEqual(self.reasons(), [(2, EVIDENCE_REASON_UNRESOLVED)])


class UnreadableStoreTest(_PlanHomeTest):
    def test_an_unreadable_store_is_reported_never_closes_and_does_not_drive(self):
        (self.hermes / "state.db").write_bytes(b"this is not a sqlite database at all" * 8)
        self.write_plan([done("land the fix")])

        self.assertEqual(self.reasons(), [(1, EVIDENCE_REASON_UNREADABLE)])
        # Not closed by evidence, and not a reason to keep the turn going.
        self.assertIsNone(open_plan_position(self.todo(), self.unverified()))
        self.assertIsNone(self.fire())

    def test_it_is_said_in_a_line_that_renders_for_other_work(self):
        (self.hermes / "state.db").write_bytes(b"this is not a sqlite database at all" * 8)
        self.write_plan([done("land the fix"), {"text": "report", "state": "active"}])

        message = self.fire()["message"]

        self.assertIn("[OMH plan todo] 1/2 done · next: report", message)
        self.assertIn("the session record could not be read", message)
        self.assertNotIn(TODO_EVIDENCE_RULE, message)


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
                (SESSION, "terminal", "toolu_rewound", _terminal(0), None, T0 + 6, 0, 0),
            ],
        )

    def reason_for(self, ref: dict) -> str:
        self.write_plan([done("land the fix", ref)])
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

    def test_a_rewound_call_does_not_close(self):
        self.assertEqual(self.reason_for(evidence("tool_call", "toolu_rewound")), EVIDENCE_REASON_UNRESOLVED)

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
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_red", _terminal(1), None, T0)])

    def test_a_blocked_next_item_stops_the_plan_line_despite_unverified_done_items(self):
        self.write_plan(
            [
                done("land the fix", evidence("tool_call", "toolu_red")),
                {"text": "merge", "state": "pending", "blocked_reason": "waiting on the owner's review"},
            ]
        )

        self.assertTrue(self.unverified())
        self.assertIsNone(self.fire())

    def test_a_done_item_carrying_a_reason_is_closed_as_skipped(self):
        self.write_plan(
            [{**done("land the fix", evidence("tool_call", "toolu_red")), "blocked_reason": "flaky host, see #12"}]
        )

        self.assertEqual(self.unverified(), [])
        self.assertIsNone(self.fire())


class ItemsDoneBeforeEvidenceExistedTest(_PlanHomeTest):
    def test_a_pre_evidence_record_loads_projects_unchanged_and_its_done_items_still_count(self):
        # A session that has run commands, and a record written before items
        # carried any binding: its done items are grandfathered, not re-opened.
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_any", _terminal(1), None, time.time())])
        legacy = {
            "schema_version": "omh_todo/v1",
            "title": "plan",
            "source": "omh_todo",
            "updated_at": build_todo_record("t", [{"text": "x"}], source="s")["updated_at"],
            "session_ref": SESSION,
            "items": [
                {"text": "land the fix", "state": "done"},
                {"text": "run the suite", "state": "done"},
            ],
            "claim_boundary": "legacy",
        }
        destination = todo_path(self.home, SESSION)
        destination.parent.mkdir(parents=True)
        destination.write_text(json.dumps(legacy), encoding="utf-8", newline="\n")

        todo = self.todo()

        self.assertEqual(todo["status"], "all_done")
        self.assertEqual(
            todo["items"],
            [{"text": "land the fix", "state": "done"}, {"text": "run the suite", "state": "done"}],
        )
        self.assertEqual(self.unverified(), [])
        self.assertIsNone(self.fire())

    def test_a_record_without_bindings_is_byte_identical_to_before(self):
        record = build_todo_record("plan", [{"text": "a", "state": "done"}], source="s", session_ref=SESSION)

        self.assertEqual(record["items"], [{"text": "a", "state": "done"}])


class StoreContractTest(unittest.TestCase):
    def test_done_at_is_refused_on_an_open_item_and_the_sticky_fields_are_not(self):
        with self.assertRaisesRegex(TodoValidationError, "only on a done item"):
            build_todo_record("plan", [{"text": "a", "state": "active", "done_at": iso(T0)}], source="s")
        record = build_todo_record(
            "plan",
            [{"text": "a", "state": "active", "evidence": evidence("tool_call", "toolu_red"), "window_start": iso(T0)}],
            source="s",
        )
        self.assertEqual(record["items"][0]["evidence"], evidence("tool_call", "toolu_red"))

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

    def test_one_reference_cannot_be_bound_to_two_items(self):
        shared = evidence("tool_call", "toolu_one")
        with self.assertRaisesRegex(TodoValidationError, "already bound"):
            build_todo_record(
                "plan",
                [{"text": "a", "state": "done", "evidence": shared}, {"text": "b", "state": "done", "evidence": shared}],
                source="s",
            )

    def test_bind_ignores_what_the_writer_sent_and_gives_each_call_to_one_item(self):
        opened = iso(T0)
        calls = [
            {"evidence": evidence("tool_call", "toolu_1"), "at": T0 + 1},
            {"evidence": evidence("tool_call", "toolu_2"), "at": T0 + 2},
        ]
        prior = [
            {"text": "verified", "state": "done", "evidence": evidence("tool_call", "toolu_old"), **WINDOW},
            {"text": "grandfathered", "state": "done"},
            {"text": "next", "state": "active", "window_start": opened},
            {"text": "then", "state": "pending", "window_start": opened},
            {"text": "and then", "state": "pending", "window_start": opened},
            {"text": "reopened", "state": "done", "evidence": evidence("tool_call", "toolu_red"), **WINDOW},
        ]
        sent = [
            {"text": "verified", "state": "done", "evidence": evidence("tool_call", "toolu_forged")},
            {"text": "grandfathered", "state": "done"},
            {"text": "next", "state": "done", "evidence": evidence("tool_call", "toolu_old")},
            {"text": "then", "state": "done"},
            {"text": "and then", "state": "done"},
            {"text": "reopened", "state": "pending"},
            {"text": "renamed", "state": "done"},
            {"text": "open", "state": "pending", "evidence": evidence("tool_call", "toolu_x"), "done_at": iso(T0)},
        ]

        bound = bind_done_items(sent, prior_items=prior, calls=calls, fallback_start="F", now="N")

        self.assertEqual(bound[0], {"text": "verified", "state": "done", "evidence": evidence("tool_call", "toolu_old"), **WINDOW})
        self.assertEqual(bound[1], {"text": "grandfathered", "state": "done"})
        # The latest unheld call goes first, and each call to one item.
        self.assertEqual(
            [item.get("evidence") for item in bound[2:5]],
            [evidence("tool_call", "toolu_2"), evidence("tool_call", "toolu_1"), None],
        )
        self.assertEqual({(item["window_start"], item["done_at"]) for item in bound[2:5]}, {(opened, "N")})
        # Leaving done keeps a failed binding, drops done_at, reopens the window.
        self.assertEqual(
            bound[5],
            {"text": "reopened", "state": "pending", "evidence": evidence("tool_call", "toolu_red"), "window_start": "N"},
        )
        # A renamed item is a new item: its window opens at this write.
        self.assertEqual(bound[6], {"text": "renamed", "state": "done", "window_start": "N", "done_at": "N"})
        self.assertEqual(bound[7], {"text": "open", "state": "pending", "window_start": "N"})


class ToolBindsEvidenceTest(_PlanHomeTest):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"OMH_HOME": str(self.home), "HERMES_HOME": str(self.hermes)})
        env.start()
        self.addCleanup(env.stop)
        build_state_db(self.hermes, [(SESSION, "terminal", "toolu_setup", _terminal(0), None, T0)])

    def call(self, args: dict) -> dict:
        return json.loads(omh_todo_handler(args, session_id=SESSION))

    def stored(self) -> dict:
        return json.loads(todo_path(self.home, SESSION).read_text(encoding="utf-8"))

    def run_command(self, call_id: str, exit_code: int = 0) -> None:
        """Record a command after the plan's last write, then let the clock move past it."""
        stamp = datetime.fromisoformat(self.stored()["updated_at"].replace("Z", "+00:00")).timestamp()
        add_rows(self.hermes, [(SESSION, "terminal", call_id, _terminal(exit_code), None, stamp + 0.001)])
        time.sleep(0.02)

    def test_advance_binds_the_command_that_ran_since_the_last_plan_write(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}, {"text": "ship"}]})
        self.run_command("toolu_suite")

        result = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertEqual(result["status"], "written")
        self.assertEqual(self.stored()["items"][0]["evidence"], evidence("tool_call", "toolu_suite"))
        self.assertNotIn("done_unverified", result)

    def test_a_copied_reference_is_ignored_and_cannot_close_other_items(self):
        # The reviewer's probe: item 1 closes on a real command, whose id the
        # result then shows; the writer copies it onto two more done items.
        self.call({"action": "set", "items": [{"text": "a", "state": "active"}, {"text": "b"}, {"text": "c"}]})
        self.run_command("toolu_one")
        first = self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})
        shown = first["todo"]["items"][0]["evidence"]
        self.assertEqual(shown, evidence("tool_call", "toolu_one"))
        self.run_command("toolu_red", exit_code=1)

        result = self.call(
            {
                "action": "set",
                "items": [
                    {"text": "a", "state": "done"},
                    {"text": "b", "state": "done", "evidence": shown},
                    {"text": "c", "state": "done", "evidence": shown},
                ],
            }
        )

        self.assertEqual(result["status"], "written")
        items = self.stored()["items"]
        self.assertEqual(items[0]["evidence"], shown)
        self.assertEqual(items[1]["evidence"], evidence("tool_call", "toolu_red"))
        self.assertNotIn("evidence", items[2])
        # Item 3 is failed too: the last command the session ran before it
        # was marked done did not pass.
        self.assertEqual(
            [(entry["item"], entry["reason"]) for entry in result["done_unverified"]],
            [(2, EVIDENCE_REASON_FAILED), (3, EVIDENCE_REASON_FAILED)],
        )
        self.assertNotEqual(self.todo()["status"], "absent")
        self.assertIsNotNone(open_plan_position(self.todo(), self.unverified()))

    def test_advance_never_binds_a_call_another_item_already_holds(self):
        # b's window opened with a's, so a's call sits inside both; it closes
        # one item, not two.
        self.call({"action": "set", "items": [{"text": "a", "state": "active"}, {"text": "b"}]})
        self.run_command("toolu_shared")
        self.call({"action": "set", "items": [{"text": "a", "state": "done"}, {"text": "b"}]})
        self.assertEqual(self.stored()["items"][0]["evidence"], evidence("tool_call", "toolu_shared"))

        result = self.call({"action": "advance", "item": 2, "item_text": "b", "state": "done"})

        self.assertEqual(result["status"], "written")
        self.assertNotIn("evidence", self.stored()["items"][1])

    def test_one_command_closes_at_most_one_item_in_a_set(self):
        self.call({"action": "set", "items": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        self.run_command("toolu_only")

        result = self.call(
            {"action": "set", "items": [{"text": "a", "state": "done"}, {"text": "b", "state": "done"}, {"text": "c", "state": "done"}]}
        )

        self.assertEqual(
            [item.get("evidence") for item in self.stored()["items"]],
            [evidence("tool_call", "toolu_only"), None, None],
        )
        self.assertEqual(
            [(entry["item"], entry["reason"]) for entry in result["done_unverified"]],
            [(2, EVIDENCE_REASON_NONE), (3, EVIDENCE_REASON_NONE)],
        )

    def test_a_first_declaration_cannot_be_closed_by_what_ran_before_it(self):
        # `toolu_setup` ran before any plan existed; a plan declared with its
        # items already done opens its window at this write.
        result = self.call({"action": "set", "items": [{"text": "a", "state": "done"}, {"text": "b", "state": "done"}]})

        items = self.stored()["items"]
        self.assertEqual([item.get("evidence") for item in items], [None, None])
        self.assertEqual(items[0]["window_start"], items[0]["done_at"])
        self.assertNotIn("done_unverified", result)

    def test_a_failed_command_is_bound_and_a_passing_rerun_closes_a_finished_plan(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}]})
        self.run_command("toolu_red", exit_code=1)
        first = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})
        self.assertEqual(first["done_unverified"][0]["reason"], EVIDENCE_REASON_FAILED)

        self.run_command("toolu_green")
        second = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertEqual(second["status"], "written")
        self.assertEqual(self.stored()["items"][0]["evidence"], evidence("tool_call", "toolu_green"))
        self.assertNotIn("done_unverified", second)

    def test_a_no_op_re_advance_does_not_restamp_the_plan(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}]})
        self.run_command("toolu_red", exit_code=1)
        self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})
        before = todo_path(self.home, SESSION).read_bytes()
        time.sleep(0.02)

        self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertEqual(todo_path(self.home, SESSION).read_bytes(), before)

    def test_five_no_op_re_advances_buy_one_nudge_not_five(self):
        # Probe P4: the host offers several turn-end attempts; between them the
        # model re-advances the same item with no new command.
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}]})
        self.run_command("toolu_red", exit_code=1)
        self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        nudges = 0
        for attempt in range(5):
            if self.fire(attempt=attempt):
                nudges += 1
            self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertEqual(nudges, 1)

    def test_leaving_done_keeps_the_binding_drops_done_at_and_reopens_the_window(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}, {"text": "ship"}]})
        self.run_command("toolu_red", exit_code=1)
        self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})
        closed_window = self.stored()["items"][0]["window_start"]

        self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "active"})

        item = self.stored()["items"][0]
        self.assertEqual(item["evidence"], evidence("tool_call", "toolu_red"))
        self.assertNotIn("done_at", item)
        self.assertGreater(item["window_start"], closed_window)

    def _failing_then(self) -> None:
        self.call({"action": "set", "items": [{"text": "a", "state": "active"}, {"text": "b"}]})
        self.run_command("toolu_red", exit_code=1)

    def _a_reason(self) -> list[tuple[int, str]]:
        return [pair for pair in self.reasons() if pair[0] == 1]

    def test_l1_reopening_a_failed_item_and_marking_it_done_again_does_not_launder_it(self):
        self._failing_then()
        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})
        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "pending"})
        time.sleep(0.02)

        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})

        self.assertEqual(self._a_reason(), [(1, EVIDENCE_REASON_FAILED)])

    def test_l2_another_items_write_between_the_failure_and_the_done_mark_does_not_hide_it(self):
        self._failing_then()
        self.call({"action": "advance", "item": 2, "item_text": "b", "state": "active"})
        time.sleep(0.02)

        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})

        self.assertEqual(self.stored()["items"][0]["evidence"], evidence("tool_call", "toolu_red"))
        self.assertEqual(self._a_reason(), [(1, EVIDENCE_REASON_FAILED)])

    def test_l3_renaming_the_item_in_a_set_does_not_reset_the_failure(self):
        self._failing_then()
        self.call({"action": "set", "items": [{"text": "a", "state": "done"}, {"text": "b"}]})
        time.sleep(0.02)

        self.call({"action": "set", "items": [{"text": "a.", "state": "done"}, {"text": "b"}]})

        self.assertEqual(self._a_reason(), [(1, EVIDENCE_REASON_FAILED)])

    def test_l4_clear_then_set_does_not_reset_the_failure(self):
        self._failing_then()
        self.assertEqual(self.call({"action": "clear"})["status"], "cleared")

        result = self.call({"action": "set", "items": [{"text": "a", "state": "done"}]})

        self.assertEqual(result["done_unverified"][0]["reason"], EVIDENCE_REASON_FAILED)

    def test_a_sticky_failure_outlives_a_pass_that_belongs_to_another_item(self):
        # The pass ran for b before a was reopened, so it is neither in a's
        # new window nor the session's last word on a -- only the binding is.
        self._failing_then()
        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})
        self.call({"action": "advance", "item": 2, "item_text": "b", "state": "active"})
        self.run_command("toolu_green_for_b")
        self.call({"action": "advance", "item": 2, "item_text": "b", "state": "done"})
        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "active"})
        time.sleep(0.02)

        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})

        self.assertEqual(self._a_reason(), [(1, EVIDENCE_REASON_FAILED)])

    def test_a_re_advance_over_a_bound_failure_never_binds_an_older_pass(self):
        self.call({"action": "set", "items": [{"text": "a", "state": "active"}]})
        self.run_command("toolu_green_first")
        self.run_command("toolu_red_last", exit_code=1)
        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})
        self.assertEqual(self.stored()["items"][0]["evidence"], evidence("tool_call", "toolu_red_last"))

        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})

        self.assertEqual(self.stored()["items"][0]["evidence"], evidence("tool_call", "toolu_red_last"))

    def test_a_call_recorded_after_the_done_write_is_never_bound_to_it(self):
        self.call({"action": "set", "items": [{"text": "a", "state": "active"}]})
        add_rows(self.hermes, [(SESSION, "terminal", "toolu_future", _terminal(0), None, 4_000_000_000.0)])

        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})

        self.assertNotIn("evidence", self.stored()["items"][0])

    def test_a_passing_command_in_the_window_replaces_a_sticky_failure(self):
        self._failing_then()
        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})
        self.call({"action": "advance", "item": 1, "item_text": "a", "state": "active"})
        self.run_command("toolu_green")

        result = self.call({"action": "advance", "item": 1, "item_text": "a", "state": "done"})

        self.assertEqual(self.stored()["items"][0]["evidence"], evidence("tool_call", "toolu_green"))
        self.assertNotIn("done_unverified", result)

    def test_a_finished_plan_still_refuses_a_move_out_of_done(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "done"}]})

        result = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "active"})

        self.assertEqual(result["status"], "invalid_todo")
        self.assertIn("this plan is finished", result["error"])

    def test_the_tool_reports_an_unreadable_store_once_at_the_write(self):
        self.call({"action": "set", "items": [{"text": "fix", "state": "active"}]})
        self.run_command("toolu_suite")
        (self.hermes / "state.db").unlink()
        (self.hermes / "state.db").write_bytes(b"not a database" * 16)

        result = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "done"})

        self.assertEqual(result["done_unverified"][0]["reason"], EVIDENCE_REASON_UNREADABLE)
        self.assertIsNone(self.fire())


class ReaderNeverRaisesTest(unittest.TestCase):
    def test_a_store_without_the_expected_tables_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp)
            sqlite3.connect(hermes / "state.db").close()

            reading = todo_evidence.item_verdicts(
                hermes, SESSION, [{"evidence": evidence("tool_call", "toolu_ok"), "from": None, "to": None}]
            )

        self.assertEqual(reading["store"], todo_evidence.STORE_UNREADABLE)

    def test_no_store_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            reading = todo_evidence.item_verdicts(tmp, SESSION, [])

        self.assertEqual(reading["store"], todo_evidence.STORE_ABSENT)


if __name__ == "__main__":
    unittest.main()
