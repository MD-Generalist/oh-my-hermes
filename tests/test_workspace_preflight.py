"""The four pre-spawn blockers, observed against a real temporary git repository.

Every pass case runs real `git`; the failure cases are built the most
deterministic way each one allows, and the one case that cannot be built
offline on every CI filesystem (a promisor clone with its objects gone) is
driven through an injected runner, which is said so in the test name.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.unit_execution_state import (  # noqa: E402
    UNIT_STATE_DATA_MISSING,
    UNIT_STATE_PERMISSION_BLOCKED,
)
from omh.coding.workspace_preflight import (  # noqa: E402
    CHECK_CASE_COLLISION,
    CHECK_FILE_WRITE,
    CHECK_GIT_INDEX_WRITE,
    CHECK_OBJECTS_PRESENT,
    WORKSPACE_PREFLIGHT_CHECKS,
    WORKSPACE_PREFLIGHT_SCHEMA_VERSION,
    probe_workspace,
    workspace_preflight_reason,
    workspace_preflight_unit_state,
)


def _git(repo: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv], cwd=str(repo), check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _make_repo(root: Path, name: str = "repo") -> tuple[Path, str]:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _by_name(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(check["name"]): check for check in report["checks"]}  # type: ignore[index]


def _case_insensitive(path: Path) -> bool:
    probe = path / "omh-case-probe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        return (path / "OMH-CASE-PROBE.TMP").exists()
    finally:
        probe.unlink()


class WorkspacePreflightPassTests(unittest.TestCase):
    def test_a_healthy_worktree_passes_every_check_and_leaves_nothing_behind(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            report = probe_workspace(repo, base_ref=sha, target_ref=sha)

            self.assertEqual(report["schema_version"], WORKSPACE_PREFLIGHT_SCHEMA_VERSION)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["blocking"], [])
            self.assertEqual(
                [check["name"] for check in report["checks"]], list(WORKSPACE_PREFLIGHT_CHECKS)
            )
            self.assertIn("not a partial clone", str(_by_name(report)[CHECK_OBJECTS_PRESENT]["detail"]))
            # The probe may not change what the unit would report as its work.
            self.assertEqual(_git(repo, "status", "--porcelain"), "")
            self.assertEqual(
                sorted(entry.name for entry in repo.iterdir()), [".git", "seed.txt"]
            )
            # Nor may it leave scratch paths inside the git directory.
            git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
            self.assertEqual(
                [entry.name for entry in git_dir.iterdir() if "workspace-preflight" in entry.name],
                [],
            )

    def test_a_passing_report_names_no_state_and_no_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            report = probe_workspace(repo, base_ref=sha, target_ref=None)

            self.assertEqual(workspace_preflight_unit_state(report), "")
            self.assertEqual(workspace_preflight_reason(report), "")

    def test_the_real_index_is_never_touched_by_the_index_write_check(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))
            index = Path(_git(repo, "rev-parse", "--absolute-git-dir")) / "index"
            before = index.read_bytes()

            report = probe_workspace(repo, base_ref=sha, target_ref=None)

            self.assertTrue(report["ok"], report)
            self.assertEqual(index.read_bytes(), before)
            self.assertEqual(_git(repo, "ls-files"), "seed.txt")


class WorkspacePreflightFileWriteTests(unittest.TestCase):
    def test_a_worktree_that_does_not_exist_blocks_on_file_write(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-worktree"

            report = probe_workspace(missing, base_ref=None, target_ref=None)

            self.assertFalse(report["ok"])
            self.assertIn(CHECK_FILE_WRITE, report["blocking"])
            self.assertEqual(workspace_preflight_unit_state(report), UNIT_STATE_PERMISSION_BLOCKED)
            self.assertIn("could not write a scratch file", workspace_preflight_reason(report))

    @unittest.skipIf(
        os.name != "posix", "directory mode bits are the POSIX way to make a path unwritable"
    )
    @unittest.skipIf(
        os.geteuid() == 0 if hasattr(os, "geteuid") else False,
        "root ignores the write bit, so a read-only directory proves nothing",
    )
    def test_an_unwritable_worktree_blocks_before_any_spawn(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))
            os.chmod(repo, 0o500)
            try:
                report = probe_workspace(repo, base_ref=sha, target_ref=None)
            finally:
                os.chmod(repo, 0o700)

            self.assertFalse(report["ok"])
            self.assertIn(CHECK_FILE_WRITE, report["blocking"])
            self.assertEqual(workspace_preflight_unit_state(report), UNIT_STATE_PERMISSION_BLOCKED)


class WorkspacePreflightIndexWriteTests(unittest.TestCase):
    def test_a_path_that_is_not_a_repository_blocks_on_index_write(self) -> None:
        with TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()

            report = probe_workspace(plain, base_ref=None, target_ref=None)

            self.assertFalse(report["ok"])
            # The file write succeeds -- the directory is fine. What fails is
            # everything that needs a repository, which is the distinction the
            # per-check detail exists to make.
            self.assertTrue(_by_name(report)[CHECK_FILE_WRITE]["ok"])
            self.assertIn(CHECK_GIT_INDEX_WRITE, report["blocking"])
            self.assertEqual(workspace_preflight_unit_state(report), UNIT_STATE_PERMISSION_BLOCKED)

    def test_an_object_store_that_refuses_a_write_blocks_on_index_write(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))
            git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))

            def runner(argv, **kwargs):
                if argv[:2] == ["git", "hash-object"]:
                    return subprocess.CompletedProcess(argv, 128, "", "error: unable to write object")
                return subprocess.run(argv, **kwargs)

            report = probe_workspace(repo, base_ref=sha, target_ref=None, runner=runner)

            self.assertFalse(report["ok"])
            self.assertIn(CHECK_GIT_INDEX_WRITE, report["blocking"])
            self.assertIn(
                "refused a write", str(_by_name(report)[CHECK_GIT_INDEX_WRITE]["detail"])
            )
            self.assertEqual(
                [entry.name for entry in git_dir.iterdir() if "workspace-preflight" in entry.name],
                [],
            )

    def test_a_redirected_gitfile_gets_no_dispatcher_write_in_its_git_dir(self) -> None:
        # #2040: the git directory is whatever the worktree's `.git` file
        # names, and a reused worktree's `.git` file is the unit's to rewrite.
        # The dispatcher process itself may write nothing there; what lands in
        # it is git's own work, which the unit's fence confines on a reused
        # worktree. Observed at every git call, because a scratch file the
        # probe removes again leaves nothing to find afterwards.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _make_repo(root)
            worktree = root / "unit"
            _git(repo, "worktree", "add", "-q", "-b", "agent/unit", str(worktree))
            decoy = root / "decoy.git"
            _git(root, "clone", "-q", "--bare", str(repo), str(decoy))
            (worktree / ".git").write_text(f"gitdir: {decoy}\n", encoding="utf-8")
            self.assertEqual(Path(_git(worktree, "rev-parse", "--absolute-git-dir")).resolve(), decoy.resolve())
            seen_before_git_index_write: list[str] = []
            index_written = False

            def runner(argv, **kwargs):
                nonlocal index_written
                if not index_written:
                    seen_before_git_index_write.extend(
                        entry.name for entry in decoy.iterdir() if "workspace-preflight" in entry.name
                    )
                if argv[:2] == ["git", "read-tree"]:
                    index_written = True
                return subprocess.run(argv, **kwargs)

            report = probe_workspace(worktree, base_ref=sha, target_ref=None, runner=runner)

            self.assertTrue(_by_name(report)[CHECK_GIT_INDEX_WRITE]["ok"], report)
            self.assertTrue(index_written)
            self.assertEqual(seen_before_git_index_write, [])
            self.assertEqual(
                [entry.name for entry in decoy.iterdir() if "workspace-preflight" in entry.name], []
            )

    def test_the_dispatchers_own_runner_hands_git_the_blob_on_stdin(self) -> None:
        # The blob now reaches `hash-object` as runner input, so the runner the
        # dispatcher actually uses has to deliver it, or every dispatch would
        # block on an object-store write that never happened.
        from omh.coding.fanout_dispatch import signal_safe_unit_runner

        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            report = probe_workspace(repo, base_ref=sha, target_ref=None, runner=signal_safe_unit_runner)

            self.assertTrue(_by_name(report)[CHECK_GIT_INDEX_WRITE]["ok"], report)

    def test_a_runner_with_a_non_numeric_exit_code_is_treated_as_a_blocker(self) -> None:
        # An unanswerable question is a blocker, never a pass: a probe that
        # cannot observe the index write must not report that it observed one.
        class _NoReturnCode:
            returncode = None
            stdout = ""
            stderr = ""

        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            def runner(argv, **kwargs):
                if argv[:2] == ["git", "update-index"]:
                    return _NoReturnCode()
                return subprocess.run(argv, **kwargs)

            report = probe_workspace(repo, base_ref=sha, target_ref=None, runner=runner)

            self.assertFalse(report["ok"])
            self.assertIn(CHECK_GIT_INDEX_WRITE, report["blocking"])

    def test_a_git_binary_that_cannot_be_run_is_a_blocker_not_an_exception(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            def runner(argv, **kwargs):
                raise OSError("git is not executable here")

            report = probe_workspace(repo, base_ref=sha, target_ref=None, runner=runner)

            self.assertFalse(report["ok"])
            self.assertEqual(
                sorted(report["blocking"]),
                sorted([CHECK_GIT_INDEX_WRITE, CHECK_OBJECTS_PRESENT, CHECK_CASE_COLLISION])
                if _case_insensitive(repo)
                else sorted([CHECK_GIT_INDEX_WRITE, CHECK_OBJECTS_PRESENT]),
            )


class WorkspacePreflightObjectsTests(unittest.TestCase):
    def test_a_base_ref_that_is_not_present_blocks_as_data_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, _sha = _make_repo(Path(tmp))

            report = probe_workspace(
                repo, base_ref="0" * 40, target_ref=None
            )

            self.assertFalse(report["ok"])
            self.assertEqual(report["blocking"], [CHECK_OBJECTS_PRESENT])
            self.assertEqual(workspace_preflight_unit_state(report), UNIT_STATE_DATA_MISSING)
            self.assertIn("base_ref", str(_by_name(report)[CHECK_OBJECTS_PRESENT]["detail"]))

    def test_two_histories_with_no_merge_base_block_as_data_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _make_repo(root)
            # An orphan branch shares no commit with the base, which is the
            # same answer the incident's missing merge-base produced.
            _git(repo, "checkout", "-q", "--orphan", "other")
            (repo / "other.txt").write_text("other\n", encoding="utf-8")
            _git(repo, "add", "other.txt")
            _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "orphan")
            orphan = _git(repo, "rev-parse", "HEAD")

            report = probe_workspace(repo, base_ref=sha, target_ref=orphan)

            self.assertFalse(report["ok"])
            self.assertEqual(report["blocking"], [CHECK_OBJECTS_PRESENT])
            self.assertIn("no merge base", str(_by_name(report)[CHECK_OBJECTS_PRESENT]["detail"]))

    def test_a_partial_clone_missing_objects_is_the_incident_condition(self) -> None:
        # Driven through an injected runner rather than a real `--filter=blob:none`
        # clone: building one offline needs a promisor remote whose objects are
        # then removed, and whether git reaches for that remote varies by
        # version and by whether `GIT_NO_LAZY_FETCH` is understood. The runner
        # states the two facts the check reads -- the clone is partial, and the
        # scan printed a missing object -- without depending on either.
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            def runner(argv, **kwargs):
                if argv[:3] == ["git", "config", "--get"]:
                    if argv[3] == "extensions.partialClone":
                        return subprocess.CompletedProcess(argv, 0, "origin\n", "")
                    if argv[3] == "core.repositoryFormatVersion":
                        return subprocess.CompletedProcess(argv, 0, "1\n", "")
                if argv[:2] == ["git", "rev-list"]:
                    return subprocess.CompletedProcess(
                        argv, 0, f"{sha}\n?{'a' * 40}\n?{'b' * 40}\n", ""
                    )
                return subprocess.run(argv, **kwargs)

            report = probe_workspace(repo, base_ref=sha, target_ref=None, runner=runner)

            self.assertFalse(report["ok"])
            self.assertEqual(report["blocking"], [CHECK_OBJECTS_PRESENT])
            self.assertEqual(workspace_preflight_unit_state(report), UNIT_STATE_DATA_MISSING)
            detail = str(_by_name(report)[CHECK_OBJECTS_PRESENT]["detail"])
            self.assertIn("2 object(s)", detail)
            self.assertIn("fetching them is not permitted here", detail)

    def test_a_partial_clone_with_every_object_present_still_passes(self) -> None:
        # A partial clone is not a failure. It is reported, and only an
        # actually absent object blocks.
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            def runner(argv, **kwargs):
                if argv[:4] == ["git", "config", "--get", "extensions.partialClone"]:
                    return subprocess.CompletedProcess(argv, 0, "origin\n", "")
                return subprocess.run(argv, **kwargs)

            report = probe_workspace(repo, base_ref=sha, target_ref=None, runner=runner)

            self.assertTrue(report["ok"], report)
            detail = str(_by_name(report)[CHECK_OBJECTS_PRESENT]["detail"])
            self.assertIn("partial clone", detail)
            self.assertIn("no absent objects", detail)

    def test_the_missing_object_scan_never_reaches_a_network(self) -> None:
        seen: list[list[str]] = []

        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            def runner(argv, **kwargs):
                seen.append(list(argv))
                self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
                self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                if argv[:4] == ["git", "config", "--get", "extensions.partialClone"]:
                    return subprocess.CompletedProcess(argv, 0, "origin\n", "")
                return subprocess.run(argv, **kwargs)

            probe_workspace(repo, base_ref=sha, target_ref=None, runner=runner)

        for argv in seen:
            self.assertNotIn(argv[1], {"fetch", "pull", "clone", "remote", "ls-remote"})
        self.assertIn(["git", "rev-list", "--objects", "--missing=print", "-n", "200", "HEAD"], seen)


class WorkspacePreflightCaseCollisionTests(unittest.TestCase):
    def test_tracked_paths_differing_only_in_case_block_on_an_insensitive_filesystem(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, _sha = _make_repo(Path(tmp))
            # `update-index --cacheinfo` writes both spellings into the tree
            # without either ever existing on disk, which is the only way to
            # build this fixture on a case-insensitive filesystem.
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=str(repo), input="x\n", text=True, capture_output=True, check=True,
            ).stdout.strip()
            for name in ("README.md", "readme.md"):
                _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
            _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "collide")
            sha = _git(repo, "rev-parse", "HEAD")

            report = probe_workspace(repo, base_ref=sha, target_ref=None)

            check = _by_name(report)[CHECK_CASE_COLLISION]
            if not _case_insensitive(repo):
                self.assertTrue(check["ok"])
                self.assertIn("case-sensitive", str(check["detail"]))
                return
            self.assertFalse(report["ok"])
            self.assertIn(CHECK_CASE_COLLISION, report["blocking"])
            # It cannot be cleared by retrying, so it is a permission-shaped
            # block rather than missing data.
            self.assertEqual(workspace_preflight_unit_state(report), UNIT_STATE_PERMISSION_BLOCKED)
            self.assertIn("README.md vs readme.md", str(check["detail"]))

    def test_a_tree_without_collisions_passes_on_either_filesystem(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, sha = _make_repo(Path(tmp))

            report = probe_workspace(repo, base_ref=sha, target_ref=None)

            check = _by_name(report)[CHECK_CASE_COLLISION]
            self.assertTrue(check["ok"], check)
            self.assertIn(
                "case-insensitive" if _case_insensitive(repo) else "case-sensitive",
                str(check["detail"]),
            )


class WorkspacePreflightReportingTests(unittest.TestCase):
    def test_the_first_blocking_check_decides_the_state(self) -> None:
        # File write is checked first because it EXPLAINS the index write, so
        # a report carrying both must send the repair to the filesystem.
        report = {
            "blocking": [CHECK_FILE_WRITE, CHECK_OBJECTS_PRESENT],
            "checks": [
                {"name": CHECK_FILE_WRITE, "ok": False, "detail": "denied"},
                {"name": CHECK_OBJECTS_PRESENT, "ok": False, "detail": "absent"},
            ],
        }

        self.assertEqual(workspace_preflight_unit_state(report), UNIT_STATE_PERMISSION_BLOCKED)
        reason = workspace_preflight_reason(report)
        self.assertIn(CHECK_FILE_WRITE, reason)
        self.assertIn(CHECK_OBJECTS_PRESENT, reason)
        self.assertIn("denied", reason)

    def test_every_check_name_maps_to_an_execution_state(self) -> None:
        for name in WORKSPACE_PREFLIGHT_CHECKS:
            report = {"blocking": [name], "checks": [{"name": name, "ok": False, "detail": "d"}]}
            self.assertIn(
                workspace_preflight_unit_state(report),
                {UNIT_STATE_PERMISSION_BLOCKED, UNIT_STATE_DATA_MISSING},
                name,
            )


if __name__ == "__main__":
    unittest.main()
