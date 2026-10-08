"""Allowlisted subprocess boundary for local diagnostic providers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread, Timer
from typing import Iterator

from .diagnostic_execution import CancellationSignal, ProviderObservation
from .diagnostic_providers import GLOBAL_MAX_DIAGNOSTICS_PER_CHECK
from .fanout_confinement import FanoutFilesystemConfinement, prepare_dispatcher_git_fence
from .local_diagnostic_capture import DiagnosticPipeDrainer
from .local_diagnostic_parsing import parse_local_diagnostics
from .local_diagnostic_process_owner import ProcessTreeOwner, start_owned_process


_COMMAND_ARGS: dict[str, tuple[str, ...]] = {
    "pyright": ("--outputjson",),
    "basedpyright": ("--outputjson",),
    "ruff": ("check", "--no-cache", "--isolated", "--output-format=json", "--"),
}
_MAX_OUTPUT_BYTES = 2_000_000
_TERMINATE_GRACE_SECONDS = 1.0
_GIT_TIMEOUT_SECONDS = 30


class WorkspaceGitFences:
    """One dispatcher-git fence per unit worktree, shared by the engine's adapters.

    The worktree is the unit's, so its git configuration, hooks and filters may
    be the unit's too (#1999). A fence is prepared the first time a worktree is
    named and reused for every later git call of the same engine (one dispatch),
    rather than prepared, and its scratch written on the host, per call.
    """

    def __init__(self, *, allow_unconfined: bool = False) -> None:
        self.allow_unconfined = allow_unconfined
        self._fences: dict[str, FanoutFilesystemConfinement] = {}
        self._lock = Lock()

    def command(self, workspace: str | Path, argv: Sequence[str]) -> tuple[list[str], dict[str, str] | None]:
        """`argv` placed inside the worktree's fence, and the environment to spawn it with.

        Fenced, the command has no network and the environment only what git
        needs (#2035); unfenced by the operator's opt-in, the environment is
        None (inherited). Raises OSError when no fence can be proven and the
        operator did not pass `--allow-unconfined`; the engine reports that as
        a crashed diagnostic, never as one that ran.
        """
        key = str(Path(workspace).resolve())
        with self._lock:
            fence = self._fences.get(key)
            if fence is None:
                fence = prepare_dispatcher_git_fence(Path(key), allow_unconfined=self.allow_unconfined)
                self._fences[key] = fence
        fenced = fence.dispatcher_git_command(argv)
        if fenced is not None:
            return list(fenced[0]), fenced[1]
        if fence.unconfined_allowed:
            return list(argv), None
        raise OSError("local diagnostics found no write fence for git in the unit worktree")


def _snapshot_member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    """Keep only regular files and directories, then apply the stdlib `data` filter.

    The archive is of a tree the unit committed. Links and special files are
    dropped before `data_filter` sees them: the published bypasses of that
    filter (CVE-2025-4517, CVE-2025-4138, CVE-2025-4330, CVE-2024-12718) all
    go through a link, and Python releases before 3.11.13/3.12.11/3.13.4 carry
    them.
    """
    if not (member.isreg() or member.isdir()):
        return None
    return tarfile.data_filter(member, destination)


class LocalDiagnosticProviderRunner:
    """Run one closed-set provider against one immutable Git snapshot."""

    def __init__(self, executables: Mapping[str, str], *, git: WorkspaceGitFences | None = None) -> None:
        unknown = set(executables) - set(_COMMAND_ARGS)
        if unknown:
            raise ValueError(
                f"local diagnostic provider is not allowlisted: {sorted(unknown)}"
            )
        checked: dict[str, str] = {}
        for provider_id, executable in executables.items():
            path = Path(executable).expanduser()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(
                    f"local diagnostic executable is unavailable for {provider_id}"
                )
            checked[provider_id] = str(path.resolve())
        self.executables = checked
        self.git = WorkspaceGitFences() if git is None else git

    def run(
        self,
        provider_id: str,
        workspace_id: str,
        revision: str,
        files: tuple[str, ...],
        timeout_ms: int,
        cancelled: CancellationSignal | None,
    ) -> ProviderObservation:
        executable = self.executables.get(provider_id)
        if executable is None:
            return ProviderObservation.unavailable()
        if getattr(tarfile, "data_filter", None) is None:
            # Python before 3.11.4 has no extraction filter, and a unit's tree
            # is not extracted without one.
            return ProviderObservation.unavailable()
        if cancelled is not None and cancelled.is_set():
            return ProviderObservation("cancelled")
        with _revision_snapshot(
            Path(workspace_id),
            revision,
            self.git,
        ) as snapshot:
            existing = tuple(
                path for path in files if (snapshot / path).is_file()
            )
            if not existing:
                return ProviderObservation.completed(files, ())
            argv = [
                executable,
                *_COMMAND_ARGS[provider_id],
                *existing,
            ]
            return self._execute(
                provider_id,
                argv,
                snapshot,
                files,
                timeout_ms,
                cancelled,
            )

    def _execute(
        self,
        provider_id: str,
        argv: Sequence[str],
        snapshot: Path,
        files: tuple[str, ...],
        timeout_ms: int,
        cancelled: CancellationSignal | None,
    ) -> ProviderObservation:
        process, process_owner = start_owned_process(
            argv,
            cwd=snapshot,
            env=_diagnostic_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise OSError("local diagnostic subprocess pipes were unavailable")
        stdout = DiagnosticPipeDrainer(
            process.stdout,
            max_bytes=_MAX_OUTPUT_BYTES,
            name=f"diagnostic-stdout-{process.pid}",
        )
        stderr = DiagnosticPipeDrainer(
            process.stderr,
            max_bytes=_MAX_OUTPUT_BYTES,
            name=f"diagnostic-stderr-{process.pid}",
        )
        stdout.start()
        stderr.start()
        stopped = Event()
        cancelled_during_run = Event()
        watcher = Thread(
            target=_watch_cancellation,
            args=(
                cancelled,
                process_owner,
                stopped,
                cancelled_during_run,
            ),
            daemon=True,
        )
        watcher.start()
        cleanup_signal = signal.SIGTERM
        process_group_clean = False
        try:
            try:
                process.wait(timeout=timeout_ms / 1000)
            except subprocess.TimeoutExpired:
                raise
            except KeyboardInterrupt:
                cleanup_signal = signal.SIGINT
                raise
        finally:
            stopped.set()
            watcher.join(timeout=3)
            process_group_clean = process_owner.terminate(cleanup_signal)
            stdout_capture = stdout.finish(1)
            stderr_capture = stderr.finish(1)
        if not process_group_clean:
            return ProviderObservation.crashed()
        if cancelled_during_run.is_set() or (
            cancelled is not None and cancelled.is_set()
        ):
            return ProviderObservation("cancelled")
        if process.returncode not in (0, 1):
            return ProviderObservation.crashed()
        if stdout_capture.truncated or stderr_capture.truncated:
            return ProviderObservation("completed", (), ())
        try:
            diagnostics = parse_local_diagnostics(
                provider_id,
                stdout_capture.data,
                snapshot,
                files,
            )
        except ValueError:
            return ProviderObservation.crashed()
        if len(diagnostics) > GLOBAL_MAX_DIAGNOSTICS_PER_CHECK:
            return ProviderObservation("completed", (), ())
        return ProviderObservation.completed(files, diagnostics)


def _watch_cancellation(
    cancelled: CancellationSignal | None,
    process_owner: ProcessTreeOwner,
    stopped: Event,
    cancelled_during_run: Event,
) -> None:
    if cancelled is None:
        return
    while not stopped.wait(0.05):
        if cancelled.is_set():
            cancelled_during_run.set()
            process_owner.terminate(signal.SIGTERM)
            return


@contextmanager
def _revision_snapshot(
    workspace: Path,
    revision: str,
    git: WorkspaceGitFences,
) -> Iterator[Path]:
    """Materialize `revision` into a private directory without writing the repository.

    `git archive` runs inside the unit worktree's fence and only streams the
    tree; this process extracts it. A `git worktree add` here ran the unit's
    post-checkout hook and smudge filters on the host, and inside the fence it
    could not register a worktree under the shared git directory at all.
    """
    with TemporaryDirectory(prefix="omh-diagnostics-") as raw:
        snapshot = Path(raw) / "checkout"
        snapshot.mkdir()
        command, environment = git.command(workspace, ["git", "archive", "--format=tar", revision])
        with subprocess.Popen(
            command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as archive:
            watchdog = Timer(_GIT_TIMEOUT_SECONDS, archive.kill)
            watchdog.start()
            try:
                with tarfile.open(fileobj=archive.stdout, mode="r|") as stream:
                    stream.extractall(snapshot, filter=_snapshot_member)
            except (tarfile.TarError, OSError) as exc:
                archive.kill()
                raise OSError("local diagnostics could not materialize the revision") from exc
            finally:
                watchdog.cancel()
            code = archive.wait(timeout=_GIT_TIMEOUT_SECONDS)
        if code != 0:
            raise OSError("local diagnostics could not materialize the revision")
        yield snapshot


def _diagnostic_environment() -> dict[str, str]:
    retained = (
        "HOME",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "VIRTUAL_ENV",
        "WINDIR",
    )
    environment = {
        key: os.environ[key]
        for key in retained
        if key in os.environ
    }
    environment.update({"NO_COLOR": "1", "PYTHONUTF8": "1"})
    return environment
