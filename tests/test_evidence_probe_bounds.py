"""`omh_gather_evidence` runs only bounded probes inside the host working directory.

The model chooses `project_root` and the command suffix, so both are bounded:
the root must be the host working directory or inside it, `-m unittest` takes
only reporting flags, `git diff` refuses options that write a file, run a
configured program, or read outside the repository, and no `uv run` prefix is
shipped.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from omh.plugin_bundle.omh import runtime_paths
from omh.plugin_bundle.omh.tools import evidence_tool


class EvidenceProbeBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.host_cwd = base / "project"
        self.host_cwd.mkdir()
        self.outside = base / "elsewhere"
        self.outside.mkdir()
        cwd_patch = patch.object(runtime_paths, "runtime_cwd", return_value=self.host_cwd)
        cwd_patch.start()
        self.addCleanup(cwd_patch.stop)
        run_patch = patch.object(evidence_tool.subprocess, "run",
                                 return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.child = run_patch.start()
        self.addCleanup(run_patch.stop)

    def gather(self, commands: list[str], **args: str) -> dict:
        return json.loads(evidence_tool.omh_evidence_handler({"commands": commands, **args}))

    def assert_refused(self, command: str, reason: str) -> None:
        with self.subTest(command=command):
            self.child.reset_mock()
            result = self.gather([command])
            self.assertFalse(result["all_pass"])
            self.assertEqual(result["results"][0]["evidence_type"], "rejected")
            self.assertIn(reason, result["results"][0]["output_tail"])
            self.child.assert_not_called()

    def test_project_root_defaults_to_the_host_working_directory(self) -> None:
        result = self.gather(["python -m unittest"])
        self.assertTrue(result["all_pass"])
        self.assertEqual(result["project_root"], str(self.host_cwd))
        self.assertEqual(self.child.call_args.kwargs["cwd"], str(self.host_cwd))

    def test_project_root_inside_the_host_working_directory_runs(self) -> None:
        inner = self.host_cwd / "pkg"
        inner.mkdir()
        result = self.gather(["python -m unittest -v"], project_root=str(inner))
        self.assertTrue(result["all_pass"])
        self.assertEqual(self.child.call_args.kwargs["cwd"], str(inner))

    def test_project_root_outside_the_host_working_directory_is_refused(self) -> None:
        for root in (self.outside, self.host_cwd.parent, Path(os.sep)):
            with self.subTest(root=str(root)):
                result = self.gather(["python -m unittest"], project_root=str(root))
                self.assertEqual(result["error"], "project_root must be the host working directory or inside it")
        self.child.assert_not_called()

    def test_a_symlink_inside_the_working_directory_cannot_leave_it(self) -> None:
        link = self.host_cwd / "escape"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = self.gather(["python -m unittest"], project_root=str(link))
        self.assertEqual(result["error"], "project_root must be the host working directory or inside it")
        result = self.gather(["python -m unittest"], workdir=str(link))
        self.assertEqual(result["error"], "workdir must stay within project_root")
        self.child.assert_not_called()

    def test_unittest_takes_only_reporting_flags(self) -> None:
        for command in (
            "python -m unittest discover",
            "python -m unittest discover -s /tmp",
            "python3 -m unittest -s tests",
            "python -m unittest -t .",
            "python -m unittest -p '*.py'",
            "python -m unittest tests.test_x",
            "python -m unittest -v evil_module",
            "python3 -m unittest /tmp/payload.py",
        ):
            self.assert_refused(command, "unittest argument not allowed")

    def test_unittest_reporting_flags_are_allowed(self) -> None:
        for command in ("python -m unittest", "python3 -m unittest -v -f --locals",
                        "python -m unittest -q -b -c", "python -m unittest -k test_name"):
            with self.subTest(command=command):
                self.child.reset_mock()
                self.assertTrue(self.gather([command])["all_pass"])
                self.child.assert_called_once()

    def test_an_operator_entry_naming_a_module_permits_only_that_module(self) -> None:
        with patch.object(evidence_tool, "_allowlist", return_value=("python -m unittest tests.test_a",)):
            self.assertTrue(self.gather(["python -m unittest tests.test_a -v"])["all_pass"])
            self.assert_refused("python -m unittest tests.test_a tests.test_b", "unittest argument not allowed")

    def test_no_uv_run_prefix_is_shipped(self) -> None:
        for entry in evidence_tool._allowlist() + evidence_tool._DEFAULT_ALLOWLIST:
            with self.subTest(entry=entry):
                self.assertNotEqual(entry.split()[:2], ["uv", "run"])
        self.assert_refused("uv run python -m unittest", "command not in allowlist")

    def test_an_operator_added_uv_run_prefix_is_still_bounded(self) -> None:
        with patch.object(evidence_tool, "_allowlist", return_value=("uv run python -m unittest",)):
            self.assert_refused("uv run python -m unittest discover -s /tmp", "unittest argument not allowed")

    def test_git_diff_refuses_file_writes_external_programs_and_outside_reads(self) -> None:
        with patch.object(evidence_tool, "_allowlist", return_value=("git diff --check", "git diff")):
            for command in ("git diff --check --output=/tmp/x", "git diff --output /tmp/x",
                            "git diff --ext-diff", "git diff --textconv", "git diff --no-index /etc/hosts a"):
                self.assert_refused(command, "git diff option not allowed")
            self.assertTrue(self.gather(["git diff --check", "git diff HEAD~1.. -- tests"])["all_pass"])


if __name__ == "__main__":
    unittest.main()
