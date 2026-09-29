"""`omh_team`: the checked delegate lane (T1-T12) and the `omh_agent_board` golden pin.

Each class names the contract from the teammate-lane critique it pins and the
mutation that must turn it red. The engine is driven with a fake runner and a
fake working-tree fingerprint, so no test here spawns a process; the one real
spawn path (`evidence_tool.run_verification_command`) is exercised by its own
tests and by the handler test below through a patched runner.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from _local_package import load_local_package
from _module_patch import patch_modules

load_local_package()

from omh.plugin_bundle.omh import team_gate  # noqa: E402
from omh.plugin_bundle.omh.hooks import nudge_budget  # noqa: E402
from omh.plugin_bundle.omh.todo_store import todo_items_digest  # noqa: E402
from omh.workflows import team  # noqa: E402

CHECK_A = "python -m unittest tests/test_a.py"
CHECK_B = "python -m unittest tests/test_b.py"
CHECK_C = "python -m unittest tests/test_c.py"
SESSION = "20260928_120000_team01"


def plan_for(*commands: str, stage: str = "accepted", own: bool = True) -> dict[str, object]:
    items = [{"text": f"Part {index}: check: `{command}`", "state": "pending"}
             for index, command in enumerate(commands, start=1)]
    plan: dict[str, object] = {"own_record": own, "status": "established", "items": items}
    if stage:
        plan["plan_stage"] = stage
    plan["items_digest"] = todo_items_digest(items)
    return plan


def unit(unit_id: str, command: str, depends_on: list[str] | None = None) -> dict[str, object]:
    return {"unit_id": unit_id, "title": f"Part {unit_id}", "verification_command": command,
            "depends_on": depends_on or []}


class FakeRunner:
    """Exit codes per command, consumed in order; the last one repeats."""

    def __init__(self, script: dict[str, list[team.CheckRun]]) -> None:
        self.script = {command: list(runs) for command, runs in script.items()}
        self.calls: list[str] = []
        self.during: Callable[[], None] | None = None

    def __call__(self, tokens: list[str], workdir: Path, timeout: int) -> team.CheckRun:
        command = " ".join(tokens)
        self.calls.append(command)
        if self.during is not None:
            hook, self.during = self.during, None
            hook()
        runs = self.script[command]
        return runs.pop(0) if len(runs) > 1 else runs[0]


def exited(code: int) -> team.CheckRun:
    return team.CheckRun(outcome="exited", exit_code=code, output_tail=f"exit {code}")


class TeamHarness(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "omh"
        self.workdir = Path(temporary.name) / "work"
        self.workdir.mkdir()
        self.clock = 1_790_000_000.0
        self.runner = FakeRunner({CHECK_A: [exited(0)], CHECK_B: [exited(0)], CHECK_C: [exited(0)]})
        self.fingerprints: list[tuple[str, str | None]] = []
        # Process memory (`TeamContext`): one set pair per simulated Hermes process.
        self.approved: set[tuple[str, str, str]] = set()
        self.observed: set[str] = set()
        self.host_refuses: dict[str, str] = {}

    def now(self) -> float:
        self.clock += 1.0
        return self.clock

    def fingerprint(self, workdir: Path) -> tuple[str, str | None]:
        return self.fingerprints.pop(0) if self.fingerprints else ("clean", "tree-1")

    def host_guard(self, command: str) -> str | None:
        return self.host_refuses.get(command)

    def restart(self) -> None:
        """A new Hermes process: nothing approved, no check observed."""
        self.approved = set()
        self.observed = set()

    @property
    def ctx(self) -> team.TeamContext:
        return team.TeamContext(omh_home=self.home, session_ref=SESSION, runner=self.runner,
                                fingerprint=self.fingerprint, now=self.now, host_guard=self.host_guard,
                                approved=self.approved, observed_checks=self.observed)

    def start(self, units: list[dict[str, object]], *, plan: dict[str, object] | None = None,
              **extra: object) -> dict[str, object]:
        commands = [str(item["verification_command"]) for item in units]
        plan = plan if plan is not None else plan_for(*commands)
        result = team.team_start(self.ctx, team_id="t1", units=units, plan=plan,
                                 plan_ref=extra.pop("plan_ref", plan["items_digest"]),
                                 workdir=self.workdir, **extra)
        # What the handler does once the host's approval gate let the call through.
        self.approved.add((SESSION, "t1", result["commands_digest"]))
        return result

    def dispatch_all(self, result: dict[str, object]) -> list[str]:
        """Play the host: start every returned helper, then report each back."""
        children = []
        for index, entry in enumerate(self.entries(result)):
            child = f"child-{self.clock}-{index}"
            self.assertTrue(team.observe_dispatch(self.home, SESSION, child_session_id=child,
                                                  goal=entry["goal"], now=self.now()))
            children.append(child)
        for child in children:
            self.assertTrue(team.observe_return(self.home, SESSION, child_session_id=child, now=self.now()))
        return children

    @staticmethod
    def entries(result: dict[str, object]) -> list[dict[str, str]]:
        prepared = result.get("delegate_task")
        return list(prepared["arguments"]["tasks"]) if isinstance(prepared, dict) else []

    def reconcile(self) -> dict[str, object]:
        return team.team_reconcile(self.ctx, team_id="t1")

    def record(self) -> dict[str, object]:
        record = team.read_team(team.team_path(self.home, SESSION, "t1"))
        assert record is not None
        return record

    def states(self, result: dict[str, object]) -> dict[str, str]:
        return {view["unit_id"]: view["state"] for view in result["units"]}


class T1AcceptedOnlyOnObservedExitZero(TeamHarness):
    """Mutation: treat a helper's return (the host's `subagent_stop`) as acceptance."""

    def test_a_returned_helper_is_awaiting_check_not_accepted(self) -> None:
        started = self.start([unit("a", CHECK_A)])
        self.dispatch_all(started)
        status = team.team_status(self.ctx, team_id="t1")
        self.assertEqual(self.states(status), {"a": "awaiting_check"})
        self.assertEqual(status["team_state"], "running")
        self.assertEqual(self.runner.calls, [])

    def test_exit_zero_observed_by_omh_accepts_and_names_the_evidence(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        result = self.reconcile()
        self.assertEqual(self.runner.calls, [CHECK_A])
        self.assertEqual(self.states(result), {"a": "accepted"})
        self.assertEqual(result["team_state"], "done")
        self.assertEqual(result["units"][0]["evidence"], {"kind": "team_check", "ref": "t1/a/attempt-1/check"})
        self.assertEqual(result["say"], "All 1 parts passed their checks.")

    def test_non_zero_exit_is_not_acceptance(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(1)]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        result = self.reconcile()
        self.assertNotEqual(self.states(result)["a"], "accepted")
        self.assertNotIn("evidence", result["units"][0])


class T2DownstreamWaitsForEveryParent(TeamHarness):
    """Mutation: `all` -> `any` in `_ready`, or release a child on a parent's return."""

    def test_child_waits_until_all_parents_are_accepted(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(0)], CHECK_B: [exited(1), exited(0)], CHECK_C: [exited(0)]})
        started = self.start([unit("a", CHECK_A), unit("b", CHECK_B), unit("c", CHECK_C, ["a", "b"])])
        self.assertEqual([entry["goal"].split("]")[0] for entry in self.entries(started)],
                         ["[omh-team:t1/a/attempt-1", "[omh-team:t1/b/attempt-1"])
        self.dispatch_all(started)
        first = self.reconcile()
        self.assertEqual(self.states(first), {"a": "accepted", "b": "repairing", "c": "waiting"})
        self.assertEqual([entry["goal"].split("]")[0] for entry in self.entries(first)],
                         ["[omh-team:t1/b/attempt-2"])
        self.dispatch_all(first)
        second = self.reconcile()
        self.assertEqual(self.states(second)["b"], "accepted")
        self.assertEqual([entry["goal"].split("]")[0] for entry in self.entries(second)],
                         ["[omh-team:t1/c/attempt-1"])

    def test_a_returned_but_unchecked_parent_releases_nothing(self) -> None:
        started = self.start([unit("a", CHECK_A), unit("c", CHECK_C, ["a"])])
        self.dispatch_all(started)
        self.assertEqual(self.states(team.team_status(self.ctx, team_id="t1"))["c"], "waiting")


class T3AttemptBudget(TeamHarness):
    """Mutation: `<` -> `<=` on the attempt limit, or drop the cap."""

    def test_default_is_two_repairs_then_blocked_with_the_last_check(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(3)]})
        result = self.start([unit("a", CHECK_A)])
        for _ in range(3):
            self.dispatch_all(result)
            result = self.reconcile()
        self.assertEqual(self.states(result), {"a": "blocked"})
        self.assertEqual(result["team_state"], "blocked")
        view = result["units"][0]
        self.assertEqual((view["attempts_used"], view["attempts_max"]), (3, 3))
        self.assertEqual(view["blocked"]["reason"], "repair_budget_exhausted")
        self.assertEqual(set(view["blocked"]["last_check"]), {"command", "exit_code", "observed_at"})
        self.assertEqual(view["blocked"]["last_check"]["exit_code"], 3)
        self.assertIsNone(result["delegate_task"])
        self.assertIn("still fails its check after 3 tries", result["say"])
        self.assertEqual(len(self.runner.calls), 3)

    def test_the_cap_is_three_repairs(self) -> None:
        with self.assertRaises(team.TeamRefusal) as refused:
            self.start([unit("a", CHECK_A)], max_repair_attempts=4)
        self.assertEqual(refused.exception.reason, "invalid_max_repair_attempts")
        started = self.start([unit("a", CHECK_A)], max_repair_attempts=3)
        self.assertEqual(started["units"][0]["attempts_max"], 4)


class T4RepairKeyCarriesTheAttempt(TeamHarness):
    """Mutation: drop the attempt number from the key, so a repair reuses the original."""

    def test_repair_entry_is_a_new_deterministic_attempt(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(1), exited(0)]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        repair = self.entries(self.reconcile())
        self.assertEqual(len(repair), 1)
        self.assertTrue(repair[0]["goal"].startswith("[omh-team:t1/a/attempt-2] "))
        self.assertIn('{"command": "python -m unittest tests/test_a.py", "exit_code": 1}', repair[0]["context"])
        self.assertNotIn("exit 1", repair[0]["context"])  # the output tail never travels
        # The original attempt's marker no longer binds: it is not the reserved attempt.
        self.assertFalse(team.observe_dispatch(self.home, SESSION, child_session_id="late",
                                               goal="[omh-team:t1/a/attempt-1] Part a", now=self.now()))
        self.assertEqual(team.attempt_key("t1", "a", 2), "t1/a/attempt-2")


class T5DoubleReconcileChecksOnce(TeamHarness):
    """Mutation: remove the lease, so a concurrent reconcile runs the check again."""

    def test_a_reconcile_during_a_running_check_skips_the_leased_unit(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(1)]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        nested: list[dict[str, object]] = []
        self.runner.during = lambda: nested.append(self.reconcile())
        self.reconcile()
        self.assertEqual(self.runner.calls, [CHECK_A])
        self.assertEqual(nested[0]["reason"], "check_in_progress")
        self.assertEqual(len(self.record()["units"][0]["attempts"]), 2)

    def test_an_expired_lease_reruns_without_spending_an_attempt(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        path = team.team_path(self.home, SESSION, "t1")
        record = self.record()
        record["units"][0]["lease"] = {"attempt": 1, "nonce": "dead", "started_at": team._iso(self.clock - 3600)}
        team.write_team(path, record)
        result = self.reconcile()
        self.assertEqual(self.states(result), {"a": "accepted"})
        self.assertEqual(result["units"][0]["attempts_used"], 1)


class T6InconclusiveSpendsNothing(TeamHarness):
    """Mutation: treat a check that never ran (or ran on a moving tree) as a failure."""

    def run_one(self, run: team.CheckRun) -> dict[str, object]:
        self.runner = FakeRunner({CHECK_A: [run]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        return self.reconcile()

    def test_timeout_and_not_found_spend_no_attempt(self) -> None:
        for run, reason in ((team.CheckRun("timeout", None, ""), "check_timeout"),
                            (team.CheckRun("not_found", None, ""), "command_not_found")):
            with self.subTest(reason=reason):
                self.setUp()
                result = self.run_one(run)
                view = result["units"][0]
                self.assertEqual((view["state"], view["attempts_used"]), ("awaiting_check", 1))
                self.assertIsNone(result["delegate_task"])
                self.assertEqual(self.record()["units"][0]["attempts"][0]["inconclusive"][0]["reason"], reason)

    def test_missing_workspace_is_inconclusive(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        self.workdir.rmdir()
        result = self.reconcile()
        self.assertEqual(result["units"][0]["attempts_used"], 1)
        self.assertEqual(self.runner.calls, [])

    def test_a_tree_that_moved_during_the_check_is_inconclusive(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(1)]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        self.fingerprints = [("dirty", "before"), ("dirty", "after")]
        result = self.reconcile()
        self.assertEqual(result["units"][0]["attempts_used"], 1)
        self.assertEqual(self.record()["units"][0]["attempts"][0]["inconclusive"][0]["reason"],
                         "workspace_changed_during_check")

    def test_three_inconclusive_checks_block_the_unit(self) -> None:
        self.runner = FakeRunner({CHECK_A: [team.CheckRun("timeout", None, "")]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        for _ in range(3):
            result = self.reconcile()
        self.assertEqual(result["units"][0]["blocked"]["reason"], "check_inconclusive")
        self.assertEqual(result["team_state"], "blocked")


class T7CommandsAreFrozen(TeamHarness):
    """Mutation: drop the frozen-digest checks on restart and on reconcile."""

    def test_restart_with_a_changed_command_is_refused(self) -> None:
        self.start([unit("a", CHECK_A)])
        changed = plan_for(CHECK_B)
        with self.assertRaises(team.TeamRefusal) as refused:
            team.team_start(self.ctx, team_id="t1", units=[unit("a", CHECK_B)], plan=changed,
                            plan_ref=changed["items_digest"], workdir=self.workdir)
        self.assertEqual(refused.exception.reason, "commands_frozen")

    def test_a_command_edited_in_the_record_runs_nothing(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        record = self.record()
        record["units"][0]["verification_command"] = "python -m unittest tests/test_other.py"
        team.write_team(team.team_path(self.home, SESSION, "t1"), record)
        with self.assertRaises(team.TeamRefusal) as refused:
            self.reconcile()
        self.assertEqual(refused.exception.reason, "frozen_commands_changed")
        self.assertEqual(self.runner.calls, [])


class T8ApprovalBinding(TeamHarness):
    """Mutation: remove any one predicate of the plan binding or the command policy."""

    def refused(self, **kwargs: object) -> str:
        with self.assertRaises(team.TeamRefusal) as refused:
            self.start(**kwargs)
        return refused.exception.reason

    def test_command_must_be_verbatim_in_the_accepted_plan(self) -> None:
        self.assertEqual(self.refused(units=[unit("a", CHECK_A)], plan=plan_for(CHECK_B)),
                         "command_not_in_accepted_plan")

    def test_plan_must_be_accepted_owned_and_the_same_plan(self) -> None:
        self.assertEqual(self.refused(units=[unit("a", CHECK_A)], plan=plan_for(CHECK_A, stage="")),
                         "plan_not_accepted")
        self.assertEqual(self.refused(units=[unit("a", CHECK_A)],
                                      plan=plan_for(CHECK_A, stage="awaiting_acceptance")), "plan_not_accepted")
        self.assertEqual(self.refused(units=[unit("a", CHECK_A)], plan=plan_for(CHECK_A, own=False)),
                         "plan_not_found")
        self.assertEqual(self.refused(units=[unit("a", CHECK_A)], plan_ref="0" * 32), "plan_ref_mismatch")

    def test_command_policy_refuses_forge_remote_git_shells_and_inline_python(self) -> None:
        cases = {
            "gh pr merge 12": "command_talks_to_a_forge",
            "uv run gh api repos": "command_talks_to_a_forge",
            "git push origin main": "command_moves_a_remote",
            "git -C repo pull": "command_moves_a_remote",
            "bash scripts/test.sh": "command_runs_a_shell",
            "/bin/sh -x run.sh": "command_runs_a_shell",
            "env FOO=1 pytest": "command_runs_a_shell",
            "python -c print": "command_inline_program",
            "uv run python3 -c print": "command_inline_program",
            "pytest && rm -rf x": "command_shell_syntax",
            "pytest | tee log": "command_shell_syntax",
        }
        for command, reason in cases.items():
            with self.subTest(command=command):
                with self.assertRaises(team.TeamRefusal) as refused:
                    team.validate_team_command(command)
                self.assertEqual(refused.exception.reason, reason)
        self.assertEqual(team.validate_team_command("git diff --check"), ["git", "diff", "--check"])
        self.assertEqual(team.validate_team_command("npm test"), ["npm", "test"])

    def test_start_escalates_to_the_person_with_the_exact_commands(self) -> None:
        args = {"action": "team_start", "team_id": "t1", "plan_ref": "ref",
                "units": [unit("a", CHECK_A), unit("b", CHECK_B)]}
        directive = team_gate.team_start_directive(tool_name="omh_team", tool_input=args, escalation_allowed=True)
        assert directive is not None
        self.assertEqual(directive["action"], "approve")
        self.assertIn(f"1. {CHECK_A}\n2. {CHECK_B}", directive["message"])
        self.assertIn("real home folder", directive["message"])
        self.assertEqual(directive["rule_key"], team.approval_rule_key([CHECK_A, CHECK_B]))
        unattended = team_gate.team_start_directive(tool_name="omh_team", tool_input=args, escalation_allowed=False)
        self.assertEqual(unattended["action"], "block")
        # No plan_ref: the engine refuses before freezing anything, so the person is not asked twice.
        for other in ({**args, "action": "team_reconcile"}, {**args, "units": []}, {**args, "plan_ref": ""}):
            self.assertIsNone(team_gate.team_start_directive(tool_name="omh_team", tool_input=other,
                                                             escalation_allowed=True))
        self.assertIsNone(team_gate.team_start_directive(tool_name="omh_agent_board", tool_input=args,
                                                         escalation_allowed=True))

    def test_pre_tool_call_returns_the_team_start_approval(self) -> None:
        from omh.plugin_bundle.omh.hooks.tool_hooks import pre_tool_call

        with tempfile.TemporaryDirectory() as home:
            directive = pre_tool_call(tool_name="omh_team", session_id="s-approve", omh_home=home, hermes_home=home,
                                      args={"action": "team_start", "team_id": "t1", "plan_ref": "ref",
                                            "units": [unit("a", CHECK_A)]})
        assert directive is not None
        self.assertEqual(directive["action"], "approve")
        self.assertIn(CHECK_A, directive["message"])


class T9CallerGuard(unittest.TestCase):
    """Mutation: remove the board-worker or delegate-child refusal in the handler."""

    def setUp(self) -> None:
        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)

    def call(self, session: str) -> dict[str, object]:
        from omh.plugin_bundle.omh.tools.team_tool import omh_team_handler

        return json.loads(omh_team_handler({"action": "team_status", "team_id": "t1"}, session_id=session))

    def test_a_board_worker_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "task-7"}):
            result = self.call("worker-session")
        self.assertEqual((result["status"], result["reason"]), ("refused", "called_from_board_worker"))

    def test_a_delegate_child_is_refused(self) -> None:
        from omh.plugin_bundle.omh import runtime_paths

        nudge_budget.note_delegated_session("helper-session", omh_home=str(runtime_paths.plugin_home(None)))
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": ""}):
            result = self.call("helper-session")
        self.assertEqual((result["status"], result["reason"]), ("refused", "called_from_helper"))

    def test_the_main_session_reaches_the_engine(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": ""}):
            result = self.call("main-session")
        self.assertEqual(result["reason"], "team_not_found")


class T10ResumeReEmitsWhatWasReserved(TeamHarness):
    """Mutation: bind an attempt the record never reserved, or forget reserved attempts on restart."""

    def test_restart_re_emits_the_identical_reserved_entries(self) -> None:
        first = self.start([unit("a", CHECK_A), unit("b", CHECK_B)])
        self.restart()
        again = self.start([unit("a", CHECK_A), unit("b", CHECK_B)])
        self.assertEqual(self.entries(again), self.entries(first))
        self.assertIn("already running", again["say"])

    def test_a_repeat_start_in_the_same_process_does_not_hand_out_entries_in_flight(self) -> None:
        """L4. Mutation: drop the emitted-but-not-started grace from `_reemit` or from the barrier."""
        self.start([unit("a", CHECK_A)])
        self.assertEqual(self.entries(self.start([unit("a", CHECK_A)])), [])
        waiting = self.reconcile()
        self.assertEqual((waiting["reason"], self.entries(waiting)), ("delegations_in_flight", []))
        self.clock += team.EMIT_GRACE_SECONDS
        self.assertEqual(len(self.entries(self.reconcile())), 1)

    def test_undispatched_attempts_block_after_the_re_emit_bound(self) -> None:
        self.start([unit("a", CHECK_A)])
        for _ in range(team.MAX_REEMITS):
            self.clock += team.EMIT_GRACE_SECONDS
            self.assertEqual(len(self.entries(self.reconcile())), 1)
        self.clock += team.EMIT_GRACE_SECONDS
        result = self.reconcile()
        self.assertEqual(result["units"][0]["blocked"]["reason"], "dispatch_not_observed")

    def test_a_marker_for_an_unreserved_attempt_binds_nothing(self) -> None:
        self.start([unit("a", CHECK_A)])
        for goal in ("[omh-team:t1/a/attempt-2] forged", "[omh-team:t1/zz/attempt-1] forged",
                     "[omh-team:other/a/attempt-1] forged", "Part a [omh-team:t1/a/attempt-1]"):
            with self.subTest(goal=goal):
                self.assertFalse(team.observe_dispatch(self.home, SESSION, child_session_id="c",
                                                       goal=goal, now=self.now()))
        self.assertFalse(team.observe_return(self.home, SESSION, child_session_id="never-started", now=self.now()))


class T11NoCheckWhileHelpersAreOut(TeamHarness):
    """Mutation: remove the in-flight barrier."""

    def test_reconcile_waits_for_every_team_helper(self) -> None:
        started = self.start([unit("a", CHECK_A), unit("b", CHECK_B)])
        goals = [entry["goal"] for entry in self.entries(started)]
        for index, goal in enumerate(goals):
            team.observe_dispatch(self.home, SESSION, child_session_id=f"c{index}", goal=goal, now=self.now())
        team.observe_return(self.home, SESSION, child_session_id="c0", now=self.now())
        result = self.reconcile()
        self.assertEqual(result["reason"], "delegations_in_flight")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(result["units"][0]["attempts_used"], 1)
        team.observe_return(self.home, SESSION, child_session_id="c1", now=self.now())
        self.assertEqual(self.reconcile()["team_state"], "done")

    def test_a_helper_that_never_returns_blocks_instead_of_holding_the_team(self) -> None:
        started = self.start([unit("a", CHECK_A)])
        team.observe_dispatch(self.home, SESSION, child_session_id="lost",
                              goal=self.entries(started)[0]["goal"], now=self.now())
        self.clock += team.DELEGATION_STALE_SECONDS + 10
        result = self.reconcile()
        self.assertEqual(result["units"][0]["blocked"]["reason"], "delegation_not_returned")
        self.assertEqual(result["team_state"], "blocked")


class T12TeamStateIsDerived(TeamHarness):
    """Mutation: store a team state and read it back instead of deriving it."""

    def test_no_state_is_written_and_a_planted_one_is_ignored(self) -> None:
        self.start([unit("a", CHECK_A)])
        record = self.record()
        self.assertNotIn("team_state", record)
        self.assertNotIn("state", record)
        record["team_state"] = "done"
        record["state"] = "done"
        team.write_team(team.team_path(self.home, SESSION, "t1"), record)
        self.assertEqual(team.team_status(self.ctx, team_id="t1")["team_state"], "running")


class TeamStatusSpeaksPlainly(TeamHarness):
    def test_status_carries_counts_caveat_cost_and_plain_say_lines(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(2)], CHECK_B: [exited(0)]})
        started = self.start([unit("a", CHECK_A), unit("b", CHECK_B)])
        self.assertEqual(started["say"],
                         "Split into 2 parts; 2 start now in parallel, and each part is done only when its check passes.")
        self.dispatch_all(started)
        repaired = self.reconcile()
        self.assertIn("sending it back for a fix, try 2 of 3", repaired["say"])
        status = team.team_status(self.ctx, team_id="t1", cost={"status": "observed", "conversation_cost_usd": 0.5})
        self.assertEqual(status["counts"]["accepted"], 1)
        self.assertEqual(status["counts"]["repairing"], 1)
        self.assertEqual(status["cost"]["conversation_cost_usd"], 0.5)
        self.assertEqual(status["caveat"], team.TEAM_CAVEAT)
        self.assertEqual(status["units"][0]["last_check"]["exit_code"], 2)
        record_vocabulary = ("prepared_not_observed", "ledger", "receipt", "lane", "omh_team", "schema")
        for text in (started["say"], repaired["say"], status["say"], status["caveat"],
                     *team._BLOCKED_SAY.values(), *team._REASON_SAY.values()):
            for word in record_vocabulary:
                self.assertNotIn(word, text)


class TeamEventsAreRenderable(TeamHarness):
    """A header (teammate + event), one plain summary line, and a reference: nothing else."""

    def lifecycle(self) -> list[dict[str, object]]:
        self.runner = FakeRunner({CHECK_A: [exited(1), exited(0)]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        self.dispatch_all(self.reconcile())
        return list(self.reconcile()["events"])

    def test_one_lifecycle_in_order_with_a_monotonic_seq(self) -> None:
        events = self.lifecycle()
        self.assertEqual([item["event"] for item in events],
                         ["started", "finished", "check_failed", "repairing", "started", "finished",
                          "check_passed", "done"])
        seqs = [item["seq"] for item in events]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        for item in events:
            self.assertEqual(set(item), {"seq", "at", "unit_id", "teammate", "event", "summary", "detail_ref"})
            self.assertIn(item["event"], team.TEAM_EVENTS)
            self.assertNotEqual(item["teammate"], item["unit_id"])
        failed = events[2]
        self.assertEqual((failed["teammate"], failed["detail_ref"]), ("Part a", "t1/a/attempt-1/check"))
        self.assertEqual(events[-1]["teammate"], "The team")

    def test_since_seq_returns_only_newer_events(self) -> None:
        events = self.lifecycle()
        status = team.team_status(self.ctx, team_id="t1", since_seq=5)
        self.assertEqual([item["seq"] for item in status["events"]], [item["seq"] for item in events if item["seq"] > 5])
        self.assertEqual(status["last_seq"], events[-1]["seq"])
        self.assertFalse(status["events_truncated"])
        self.assertEqual(team.team_status(self.ctx, team_id="t1", since_seq=status["last_seq"])["events"], [])

    def test_every_summary_passes_the_reply_lint(self) -> None:
        from omh.quality.reply_lint import build_reply_lint

        events = self.lifecycle()
        self.runner = FakeRunner({CHECK_B: [exited(4)]})
        record = self.record()
        team._block(record, record["units"][0], self.now(), {"reason": "dispatch_not_observed"})
        summaries = {item["summary"] for item in events} | {record["events"][-1]["summary"]}
        summaries |= {f"Stopped: {why}." for why in team._BLOCKED_SAY.values()}
        for summary in summaries:
            with self.subTest(summary=summary):
                self.assertTrue(build_reply_lint(summary)["ok"], build_reply_lint(summary)["findings"])
                self.assertNotIn("\n", summary)

    def test_the_list_is_capped_and_seq_keeps_counting(self) -> None:
        self.start([unit("a", CHECK_A)])
        record = self.record()
        for _ in range(team.MAX_TEAM_EVENTS + 6):
            team._emit(record, self.now(), unit=record["units"][0], event="started",
                       summary="Started working on this part.", detail_ref="t1/a/attempt-1")
        self.assertEqual(len(record["events"]), team.MAX_TEAM_EVENTS)
        self.assertEqual(record["events"][0]["seq"], 7)
        self.assertEqual(record["events"][-1]["seq"], team.MAX_TEAM_EVENTS + 6)
        view = team._events_since(record, 0)
        self.assertTrue(view["events_truncated"])
        self.assertFalse(team._events_since(record, 6)["events_truncated"])


def fake_hermes_floor(blocked: frozenset[str] = frozenset(), *, cron: bool = False,
                      raises: bool = False) -> dict[str, object]:
    """`tools.approval` / `tools.approval_context` as the Hermes process would provide them."""
    def floor(command: str) -> dict[str, object] | None:
        if raises:
            raise RuntimeError("floor exploded")
        return {"approved": False} if command in blocked else None

    approval = SimpleNamespace(_floor_block=floor, _user_deny_block=lambda command: None)
    context = SimpleNamespace(_is_cron_approval_context=lambda: cron)
    return {"tools": SimpleNamespace(approval=approval, approval_context=context),
            "tools.approval": approval, "tools.approval_context": context}


class TeamToolHandler(unittest.TestCase):
    """The plugin handler end to end: plan read, start, the host floor, and process-memory approval."""

    SESSION = "20260928_130000_handler"

    def setUp(self) -> None:
        from omh.plugin_bundle.omh import runtime_paths
        from omh.plugin_bundle.omh.todo_store import build_todo_record, write_todo
        from omh.plugin_bundle.omh.tools import team_tool

        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        for memory in (team_tool._APPROVED_TEAMS, team_tool._OBSERVED_CHECKS):
            memory.clear()
            self.addCleanup(memory.clear)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name)
        self.team_tool = team_tool
        record = build_todo_record("Team plan", [{"text": f"Build it; check: `{CHECK_A}`", "state": "active"}],
                                   source="omh_todo", session_ref=self.SESSION, plan_stage="accepted")
        write_todo(runtime_paths.default_omh_home(), record)
        self.args = {"action": "team_start", "team_id": "handler", "units": [unit("a", CHECK_A)],
                     "plan_ref": todo_items_digest(record["items"])}
        env = mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "", "HERMES_CRON_SESSION": ""})
        cwd = mock.patch.object(runtime_paths, "runtime_cwd", return_value=self.workdir)
        env.start()
        cwd.start()
        self.addCleanup(env.stop)
        self.addCleanup(cwd.stop)

    def call(self, args: dict[str, object], hermes: dict[str, object] | None = None) -> dict[str, object]:
        with patch_modules(hermes if hermes is not None else fake_hermes_floor()):
            return json.loads(self.team_tool.omh_team_handler(args, session_id=self.SESSION))

    def test_start_reads_this_sessions_accepted_plan_and_hands_back_the_plan_ref(self) -> None:
        refused = self.call({**self.args, "plan_ref": "x"})
        self.assertEqual(refused["reason"], "plan_ref_mismatch")
        started = self.call({**self.args, "plan_ref": refused["plan_ref"]})
        self.assertEqual(started["status"], "ok")
        self.assertEqual(started["team_state"], "running")
        self.assertEqual(len(started["delegate_task"]["arguments"]["tasks"]), 1)

    def test_no_reachable_hermes_floor_refuses_every_start(self) -> None:
        """H1(f). Mutation: let `_host_floor` return None when `tools.approval` cannot be imported."""
        missing = self.call(self.args, hermes={"tools": SimpleNamespace()})
        self.assertEqual((missing["status"], missing["reason"]), ("refused", "host_command_floor_unavailable"))
        raising = self.call(self.args, hermes=fake_hermes_floor(raises=True))
        self.assertEqual(raising["reason"], "host_command_floor_unavailable")
        self.assertEqual(self.team_tool._APPROVED_TEAMS, set())

    def test_the_hermes_floor_refuses_a_command_it_blocks(self) -> None:
        """H1(f). Mutation: ignore `_floor_block`'s answer."""
        refused = self.call(self.args, hermes=fake_hermes_floor(frozenset({CHECK_A})))
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "command_refused_by_host"))

    def test_a_cron_session_cannot_start_a_team(self) -> None:
        """M3. Mutation: drop the cron refusal."""
        refused = self.call(self.args, hermes=fake_hermes_floor(cron=True))
        self.assertEqual((refused["status"], refused["reason"]), ("refused", "called_from_cron"))

    def test_only_a_start_in_this_process_approves_a_reconcile(self) -> None:
        """H2. Mutation: add to `_APPROVED_TEAMS` anywhere but after a successful start, or not at all."""
        reconcile = {"action": "team_reconcile", "team_id": "handler"}
        self.assertEqual(self.call(self.args)["status"], "ok")
        self.assertEqual(self.call(reconcile)["reason"], "delegations_in_flight")
        self.team_tool._APPROVED_TEAMS.clear()  # a Hermes restart
        self.assertEqual(self.call(reconcile)["reason"], "team_not_approved_here")
        self.assertEqual(self.call(self.args)["status"], "ok")
        self.assertEqual(self.call(reconcile)["status"], "ok")


class HostLifecycleHooksDriveTheBarrier(TeamHarness):
    """The registered `subagent_start` / `subagent_stop` hooks, not the engine calls, move a helper."""

    def test_hooks_bind_start_and_return(self) -> None:
        from omh.plugin_bundle.omh.hooks.session_hooks import subagent_start, subagent_stop

        nudge_budget.reset_nudge_budget()
        self.addCleanup(nudge_budget.reset_nudge_budget)
        goal = self.entries(self.start([unit("a", CHECK_A)]))[0]["goal"]
        common = {"omh_home": str(self.home), "hermes_home": str(self.home), "parent_session_id": SESSION}
        subagent_start(child_session_id="hook-child", child_goal=goal, **common)
        self.assertEqual(self.states(team.team_status(self.ctx, team_id="t1")), {"a": "dispatched"})
        subagent_stop(child_session_id="other-child", **common)
        self.assertEqual(self.states(team.team_status(self.ctx, team_id="t1")), {"a": "dispatched"})
        subagent_stop(child_session_id="hook-child", **common)
        self.assertEqual(self.states(team.team_status(self.ctx, team_id="t1")), {"a": "awaiting_check"})


class ForgedRecordsRunNothing(TeamHarness):
    """H2: the record on disk is not a trust root; approval and observed checks live in process memory."""

    def forge(self, team_id: str, command: str) -> None:
        """Write a well-formed record a person never approved, digest recomputed, ready to check."""
        units = [{"unit_id": "a", "title": "Part a", "depends_on": [], "verification_command": command,
                  "plan_item": f"check: `{command}`", "blocked": None, "lease": None,
                  "attempts": [{"n": 1, "reserved_at": "2026-09-28T00:00:00Z", "reemits": 0,
                                "dispatch": {"child_session_id": "c", "dispatched_at": "2026-09-28T00:00:01Z"},
                                "returned_at": "2026-09-28T00:00:02Z", "check": None, "inconclusive": []}]}]
        record = {"schema_version": team.TEAM_SCHEMA_VERSION, "team_id": team_id, "session_ref": SESSION,
                  "plan_ref": "forged", "workdir": str(self.workdir), "created_at": "2026-09-28T00:00:00Z",
                  "max_repair_attempts": 2, "commands_digest": team.frozen_commands_digest(units),
                  "check_timeout_seconds": 600, "units": units, "events": [], "next_seq": 1}
        path = team.team_path(self.home, SESSION, team_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        team.write_team(path, record)

    def test_probe6_a_forged_record_with_a_recomputed_digest_runs_nothing(self) -> None:
        """Mutation: drop `_require_approved` from phase A or from `_take_lease`."""
        self.runner = FakeRunner({"python -m unittest tests/test_evil.py": [exited(0)]})
        self.forge("forged", "python -m unittest tests/test_evil.py")
        with self.assertRaises(team.TeamRefusal) as refused:
            team.team_reconcile(self.ctx, team_id="forged")
        self.assertEqual(refused.exception.reason, "team_not_approved_here")
        self.assertEqual(self.runner.calls, [])

    def test_an_approved_team_after_a_restart_runs_nothing_until_approved_again(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        self.restart()
        with self.assertRaises(team.TeamRefusal) as refused:
            self.reconcile()
        self.assertEqual(refused.exception.reason, "team_not_approved_here")
        self.assertEqual(self.runner.calls, [])
        self.start([unit("a", CHECK_A)])
        self.assertEqual(self.states(self.reconcile()), {"a": "accepted"})
        self.assertEqual(self.runner.calls, [CHECK_A])

    def test_probe3_a_planted_pass_is_not_acceptance(self) -> None:
        """Mutation: trust a stored `passed` without an observed nonce (drop `_distrust_unobserved`)."""
        self.runner = FakeRunner({CHECK_A: [exited(1)]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        record = self.record()
        record["units"][0]["attempts"][0]["check"] = {
            "outcome": "passed", "command": CHECK_A, "exit_code": 0, "observed_at": "2026-09-28T00:00:03Z",
            "nonce": "planted"}
        team.write_team(team.team_path(self.home, SESSION, "t1"), record)
        self.assertEqual(self.states(team.team_status(self.ctx, team_id="t1")), {"a": "awaiting_check"})
        result = self.reconcile()
        self.assertEqual(self.runner.calls, [CHECK_A])
        self.assertEqual(self.states(result), {"a": "repairing"})
        self.assertNotEqual(result["team_state"], "done")

    def test_a_pass_from_before_a_restart_is_earned_again(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        self.assertEqual(self.states(self.reconcile()), {"a": "accepted"})
        self.restart()
        self.start([unit("a", CHECK_A)])
        self.assertEqual(self.states(team.team_status(self.ctx, team_id="t1")), {"a": "awaiting_check"})
        self.assertEqual(self.states(self.reconcile()), {"a": "accepted"})
        self.assertEqual(self.runner.calls, [CHECK_A, CHECK_A])


class PlanBindingIsAnExactCheckField(TeamHarness):
    """H3. Mutation: bind on a substring of the item text instead of an exact `check:` field."""

    def bind(self, command: str, text: str) -> str:
        plan = plan_for(command)
        plan["items"] = [{"text": text, "state": "pending"}]
        plan["items_digest"] = todo_items_digest(plan["items"])
        try:
            self.start([unit("a", command)], plan=plan)
        except team.TeamRefusal as refused:
            return refused.reason
        return "bound"

    def test_prose_and_prefixes_bind_nothing(self) -> None:
        self.assertEqual(self.bind("make deploy", "Do NOT run make deploy under any circumstance"),
                         "command_not_in_accepted_plan")
        self.assertEqual(self.bind("make deploy", "Do NOT run `make deploy` under any circumstance"),
                         "command_not_in_accepted_plan")
        self.assertEqual(self.bind("make", "Run the suite; check: `make test`"), "command_not_in_accepted_plan")
        self.assertEqual(self.bind("make test", "Run the suite; check: `make test`"), "bound")


class ChecksAreBoundedAndLeasedOneAtATime(TeamHarness):
    """M2. Mutation: lease every unit up front, or drop MAX_CHECKS_PER_RECONCILE."""

    def test_one_reconcile_runs_at_most_the_cap_and_leases_each_just_before_it_runs(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A), unit("b", CHECK_B), unit("c", CHECK_C)]))
        leases_while_a_ran: list[object] = []
        self.runner.during = lambda: leases_while_a_ran.extend(
            item["lease"] for item in self.record()["units"])
        first = self.reconcile()
        self.assertEqual(self.runner.calls, [CHECK_A, CHECK_B][:team.MAX_CHECKS_PER_RECONCILE])
        self.assertEqual(first["reason"], "checks_pending")
        self.assertIsNotNone(leases_while_a_ran[0])
        self.assertEqual(leases_while_a_ran[1:], [None, None])
        self.assertEqual(self.states(self.reconcile()), {"a": "accepted", "b": "accepted", "c": "accepted"})
        self.assertEqual(self.runner.calls, [CHECK_A, CHECK_B, CHECK_C])


class InconclusiveOutsideGitAndOnASignal(TeamHarness):
    """M1 and L2. Mutation: run the check in a non-git workspace, or read a signal exit as a failure."""

    def test_a_workspace_outside_git_is_inconclusive_and_runs_nothing(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        for state in ("not_a_repository", "unsupported", "not_a_repository"):
            self.fingerprints = [(state, None)]
            result = self.reconcile()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(result["units"][0]["attempts_used"], 1)
        self.assertEqual(result["units"][0]["blocked"],
                         {"reason": "check_inconclusive", "last_reason": "workspace_not_tracked",
                          "observed_at": result["units"][0]["blocked"]["observed_at"]})

    def test_a_negative_return_code_is_inconclusive(self) -> None:
        self.runner = FakeRunner({CHECK_A: [exited(-9), exited(0)]})
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        killed = self.reconcile()
        self.assertEqual((self.states(killed), killed["units"][0]["attempts_used"]), ({"a": "awaiting_check"}, 1))
        self.assertIn("stopped by a signal", killed["say"])
        self.assertEqual(self.states(self.reconcile()), {"a": "accepted"})


class HostFloorAtStartAndAtRun(TeamHarness):
    """H1(f) in the engine. Mutation: skip `host_guard` at start or before a check runs."""

    def test_a_command_the_host_refuses_never_starts(self) -> None:
        self.host_refuses[CHECK_A] = "command_refused_by_host"
        with self.assertRaises(team.TeamRefusal) as refused:
            self.start([unit("a", CHECK_A)])
        self.assertEqual(refused.exception.reason, "command_refused_by_host")

    def test_a_command_the_host_refuses_later_blocks_without_running(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        self.host_refuses[CHECK_A] = "host_command_floor_unavailable"
        result = self.reconcile()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(result["units"][0]["blocked"]["reason"], "host_command_floor_unavailable")


class DamagedRecordsAreRefused(TeamHarness):
    """L1. Mutation: drop `_record_shape_ok` from `read_team`."""

    def test_a_record_missing_fields_is_a_refusal_not_a_crash(self) -> None:
        self.dispatch_all(self.start([unit("a", CHECK_A)]))
        path = team.team_path(self.home, SESSION, "t1")
        good = self.record()
        damages = {
            "title missing": lambda r: r["units"][0].pop("title"),
            "attempt number missing": lambda r: r["units"][0]["attempts"][0].pop("n"),
            "next_seq missing": lambda r: r.pop("next_seq"),
            "unknown parent": lambda r: r["units"][0].__setitem__("depends_on", ["ghost"]),
            "event without a seq": lambda r: r["events"].append({"seq": "x", "event": "started"}),
        }
        for name, damage in damages.items():
            record = json.loads(json.dumps(good))
            damage(record)
            path.write_text(json.dumps(record), encoding="utf-8", newline="\n")
            for call in (self.reconcile, lambda: team.team_status(self.ctx, team_id="t1")):
                with self.subTest(damage=name), self.assertRaises(team.TeamRefusal) as refused:
                    call()
                self.assertEqual(refused.exception.reason, "team_store_unreadable")


class HiddenCharactersAreRefused(TeamHarness):
    """L3. Mutation: drop `_HIDDEN_CHARACTERS` from the command or the title check."""

    def test_bidi_and_zero_width_characters(self) -> None:
        for character in ("\u202e", "\u2066", "\u200b", "\u200f"):
            with self.subTest(character=repr(character)):
                with self.assertRaises(team.TeamRefusal) as refused:
                    team.validate_team_command(f"pytest{character} -q")
                self.assertEqual(refused.exception.reason, "command_hidden_characters")
                titled = {**unit("a", CHECK_A), "title": f"Part{character} a"}
                with self.assertRaises(team.TeamRefusal) as refused:
                    self.start([titled])
                self.assertEqual(refused.exception.reason, "invalid_title")


class AgentBoardStaysByteIdentical(unittest.TestCase):
    """`omh_agent_board` outputs as captured at origin/main 1239b36bd, compared as strings.

    Mutation: change any prepared field, the schema text, or an unavailable reason.
    """

    def test_every_pinned_output_is_byte_identical(self) -> None:
        from _agent_board_golden import agent_board_golden_outputs

        expected = json.loads((Path(__file__).parent / "fixtures/agent_board_golden.json").read_text(encoding="utf-8"))
        actual = agent_board_golden_outputs()
        self.assertEqual(sorted(actual), sorted(expected))
        for name, value in expected.items():
            with self.subTest(case=name):
                self.assertEqual(actual[name], value)


class ApprovalKeyParity(unittest.TestCase):
    def test_the_gate_and_the_engine_derive_the_same_always_key(self) -> None:
        commands = [CHECK_A, "npm test"]
        self.assertEqual(team_gate.team_start_rule_key(commands), team.approval_rule_key(commands))


if __name__ == "__main__":
    unittest.main()
