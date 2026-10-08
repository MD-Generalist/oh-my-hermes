"""Git the dispatcher runs inside a fence gets no credentials and no network (#2035).

A unit can hand any later git call in its worktree a program of its own -- a
`core.fsmonitor`, a clean filter, a hook. Since #1995/#1999 such a call runs
inside a write fence, but it still ran with the operator's whole environment and
with the network allowed, so the planted program could read a credential the
unit itself was denied and send it out. The planted program here dumps its
environment and tries a TCP connect to a listener this test owns, writing both
results inside the worktree (the one place a fenced call may write).
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from _local_package import load_local_package
from _platform_support import requires_posix

load_local_package()

from omh.coding.fanout_confinement import (  # noqa: E402
    FanoutFilesystemConfinement,
    prepare_dispatcher_git_fence,
    prepare_fanout_filesystem_confinement,
)
from omh.quality.cross_harness_adapter_sandbox import ChildContext  # noqa: E402

_GIT = "/usr/bin/git"
_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.test")
_SENTINEL_NAME = "OMH_DISPATCHER_SENTINEL_TOKEN"
_SENTINEL_VALUE = "sentinel-2035-must-not-reach-a-planted-program"
_GIT_SENTINEL_NAME = "GIT_OMH_SENTINEL_2035"


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.run((_GIT, *arguments), cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _planted_worktree(root: Path, port: int) -> tuple[Path, Path]:
    """A linked unit worktree whose git runs a planted fsmonitor and clean filter.

    Returns the worktree and the directory the planted program reports into.
    """
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(".unit-git/\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("seed filter=planted\n", encoding="utf-8")
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", ".gitattributes", "seed")
    _git(repo, *_IDENTITY, "commit", "-qm", "init")
    worktree = root / "linked-worktree"
    _git(repo, "worktree", "add", "-qb", "agent/unit", str(worktree), "HEAD")
    reports = worktree / ".unit-git"
    reports.mkdir()
    fire = reports / "fire"
    q = shlex.quote
    connect = (
        "import socket,sys\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=2).close(); r='connected'\n"
        "except OSError as e:\n"
        "    r='denied ' + type(e).__name__\n"
        "open(sys.argv[1], 'a').write(r + '\\n')\n"
    )
    fire.write_text(
        "#!/bin/sh\n"
        f"/usr/bin/env > {q(str(reports))}/env-$$\n"
        f"{q(sys.executable)} -I -c {q(connect)} {q(str(reports / 'network'))}\n"
        'case $1 in clean|smudge) exec cat;; esac\n',
        encoding="utf-8",
    )
    fire.chmod(0o755)
    _git(worktree, "config", "core.fsmonitor", str(fire))
    _git(worktree, "config", "filter.planted.clean", f"{fire} clean")
    (worktree / "seed").write_text("changed\n", encoding="utf-8")
    return worktree, reports


def _dumps(reports: Path) -> list[str]:
    return [path.read_text(encoding="utf-8") for path in sorted(reports.glob("env-*"))]


class _PlantedWorktree(unittest.TestCase):
    def setUp(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.2)
        self.addCleanup(self.listener.close)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worktree, self.reports = _planted_worktree(self.root, self.listener.getsockname()[1])
        secrets = mock.patch.dict(os.environ, {_SENTINEL_NAME: _SENTINEL_VALUE, _GIT_SENTINEL_NAME: "1"})
        secrets.start()
        self.addCleanup(secrets.stop)

    def _assert_planted_program_saw_nothing(self, *, fenced: bool = True) -> None:
        dumps = _dumps(self.reports)
        self.assertTrue(dumps, "the planted program did not run, so this measured nothing")
        for dump in dumps:
            self.assertNotIn(_SENTINEL_VALUE, dump)
            self.assertNotIn(_GIT_SENTINEL_NAME, dump)
            self.assertIn("GIT_TERMINAL_PROMPT=0", dump)
        if fenced:
            attempts = (self.reports / "network").read_text(encoding="utf-8").split("\n")
            self.assertNotIn("connected", attempts)


@unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
class PlantedProgramEnvironmentTests(_PlantedWorktree):
    """Each dispatcher-git surface, with the unit's program planted and a secret in the dispatcher."""

    def test_the_planted_program_sees_the_secret_on_plain_host_git(self) -> None:
        """The control: without a fence the plant fires and reads the dispatcher's environment."""
        subprocess.run((_GIT, "status", "--porcelain"), cwd=self.worktree, capture_output=True, check=False)
        self.assertTrue(any(_SENTINEL_VALUE in dump for dump in _dumps(self.reports)))
        self.assertIn("connected", (self.reports / "network").read_text(encoding="utf-8"))

    def test_the_status_probe_of_a_unit_worktree(self) -> None:
        from omh.coding.fanout_executor_sessions import observe_session_workspace

        fence = prepare_dispatcher_git_fence(self.worktree)
        self.assertTrue(fence.receipt["enforced"])
        self.assertFalse(fence.allow_network)
        snapshot = observe_session_workspace(str(self.worktree), confinement=fence)
        self.assertIsNotNone(snapshot)
        self._assert_planted_program_saw_nothing()

    def test_the_dispatchers_git_inside_the_units_own_fence(self) -> None:
        from omh.coding.fanout_dispatch import _fenced_dispatcher_runner, _git_text, signal_safe_unit_runner

        confinement = prepare_fanout_filesystem_confinement(
            self.worktree, {}, (("/bin/sh", "-c", "exit 0"),), unit_branch="agent/unit", repo_root=self.root / "repo",
        )
        self.assertTrue(confinement.receipt["enforced"])
        runner = _fenced_dispatcher_runner(signal_safe_unit_runner, confinement, self.worktree)
        status = _git_text(runner, self.worktree, ["git", "status", "--porcelain=v1", "--untracked-files=all"])
        self.assertEqual(status, " M seed\n")
        self._assert_planted_program_saw_nothing()

    def test_the_diagnostics_revision_reader(self) -> None:
        from omh.coding.local_diagnostic_engine import GitRevisionReader

        self.assertEqual(GitRevisionReader().read(str(self.worktree), "HEAD"), "workspace-dirty")
        self._assert_planted_program_saw_nothing()


@requires_posix
class UnfencedOptInEnvironmentTests(_PlantedWorktree):
    """`--allow-unconfined` skips the fence, which a unit can force; the environment is still scrubbed.

    The network is not: with no fence there is nothing to deny it with.
    """

    def _opted_in_fence(self) -> FanoutFilesystemConfinement:
        with mock.patch("omh.coding.fanout_confinement.backend_available", return_value=False):
            fence = prepare_dispatcher_git_fence(self.worktree, allow_unconfined=True)
        self.assertNotEqual(fence.receipt["enforced"], True)
        self.assertTrue(fence.unconfined_allowed)
        return fence

    def test_the_status_probe_of_a_unit_worktree(self) -> None:
        from omh.coding.fanout_executor_sessions import observe_session_workspace

        self.assertIsNotNone(observe_session_workspace(str(self.worktree), confinement=self._opted_in_fence()))
        self._assert_planted_program_saw_nothing(fenced=False)

    def test_the_diagnostics_revision_reader(self) -> None:
        from omh.coding.local_diagnostic_engine import GitRevisionReader
        from omh.coding.local_diagnostic_process import WorkspaceGitFences

        with mock.patch("omh.coding.fanout_confinement.backend_available", return_value=False):
            reader = GitRevisionReader(git=WorkspaceGitFences(allow_unconfined=True))
            self.assertEqual(reader.read(str(self.worktree), "HEAD"), "workspace-dirty")
        self._assert_planted_program_saw_nothing(fenced=False)


class DispatcherGitEnvironmentKeepListTests(unittest.TestCase):
    """The keep-list per platform, decided by `os.name` at call time, so every job runs both."""

    _OPERATOR = {
        "PATH": "/usr/bin", "HOME": "/home/operator", "TMPDIR": "/operator-tmp", "LC_ALL": "C.UTF-8",
        "SYSTEMROOT": "C:\\Windows", "WINDIR": "C:\\Windows", "COMSPEC": "C:\\Windows\\cmd.exe",
        "PATHEXT": ".COM;.EXE", "USERPROFILE": "C:\\Users\\operator", "HOMEDRIVE": "C:", "HOMEPATH": "\\Users\\operator",
        "TEMP": "C:\\Temp", "TMP": "C:\\Temp",
        _SENTINEL_NAME: _SENTINEL_VALUE, "GITHUB_TOKEN": "ghp-sentinel", "GIT_DIR": "/elsewhere",
    }
    _SWITCHES = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}

    def _environment(self, platform: str) -> dict[str, str]:
        from omh.coding.fanout_confinement import dispatcher_git_environment

        with mock.patch.dict(os.environ, self._OPERATOR, clear=True), mock.patch.object(os, "name", platform):
            return dispatcher_git_environment()

    def test_windows_keeps_what_git_needs_to_start_there(self) -> None:
        windows = {key: value for key, value in self._OPERATOR.items() if key not in {
            _SENTINEL_NAME, "GITHUB_TOKEN", "GIT_DIR",
        }}
        self.assertEqual(self._environment("nt"), {**windows, **self._SWITCHES})

    def test_posix_keeps_none_of_the_windows_variables(self) -> None:
        self.assertEqual(self._environment("posix"), {
            "PATH": "/usr/bin", "HOME": "/home/operator", "TMPDIR": "/operator-tmp", "LC_ALL": "C.UTF-8",
            **self._SWITCHES,
        })


@requires_posix
class DispatcherGitCommandTests(unittest.TestCase):
    """The command and environment themselves, built without a sandbox, so every POSIX job runs them."""

    def _fence(self, worktree: Path, selected: str) -> FanoutFilesystemConfinement:
        child = ChildContext(worktree, worktree, worktree, worktree, worktree, worktree / "r", worktree / "a", "test")
        return FanoutFilesystemConfinement(
            selected, (worktree,), (worktree,), (), child, {}, "", {}, {"enforced": True},
        )

    def test_the_environment_keeps_what_git_needs_and_nothing_else(self) -> None:
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            operator = {
                "PATH": "/usr/bin:/bin", "HOME": "/home/operator", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                _SENTINEL_NAME: _SENTINEL_VALUE, "GITHUB_TOKEN": "ghp-sentinel", "SSH_AUTH_SOCK": "/tmp/agent",
                "GIT_DIR": "/elsewhere", "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='x'",
            }
            with mock.patch.dict(os.environ, operator, clear=True), mock.patch(
                "omh.coding.fanout_confinement.shutil.which", return_value="/usr/bin/git",
            ):
                fenced = self._fence(worktree, "sandbox-exec").dispatcher_git_command(
                    ["git", "read-tree", "HEAD"], {**operator, "GIT_INDEX_FILE": str(worktree / "index.tmp")},
                )
        assert fenced is not None
        _command, environment = fenced
        self.assertEqual(environment, {
            "PATH": "/usr/bin:/bin", "HOME": "/home/operator", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TMPDIR": str(worktree / ".omh" / "confinement-tmp"),
            "GIT_INDEX_FILE": str(worktree / "index.tmp"),
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
        })

    def test_the_command_denies_the_network_where_the_backend_can(self) -> None:
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            with mock.patch("omh.coding.fanout_confinement.shutil.which", return_value="/usr/bin/git"):
                fence = self._fence(worktree, "sandbox-exec")
                git = fence.dispatcher_git_command(["git", "status"])
                check = fence.dispatcher_command(["git", "status"])
            with (
                mock.patch("omh.coding.fanout_confinement.shutil.which", return_value="/usr/bin/git"),
                mock.patch("omh.quality.cross_harness_adapter_sandbox._trusted_bwrap", return_value="/usr/bin/bwrap"),
            ):
                bwrap = self._fence(worktree, "bwrap").dispatcher_git_command(["git", "status"])
        assert git is not None and check is not None and bwrap is not None
        self.assertNotIn("(allow network*)", git[0][2])
        # A check the dispatcher runs for the unit keeps the unit's network.
        self.assertIn("(allow network*)", check[2])
        self.assertIn("--unshare-net", bwrap[0])
        self.assertTrue(fence.allow_network)

    def test_no_command_without_an_enforced_fence(self) -> None:
        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            unenforced = replace(self._fence(worktree, "sandbox-exec"), receipt={"enforced": False})
            self.assertIsNone(unenforced.dispatcher_git_command(["git", "status"]))


if __name__ == "__main__":
    unittest.main()
