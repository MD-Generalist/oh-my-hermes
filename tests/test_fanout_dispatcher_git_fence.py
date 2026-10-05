"""Git the dispatcher runs in a unit worktree runs inside that unit's write fence (#1990)."""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

from _local_package import load_local_package
from _platform_support import requires_posix

load_local_package()

from omh.coding.fanout_confinement import (  # noqa: E402
    FanoutFilesystemConfinement,
    prepare_fanout_filesystem_confinement,
)
from omh.coding.fanout_capacity import AdmissionBinding  # noqa: E402
from omh.coding.fanout_dispatch import (  # noqa: E402
    _capacity_lineage,
    _capture_unit_recovery,
    _fenced_dispatcher_runner,
    _git_text,
    _observed_clean_producer_head,
    _run_verification_command,
    signal_safe_unit_runner,
)
from omh.coding.fanout_executor_sessions import observe_session_workspace  # noqa: E402
from omh.quality.cross_harness_adapter_sandbox import ChildContext  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


def _linked_worktree(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _ = subprocess.run(("/usr/bin/git", "init", "-q"), cwd=repo, check=True)
    (repo / "seed").write_text("seed", encoding="utf-8")
    _ = subprocess.run(("/usr/bin/git", "add", "seed"), cwd=repo, check=True)
    _ = subprocess.run(
        ("/usr/bin/git", "-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "init"),
        cwd=repo,
        check=True,
    )
    worktree = root / "linked-worktree"
    _ = subprocess.run(("/usr/bin/git", "worktree", "add", "-qb", "agent/unit", str(worktree), "HEAD"), cwd=repo, check=True)
    return worktree


def _host_git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(("/usr/bin/git", *arguments), cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> SimpleNamespace:
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout="observed\n")


def _fence(*, enforced: bool = True, wraps: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        receipt={"enforced": enforced},
        dispatcher_command=lambda argv: ("fence", *argv) if wraps else None,
    )


class FencedDispatcherRunnerTests(unittest.TestCase):
    """The wrapper's own decisions, with no sandbox involved, so every job runs them."""

    def test_without_an_enforced_fence_the_runner_is_returned_unchanged(self) -> None:
        runner = _Recorder()
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            self.assertIs(_fenced_dispatcher_runner(runner, None, worktree), runner)
            self.assertIs(_fenced_dispatcher_runner(runner, _fence(enforced=False), worktree), runner)  # type: ignore[arg-type]

    def test_a_call_in_the_unit_worktree_is_wrapped_in_the_fence(self) -> None:
        runner = _Recorder()
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            fenced = _fenced_dispatcher_runner(runner, _fence(), worktree)  # type: ignore[arg-type]
            self.assertEqual(_git_text(fenced, worktree, ["git", "rev-parse", "HEAD"]), "observed\n")
        (argv, kwargs), = runner.calls
        self.assertEqual(argv, ["git", "rev-parse", "HEAD"])
        self.assertEqual(kwargs["confinement_command"], ("fence", "git", "rev-parse", "HEAD"))

    def test_a_call_with_another_cwd_passes_through_unwrapped(self) -> None:
        runner = _Recorder()
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary) / "worktree"
            elsewhere = Path(temporary) / "repo"
            worktree.mkdir()
            elsewhere.mkdir()
            fenced = _fenced_dispatcher_runner(runner, _fence(), worktree)  # type: ignore[arg-type]
            _ = fenced(["git", "check-ignore", "-q", "--", "node_modules"], cwd=str(elsewhere))
            _ = fenced(["git", "check-ignore", "-q", "--", "node_modules"])
        self.assertEqual([("confinement_command" in kwargs) for _argv, kwargs in runner.calls], [False, False])

    def test_a_call_below_the_unit_worktree_is_refused_not_run_on_the_host(self) -> None:
        runner = _Recorder()
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            (worktree / "nested").mkdir()
            fenced = _fenced_dispatcher_runner(runner, _fence(), worktree)  # type: ignore[arg-type]
            with self.assertRaises(OSError):
                _ = fenced(["git", "status"], cwd=str(worktree / "nested"))
        self.assertEqual(runner.calls, [])

    def test_a_call_that_cannot_be_wrapped_is_never_run(self) -> None:
        runner = _Recorder()
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            fenced = _fenced_dispatcher_runner(runner, _fence(wraps=False), worktree)  # type: ignore[arg-type]
            self.assertIsNone(_git_text(fenced, worktree, ["git", "rev-parse", "HEAD"]))
            self.assertIsNone(_observed_clean_producer_head(fenced, worktree))
        self.assertEqual(runner.calls, [])


_DISPATCH_SOURCE = Path(__file__).resolve().parents[1] / "src" / "coding" / "fanout_dispatch.py"
# Every call in `_dispatch_unit`, once the fence exists, that still receives the
# BARE runner, with why that is right. Anything else that takes it fails below.
_BARE_RUNNER_CALLEES = {
    "getattr": "reads a capability flag off the runner; runs nothing",
    "_run_unit_verification": (
        "needs the runner's identity to prepare a fence; each command goes through "
        "`_run_verification_command`, which fences it or does not run it, and the revision read is wrapped"
    ),
    "_observe_reproduction": (
        "hands the declared command to `_run_verification_command`, which fences it or does not run it"
    ),
}
# The one place the bare runner is CALLED after the fence exists: the unit's own
# spawn, which carries `confinement_command` itself.
_BARE_RUNNER_DIRECT_CALLS = 1


def _is_bare_runner(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "runner"


class DispatchWiringTests(unittest.TestCase):
    """Re-derived from source, so a new or reverted call on the bare runner fails on every job.

    It sees what `_dispatch_unit` hands its runner to. It cannot see a helper
    that spawns a process of its own without taking a runner at all.
    """

    def setUp(self) -> None:
        tree = ast.parse(_DISPATCH_SOURCE.read_text(encoding="utf-8"))
        self.functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        dispatch = self.functions["_dispatch_unit"]
        (self.fence_line,) = [
            node.lineno for node in ast.walk(dispatch)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "unit_git_runner" for target in node.targets)
        ]
        self.calls_after_fence = [
            node for node in ast.walk(dispatch) if isinstance(node, ast.Call) and node.lineno > self.fence_line
        ]

    def test_only_the_listed_callees_receive_the_bare_runner_once_the_fence_exists(self) -> None:
        receiving = {
            ast.unparse(call.func)
            for call in self.calls_after_fence
            if any(_is_bare_runner(argument) for argument in call.args)
            or any(_is_bare_runner(keyword.value) for keyword in call.keywords)
        }
        self.assertEqual(receiving, set(_BARE_RUNNER_CALLEES))

    def test_the_bare_runner_is_called_only_for_the_units_own_spawn(self) -> None:
        direct = [call for call in self.calls_after_fence if _is_bare_runner(call.func)]
        self.assertEqual(len(direct), _BARE_RUNNER_DIRECT_CALLS)
        self.assertIn("spawn_kwargs", ast.unparse(direct[0]))

    def test_the_fenced_runner_is_built_from_the_units_own_fence_and_worktree(self) -> None:
        dispatch = self.functions["_dispatch_unit"]
        (assignment,) = [
            node for node in ast.walk(dispatch) if isinstance(node, ast.Assign) and node.lineno == self.fence_line
        ]
        self.assertEqual(ast.unparse(assignment.value), "_fenced_dispatcher_runner(runner, confinement, worktree)")
        handed_on = [
            call for call in self.calls_after_fence
            if any(ast.unparse(argument) == "unit_git_runner" for argument in call.args)
            or any(ast.unparse(keyword.value) == "unit_git_runner" for keyword in call.keywords)
        ]
        self.assertGreaterEqual(len(handed_on), 6)

    def test_every_session_workspace_probe_after_the_fence_is_given_it(self) -> None:
        probes = [
            call for call in self.calls_after_fence
            if isinstance(call.func, ast.Name) and call.func.id in {"observe_session_workspace", "_capacity_lineage", "_capacity_entry"}
        ]
        self.assertGreaterEqual(len(probes), 5)
        for probe in probes:
            fences = [ast.unparse(keyword.value) for keyword in probe.keywords if keyword.arg == "confinement"]
            self.assertEqual(fences, ["confinement"], ast.unparse(probe))

    def test_planned_verification_reads_the_worktree_revision_through_the_fence(self) -> None:
        revisions = [
            node for node in ast.walk(self.functions["_run_planned_verification"])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_verification_worktree_revision"
        ]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(
            ast.unparse(revisions[0].args[0]), "_fenced_dispatcher_runner(runner, confinement, worktree)",
        )

    def test_capacity_lineage_probes_the_worktree_with_the_fence_it_was_handed(self) -> None:
        fence = object()
        binding = AdmissionBinding("codex", "fanout", "unit", "run", 1, "0" * 40, "/nonexistent/worktree")
        with mock.patch("omh.coding.fanout_dispatch.observe_session_workspace", return_value=None) as probe:
            lineage = _capacity_lineage({}, binding, "digest", confinement=fence)  # type: ignore[arg-type]
        probe.assert_called_once_with("/nonexistent/worktree", confinement=fence)
        self.assertIsNone(lineage["incarnation_id"])


class VerificationCommandFenceTests(unittest.TestCase):
    """A check the fence was not prepared for is fenced anyway, or it does not run."""

    def _fence(self, *, locates: bool) -> SimpleNamespace:
        self.located_with: list[str | None] = []

        def dispatcher_command(argv: Any, *, path: str | None = None) -> tuple[str, ...] | None:
            self.located_with.append(path)
            return ("fence", *argv) if locates else None

        return SimpleNamespace(
            receipt={"enforced": True},
            command=lambda argv: None,
            dispatcher_command=dispatcher_command,
            command_environment=lambda environment: dict(environment),
        )

    def test_a_check_not_named_at_preparation_is_fenced_by_its_own_path(self) -> None:
        runner = _Recorder()
        with TemporaryDirectory() as temporary:
            status, _message, _truncation = _run_verification_command(
                "task-linked-runner tests/test_one.py", Path(temporary), runner,
                child_env={"PATH": "/opt/unit-tools/bin"}, confinement=self._fence(locates=True),  # type: ignore[arg-type]
            )
        self.assertEqual(status, "passed")
        (argv, kwargs), = runner.calls
        self.assertEqual(kwargs["confinement_command"], ("fence", *argv))
        self.assertEqual(self.located_with, ["/opt/unit-tools/bin"])

    def test_a_check_the_fence_cannot_wrap_is_a_failed_check_and_never_runs(self) -> None:
        runner = _Recorder()
        with TemporaryDirectory() as temporary:
            status, message, _truncation = _run_verification_command(
                "task-linked-runner tests/test_one.py", Path(temporary), runner,
                child_env={"PATH": "/opt/unit-tools/bin"}, confinement=self._fence(locates=False),  # type: ignore[arg-type]
            )
        self.assertEqual(status, "failed")
        self.assertIn("not run outside the unit's write fence", message)
        self.assertEqual(runner.calls, [])


@requires_posix
class DispatcherCommandTests(unittest.TestCase):
    """`dispatcher_command` builds policy text only, so this runs without a sandbox."""

    def _confinement(self, worktree: Path, *, enforced: bool) -> FanoutFilesystemConfinement:
        child = ChildContext(
            worktree, worktree, worktree, worktree, worktree,
            worktree / ".omh-confinement-request", worktree / ".omh-confinement-artifact", "f" * 64,
        )
        return FanoutFilesystemConfinement(
            "sandbox-exec", (worktree,), (worktree,), (), child, {}, "digest", {}, {"enforced": enforced},
        )

    def test_git_is_fenced_although_it_was_not_named_when_the_fence_was_prepared(self) -> None:
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            confinement = self._confinement(worktree, enforced=True)
            self.assertIsNone(confinement.command(("git", "status")))
            command = confinement.dispatcher_command(("git", "status", "--porcelain=v1"))
        located = shutil.which("git")
        assert located is not None and command is not None
        self.assertEqual(command[0], "/usr/bin/sandbox-exec")
        self.assertEqual(command[3:], (str(Path(located).resolve()), "status", "--porcelain=v1"))
        self.assertIn(f'(allow file-write* (subpath "{worktree}"))', command[2])

    def test_a_relative_executable_is_the_one_in_the_unit_worktree(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = root / "worktree"
            elsewhere = root / "dispatcher-cwd"
            for directory in (worktree, elsewhere, worktree / "tools", elsewhere / "tools"):
                directory.mkdir()
                script = directory / ("runner" if directory.name == "tools" else "repro.sh")
                script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                script.chmod(0o755)
            confinement = self._confinement(worktree, enforced=True)
            with contextlib.chdir(elsewhere):
                by_path = confinement.dispatcher_command(("./repro.sh", "--once"))
                by_search = confinement.dispatcher_command(("runner",), path="tools")
                (worktree / "repro.sh").unlink()
                missing = confinement.dispatcher_command(("./repro.sh",))
        assert by_path is not None and by_search is not None
        self.assertEqual(by_path[3:], (str(worktree / "repro.sh"), "--once"))
        self.assertEqual(by_search[3:], (str(worktree / "tools" / "runner"),))
        self.assertIsNone(missing)

    def test_no_command_without_an_enforced_receipt_or_a_locatable_executable(self) -> None:
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            self.assertIsNone(self._confinement(worktree, enforced=False).dispatcher_command(("git", "status")))
            enforced = self._confinement(worktree, enforced=True)
            self.assertIsNone(enforced.dispatcher_command(("omh-no-such-executable", "status")))
            self.assertIsNone(enforced.dispatcher_command(()))


@requires_posix
class SessionWorkspaceProbeFenceTests(unittest.TestCase):
    def test_every_probe_goes_through_the_fence_it_was_given(self) -> None:
        seen: list[tuple[str, ...]] = []

        def through_env(argv: Any) -> tuple[str, ...]:
            seen.append(tuple(argv))
            return ("/usr/bin/env", *argv)

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = _linked_worktree(root)
            fence = SimpleNamespace(receipt={"enforced": True}, dispatcher_command=through_env)
            snapshot = observe_session_workspace(str(worktree), confinement=fence)  # type: ignore[arg-type]
            head = _host_git(root / "repo", "rev-parse", "agent/unit")
        assert snapshot is not None
        self.assertEqual(snapshot.head, head)
        self.assertGreaterEqual(len(seen), 6)
        self.assertEqual({argv[0] for argv in seen}, {"git"})

    def test_a_probe_that_cannot_be_fenced_observes_nothing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = _linked_worktree(root)
            self.assertIsNotNone(observe_session_workspace(str(worktree)))
            self.assertIsNone(observe_session_workspace(str(worktree), confinement=_fence(wraps=False)))  # type: ignore[arg-type]


@unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
class MacosDispatcherGitFenceTests(unittest.TestCase):
    """Real sandbox-exec spawns through the dispatch path's own runner."""

    def _prepared(self, root: Path, *, git_roots: bool = True) -> tuple[Path, FanoutFilesystemConfinement, Any]:
        worktree = _linked_worktree(root)
        options: dict[str, Any] = {"unit_branch": "agent/unit", "repo_root": root / "repo"} if git_roots else {}
        confinement = prepare_fanout_filesystem_confinement(
            worktree, {}, (("/bin/sh", "-c", "exit 0"),), owner="", **options,
        )
        self.assertTrue(confinement.receipt["enforced"])
        return worktree, confinement, _fenced_dispatcher_runner(signal_safe_unit_runner, confinement, worktree)

    def _unit(self, confinement: FanoutFilesystemConfinement, worktree: Path, script: str) -> None:
        completed = subprocess.run(
            confinement.command(("/bin/sh", "-c", script)), cwd=worktree,
            env=confinement.command_environment(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_the_clean_head_observation_reads_the_units_commit_from_inside_the_fence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, confinement, runner = self._prepared(root)
            self._unit(
                confinement, worktree,
                "echo y >> seed && /usr/bin/git add seed && "
                "/usr/bin/git -c user.name=t -c user.email=t@example.test commit -qm unit",
            )
            self.assertEqual(
                _observed_clean_producer_head(runner, worktree), _host_git(root / "repo", "rev-parse", "agent/unit"),
            )
            snapshot = observe_session_workspace(str(worktree), confinement=confinement)
            assert snapshot is not None
            self.assertEqual(snapshot.head, _host_git(root / "repo", "rev-parse", "agent/unit"))
            self.assertFalse(snapshot.dirty)

    def test_what_a_fenced_dispatcher_call_starts_can_write_only_where_the_unit_could(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, _confinement, runner = self._prepared(root)
            outside = root / "outside"
            script = f'printf x > "{worktree}/inside" && printf x > "{outside}"'
            fenced = runner(["/bin/sh", "-c", script], cwd=str(worktree), text=True, capture_output=True, timeout=30)
            self.assertNotEqual(fenced.returncode, 0)
            self.assertIn("Operation not permitted", fenced.stderr)
            self.assertTrue((worktree / "inside").exists())
            self.assertFalse(outside.exists())
            # The same call through the bare runner is what the dispatcher used to make.
            bare = signal_safe_unit_runner(
                ["/bin/sh", "-c", script], cwd=str(worktree), text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(bare.returncode, 0, bare.stderr)
            self.assertTrue(outside.exists())

    def test_a_check_the_fence_was_not_prepared_for_still_cannot_write_outside_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, confinement, _runner = self._prepared(root)
            self.assertIsNone(confinement.command(("/usr/bin/touch", "x")))
            outside = root / "outside"
            inside = _run_verification_command(
                f"/usr/bin/touch {worktree}/inside", worktree, signal_safe_unit_runner, confinement=confinement,
            )
            escaped = _run_verification_command(
                f"/usr/bin/touch {outside}", worktree, signal_safe_unit_runner, confinement=confinement,
            )
            self.assertEqual(inside[0], "passed", inside)
            self.assertTrue((worktree / "inside").exists())
            self.assertEqual(escaped[0], "failed", escaped)
            self.assertFalse(outside.exists())
            # A script the unit wrote, named the way a reproduction command names it.
            (worktree / "repro.sh").write_text("#!/bin/sh\nprintf ran > repro-ran\n", encoding="utf-8")
            (worktree / "repro.sh").chmod(0o755)
            relative = _run_verification_command(
                "./repro.sh", worktree, signal_safe_unit_runner, confinement=confinement,
            )
            self.assertEqual(relative[0], "passed", relative)
            self.assertEqual((worktree / "repro-ran").read_text(encoding="utf-8"), "ran")

    def test_recovery_capture_measures_a_failed_units_work_from_inside_the_fence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, confinement, runner = self._prepared(root)
            base_sha = _host_git(root / "repo", "rev-parse", "HEAD")
            self._unit(confinement, worktree, "echo changed >> seed && echo new > created")
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            recovery = _capture_unit_recovery(
                paths, fanout_id="", unit_id="", worktree=worktree, base_sha=base_sha, runner=runner,
            )
            assert recovery is not None
            self.assertEqual(recovery["outcome"], "recovery_available", recovery)
            self.assertEqual(recovery["paths_changed"], 2)
            self.assertEqual(sorted(recovery["paths"]), ["created", "seed"])

    def test_without_git_roots_an_untouched_worktree_still_reads_as_no_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, _confinement, runner = self._prepared(root, git_roots=False)
            base_sha = _host_git(root / "repo", "rev-parse", "HEAD")
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            recovery = _capture_unit_recovery(
                paths, fanout_id="", unit_id="", worktree=worktree, base_sha=base_sha, runner=runner,
            )
            assert recovery is not None
            self.assertEqual(recovery["outcome"], "no_changes", recovery)

    def test_without_git_roots_a_created_file_alone_is_not_read_as_no_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, confinement, runner = self._prepared(root, git_roots=False)
            base_sha = _host_git(root / "repo", "rev-parse", "HEAD")
            self._unit(confinement, worktree, "echo new > created")
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            recovery = _capture_unit_recovery(
                paths, fanout_id="", unit_id="", worktree=worktree, base_sha=base_sha, runner=runner,
            )
            assert recovery is not None
            self.assertNotIn(recovery["outcome"], {"no_changes", "recovery_available"}, recovery)
            self.assertIn("git add -N failed", str(recovery))

    def test_without_git_roots_the_capture_says_it_could_not_measure_created_files(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, confinement, runner = self._prepared(root, git_roots=False)
            base_sha = _host_git(root / "repo", "rev-parse", "HEAD")
            self._unit(confinement, worktree, "echo changed >> seed && echo new > created")
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            recovery = _capture_unit_recovery(
                paths, fanout_id="", unit_id="", worktree=worktree, base_sha=base_sha, runner=runner,
            )
            assert recovery is not None
            self.assertNotEqual(recovery["outcome"], "recovery_available")
            self.assertIn("git add -N failed", str(recovery))
            self.assertEqual(recovery["tracked_paths_seen"], 1)


if __name__ == "__main__":
    unittest.main()
