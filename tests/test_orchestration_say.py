"""Contracts for the `say` sentence four OMH tools add to a successful result.

What each case pins:

- which statuses speak and which stay silent (a read, an error, a refusal, a
  contended write carry no `say` key at all);
- that the sentence passes the reply lint and adds no OMH vocabulary around
  the values it echoes (a category id, a reason code, a schema id, a head);
- that a result's machine fields are byte-identical with and without the
  sentence, so no consumer of the record sees anything move;
- that every table the sentence is built from covers its producer's whole
  vocabulary, and fails with the key to add when it does not.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.catalogs.model_chain_table import MODEL_DISPLAY_LABELS as CORE_MODEL_LABELS  # noqa: E402
from omh.plugin_bundle.omh import agent_board_bridge  # noqa: E402
from omh.plugin_bundle.omh.hermes_delegation import HERMES_MIXTURE_CATEGORY_CHAINS  # noqa: E402
from omh.plugin_bundle.omh.orchestration_say import (  # noqa: E402
    EXHAUSTED_ROUTE_SAY,
    LANE_ROLE_SAY,
    MODEL_DISPLAY_LABELS,
    PLAN_DONE_CRITERION,
    ROUTE_NO_PURPOSE_REASONS,
    ROUTE_PURPOSE_PHRASES,
    UNVERIFIED_REASON_PHRASES,
    board_say,
    loop_say,
    route_say,
    todo_say,
)
from omh.plugin_bundle.omh.todo_reconciliation import DONE_UNVERIFIED, EVIDENCE_REASONS  # noqa: E402
from omh.plugin_bundle.omh.tools import builtin_tool_schemas  # noqa: E402
from omh.plugin_bundle.omh.tools.agent_board_tool import omh_agent_board_handler  # noqa: E402
from omh.plugin_bundle.omh.tools.delegate_route_tool import omh_delegate_route_handler  # noqa: E402
from omh.plugin_bundle.omh.tools.loop_tool import omh_loop_handler  # noqa: E402
from omh.plugin_bundle.omh.tools.todo_tool import omh_todo_handler  # noqa: E402
from omh.quality.reply_lint import build_reply_lint  # noqa: E402
from omh.workflows.agent_board import LANE_ROLES, HostIdentity  # noqa: E402
from _module_patch import patch_modules  # noqa: E402
from test_todo_evidence_completion import SESSION as TODO_SESSION, add_rows, build_state_db  # noqa: E402
from omh.plugin_bundle.omh.todo_store import todo_path  # noqa: E402

RELAY_SENTENCE = "Relay any `say` field to the user once, in their language and your own words."
SAY_TOOLS = ("omh_delegate_route", "omh_todo", "omh_loop", "omh_agent_board")


def _lint_findings(text: str) -> Counter[tuple[str, str]]:
    return Counter((item["kind"], item["match"]) for item in build_reply_lint(text)["findings"])


def _assert_plain(case: unittest.TestCase, say: str) -> None:
    payload = build_reply_lint(say)
    case.assertTrue(payload["ok"], (say, payload["findings"]))
    case.assertNotIn("[OMH", say)
    for category in HERMES_MIXTURE_CATEGORY_CHAINS:
        case.assertIsNone(
            re.search(r"(?<![\w-])" + re.escape(category) + r"(?![\w-])", say),
            f"category id {category!r} in {say!r}",
        )
    for code in EVIDENCE_REASONS + (DONE_UNVERIFIED,):
        case.assertNotIn(code, say)


class VendoredTableParityTests(unittest.TestCase):
    """Each table covers its producer's whole vocabulary; a gap names the key."""

    def test_model_labels_match_the_catalog(self) -> None:
        missing = sorted(set(CORE_MODEL_LABELS) - set(MODEL_DISPLAY_LABELS))
        extra = sorted(set(MODEL_DISPLAY_LABELS) - set(CORE_MODEL_LABELS))
        self.assertEqual(missing, [], "add these aliases to orchestration_say.MODEL_DISPLAY_LABELS")
        self.assertEqual(extra, [], "these aliases left omh.catalogs.model_chain_table; remove them")
        self.assertEqual(MODEL_DISPLAY_LABELS, CORE_MODEL_LABELS)

    def test_every_alias_in_a_shipped_chain_has_a_label(self) -> None:
        for category, chain in HERMES_MIXTURE_CATEGORY_CHAINS.items():
            for alias, _effort in chain:
                with self.subTest(category=category, alias=alias):
                    self.assertIn(alias, MODEL_DISPLAY_LABELS)

    def test_every_routable_category_has_a_phrase_or_a_stated_reason(self) -> None:
        phrased, unphrased = set(ROUTE_PURPOSE_PHRASES), set(ROUTE_NO_PURPOSE_REASONS)
        self.assertEqual(phrased & unphrased, set(), "a category sits in both maps")
        missing = sorted(set(HERMES_MIXTURE_CATEGORY_CHAINS) - phrased - unphrased)
        self.assertEqual(
            missing, [], "give these categories a phrase in ROUTE_PURPOSE_PHRASES or a reason in ROUTE_NO_PURPOSE_REASONS"
        )
        self.assertEqual(sorted((phrased | unphrased) - set(HERMES_MIXTURE_CATEGORY_CHAINS)), [])
        for category, reason in ROUTE_NO_PURPOSE_REASONS.items():
            with self.subTest(category=category):
                self.assertTrue(reason.strip())

    def test_every_done_unverified_reason_has_a_phrase(self) -> None:
        for code in EVIDENCE_REASONS:
            with self.subTest(code=code):
                self.assertIn(code, UNVERIFIED_REASON_PHRASES, "add a plain phrase for this reason code")
        self.assertEqual(sorted(UNVERIFIED_REASON_PHRASES), sorted(EVIDENCE_REASONS))

    def test_every_lane_role_has_a_sentence(self) -> None:
        self.assertEqual(sorted(LANE_ROLE_SAY), sorted(LANE_ROLES), "add a sentence per lane_role")

    def test_every_table_value_is_plain(self) -> None:
        values = (
            list(ROUTE_PURPOSE_PHRASES.values())
            + list(UNVERIFIED_REASON_PHRASES.values())
            + list(LANE_ROLE_SAY.values())
            + [PLAN_DONE_CRITERION, EXHAUSTED_ROUTE_SAY]
        )
        for value in values:
            with self.subTest(value=value):
                _assert_plain(self, value)


class RelaySentenceTests(unittest.TestCase):
    def test_exactly_the_four_tools_that_return_say_carry_the_relay_sentence(self) -> None:
        schemas = {schema["name"]: schema for schema in builtin_tool_schemas()}
        carrying = sorted(name for name, schema in schemas.items() if RELAY_SENTENCE in schema["description"])
        self.assertEqual(carrying, sorted(SAY_TOOLS))
        for name in SAY_TOOLS:
            self.assertEqual(schemas[name]["description"].count(RELAY_SENTENCE), 1)


class RouteSayTests(unittest.TestCase):
    def test_every_category_head_routes_to_a_plain_sentence(self) -> None:
        for category, chain in HERMES_MIXTURE_CATEGORY_CHAINS.items():
            alias = chain[0][0]
            with self.subTest(category=category):
                say = route_say({"status": "routed", "category": category, "applied": {"alias": alias}})
                self.assertIsNotNone(say)
                _assert_plain(self, str(say))
                self.assertIn(MODEL_DISPLAY_LABELS[alias], str(say))
                phrase = ROUTE_PURPOSE_PHRASES.get(category)
                if phrase:
                    self.assertIn(f" for {phrase}.", str(say))
                else:
                    self.assertNotIn(" for ", str(say))

    def test_a_fallback_names_the_next_model(self) -> None:
        say = route_say({"status": "fell_back", "category": "quick", "applied": {"alias": "kimi-k3"}})
        self.assertEqual(say, "This part moves to the next model in line: Kimi K3 for short tasks.")
        _assert_plain(self, str(say))

    def test_an_exhausted_chain_has_its_own_sentence(self) -> None:
        say = route_say({"status": "exhausted_to_inherit", "category": "quick", "from": "glm-5.3-flash"})
        self.assertEqual(say, EXHAUSTED_ROUTE_SAY)
        for word in ("chain", "tier", "inherit", "exhausted", "fallback"):
            self.assertNotIn(word, str(say).lower())

    def test_an_unlabelled_alias_is_said_as_written(self) -> None:
        say = route_say({"status": "routed", "category": "", "applied": {"alias": "my-local-model"}})
        self.assertEqual(say, "This part will run on my-local-model.")

    def test_every_other_status_is_silent(self) -> None:
        for status in ("status", "cleared", "restored", "error", "unrecorded_value_not_ours", "foreign_edit", ""):
            with self.subTest(status=status):
                self.assertIsNone(
                    route_say({"status": status, "category": "quick", "applied": {"alias": "kimi-k3"}})
                )
        self.assertIsNone(route_say({"status": "routed", "applied": {}}))


class RouteHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.omh_home = self.home / ".omh"
        self.enterContext(patch_modules({"hermes_constants": None}))

    def call(self, **args: object) -> dict:
        return json.loads(
            omh_delegate_route_handler({"hermes_home": str(self.home), "omh_home": str(self.omh_home), **args})
        )

    def test_set_fallback_and_exhaustion_each_carry_a_sentence(self) -> None:
        routed = self.call(action="set", category="quick")
        self.assertEqual(routed["say"], "This part will run on GLM 5.3 Flash for short tasks.")
        fell_back = self.call(action="fallback", category="quick")
        self.assertEqual(fell_back["status"], "fell_back")
        self.assertEqual(fell_back["say"], "This part moves to the next model in line: Kimi K3 for short tasks.")
        statuses = []
        for _ in range(3):
            statuses.append(self.call(action="fallback", category="quick"))
        self.assertEqual(statuses[-1]["status"], "exhausted_to_inherit")
        self.assertEqual(statuses[-1]["say"], EXHAUSTED_ROUTE_SAY)

    def test_reads_clears_and_errors_carry_no_sentence(self) -> None:
        self.assertNotIn("say", self.call(action="status"))
        self.assertNotIn("say", self.call(action="set", category="galaxybrain"))
        self.assertNotIn("say", self.call(action="fallback"))
        self.assertNotIn("say", self.call(action="nonsense"))
        self.call(action="set", category="quick")
        self.assertNotIn("say", self.call(action="clear"))

    def test_the_sentence_is_the_only_field_it_adds(self) -> None:
        with_say = self.call(action="set", category="architect")
        self.call(action="clear")
        with mock.patch("omh.plugin_bundle.omh.tools.delegate_route_tool.route_say", return_value=None):
            without = self.call(action="set", category="architect")
        self.assertEqual(with_say.pop("say"), "This part will run on Claude Fable 5.1 for architecture and system design.")
        for payload in (with_say, without):
            payload.pop("route_provenance", None)
            payload.pop("observation", None)
        self.assertEqual(with_say, without)


def _todo_env(case: unittest.TestCase) -> tuple[Path, Path]:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    home, hermes = root / "omh", root / "hermes"
    hermes.mkdir(parents=True)
    env = mock.patch.dict(os.environ, {"OMH_HOME": str(home), "HERMES_HOME": str(hermes)})
    env.start()
    case.addCleanup(env.stop)
    return home, hermes


class TodoSayTests(unittest.TestCase):
    TODO = {
        "title": "Ship the fix",
        "items": [
            {"text": "Reproduce the bug", "state": "done"},
            {"text": "Write the fix", "state": "active", "blocked_reason": "waiting on the owner's approval"},
            {"text": "Run the suite", "state": "pending"},
        ],
    }

    def test_a_set_states_the_plan_and_the_recorded_result_criterion(self) -> None:
        say = todo_say("set", "written", self.TODO)
        self.assertEqual(say, f"Plan: Ship the fix (3 steps). {PLAN_DONE_CRITERION}")
        self.assertIn("recorded result", PLAN_DONE_CRITERION)
        _assert_plain(self, str(say))

    def test_a_set_without_a_title_still_counts_the_steps(self) -> None:
        say = todo_say("set", "written", {"title": "", "items": [{"text": "one", "state": "pending"}]})
        self.assertEqual(say, f"Plan: 1 step. {PLAN_DONE_CRITERION}")

    def test_an_advance_that_blocks_names_the_step_and_the_recorded_reason(self) -> None:
        say = todo_say("advance", "written", self.TODO, item=2)
        self.assertEqual(say, "Step 2 (Write the fix) is blocked: waiting on the owner's approval.")
        _assert_plain(self, str(say))

    def test_an_advance_that_does_not_block_is_silent(self) -> None:
        self.assertIsNone(todo_say("advance", "written", self.TODO, item=1))
        self.assertIsNone(todo_say("advance", "written", self.TODO, item=9))
        self.assertIsNone(todo_say("advance", "written", self.TODO, item=True))

    def test_every_unverified_reason_becomes_a_plain_clause(self) -> None:
        for code in EVIDENCE_REASONS:
            with self.subTest(code=code):
                unverified = [{"item": 1, "state": DONE_UNVERIFIED, "reason": code}]
                say = str(todo_say("advance", "written", self.TODO, item=1, unverified=unverified))
                self.assertIn(UNVERIFIED_REASON_PHRASES[code], say)
                self.assertTrue(say.startswith("Step 1 (Reproduce the bug) is marked done, but "))
                _assert_plain(self, say)

    def test_an_unknown_reason_code_is_never_echoed(self) -> None:
        unverified = [{"item": 1, "state": DONE_UNVERIFIED, "reason": "evidence_teleported"}]
        say = str(todo_say("advance", "written", self.TODO, item=1, unverified=unverified))
        self.assertNotIn("evidence_teleported", say)
        self.assertIn("no recorded result confirms it yet", say)

    def test_more_than_one_unverified_item_is_counted(self) -> None:
        unverified = [
            {"item": 1, "state": DONE_UNVERIFIED, "reason": "no_evidence"},
            {"item": 3, "state": DONE_UNVERIFIED, "reason": "evidence_failed"},
        ]
        say = str(todo_say("set", "written", self.TODO, unverified=unverified))
        self.assertTrue(say.endswith(" 1 more marked-done step also counts as open."))

    def test_only_a_write_speaks(self) -> None:
        for action, status in (
            ("show", "read"),
            ("clear", "cleared"),
            ("clear", "already_absent"),
            ("set", "invalid_todo"),
            ("set", "contended"),
            ("advance", "contended"),
            ("advance", "invalid_todo"),
            ("nonsense", "invalid_action"),
            ("checkpoint", "written"),
        ):
            with self.subTest(action=action, status=status):
                self.assertIsNone(todo_say(action, status, self.TODO, item=2))

    def test_echoed_values_carry_only_their_own_vocabulary(self) -> None:
        # Model-written values can hold OMH words (the audit found `lane` and
        # `handoff` in item text). The sentence may carry them -- it echoes as
        # written -- but its own template must add nothing.
        title = "Open the coding lane and prepare the handoff"
        text = "Record the run record for omh_todo_result/v1"
        reason = "the evidence boundary is not_observed"
        todo = {"title": title, "items": [{"text": text, "state": "active", "blocked_reason": reason}]}
        cases = (
            (todo_say("set", "written", todo), [title]),
            (todo_say("advance", "written", todo, item=1), [text, reason]),
            (
                todo_say(
                    "set", "written", todo, unverified=[{"item": 1, "state": DONE_UNVERIFIED, "reason": "no_evidence"}]
                ),
                [title, text],
            ),
        )
        for say, echoed in cases:
            with self.subTest(say=say):
                expected: Counter[tuple[str, str]] = Counter()
                for value in echoed:
                    expected += _lint_findings(value)
                self.assertTrue(expected, "the fixture must carry vocabulary to be a test")
                self.assertEqual(_lint_findings(str(say)), expected)


class TodoHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home, self.hermes = _todo_env(self)

    def call(self, args: dict) -> dict:
        return json.loads(omh_todo_handler(args, session_id=TODO_SESSION))

    def test_set_and_blocking_advance_speak_and_nothing_else_does(self) -> None:
        written = self.call({"action": "set", "title": "Fix login", "items": [{"text": "fix", "state": "active"}, {"text": "ship"}]})
        self.assertEqual(written["say"], f"Plan: Fix login (2 steps). {PLAN_DONE_CRITERION}")
        blocked = self.call(
            {"action": "advance", "item": 2, "item_text": "ship", "state": "pending", "blocked_reason": "needs the deploy key"}
        )
        self.assertEqual(blocked["say"], "Step 2 (ship) is blocked: needs the deploy key.")
        moved = self.call({"action": "advance", "item": 1, "item_text": "fix", "state": "active"})
        self.assertNotIn("say", moved)
        self.assertNotIn("say", self.call({"action": "show"}))
        self.assertNotIn("say", self.call({"action": "set", "items": "not a list"}))
        self.assertNotIn("say", self.call({"action": "nonsense"}))
        self.assertNotIn("say", self.call({"action": "clear"}))

    def test_a_done_mark_nothing_closes_is_said_plainly(self) -> None:
        # One command in the window closes at most one item (#1928), so the
        # second done mark is left with nothing recorded behind it.
        build_state_db(self.hermes, [(TODO_SESSION, "terminal", "toolu_setup", '{"output": "", "exit_code": 0}', None, 1.0)])
        self.call({"action": "set", "items": [{"text": "fix"}, {"text": "ship"}]})
        stored = json.loads(todo_path(self.home, TODO_SESSION).read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(stored["updated_at"].replace("Z", "+00:00")).timestamp()
        add_rows(self.hermes, [(TODO_SESSION, "terminal", "toolu_only", '{"output": "", "exit_code": 0}', None, stamp + 0.001)])
        time.sleep(0.02)
        result = self.call({"action": "set", "items": [{"text": "fix", "state": "done"}, {"text": "ship", "state": "done"}]})
        self.assertEqual([(e["item"], e["reason"]) for e in result["done_unverified"]], [(2, "no_evidence")])
        self.assertEqual(
            result["say"],
            f"Plan: 2 steps. {PLAN_DONE_CRITERION} Step 2 (ship) is marked done, but "
            f"{UNVERIFIED_REASON_PHRASES['no_evidence']}, so it still counts as open.",
        )

    def test_the_sentence_is_the_only_field_it_adds(self) -> None:
        args = {"action": "set", "title": "Fix login", "items": [{"text": "fix", "state": "active"}]}
        with_say = self.call(args)
        with mock.patch(
            "omh.plugin_bundle.omh.tools.todo_tool.todo_say", return_value=None
        ):
            without = self.call(args)
        self.assertIn("say", with_say)
        with_say.pop("say")
        for payload in (with_say, without):
            for key in ("updated_at", "updated_age_seconds", "stall"):
                payload["todo"].pop(key, None)
            payload.pop("observation", None)
        self.assertEqual(with_say, without)


class LoopSayTests(unittest.TestCase):
    def test_start_states_the_goal_and_its_criteria(self) -> None:
        say = loop_say(
            {"action": "start", "goal_reframe": "Ship the fix.", "success_criteria": ["tests pass", "docs updated."]},
            {"status": "ok"},
        )
        self.assertEqual(say, "Goal: Ship the fix. Done when: tests pass; docs updated.")
        _assert_plain(self, str(say))

    def test_an_external_wait_is_said(self) -> None:
        say = loop_say({"action": "feedback", "external_wait": "a maintainer review"}, {"status": "ok"})
        self.assertEqual(say, "Waiting on something outside this work: a maintainer review.")

    def test_everything_else_is_silent(self) -> None:
        for request, envelope in (
            ({"action": "start", "goal_reframe": "x", "success_criteria": ["y"]}, {"status": "error"}),
            ({"action": "start", "goal_reframe": "x", "success_criteria": []}, {"status": "ok"}),
            ({"action": "feedback", "internal_gap": "QA gate missing"}, {"status": "ok"}),
            ({"action": "feedback", "external_wait": "review"}, {"status": "error"}),
            ({"action": "status"}, {"status": "ok"}),
            ({"action": "run_once"}, {"status": "ok"}),
            ({"action": "permit"}, {"status": "ok"}),
        ):
            with self.subTest(request=request, envelope=envelope):
                self.assertIsNone(loop_say(request, envelope))

    def test_echoed_goal_carries_only_its_own_vocabulary(self) -> None:
        goal, criterion = "Open the coding lane", "the handoff is prepared_not_observed"
        say = str(loop_say({"action": "start", "goal_reframe": goal, "success_criteria": [criterion]}, {"status": "ok"}))
        self.assertEqual(_lint_findings(say), _lint_findings(goal) + _lint_findings(criterion))


class LoopHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        _todo_env(self)

    def call(self, **args: object) -> dict:
        return json.loads(omh_loop_handler(dict(args), session_id="loop-session"))

    def test_start_and_external_wait_speak_reads_and_errors_do_not(self) -> None:
        started = self.call(
            action="start", goal_summary="Make login work", goal_reframe="Ship the login fix",
            success_criteria=["login test passes"],
        )
        self.assertEqual(started["status"], "ok", started)
        self.assertEqual(started["say"], "Goal: Ship the login fix. Done when: login test passes.")
        loop_id, revision = started["loop_id"], started["record_revision"]
        self.assertNotIn("say", self.call(action="status", loop_id=loop_id))
        waited = self.call(action="feedback", loop_id=loop_id, expected_revision=revision, external_wait="CI on the PR")
        self.assertEqual(waited["say"], "Waiting on something outside this work: CI on the PR.")
        gap = self.call(
            action="feedback", loop_id=loop_id, expected_revision=waited["record_revision"], internal_gap="QA gate"
        )
        self.assertEqual(gap["status"], "ok")
        self.assertNotIn("say", gap)
        stale = self.call(action="feedback", loop_id=loop_id, expected_revision=0, external_wait="CI")
        self.assertEqual(stale["status"], "error")
        self.assertNotIn("say", stale)

    def test_the_sentence_is_the_only_field_it_adds(self) -> None:
        self.assertNotIn("say", self.call(action="assess", message="Ship the login fix with tests"))
        args = {
            "goal_summary": "Make login work", "goal_reframe": "Ship it", "success_criteria": ["done"],
        }
        with_say = self.call(action="start", loop_id="loop-with", **args)
        self.assertEqual(with_say["status"], "ok", with_say)
        with mock.patch("omh.plugin_bundle.omh.tools.loop_tool.loop_say", return_value=None):
            without = self.call(action="start", loop_id="loop-without", **args)
        self.assertEqual(with_say.pop("say"), "Goal: Ship it. Done when: done.")
        self.assertEqual(set(with_say), set(without))
        for key in (
            "schema_version", "status", "action", "record_revision", "mutation_applied", "warnings",
            "next_actions", "prepared_versus_observed", "claim_boundary", "plugin_tool",
        ):
            with self.subTest(key=key):
                self.assertEqual(with_say[key], without[key])


def _prepared(**overrides: object) -> dict:
    base = {"state": "prepared", "operation": "create", "lane_role": "builder"}
    base.update(overrides)
    return base


class BoardSayTests(unittest.TestCase):
    def test_every_role_on_a_prepared_create_speaks_plainly(self) -> None:
        for role in LANE_ROLES:
            with self.subTest(role=role):
                say = board_say(_prepared(lane_role=role))
                self.assertEqual(say, LANE_ROLE_SAY[role])
                self.assertTrue(str(say).startswith("Setting up"))
                _assert_plain(self, str(say))

    def test_nothing_else_speaks(self) -> None:
        for prepared in (
            _prepared(lane_role=""),
            _prepared(lane_role="architect"),
            _prepared(state="denied"),
            _prepared(state="unavailable"),
            _prepared(state="observed"),
            _prepared(operation="show"),
            {"state": "unavailable", "reason": "board_required"},
        ):
            with self.subTest(prepared=prepared):
                self.assertIsNone(board_say(prepared))


class BoardHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        from five_issue_cases.kanban import supplied_schemas

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bridge = agent_board_bridge.AgentBoardBridge(Path(tmp.name), root_identity="fixture-root")
        hooks = frozenset({"pre_tool_call", "post_tool_call"})
        for name, value in (
            ("installed_bridge", lambda board: bridge),
            ("host_capabilities", lambda: (supplied_schemas(), hooks)),
            ("handler_identity", lambda args, kwargs: HostIdentity("session", "host-task", "prepare")),
        ):
            patcher = mock.patch.object(agent_board_bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, args: dict) -> dict:
        return json.loads(omh_agent_board_handler(args))

    def test_a_prepared_role_create_speaks_and_a_plain_create_does_not(self) -> None:
        from five_issue_cases.kanban import request

        base = {"title": "lane-task", "assignee": "worker-profile"}
        builder = self.call(request("create", "lane-1", dict(base, lane_role="builder")))
        self.assertEqual(builder["state"], "prepared", builder)
        self.assertEqual(builder["say"], LANE_ROLE_SAY["builder"])
        plain = self.call(request("create", "lane-2", dict(base)))
        self.assertEqual(plain["state"], "prepared")
        self.assertNotIn("say", plain)
        refused = self.call(request("create", "lane-3", dict(base, lane_role="reviewer")))
        self.assertNotIn("say", refused)
        self.assertNotIn("say", self.call({"action": "status", "request_id": "lane-1"}))

    def test_the_sentence_is_the_only_field_it_adds(self) -> None:
        from five_issue_cases.kanban import request

        arguments = {"title": "t", "assignee": "p", "lane_role": "qa"}
        with_say = self.call(request("create", "lane-8", dict(arguments)))
        with mock.patch("omh.plugin_bundle.omh.tools.agent_board_tool.board_say", return_value=None):
            without = self.call(request("create", "lane-9", dict(arguments)))
        self.assertEqual(with_say.pop("say"), LANE_ROLE_SAY["qa"])
        self.assertEqual(set(with_say), set(without))
        # The digest and native action name the request id, which differs.
        for key in ("state", "reason", "route", "operation", "lane_role", "required_capabilities"):
            with self.subTest(key=key):
                self.assertEqual(with_say[key], without[key])


if __name__ == "__main__":
    unittest.main()
