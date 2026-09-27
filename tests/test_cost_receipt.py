"""Contracts for the cost receipt: what one piece of work cost, from records only.

Every fixture is a temp Hermes home and a temp OMH home; nothing reads the
real ``~/.hermes``. The state.db columns are the ones Hermes declares in
``hermes_state_common.py`` that the receipt reads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _cli_harness import run_cli
from omh.plugin_bundle.omh.cost_receipt import build_cost_receipt
from omh.plugin_bundle.omh.tools.run_summary_tool import omh_run_summary_handler

MAIN = "20260926_100000_main01"
CONTINUED = "20260926_110000_main02"
CHILD = "20260926_100500_child1"
GRANDCHILD = "20260926_100600_grand1"
SILENT_CHILD = "20260926_100700_quiet1"
BRANCH = "20260926_100800_branch"
OTHER = "20260926_090000_other1"
SESSION_START = 1_000_000.0


def _create_state_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, model_config TEXT,
            parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
            api_call_count INTEGER DEFAULT 0
        );
        CREATE TABLE session_model_usage (
            session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
            api_call_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,
            cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL
        );
        """
    )


def _insert_usage(connection: sqlite3.Connection, rows: list[tuple]) -> None:
    for row in rows:
        connection.execute(
            "INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens, "
            "estimated_cost_usd, actual_cost_usd, cost_status, cost_source, api_call_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            row,
        )


def _build_state_db(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(home / "state.db")
    _create_state_schema(connection)
    sessions = [
        # (id, source, model, model_config, parent, started_at, end_reason)
        (MAIN, "tui", "gpt-6-sol", "{}", None, SESSION_START, "compression"),
        (CONTINUED, "tui", "gpt-6-sol", "{}", MAIN, SESSION_START + 3600, None),
        (CHILD, "tui", "glm-5.3-ultrafast", json.dumps({"_delegate_from": MAIN}), MAIN, SESSION_START + 300, None),
        (GRANDCHILD, "tui", "kimi-k3", json.dumps({"_delegate_from": CHILD}), CHILD, SESSION_START + 360, None),
        (SILENT_CHILD, "tui", "gpt-6-luna", json.dumps({"_delegate_from": CONTINUED}), CONTINUED, SESSION_START + 3700, None),
        (BRANCH, "tui", "gpt-6-sol", json.dumps({"_branched_from": MAIN}), MAIN, SESSION_START + 400, None),
        (OTHER, "tui", "gpt-6-sol", "{}", None, SESSION_START - 3600, None),
    ]
    for session_id, source, model, config, parent, started, end_reason in sessions:
        connection.execute(
            "INSERT INTO sessions (id, source, model, model_config, parent_session_id, started_at, end_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, source, model, config, parent, started, end_reason),
        )
    usage = [
        # (session, model, input, output, estimated, actual, status, source)
        (MAIN, "gpt-6-sol", 100_000, 20_000, 0.40, 0.0, "estimated", "official_docs_snapshot"),
        (CONTINUED, "gpt-6-sol", 50_000, 10_000, 0.20, 0.0, "estimated", "official_docs_snapshot"),
        (CHILD, "glm-5.3-ultrafast", 30_000, 5_000, 0.0, 0.0, "unknown", "none"),
        (GRANDCHILD, "kimi-k3", 8_000, 2_000, 0.0, 0.0, "included", "none"),
        (BRANCH, "gpt-6-sol", 999_000, 1_000, 9.0, 0.0, "estimated", "official_docs_snapshot"),
        (OTHER, "gpt-6-sol", 777_000, 1_000, 7.0, 0.0, "estimated", "official_docs_snapshot"),
    ]
    _insert_usage(connection, usage)
    connection.commit()
    connection.close()


WORKER = "20260926_101000_work01"
WORKER_CHILD = "20260926_101100_wchild"
DEFAULT_WORKER = "20260926_101200_work02"
OPS_WORKER = "20260926_101300_work03"
FOREIGN_WORKER = "20260926_101400_work04"


def _create_board(path: Path) -> sqlite3.Connection:
    """The `kanban.db` columns the receipt reads, as `hermes_cli/kanban_db.py` declares them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT, status TEXT NOT NULL,
            created_at INTEGER NOT NULL, session_id TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT,
            status TEXT NOT NULL, worker_pid INTEGER, started_at INTEGER NOT NULL,
            ended_at INTEGER, outcome TEXT, metadata TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
        );
        """
    )
    return connection


def _add_run(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    session_id: str | None,
    assignee: str = "default",
    status: str = "done",
    spawned: bool = True,
    worker_session: str = "",
) -> None:
    started = int(SESSION_START) + 500
    if not connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        connection.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, session_id) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "a task", assignee, status, started, session_id),
        )
    metadata = json.dumps({"worker_session_id": worker_session}) if worker_session else None
    run_id = connection.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, assignee, status, started, None if status == "running" else started + 60,
         None if status == "running" else status, metadata),
    ).lastrowid
    if spawned:
        connection.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'spawned', '{}', ?)",
            (task_id, run_id, started),
        )


def _build_kanban(hermes_home: Path) -> None:
    """Boards and worker sessions: what the conversation's kanban work recorded, and what it did not."""
    # The `coder` profile's worker ran under that profile's own state.db.
    profile = hermes_home / "profiles" / "coder"
    profile.mkdir(parents=True)
    connection = sqlite3.connect(profile / "state.db")
    _create_state_schema(connection)
    for session_id, model, config, parent in (
        (WORKER, "gpt-6-sol", "{}", None),
        (WORKER_CHILD, "kimi-k3", json.dumps({"_delegate_from": WORKER}), WORKER),
    ):
        connection.execute(
            "INSERT INTO sessions (id, source, model, model_config, parent_session_id, started_at) "
            "VALUES (?, 'kanban', ?, ?, ?, ?)",
            (session_id, model, config, parent, SESSION_START + 500),
        )
    _insert_usage(connection, [
        (WORKER, "gpt-6-sol", 15_000, 5_000, 0.05, 0.0, "estimated", "official_docs_snapshot"),
        # The worker's own delegate, with no recorded price.
        (WORKER_CHILD, "kimi-k3", 6_000, 1_000, 0.0, 0.0, "unknown", "none"),
    ])
    connection.commit()
    connection.close()
    # Default-profile workers write the root state.db, the conversation's own.
    connection = sqlite3.connect(hermes_home / "state.db")
    for session_id in (DEFAULT_WORKER, OPS_WORKER, FOREIGN_WORKER):
        connection.execute(
            "INSERT INTO sessions (id, source, model, model_config, started_at) VALUES (?, 'kanban', 'gpt-6-luna', '{}', ?)",
            (session_id, SESSION_START + 600),
        )
    _insert_usage(connection, [
        (DEFAULT_WORKER, "gpt-6-luna", 3_000, 1_000, 0.0, 0.02, "actual", "provider"),
        (OPS_WORKER, "gpt-6-luna", 800, 200, 0.0, 0.0, "included", "none"),
        (FOREIGN_WORKER, "gpt-6-luna", 400_000, 1_000, 6.0, 0.0, "estimated", "official_docs_snapshot"),
    ])
    connection.commit()
    connection.close()

    board = _create_board(hermes_home / "kanban.db")
    # Created in MAIN; the coder worker reported and stamped its session.
    _add_run(board, "t_00000001", session_id=MAIN, assignee="coder", worker_session=WORKER)
    # A retry of the same task whose worker crashed before reporting: missing.
    _add_run(board, "t_00000001", session_id=MAIN, assignee="coder", status="crashed")
    # Created in the continuation, run by a default-profile worker.
    _add_run(board, "t_00000002", session_id=CONTINUED, worker_session=DEFAULT_WORKER)
    # A run stamping a session the delegate walk already summed: counted once.
    _add_run(board, "t_00000003", session_id=MAIN, worker_session=GRANDCHILD)
    # Another conversation's task: never on this receipt.
    _add_run(board, "t_00000004", session_id=OTHER, worker_session=FOREIGN_WORKER)
    # A spawn that never happened: no worker, nothing spent, nothing missing.
    _add_run(board, "t_00000005", session_id=MAIN, status="spawn_failed", spawned=False)
    # A worker still running: it has not stamped its session yet.
    _add_run(board, "t_00000006", session_id=MAIN, status="running")
    # A stamp naming a session its profile's state.db does not hold.
    _add_run(board, "t_00000007", session_id=MAIN, assignee="coder", worker_session="20260926_101500_gone01")
    # A task created from the CLI records no originating session.
    _add_run(board, "t_00000008", session_id=None, worker_session=FOREIGN_WORKER)
    board.commit()
    board.close()
    # A named board holds the conversation's task too.
    ops =_create_board(hermes_home / "kanban" / "boards" / "ops" / "kanban.db")
    _add_run(ops, "t_00000009", session_id=CHILD, worker_session=OPS_WORKER)
    ops.commit()
    ops.close()


def _write_summary(omh_home: Path, fanout_id: str, summary: dict, *, mtime: float) -> None:
    directory = omh_home / "coding" / "fanout" / fanout_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "dispatch_summary.json"
    path.write_text(json.dumps({"schema_version": "fanout_dispatch_summary/v1", **summary}), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _build_fanout(omh_home: Path) -> None:
    _write_summary(
        omh_home,
        "fanout-aaaaaaaaaaaa",
        {
            "origin_session_id": CONTINUED,
            "units": [
                # Claude reports its own cost.
                {"unit_id": "api", "owner": "claude", "exit_code": 0, "tokens_total": 40_000,
                 "cost_usd": 0.12, "origin_session_id": CONTINUED},
                # Codex reports tokens and no cost: unpriced, never priced by OMH.
                {"unit_id": "docs", "owner": "codex", "model": "gpt-6-sol", "exit_code": 0,
                 "tokens_total": 25_000, "origin_session_id": MAIN},
                # Ran and reported nothing: missing, not zero.
                {"unit_id": "ui", "owner": "codex", "exit_code": 1, "origin_session_id": CHILD},
                # Never started: nothing to bill and nothing missing.
                {"unit_id": "late", "owner": "codex", "status": "blocked_by_dependency",
                 "origin_session_id": MAIN},
                # Another conversation's unit.
                {"unit_id": "foreign", "owner": "claude", "exit_code": 0, "cost_usd": 5.0,
                 "origin_session_id": OTHER},
                # A unit dispatched before the link existed.
                {"unit_id": "legacy", "owner": "claude", "exit_code": 0, "cost_usd": 3.0},
            ],
            "failure_recovery": {
                "decisions": [
                    {"unit_id": "ui", "attempt": {
                        "status": "completed", "exit_code": 0, "run_id": "ui-hermes-recovery",
                        "usage": {"total_tokens": 9_000, "estimated_cost_usd": 0.03,
                                  "cost_status": "estimated", "model": "deepseek-v4.1-flash"},
                    }},
                ]
            },
        },
        mtime=SESSION_START + 4000,
    )
    # Written before the conversation began: cannot hold its units.
    _write_summary(
        omh_home,
        "fanout-bbbbbbbbbbbb",
        {"origin_session_id": MAIN,
         "units": [{"unit_id": "old", "owner": "claude", "exit_code": 0, "cost_usd": 8.0, "origin_session_id": MAIN}]},
        mtime=SESSION_START - 10,
    )


class CostReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.hermes_home = root / "hermes"
        self.omh_home = root / "omh"
        _build_state_db(self.hermes_home)
        _build_fanout(self.omh_home)
        # The board root follows HERMES_KANBAN_HOME; never the developer's own.
        env = mock.patch.dict(os.environ, {"HERMES_KANBAN_HOME": "", "HERMES_KANBAN_BOARD": ""})
        env.start()
        self.addCleanup(env.stop)

    def _receipt(self, session_id: str = CONTINUED) -> dict:
        return build_cost_receipt(hermes_home=self.hermes_home, omh_home=self.omh_home, session_id=session_id)

    def test_the_conversation_and_its_delegates_are_summed_and_nothing_else(self) -> None:
        receipt = self._receipt()
        self.assertEqual(receipt["status"], "observed")
        sources = receipt["sources"]
        # MAIN and its compression continuation are the conversation.
        self.assertEqual(sources["session"]["count"], 2)
        self.assertAlmostEqual(sources["session"]["cost_usd"], 0.60)
        self.assertEqual(sources["session"]["priced_tokens"], 180_000)
        # CHILD, its own delegate GRANDCHILD, and SILENT_CHILD; never BRANCH or OTHER.
        self.assertEqual(sources["delegated_children"]["count"], 3)
        self.assertEqual(receipt["sessions_counted"], 5)

    def test_unknown_price_usage_stays_out_of_the_cost(self) -> None:
        children = self._receipt()["sources"]["delegated_children"]
        # CHILD's `unknown` row is tokens without a price; GRANDCHILD's
        # `included` row is a vouched zero.
        self.assertEqual(children["unpriced_tokens"], 35_000)
        self.assertEqual(children["unpriced_models"], ["glm-5.3-ultrafast"])
        self.assertEqual(children["cost_usd"], 0.0)
        self.assertEqual(children["priced_tokens"], 10_000)
        self.assertEqual(children["cost_bases"], ["Hermes included"])

    def test_fanout_units_are_attributed_by_their_recorded_origin_only(self) -> None:
        fanout = self._receipt()["sources"]["fanout_units"]
        # api (0.12) + docs (unpriced) + ui (missing) + the Hermes-lane attempt (0.03).
        self.assertEqual(fanout["count"], 4)
        self.assertAlmostEqual(fanout["cost_usd"], 0.15)
        self.assertEqual(fanout["priced_tokens"], 49_000)
        self.assertEqual(fanout["unpriced_tokens"], 25_000)
        self.assertEqual(fanout["cost_bases"], ["Hermes estimated", "executor reported"])

    def test_records_with_no_usage_are_missing_not_zero(self) -> None:
        missing = {item["id"]: item["reason"] for item in self._receipt()["missing"]}
        self.assertEqual(
            missing,
            {SILENT_CHILD: "no usage recorded", "fanout-aaaaaaaaaaaa/ui": "ran, reported no usage"},
        )

    def test_totals_and_text_keep_observed_unpriced_and_missing_apart(self) -> None:
        receipt = self._receipt()
        self.assertAlmostEqual(receipt["observed_cost_usd"], 0.75)
        self.assertEqual(receipt["unpriced_tokens"], 60_000)
        text = receipt["text"]
        self.assertIn("- Observed cost: $0.7500", text)
        self.assertIn("- Usage with no recorded price: 60,000 tokens (not included in the cost above)", text)
        self.assertIn("- Missing usage: 2 record(s) with no usage recorded (not counted as zero)", text)
        self.assertIn(f"- {SILENT_CHILD}: no usage recorded", text)
        self.assertIn("Not covered:", text)
        self.assertNotIn("$8.0000", text)

    def test_any_session_of_the_conversation_names_the_same_work(self) -> None:
        self.assertEqual(self._receipt(MAIN)["observed_cost_usd"], self._receipt(CONTINUED)["observed_cost_usd"])

    def test_an_unknown_session_is_not_observed_rather_than_free(self) -> None:
        receipt = self._receipt("20260926_120000_nobody")
        self.assertEqual(receipt["status"], "not_observed")
        self.assertNotIn("observed_cost_usd", receipt)
        self.assertIn("No cost receipt", receipt["text"])

    def test_a_conversation_with_only_unpriced_usage_reports_no_cost(self) -> None:
        connection = sqlite3.connect(self.hermes_home / "state.db")
        connection.execute("UPDATE session_model_usage SET cost_status = 'unknown', cost_source = 'none'")
        connection.commit()
        connection.close()
        for path in (self.omh_home / "coding" / "fanout").glob("*/dispatch_summary.json"):
            path.unlink()
        receipt = self._receipt()
        self.assertIsNone(receipt["observed_cost_usd"])
        self.assertIn("- Observed cost: none recorded", receipt["text"])

    def test_the_receipt_carries_no_prompt_or_transcript_fields(self) -> None:
        serialized = json.dumps(self._receipt())
        for field in ("system_prompt", "content", "goal", "prompt"):
            self.assertNotIn(f'"{field}"', serialized)

    def test_the_run_summary_tool_answers_the_cost_question(self) -> None:
        with mock.patch.dict(os.environ, {"OMH_HOME": str(self.omh_home)}):
            result = json.loads(
                omh_run_summary_handler(
                    {"hermes_home": str(self.hermes_home), "omh_home": str(self.omh_home)},
                    session_id=CONTINUED,
                )
            )
        self.assertEqual(result["cost_receipt"]["status"], "observed")
        self.assertAlmostEqual(result["cost_receipt"]["observed_cost_usd"], 0.75)

    def test_the_cli_prints_plain_text_and_json_on_request(self) -> None:
        common = ["--hermes-home", str(self.hermes_home), "--omh-home", str(self.omh_home)]
        status, stdout, stderr = run_cli(
            [*common, "quality-evidence", "cost-receipt", "--session", CONTINUED], output_json=False
        )
        self.assertEqual(status, 0, stderr)
        self.assertIn("- Observed cost: $0.7500", stdout)
        status, stdout, stderr = run_cli(
            [*common, "quality-evidence", "cost-receipt", "--session", CONTINUED, "--json"], output_json=False
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["schema_version"], "omh_cost_receipt/v1")

    def test_the_cli_fails_on_a_session_it_cannot_observe(self) -> None:
        common = ["--hermes-home", str(self.hermes_home), "--omh-home", str(self.omh_home)]
        status, _stdout, stderr = run_cli(
            [*common, "quality-evidence", "cost-receipt", "--session", "20260926_120000_nobody"], output_json=False
        )
        self.assertNotEqual(status, 0)
        self.assertIn("no Hermes session", stderr)

    def test_no_board_is_said_rather_than_read_as_no_workers(self) -> None:
        receipt = self._receipt()
        self.assertEqual(receipt["sources"]["kanban_workers"]["count"], 0)
        self.assertEqual(receipt["kanban"], {"boards_read": [], "boards_unreadable": []})
        self.assertIn("- Kanban workers: no kanban board present", receipt["text"])


class KanbanWorkerCostTest(unittest.TestCase):
    """Kanban worker runs join the receipt only through recorded fields."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.hermes_home = root / "hermes"
        self.omh_home = root / "omh"
        _build_state_db(self.hermes_home)
        _build_kanban(self.hermes_home)
        env = mock.patch.dict(os.environ, {"HERMES_KANBAN_HOME": "", "HERMES_KANBAN_BOARD": ""})
        env.start()
        self.addCleanup(env.stop)

    def _receipt(self, session_id: str = CONTINUED) -> dict:
        return build_cost_receipt(hermes_home=self.hermes_home, omh_home=self.omh_home, session_id=session_id)

    def test_worker_runs_on_the_conversations_tasks_are_summed_across_boards(self) -> None:
        receipt = self._receipt()
        kanban = receipt["sources"]["kanban_workers"]
        # t1 (both runs), t2, t3, t6, t7 on the default board and t9 on `ops`;
        # never t4 (another conversation), t5 (never spawned) or t8 (CLI-created).
        self.assertEqual(kanban["count"], 7)
        # WORKER (0.05 estimated) + DEFAULT_WORKER (0.02 actual) + OPS_WORKER (included $0).
        self.assertAlmostEqual(kanban["cost_usd"], 0.07)
        self.assertEqual(kanban["priced_tokens"], 25_000)
        self.assertEqual(kanban["cost_bases"], ["Hermes actual", "Hermes estimated", "Hermes included"])
        self.assertEqual(receipt["kanban"], {"boards_read": ["default", "ops"], "boards_unreadable": []})
        self.assertAlmostEqual(receipt["observed_cost_usd"], 0.60 + 0.07)

    def test_a_worker_delegate_is_usage_with_no_recorded_price(self) -> None:
        kanban = self._receipt()["sources"]["kanban_workers"]
        # WORKER_CHILD, read from the coder profile's state.db through the worker's own lineage.
        self.assertEqual(kanban["unpriced_tokens"], 7_000)
        self.assertEqual(kanban["unpriced_models"], ["kimi-k3"])

    def test_a_session_the_delegate_walk_summed_is_not_counted_again(self) -> None:
        receipt = self._receipt()
        # t3's stamp names GRANDCHILD, already in the delegated children bucket.
        self.assertEqual(receipt["sources"]["delegated_children"]["priced_tokens"], 10_000)
        self.assertEqual(receipt["priced_tokens"], 180_000 + 10_000 + 25_000)

    def test_a_worker_with_no_session_record_is_missing_not_zero(self) -> None:
        missing = {
            item["id"]: item["reason"] for item in self._receipt()["missing"] if item["source"] == "kanban_workers"
        }
        self.assertEqual(
            missing,
            {
                "kanban default/t_00000001 run 2": "worker ran and recorded no session on its run",
                "kanban default/t_00000006 run 7": "worker still running; it records its session when it reports",
                "kanban default/t_00000007 run 8":
                    "worker session 20260926_101500_gone01 has no row in its profile's state.db",
            },
        )
        self.assertIn("- kanban default/t_00000001 run 2: worker ran and recorded no session on its run",
                      self._receipt()["text"])

    def test_another_conversations_tasks_are_excluded(self) -> None:
        receipt = self._receipt(OTHER)
        self.assertEqual(receipt["sources"]["kanban_workers"]["count"], 1)
        self.assertAlmostEqual(receipt["sources"]["kanban_workers"]["cost_usd"], 6.0)

    def test_an_unreadable_board_is_named_not_skipped(self) -> None:
        (self.hermes_home / "kanban" / "boards" / "ops" / "kanban.db").write_bytes(b"not a database")
        receipt = self._receipt()
        self.assertEqual(receipt["kanban"], {"boards_read": ["default"], "boards_unreadable": ["ops"]})
        self.assertEqual(receipt["sources"]["kanban_workers"]["count"], 6)
        self.assertIn("board(s) that could not be read: ops", receipt["text"])

    def test_reading_leaves_the_board_and_worker_store_unchanged(self) -> None:
        before = {
            path: path.read_bytes()
            for path in (self.hermes_home / "kanban.db", self.hermes_home / "profiles" / "coder" / "state.db")
        }
        self._receipt()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path)

if __name__ == "__main__":
    unittest.main()
