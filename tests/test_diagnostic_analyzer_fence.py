"""`--diagnostics` analyzers run inside a fence rooted at the revision snapshot (#2036).

The snapshot is a tree the unit committed, and pyright/basedpyright read their
configuration from it. Whatever an analyzer starts from that tree may write
only the snapshot, a private temporary directory, and reaches no network. The
behavioural class runs on macOS, the one host with a fence CI does not need
bwrap for; the wiring and no-backend classes run on every job.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.coding import fanout_confinement  # noqa: E402
from omh.coding.local_diagnostic_process import (  # noqa: E402
    _COMMAND_ARGS,
    LocalDiagnosticProviderRunner,
    WorkspaceGitFences,
)

_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.test")


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(("git", *arguments), cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _repository(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, *_IDENTITY, "commit", "-qm", "unit")
    return repo, _git(repo, "rev-parse", "HEAD")


class _HostGit(WorkspaceGitFences):
    """Git of a test-owned repository, unfenced, so only the analyzer path is under test."""

    def command(self, workspace: str | Path, argv: Any) -> tuple[list[str], dict[str, str]]:
        return list(argv), fanout_confinement.dispatcher_git_environment()


@unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
class AnalyzerFenceBehaviourTests(unittest.TestCase):
    """An analyzer that runs code from the snapshot writes nothing outside it and reaches no network."""

    def test_snapshot_code_run_by_the_analyzer_cannot_write_out_or_connect(self) -> None:
        with TemporaryDirectory() as temporary, socket.socket() as listener:
            root = Path(temporary).resolve()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.5)
            marker = root / "marker"
            # The fake ruff is the interpreter; `check` is the first argument
            # of ruff's argv, so the interpreter runs the unit's file of that name.
            planted = "\n".join((
                "import socket",
                "try:",
                f"    open({str(marker)!r}, 'a').write('wrote\\n')",
                "except OSError:",
                "    pass",
                "try:",
                f"    socket.create_connection(('127.0.0.1', {listener.getsockname()[1]}), timeout=2).close()",
                "except OSError:",
                "    pass",
                "print('[]')",
                "",
            ))
            repo, head = _repository(root, {"seed.py": "value = 1\n", "check": planted})
            observation = LocalDiagnosticProviderRunner({"ruff": sys.executable}, git=_HostGit()).run(
                "ruff", str(repo), head, ("seed.py",), 30_000, None,
            )
            try:
                connection, _address = listener.accept()
                connection.close()
                connected = True
            except OSError:
                connected = False
            self.assertEqual(observation.state, "completed")
            self.assertFalse(marker.exists(), "snapshot code run by the analyzer wrote outside the snapshot")
            self.assertFalse(connected, "snapshot code run by the analyzer reached the network")

    def test_the_analyzer_fence_is_enforced_and_denies_network(self) -> None:
        with TemporaryDirectory() as temporary:
            snapshot = Path(temporary).resolve()
            fence = fanout_confinement.prepare_diagnostic_analyzer_fence(
                snapshot, sys.executable, {"PATH": os.environ.get("PATH", "")},
            )
            command = fence.command((sys.executable, "--version"))
        self.assertIs(fence.receipt.get("enforced"), True)
        self.assertEqual(fence.write_roots, (snapshot,))
        assert command is not None
        self.assertNotIn("(allow network*)", command[2])


class AnalyzerFenceWiringTests(unittest.TestCase):
    """Every adapter in the closed set is spawned through the analyzer fence."""

    def test_every_adapter_spawns_its_fenced_command(self) -> None:
        class Fence:
            unconfined_allowed = False

            def command(self, argv: Any) -> tuple[str, ...]:
                return ("fenced", *argv)

            def command_environment(self, environment: Any) -> dict[str, str]:
                return {**environment, "FENCED": "1"}

        for provider_id, arguments in _COMMAND_ARGS.items():
            with self.subTest(provider=provider_id), TemporaryDirectory() as temporary:
                repo, head = _repository(Path(temporary).resolve(), {"seed.py": "value = 1\n"})
                prepared: list[tuple[Any, ...]] = []

                def prepare(snapshot: Path, executable: str, environment: Any, **options: Any) -> Fence:
                    prepared.append((snapshot, executable, dict(environment), options))
                    return Fence()

                runner = LocalDiagnosticProviderRunner({provider_id: sys.executable}, git=_HostGit())
                with mock.patch(
                    "omh.coding.local_diagnostic_process.prepare_diagnostic_analyzer_fence", side_effect=prepare
                ), mock.patch(
                    "omh.coding.local_diagnostic_process.start_owned_process", side_effect=OSError("spawn recorded")
                ) as spawn:
                    with self.assertRaisesRegex(OSError, "spawn recorded"):
                        runner.run(provider_id, str(repo), head, ("seed.py",), 1_000, None)
                executable = runner.executables[provider_id]
                ((snapshot, prepared_executable, environment, options),) = prepared
                self.assertEqual(prepared_executable, executable)
                self.assertEqual(options, {"allow_unconfined": False})
                (argv,), keywords = spawn.call_args
                self.assertEqual(tuple(argv), ("fenced", executable, *arguments, "seed.py"))
                self.assertEqual(keywords["cwd"], snapshot)
                self.assertEqual(keywords["env"], {**environment, "FENCED": "1"})

    def test_the_analyzer_fence_writes_only_the_snapshot_and_denies_network(self) -> None:
        with TemporaryDirectory() as temporary:
            snapshot = Path(temporary).resolve()
            with mock.patch.object(fanout_confinement, "_prepare_fanout_filesystem_confinement") as prepare:
                prepare.return_value = fanout_confinement._unconfined(snapshot, "test", {}, "test")
                fence = fanout_confinement.prepare_diagnostic_analyzer_fence(snapshot, sys.executable, {"PATH": "x"})
        (worktree, environment, commands), _options = prepare.call_args
        self.assertEqual((worktree, environment, commands), (snapshot, {"PATH": "x"}, ((sys.executable,),)))
        self.assertFalse(fence.allow_network)


class AnalyzerWithoutFenceTests(unittest.TestCase):
    """A host with no backend (Windows, Linux without bwrap) runs no analyzer unless the operator opts in."""

    def _run(self, *, allow_unconfined: bool) -> tuple[Any, mock.MagicMock]:
        real = fanout_confinement.prepare_diagnostic_analyzer_fence

        def unsupported(*arguments: Any, **options: Any) -> Any:
            with mock.patch.object(fanout_confinement, "backend", return_value="unsupported"):
                return real(*arguments, **options)

        git = _HostGit(allow_unconfined=allow_unconfined)
        with TemporaryDirectory() as temporary:
            repo, head = _repository(Path(temporary).resolve(), {"seed.py": "value = 1\n", "check": "print('[]')\n"})
            runner = LocalDiagnosticProviderRunner({"ruff": sys.executable}, git=git)
            from omh.coding import local_diagnostic_process

            with mock.patch.object(
                local_diagnostic_process, "prepare_diagnostic_analyzer_fence", side_effect=unsupported
            ), mock.patch.object(
                local_diagnostic_process, "start_owned_process", wraps=local_diagnostic_process.start_owned_process
            ) as spawn:
                try:
                    observation: Any = runner.run("ruff", str(repo), head, ("seed.py",), 30_000, None)
                except OSError as exc:
                    observation = exc
        return observation, spawn

    def test_no_backend_and_no_opt_in_runs_nothing(self) -> None:
        observation, spawn = self._run(allow_unconfined=False)
        self.assertIsInstance(observation, OSError)
        spawn.assert_not_called()

    def test_no_backend_with_the_opt_in_runs_the_plain_argv(self) -> None:
        observation, spawn = self._run(allow_unconfined=True)
        self.assertEqual(observation.state, "completed")
        (argv,), _keywords = spawn.call_args
        self.assertEqual(argv[0], str(Path(sys.executable).resolve()))


class OptInIsForHostCapabilityTests(unittest.TestCase):
    """`--allow-unconfined` means "this host cannot fence", not "the fenced tree broke the fence"."""

    def test_every_unenforced_reason_is_classified(self) -> None:
        import ast
        import inspect
        import textwrap

        found: set[str] = set()
        for function in (fanout_confinement._prepare_fanout_filesystem_confinement, fanout_confinement._probe):
            tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
            found.update(
                node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and (node.value.startswith("sandbox_") or node.value.startswith("no_os_"))
            )
        self.assertEqual(found, set(fanout_confinement.UNCONFINED_REASON_SOURCES))
        self.assertLessEqual(set(fanout_confinement.UNCONFINED_REASON_SOURCES.values()), {"host", "content"})

    def test_the_opt_in_is_honoured_only_for_a_host_reason(self) -> None:
        reasons = {**fanout_confinement.UNCONFINED_REASON_SOURCES, "unclassified_reason": "content"}
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            for reason, source in reasons.items():
                with self.subTest(reason=reason), mock.patch.object(
                    fanout_confinement, "_prepare_fanout_filesystem_confinement",
                    return_value=fanout_confinement._unconfined(worktree, "test", {}, reason),
                ):
                    fence = fanout_confinement.prepare_fanout_filesystem_confinement(
                        worktree, {}, (("git",),), allow_unconfined=True,
                    )
                self.assertEqual(fence.unconfined_allowed, source == "host")
                self.assertEqual(fence.receipt["unconfined_opt_in"], source == "host")

    @unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
    def test_a_committed_omh_file_refuses_the_analyzer_despite_the_opt_in(self) -> None:
        from omh.coding import local_diagnostic_process

        with TemporaryDirectory() as temporary:
            repo, head = _repository(
                Path(temporary).resolve(), {"seed.py": "value = 1\n", "check": "print('[]')\n", ".omh": "unit\n"},
            )
            runner = LocalDiagnosticProviderRunner({"ruff": sys.executable}, git=_HostGit(allow_unconfined=True))
            with mock.patch.object(
                local_diagnostic_process, "start_owned_process", wraps=local_diagnostic_process.start_owned_process
            ) as spawn:
                with self.assertRaises(OSError):
                    runner.run("ruff", str(repo), head, ("seed.py",), 30_000, None)
        spawn.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
    def test_a_committed_omh_file_refuses_dispatcher_git_despite_the_opt_in(self) -> None:
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            (worktree / ".omh").write_text("unit\n", encoding="utf-8")
            fence = fanout_confinement.prepare_dispatcher_git_fence(worktree, allow_unconfined=True)
        self.assertEqual(fence.receipt["reason_code"], "sandbox_scratch_unsafe")
        self.assertFalse(fence.unconfined_allowed)
        self.assertIsNone(fence.dispatcher_command(["git", "status"]))

    def test_a_host_without_a_backend_keeps_the_opt_in(self) -> None:
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            (worktree / ".omh").write_text("unit\n", encoding="utf-8")
            with mock.patch.object(fanout_confinement, "backend_available", return_value=False):
                fence = fanout_confinement.prepare_diagnostic_analyzer_fence(
                    worktree, sys.executable, {}, allow_unconfined=True,
                )
        if fence.receipt["reason_code"] != "no_os_confinement_backend_on_this_platform":
            self.assertEqual(fence.receipt["reason_code"], "sandbox_backend_unavailable")
        self.assertTrue(fence.unconfined_allowed)


class AnalyzerPathTests(unittest.TestCase):
    def test_relative_and_empty_path_entries_are_dropped(self) -> None:
        from omh.coding.local_diagnostic_process import _diagnostic_environment

        absolute = str(Path(sys.executable).resolve().parent)
        path = os.pathsep.join((".", "", "bin", absolute, "node_modules/.bin"))
        with mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(_diagnostic_environment()["PATH"], absolute)


_PYRIGHTS = tuple(name for name in ("pyright", "basedpyright") if shutil.which(name))


@unittest.skipUnless(_PYRIGHTS and os.name == "posix", "no pyright/basedpyright on this host")
class PlantedPyrightConfigTests(unittest.TestCase):
    """Snapshot configuration cannot steer pyright into running a snapshot interpreter or module.

    Run with the fence opted out, so a marker would prove the analyzer itself
    ran the planted program, not merely that a fence stopped its write.
    Measured on basedpyright 1.39.10 (pyright 1.1.412): `venvPath`/`venv` are
    scanned for site-packages and the interpreter there is never executed; the
    PATH interpreter it does run drops the working directory from `sys.path`
    before importing anything.
    """

    def test_a_planted_venv_and_shadow_modules_run_nothing(self) -> None:
        for name in _PYRIGHTS:
            with self.subTest(analyzer=name), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                marker = root / "marker"
                fire = f"#!/bin/sh\necho \"$0\" >> '{marker}'\nexec python3 \"$@\"\n"
                section = "basedpyright" if name == "basedpyright" else "pyright"
                files = {
                    "seed.py": 'value: int = "text"\n',
                    "pyrightconfig.json": '{"venvPath": ".", "venv": "venv"}\n',
                    "pyproject.toml": f'[tool.{section}]\nvenvPath = "."\nvenv = "venv"\n',
                    "venv/pyvenv.cfg": "home = /usr/bin\n",
                    "venv/bin/python": fire,
                    "venv/bin/python3": fire,
                    "venv/lib/python3.14/site-packages/keep": "",
                    **{
                        f"{module}.py": f"open({str(marker)!r}, 'a').write('{module}\\n')\n"
                        for module in ("json", "os", "site", "sitecustomize", "encodings")
                    },
                }
                repo, _head = _repository(root, files)
                for interpreter in ("venv/bin/python", "venv/bin/python3"):
                    (repo / interpreter).chmod(0o755)
                _git(repo, "add", "-A")
                _git(repo, *_IDENTITY, "commit", "-qm", "executable")
                head = _git(repo, "rev-parse", "HEAD")
                runner = LocalDiagnosticProviderRunner(
                    {name: str(shutil.which(name))}, git=_HostGit(allow_unconfined=True),
                )
                with mock.patch.object(fanout_confinement, "backend", return_value="unsupported"):
                    observation = runner.run(name, str(repo), head, ("seed.py",), 120_000, None)
                self.assertEqual(observation.state, "completed")
                self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")


if __name__ == "__main__":
    unittest.main()
