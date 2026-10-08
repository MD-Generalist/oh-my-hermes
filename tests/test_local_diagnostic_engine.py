"""Real-process tests for the repository-owned diagnostic adapter."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _fanout_host_fence import host_cannot_fence_fanout
from _local_package import load_local_package

load_local_package()

from omh.coding.diagnostic_execution import DiagnosticExecutionRequest  # noqa: E402
from omh.coding.local_diagnostic_process import WorkspaceGitFences  # noqa: E402
from omh.coding.local_diagnostic_engine import (  # noqa: E402
    LocalDiagnosticProviderRunner,
    build_local_diagnostic_engine,
)


class LocalDiagnosticEngineTests(unittest.TestCase):
    def test_allowlisted_provider_observes_both_revisions_without_leaking_output(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo, baseline, end = self._repository(
                root,
                self._fake_ruff_source(),
            )
            engine = build_local_diagnostic_engine(
                allow_unconfined=host_cannot_fence_fanout(),
                executable_lookup=lambda provider: (
                    sys.executable
                    if provider == "ruff"
                    else None
                )
            )

            with patch.dict(os.environ, {"OMH_DIAGNOSTIC_SECRET": "top-secret"}):
                result = engine.execute(
                    DiagnosticExecutionRequest(
                        owner="wrapper",
                        workspace_id="fanout-1:core",
                        baseline_revision=baseline,
                        end_revision=end,
                        workspace_path=str(repo),
                    )
                )

            self.assertEqual(result.status, "ok")
            self.assertEqual(len(result.results), 1)
            provider = result.results[0]
            self.assertEqual(provider.provider_id, "ruff")
            self.assertEqual(provider.status, "ok")
            self.assertEqual(provider.evidence["verdict"], "new_diagnostics_observed")
            self.assertEqual(provider.evidence["introduced_count"], 1)
            self.assertEqual(provider.evidence["introduced"][0]["code"], "F821")
            self.assertEqual(provider.evidence["introduced"][0]["path"], "seed.py")
            self.assertNotIn("message", repr(provider.evidence))
            self.assertNotIn("top-secret", repr(provider.evidence))
            worktrees = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    def test_no_discovered_provider_reports_unsupported_without_spawning(self) -> None:
        with TemporaryDirectory() as raw:
            repo, baseline, end = self._repository(Path(raw))
            result = build_local_diagnostic_engine(
                allow_unconfined=host_cannot_fence_fanout(),
                executable_lookup=lambda _provider: None
            ).execute(
                DiagnosticExecutionRequest(
                    owner="wrapper",
                    workspace_id="fanout-1:core",
                    baseline_revision=baseline,
                    end_revision=end,
                    workspace_path=str(repo),
                )
            )

        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.results, ())

    def test_runner_refuses_executable_keys_outside_the_closed_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            LocalDiagnosticProviderRunner({"shell": "/bin/sh"})

    def test_running_provider_honors_cancellation_and_cleans_its_worktree(self) -> None:
        class CancelAfterSpawn:
            def __init__(self) -> None:
                self.probes = 0

            def is_set(self) -> bool:
                self.probes += 1
                return self.probes >= 2

        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo, _baseline, end = self._repository(
                root,
                self._blocking_ruff_source(),
            )
            runner = LocalDiagnosticProviderRunner(
                {"ruff": sys.executable},
                git=WorkspaceGitFences(allow_unconfined=host_cannot_fence_fanout()),
            )

            observation = runner.run(
                "ruff",
                str(repo),
                end,
                ("seed.py",),
                5_000,
                CancelAfterSpawn(),
            )

            self.assertEqual(observation.state, "cancelled")
            worktrees = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    def test_over_limit_provider_output_is_partial_and_never_clean(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo, baseline, end = self._repository(
                root,
                self._noisy_ruff_source(),
            )
            engine = build_local_diagnostic_engine(
                allow_unconfined=host_cannot_fence_fanout(),
                executable_lookup=lambda provider: (
                    sys.executable
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

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.results[0].status, "partial")
        self.assertNotEqual(
            result.results[0].evidence["verdict"],
            "no_new_diagnostics_observed",
        )

    def _repository(
        self,
        root: Path,
        provider_source: str | None = None,
    ) -> tuple[Path, str, str]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        source = repo / "seed.py"
        source.write_text("value = 1\n", encoding="utf-8")
        tracked = ["seed.py"]
        if provider_source is not None:
            (repo / "check").write_text(provider_source, encoding="utf-8")
            tracked.append("check")
        subprocess.run(["git", "add", *tracked], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "baseline"],
            cwd=repo,
            check=True,
        )
        baseline = self._head(repo)
        source.write_text("value = missing_name\n", encoding="utf-8")
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

    def _fake_ruff_source(self) -> str:
        return "\n".join(
            (
                "import json",
                "import os",
                "from pathlib import Path",
                "import sys",
                "if os.environ.get('OMH_DIAGNOSTIC_SECRET'):",
                "    raise SystemExit(7)",
                "target = next(Path(value) for value in sys.argv[1:] if value.endswith('.py'))",
                "rows = []",
                "if 'missing_name' in target.read_text(encoding='utf-8'):",
                "    rows.append({",
                "        'code': 'F821',",
                "        'message': 'Undefined name from source',",
                "        'filename': str(target),",
                "        'location': {'row': 1, 'column': 9},",
                "    })",
                "print(json.dumps(rows))",
                "raise SystemExit(1 if rows else 0)",
                "",
            )
        )

    def _blocking_ruff_source(self) -> str:
        return "\n".join(
            (
                "import time",
                "while True:",
                "    time.sleep(60)",
                "",
            )
        )

    def _noisy_ruff_source(self) -> str:
        return "\n".join(
            (
                "print('x' * 3_000_000)",
                "",
            )
        )

if __name__ == "__main__":
    unittest.main()
