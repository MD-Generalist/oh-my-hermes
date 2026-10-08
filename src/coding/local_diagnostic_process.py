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
from threading import Event, Thread, Timer
from typing import Iterator

from .diagnostic_execution import CancellationSignal, ProviderObservation
from .diagnostic_providers import GLOBAL_MAX_DIAGNOSTICS_PER_CHECK
from .fanout_confinement import prepare_dispatcher_git_fence
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


def workspace_git_command(workspace: str | Path, argv: Sequence[str], *, allow_unconfined: bool) -> list[str]:
    """`argv` placed inside a fence for the unit worktree it runs in (#1999).

    The worktree is the unit's, so its git configuration, hooks and filters may
    be the unit's too. Raises OSError when no fence can be proven and the
    operator did not pass `--allow-unconfined`; the engine reports that as a
    crashed diagnostic, never as one that ran.
    """
    fence = prepare_dispatcher_git_fence(Path(workspace), allow_unconfined=allow_unconfined)
    command = fence.dispatcher_command(argv)
    if command is not None:
        return list(command)
    if fence.unconfined_allowed:
        return list(argv)
    raise OSError("local diagnostics found no write fence for git in the unit worktree")


class LocalDiagnosticProviderRunner:
    """Run one closed-set provider against one immutable Git snapshot."""

    def __init__(self, executables: Mapping[str, str], *, allow_unconfined: bool = False) -> None:
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
        self.allow_unconfined = allow_unconfined

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
        if cancelled is not None and cancelled.is_set():
            return ProviderObservation("cancelled")
        with _revision_snapshot(
            Path(workspace_id),
            revision,
            allow_unconfined=self.allow_unconfined,
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
    *,
    allow_unconfined: bool,
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
        command = workspace_git_command(
            workspace,
            ["git", "archive", "--format=tar", revision],
            allow_unconfined=allow_unconfined,
        )
        with subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as archive:
            watchdog = Timer(_GIT_TIMEOUT_SECONDS, archive.kill)
            watchdog.start()
            try:
                with tarfile.open(fileobj=archive.stdout, mode="r|") as stream:
                    stream.extractall(snapshot, filter="data")
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
