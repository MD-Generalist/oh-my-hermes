"""Real process-group cleanup coverage for successful diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from _fanout_host_fence import host_cannot_fence_fanout
from _local_package import load_local_package
from _platform_support import requires_posix, requires_windows

load_local_package()

from omh.coding.diagnostic_execution import DiagnosticExecutionRequest  # noqa: E402
from omh.coding._hermes_child_process import process_absent  # noqa: E402
from omh.coding.local_diagnostic_engine import build_local_diagnostic_engine  # noqa: E402
from omh.coding.local_diagnostic_process_owner import start_owned_process  # noqa: E402


class LocalDiagnosticProcessCleanupTests(unittest.TestCase):
    @requires_posix
    def test_successful_provider_reaps_background_child_group(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo, baseline, end = self._repository(root)
            ruff = self._orphaning_ruff(root)
            engine = build_local_diagnostic_engine(
                allow_unconfined=host_cannot_fence_fanout(),
                executable_lookup=lambda provider: (
                    str(ruff)
                    if provider == "ruff"
                    else None
                )
            )

            result = engine.execute(
                DiagnosticExecutionRequest(
                    owner="wrapper",
                    workspace_id="fanout-1:core",
                    baseline_revision=baseline,
                    end_revision=end,
                    workspace_path=str(repo),
                )
            )

            pids = [
                int(value)
                for value in (root / "diagnostic-children.pid")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            alive: list[int] = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                alive.append(pid)
            for pid in alive:
                os.kill(pid, signal.SIGKILL)

            self.assertEqual(result.status, "ok")
            self.assertEqual(len(pids), 2)
            self.assertEqual(alive, [])

    @requires_windows
    def test_windows_job_reaps_child_after_successful_leader_exit(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / "child.pid"
            code = "\n".join(
                (
                    "from pathlib import Path",
                    "import subprocess",
                    "import sys",
                    "child = subprocess.Popen(",
                    "    [sys.executable, '-c', 'import time; time.sleep(60)'],",
                    "    stdin=subprocess.DEVNULL,",
                    "    stdout=subprocess.DEVNULL,",
                    "    stderr=subprocess.DEVNULL,",
                    ")",
                    f"Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8')",
                )
            )
            process, owner = start_owned_process(
                [sys.executable, "-c", code],
                cwd=root,
                env=dict(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            process.wait(timeout=10)
            child_pid = int(pid_file.read_text(encoding="utf-8"))

            cleanup_verified = owner.terminate(signal.SIGTERM)

            self.assertTrue(
                cleanup_verified,
                msg=f"termination diagnostic: {owner.termination_diagnostic!r}",
            )
            self.assertTrue(process_absent(child_pid))

    def _repository(self, root: Path) -> tuple[Path, str, str]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        source = repo / "seed.py"
        source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "baseline"],
            cwd=repo,
            check=True,
        )
        baseline = self._head(repo)
        source.write_text("value = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "end"],
            cwd=repo,
            check=True,
        )
        return repo, baseline, self._head(repo)

    def _head(self, repo: Path) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _orphaning_ruff(self, root: Path) -> Path:
        executable = root / "ruff-orphaning"
        executable.write_text(
            "\n".join(
                (
                    f"#!{sys.executable}",
                    "from pathlib import Path",
                    "import subprocess",
                    "import sys",
                    "child = subprocess.Popen(",
                    "    [sys.executable, '-c', 'import time; time.sleep(60)'],",
                    "    stdin=subprocess.DEVNULL,",
                    "    stdout=subprocess.DEVNULL,",
                    "    stderr=subprocess.DEVNULL,",
                    ")",
                    "pid_file = Path(__file__).with_name('diagnostic-children.pid')",
                    "with pid_file.open('a', encoding='utf-8') as stream:",
                    "    stream.write(f'{child.pid}\\n')",
                    "print('[]')",
                    "",
                )
            ),
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable


if __name__ == "__main__":
    unittest.main()
