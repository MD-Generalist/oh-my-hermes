"""Operator-path coverage for fanout final-review execution."""

from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import textwrap
import unittest

from _local_package import load_local_package

load_local_package()

from omh.commands.main import build_parser  # noqa: E402
from omh.coding.fanout_final_review_hook import (  # noqa: E402
    run_final_review_after_integration,
)
from omh.coding.final_review_local_engine import (  # noqa: E402
    FinalReviewLocalEngineConfig,
    FinalReviewLocalEngineError,
    HermesFinalReviewEngine,
)


_FAKE_HERMES = r"""
#!/usr/bin/env python3
import json
from pathlib import Path
import sys

args = sys.argv[1:]
# Mirrors the installed Hermes CLI: only `chat --query-file -` reads stdin (#1824).
if args[:3] != ["chat", "--query-file", "-"]:
    raise SystemExit("no Hermes stdin transport in argv")
sys.stdin.read()
print("<verdict>PASS</verdict>")
"""


def _repository(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "seed",
        ],
        cwd=repo,
        check=True,
    )
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, tree

_MUTATING_FAKE_HERMES = r"""
#!/usr/bin/env python3
import json
from pathlib import Path
import sys

args = sys.argv[1:]
# Mirrors the installed Hermes CLI: only `chat --query-file -` reads stdin (#1824).
if args[:3] != ["chat", "--query-file", "-"]:
    raise SystemExit("no Hermes stdin transport in argv")
sys.stdin.read()
Path("review-mutation.txt").write_text("mutated", encoding="utf-8")
print("<verdict>PASS</verdict>")
"""


class FanoutFinalReviewCliTests(unittest.TestCase):
    def test_final_review_is_an_explicit_fanout_option(self) -> None:
        base = [
            "coding",
            "fanout",
            "dispatch",
            "fanout-1",
            "--goal-file",
            "goal.txt",
        ]

        self.assertFalse(build_parser().parse_args(base).final_review)
        self.assertTrue(
            build_parser().parse_args([*base, "--final-review"]).final_review
        )

    def test_sanctioned_hermes_engine_produces_four_observed_pass_lanes(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="omh-final-review-") as raw:
            root = Path(raw)
            repo, tree = _repository(root)
            hermes = root / "hermes.py"
            hermes.write_text(
                textwrap.dedent(_FAKE_HERMES).lstrip(),
                encoding="utf-8",
            )
            hermes.chmod(0o755)
            engine = HermesFinalReviewEngine(
                FinalReviewLocalEngineConfig(
                    worktree=repo,
                    goal_text="Review the integrated implementation.",
                    provider="fake-provider",
                    model="fake-model",
                    reasoning="medium",
                    timeout_seconds=5,
                    hermes=str(hermes),
                )
            )

            result = run_final_review_after_integration(
                engine,
                integrated_revision=tree,
                integration_green=True,
                producer_evidence=True,
                workspace_revision=lambda: tree,
            )

        self.assertEqual(result["final_review_status"], "PASS")
        self.assertEqual(len(result["final_review_records"]), 4)
        self.assertTrue(
            all(
                record["execution_observed"]
                for record in result["final_review_records"]
            )
        )
        # Each built-in child starts from a detached checkout and a prompt of
        # goal, lens, and revision only.
        self.assertEqual(
            [record["context_provenance"] for record in result["final_review_records"]],
            ["fresh_from_diff"] * 4,
        )

    def test_final_review_configuration_refuses_missing_provider(self) -> None:
        with TemporaryDirectory(prefix="omh-final-review-config-") as raw:
            with self.assertRaisesRegex(
                FinalReviewLocalEngineError,
                "--hermes-provider",
            ):
                FinalReviewLocalEngineConfig(
                    worktree=Path(raw),
                    goal_text="Review.",
                    provider="",
                    model="fake-model",
                    reasoning="medium",
                    timeout_seconds=5,
                )

    def test_mutating_child_is_contained_and_cannot_pass(self) -> None:
        with TemporaryDirectory(prefix="omh-final-review-isolation-") as raw:
            root = Path(raw)
            repo, tree = _repository(root)
            hermes = root / "hermes.py"
            hermes.write_text(
                textwrap.dedent(_MUTATING_FAKE_HERMES).lstrip(),
                encoding="utf-8",
            )
            hermes.chmod(0o755)
            engine = HermesFinalReviewEngine(
                FinalReviewLocalEngineConfig(
                    worktree=repo,
                    goal_text="Review the integrated implementation.",
                    provider="fake-provider",
                    model="fake-model",
                    reasoning="medium",
                    timeout_seconds=5,
                    hermes=str(hermes),
                )
            )

            result = run_final_review_after_integration(
                engine,
                integrated_revision=tree,
                integration_green=True,
                producer_evidence=True,
                workspace_revision=lambda: tree,
            )

            self.assertEqual(result["final_review_status"], "BLOCK")
            self.assertFalse((repo / "review-mutation.txt").exists())
            worktrees = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)


if __name__ == "__main__":
    unittest.main()
