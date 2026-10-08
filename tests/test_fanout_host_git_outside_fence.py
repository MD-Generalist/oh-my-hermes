"""Host git in a unit worktree runs inside a fence on the paths #1995 left out (#1999).

A unit can write its own worktree and, when it holds git roots, its own gitdir.
That is enough to point git at configuration of its choosing: the gitdir's
`commondir` file names where git reads `config`, so a unit can hand any later
git call in its worktree a `core.fsmonitor`, a `core.hooksPath` and a filter
driver. Each planted program here appends to a marker OUTSIDE the worktree, so
the marker existing is the proof that unit-chosen code ran with the operator's
write access. The behavioural classes run on macOS, the one host with a fence
CI does not need bwrap for; the wiring class runs on every job.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout_confinement import _git_write_roots  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402

_GIT = "/usr/bin/git"
_IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.test")


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.run((_GIT, *arguments), cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _linked_worktree(root: Path, files: dict[str, str]) -> tuple[Path, Path]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # Ignored, so what the unit plants does not itself read as dirty work.
    files = {".gitignore": ".unit-git/\n", **files}
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", *files)
    _git(repo, *_IDENTITY, "commit", "-qm", "init")
    worktree = root / "linked-worktree"
    _git(repo, "worktree", "add", "-qb", "agent/unit", str(worktree), "HEAD")
    return repo, worktree


def _plant_script(worktree: Path, gitdir: Path, common: Path, marker: Path) -> str:
    """What a unit runs to hand later git calls in its worktree its own configuration.

    Every write lands in the worktree or in the unit's gitdir, the two places a
    unit holding git roots may write. `fire` appends to `marker`; run as a
    filter it then copies stdin to stdout, so it changes no content.
    """
    planted = worktree / ".unit-git"
    fire = planted / "fire"
    config = (
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
        f"\tfsmonitor = {fire}\n\thooksPath = {planted / 'hooks'}\n"
        f'[filter "planted"]\n\tclean = {fire} clean\n\tsmudge = {fire} smudge\n'
    )
    q = shlex.quote
    record = 'printf "%s\\n" "$0 $*" >> ' + q(str(marker))
    return "; ".join((
        "set -e",
        f"mkdir -p {q(str(planted / 'hooks'))}",
        f"ln -sfn {q(str(common / 'objects'))} {q(str(planted / 'objects'))}",
        f"ln -sfn {q(str(common / 'refs'))} {q(str(planted / 'refs'))}",
        f"printf '%s\\n' '#!/bin/sh' {q(record)} {q('case $1 in clean|smudge) exec cat;; esac')} > {q(str(fire))}",
        f"chmod +x {q(str(fire))}",
        f"cp {q(str(fire))} {q(str(planted / 'hooks' / 'post-checkout'))}",
        f"printf %s {q(config)} > {q(str(planted / 'config'))}",
        f"printf '%s\\n' {q(str(planted))} > {q(str(gitdir / 'commondir'))}",
    ))


def _plant(worktree: Path, marker: Path) -> None:
    gitdir = Path(_git(worktree, "rev-parse", "--absolute-git-dir"))
    common = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    subprocess.run(("/bin/sh", "-c", _plant_script(worktree, gitdir, common, marker)), check=True)
    # The plant took: plain host git in this worktree now runs the unit's program.
    marker.write_text("", encoding="utf-8")
    subprocess.run(
        (_GIT, "status", "--porcelain"), cwd=worktree, stdin=subprocess.DEVNULL, capture_output=True, check=False,
    )
    assert marker.read_text(encoding="utf-8"), "the planted fsmonitor did not fire for plain host git"
    marker.write_text("", encoding="utf-8")


@unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
class FenceDecisionTests(unittest.TestCase):
    def test_deciding_the_git_roots_runs_no_program_the_unit_planted(self) -> None:
        """`_git_write_roots` runs git before any fence exists, by construction.

        Its four reads (`symbolic-ref`, `rev-parse --git-dir/--git-common-dir`)
        load no index and fire no hook or filter, so they stay on the host; this
        pins that, with the same plant that fires through `git status`.
        """
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo, worktree = _linked_worktree(root, {"seed": "seed\n"})
            marker = root / "marker"
            (worktree / ".gitattributes").write_text("seed filter=planted\n", encoding="utf-8")
            (worktree / "seed").write_text("changed\n", encoding="utf-8")
            _plant(worktree, marker)
            # The redirected common dir is not the repository's, so no git root is granted.
            self.assertEqual(_git_write_roots(worktree, "agent/unit", repo), ())
            self.assertEqual(marker.read_text(encoding="utf-8"), "")


@unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
class StatusResumeProbeTests(unittest.TestCase):
    """`fanout status` observes a unit worktree outside any dispatch."""

    def _resume(self, worktree: Path, **options: Any) -> tuple[dict[str, object], list[Any]]:
        from omh.coding import fanout_status

        identity = SimpleNamespace(resolved_path="/nonexistent/codex")
        binding = SimpleNamespace(
            fanout_id="fanout-1", contract_digest="digest", unit_id="unit", run_ref="run", worktree_path=str(worktree),
        )
        receipt = SimpleNamespace(
            binding=binding, capability=SimpleNamespace(executor="codex", binary_identity=identity), to_dict=dict,
        )
        observed: list[Any] = []

        def project(_receipt: object, **kwargs: Any) -> dict[str, object]:
            observed.append(kwargs["workspace"])
            return {"available": False, "reason": "projected"}

        contract = {"fanout_id": "fanout-1", "units": [{"unit_id": "unit", "run_ref": "run", "owner": "codex"}]}
        with (
            mock.patch.object(fanout_status, "read_session_receipt", return_value=SimpleNamespace(receipt=receipt, reason="observed")),
            mock.patch.object(fanout_status, "fanout_contract_digest", return_value="digest"),
            mock.patch.object(fanout_status, "observe_session_binary", return_value=identity),
            mock.patch.object(fanout_status, "project_session_resume", side_effect=project),
        ):
            resume = fanout_status._resume_for_unit({"executor_session": {}}, contract, frozenset(), **options)
        return resume, observed

    def test_the_workspace_probe_runs_inside_a_fence_and_still_observes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _repo, worktree = _linked_worktree(root, {"seed": "seed\n"})
            marker = root / "marker"
            (worktree / ".gitattributes").write_text("seed filter=planted\n", encoding="utf-8")
            (worktree / "seed").write_text("changed\n", encoding="utf-8")
            _plant(worktree, marker)
            _resume, observed = self._resume(worktree)
            self.assertEqual(marker.read_text(encoding="utf-8"), "", "unit-planted git config ran on the host")
        (workspace,) = observed
        self.assertIsNotNone(workspace)
        self.assertTrue(workspace.dirty)

    def test_a_host_that_cannot_fence_observes_nothing_unless_the_operator_opts_in(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _repo, worktree = _linked_worktree(root, {"seed": "seed\n"})
            with mock.patch("omh.coding.fanout_confinement.backend_available", return_value=False):
                refused, refused_observed = self._resume(worktree)
                allowed, allowed_observed = self._resume(worktree, allow_unconfined=True)
        self.assertEqual(refused, {**refused, "available": False, "reason": "workspace_unfenced"})
        self.assertEqual(refused_observed, [])
        self.assertEqual(allowed["reason"], "projected")
        self.assertIsNotNone(allowed_observed[0])


@unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
class DiagnosticsGitFenceTests(unittest.TestCase):
    """`--diagnostics` reads revisions and materializes them from the unit's worktree."""

    def _unit_worktree(self, root: Path) -> tuple[Path, str, str, Path]:
        _repo, worktree = _linked_worktree(root, {"seed.py": "value = 1\n", "check": "print('[]')\n"})
        baseline = _git(worktree, "rev-parse", "HEAD")
        (worktree / ".gitattributes").write_text("*.txt filter=planted\n", encoding="utf-8")
        (worktree / "notes.txt").write_text("notes\n", encoding="utf-8")
        (worktree / "seed.py").write_text("value = 2\n", encoding="utf-8")
        _git(worktree, "add", "-A")
        _git(worktree, *_IDENTITY, "commit", "-qm", "unit")
        end = _git(worktree, "rev-parse", "HEAD")
        marker = root / "marker"
        _plant(worktree, marker)
        return worktree, baseline, end, marker

    def test_revision_reads_run_no_program_the_unit_planted(self) -> None:
        from omh.coding.local_diagnostic_engine import GitChangedFileResolver, GitRevisionReader

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, baseline, end, marker = self._unit_worktree(root)
            self.assertEqual(GitRevisionReader().read(str(worktree), "HEAD"), end)
            self.assertEqual(GitRevisionReader().read(str(worktree), baseline), baseline)
            changed = GitChangedFileResolver().resolve(str(worktree), baseline, end)
            self.assertEqual(marker.read_text(encoding="utf-8"), "", "unit-planted git config ran on the host")
        self.assertEqual(set(changed), {".gitattributes", "notes.txt", "seed.py"})

    def test_materializing_a_revision_runs_no_hook_or_filter_the_unit_planted(self) -> None:
        from omh.coding.local_diagnostic_engine import LocalDiagnosticProviderRunner

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree, _baseline, end, marker = self._unit_worktree(root)
            observation = LocalDiagnosticProviderRunner({"ruff": sys.executable}).run(
                "ruff", str(worktree), end, ("seed.py",), 10_000, None,
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "", "unit-planted git config ran on the host")
            registered = _git(root / "repo", "worktree", "list", "--porcelain").count("worktree ")
        self.assertEqual(observation.state, "completed")
        self.assertEqual(registered, 2)

    def test_a_host_that_cannot_fence_reads_nothing_unless_the_operator_opts_in(self) -> None:
        from omh.coding.local_diagnostic_engine import GitRevisionReader
        from omh.coding.local_diagnostic_process import WorkspaceGitFences

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _repo, worktree = _linked_worktree(root, {"seed.py": "value = 1\n"})
            head = _git(worktree, "rev-parse", "HEAD")
            with mock.patch("omh.coding.fanout_confinement.backend_available", return_value=False):
                with self.assertRaises(OSError):
                    GitRevisionReader().read(str(worktree), "HEAD")
                self.assertEqual(GitRevisionReader(git=WorkspaceGitFences(allow_unconfined=True)).read(str(worktree), "HEAD"), head)


@unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
class RepairAttemptPreflightTests(unittest.TestCase):
    """A repair attempt reuses the worktree the failed attempt wrote."""

    def _dispatch(self, root: Path, *, plant: bool) -> tuple[dict[str, Any], Path, Path]:
        from five_issue_cases.capacity import no_stagger, ready
        from omh.coding.fanout import build_fanout_contract
        from omh.coding.fanout_artifacts import write_fanout_contract
        from omh.coding.fanout_dispatch import dispatch_fanout, signal_safe_unit_runner

        repo = root / "repo"
        repo.mkdir()
        (repo / "seed").write_text("seed\n", encoding="utf-8")
        (repo / ".gitignore").write_text(".unit-git/\n", encoding="utf-8")
        _git(repo, "init", "-q")
        _git(repo, "add", "seed", ".gitignore")
        _git(repo, *_IDENTITY, "commit", "-qm", "init")
        base = _git(repo, "rev-parse", "HEAD")
        worktree = root / "repo-fanout-unit"
        common = repo / ".git"
        marker = root / "marker"
        marker.write_text("", encoding="utf-8")
        paths = OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes")
        goal = "Commit, then fail the check so a repair attempt reuses the worktree."
        contract = write_fanout_contract(paths, build_fanout_contract(goal, [{
            "unit_id": "unit", "title": "unit", "owner": "codex", "file_scope": ["seed"],
            "verification_commands": ["/usr/bin/false"], "max_repair_attempts": 1,
        }]))
        unit = contract["units"][0]
        planting = (_plant_script(worktree, common / "worktrees" / worktree.name, common, marker),) if plant else ()

        def unit_argv(_owner: object, prompt: str, _route: object) -> list[str]:
            match = re.search(r"JSON sidecar to exactly (.+)\.", prompt)
            assert match is not None
            payload = {
                "schema_version": "fanout_unit_result/v1", "unit_id": "unit", "run_id": unit["run_ref"],
                "fanout_id": contract["fanout_id"], "base_sha": base, "head_sha": "HEAD_SHA",
                "process_status": "process_succeeded", "changed_paths": ["seed"], "checks": [], "findings": [],
            }
            q = shlex.quote
            # The unit runs inside its own fence, which lets it write its worktree and gitdir.
            return ["/bin/sh", "-c", "; ".join((
                *planting,
                f"cd {q(str(worktree))}",
                "printf 'unit\\n' >> seed",
                f"{_GIT} add seed",
                f"{_GIT} {' '.join(_IDENTITY)} commit -qm unit",
                f"head=$({_GIT} rev-parse HEAD)",
                f"printf %s {q(json.dumps(payload))} | sed \"s/HEAD_SHA/$head/\" > {q(match[1])}",
            ))]

        with (
            mock.patch("omh.coding.fanout_dispatch.build_dispatch_argv", unit_argv),
            mock.patch("omh.coding.fanout_dispatch.negotiate_session_capability", return_value=None),
            mock.patch("omh.coding.fanout_dispatch._SpawnStagger.reserve", no_stagger),
        ):
            summary = dispatch_fanout(
                paths, contract, goal_text=goal, repo_root=repo, base_sha=base, concurrency=1,
                runner=signal_safe_unit_runner, readiness=ready, max_retries=0, run_verification=True,
                env={"PATH": "/usr/bin:/bin", "HOME": str(root / "home")},
            )
        (entry,) = summary["units"]
        return entry, worktree, marker

    def test_the_repair_attempts_preflight_runs_no_program_the_first_attempt_planted(self) -> None:
        with TemporaryDirectory() as temporary:
            entry, worktree, marker = self._dispatch(Path(temporary).resolve(), plant=True)
            self.assertTrue((worktree / ".unit-git" / "fire").is_file(), "the first attempt did not plant")
            self.assertEqual(marker.read_text(encoding="utf-8"), "", "unit-planted git config ran on the host")
        # The redirected common dir costs the unit its git roots, which the
        # preflight now measures from inside the fence rather than from the host.
        self.assertEqual(entry["reason_code"], "workspace_preflight_blocked", entry)
        self.assertEqual(entry["workspace_preflight"]["blocking"], ["git_index_write"])
        self.assertEqual(entry["repair"]["attempts_used"], 0)

    def test_an_ordinary_repair_attempt_passes_its_preflight_inside_the_fence(self) -> None:
        with TemporaryDirectory() as temporary:
            entry, _worktree, marker = self._dispatch(Path(temporary).resolve(), plant=False)
            self.assertEqual(marker.read_text(encoding="utf-8"), "")
        self.assertNotIn("workspace_preflight", entry)
        self.assertEqual(entry["repair"]["attempts_used"], 1, entry["repair"])


_DISPATCH_SOURCE = Path(__file__).resolve().parents[1] / "src" / "coding" / "fanout_dispatch.py"


class PreFenceWiringTests(unittest.TestCase):
    """Re-derived from source, so the placement holds on every job, bwrap or not."""

    def setUp(self) -> None:
        tree = ast.parse(_DISPATCH_SOURCE.read_text(encoding="utf-8"))
        self.functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.dispatch = self.functions["_dispatch_unit"]
        (self.fence_line,) = [
            node.lineno for node in ast.walk(self.dispatch)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "unit_git_runner" for target in node.targets)
        ]

    def _calls(self, function: ast.FunctionDef, name: str) -> list[ast.Call]:
        return [
            node for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        ]

    def _keyword(self, call: ast.Call, name: str) -> str | None:
        values = [ast.unparse(keyword.value) for keyword in call.keywords if keyword.arg == name]
        return values[0] if values else None

    def test_a_reused_worktrees_preflight_runs_through_the_units_fence(self) -> None:
        calls = self._calls(self.dispatch, "probe_workspace")
        after = [call for call in calls if call.lineno > self.fence_line]
        before = [call for call in calls if call.lineno < self.fence_line]
        self.assertEqual([self._keyword(call, "runner") for call in after], ["unit_git_runner"])
        # Before the fence exists, only a worktree this attempt just created is probed.
        self.assertEqual([self._keyword(call, "runner") for call in before], ["runner"])
        guards = [
            node for node in ast.walk(self.dispatch)
            if isinstance(node, (ast.If, ast.IfExp)) and any(child is before[0] for child in ast.walk(node))
        ]
        self.assertTrue(any("reused_worktree" in ast.unparse(guard.test) for guard in guards))

    def test_the_claim_and_reuse_probes_before_the_fence_are_given_a_fence(self) -> None:
        for name in ("claim_answered_worktree", "ensure_fanout_unit_worktree"):
            (call,) = self._calls(self.dispatch, name)
            self.assertLess(call.lineno, self.fence_line)
            self.assertEqual(self._keyword(call, "confinement"), "probe_fence", name)

    def test_the_post_spawn_clarification_probes_are_given_the_units_fence(self) -> None:
        (bind,) = self._calls(self.dispatch, "bind_clarification")
        self.assertEqual(self._keyword(bind, "confinement"), "confinement")
        (intake,) = self._calls(self.dispatch, "_intake_unit_result")
        self.assertEqual(self._keyword(intake, "confinement"), "confinement")
        for name in ("_intake_unit_result", "_intake_stdout_unit_result"):
            (head,) = self._calls(self.functions[name], "reported_producer_head")
            self.assertEqual(self._keyword(head, "confinement"), "confinement", name)


@unittest.skipUnless(os.name == "posix", "O_NOFOLLOW scratch creation is a POSIX path")
class ScratchIgnoreLinkTests(unittest.TestCase):
    """Fence preparation writes `.omh/confinement-tmp/.gitignore` on the host, in a unit's worktree."""

    def _plants(self, root: Path) -> dict[str, tuple[Path, Path]]:
        """Each case: a worktree with one planted link, and the host file it must not touch."""
        cases: dict[str, tuple[Path, Path]] = {}
        for name in ("file_symlink", "hard_link", "omh_dir_symlink", "scratch_dir_symlink"):
            worktree = root / name / "worktree"
            host = root / name / "host"
            worktree.mkdir(parents=True)
            host.mkdir()
            victim = host / (".gitignore" if name.endswith("dir_symlink") else "authorized_keys")
            victim.write_text("ssh-ed25519 operator\n", encoding="utf-8")
            scratch = worktree / ".omh" / "confinement-tmp"
            if name == "omh_dir_symlink":
                (host / "confinement-tmp").mkdir()
                victim = host / "confinement-tmp" / ".gitignore"
                victim.write_text("ssh-ed25519 operator\n", encoding="utf-8")
                (worktree / ".omh").symlink_to(host, target_is_directory=True)
            elif name == "scratch_dir_symlink":
                (worktree / ".omh").mkdir()
                scratch.symlink_to(host, target_is_directory=True)
            else:
                scratch.mkdir(parents=True)
                if name == "file_symlink":
                    (scratch / ".gitignore").symlink_to(victim)
                else:
                    os.link(victim, scratch / ".gitignore")
            cases[name] = (worktree, victim)
        return cases

    def test_a_planted_link_is_never_written_through(self) -> None:
        from omh.coding.fanout_confinement import _write_scratch_ignore

        with TemporaryDirectory() as temporary:
            for name, (worktree, victim) in self._plants(Path(temporary).resolve()).items():
                with self.subTest(name=name):
                    self.assertFalse(_write_scratch_ignore(worktree))
                    self.assertEqual(victim.read_text(encoding="utf-8"), "ssh-ed25519 operator\n")

    def test_an_ordinary_worktree_gets_its_ignore_file(self) -> None:
        from omh.coding.fanout_confinement import _write_scratch_ignore

        with TemporaryDirectory() as temporary:
            worktree = Path(temporary).resolve()
            self.assertTrue(_write_scratch_ignore(worktree))
            self.assertTrue(_write_scratch_ignore(worktree))
            self.assertEqual((worktree / ".omh" / "confinement-tmp" / ".gitignore").read_text(encoding="utf-8"), "*\n")

    @unittest.skipUnless(sys.platform == "darwin", "sandbox-exec confinement is exercised on macOS")
    def test_the_fence_is_not_enforced_over_a_planted_link(self) -> None:
        from omh.coding.fanout_confinement import prepare_dispatcher_git_fence

        with TemporaryDirectory() as temporary:
            for name, (worktree, victim) in self._plants(Path(temporary).resolve()).items():
                with self.subTest(name=name):
                    fence = prepare_dispatcher_git_fence(worktree)
                    self.assertFalse(fence.receipt["enforced"])
                    self.assertEqual(fence.receipt["reason_code"], "sandbox_scratch_unsafe")
                    self.assertIsNone(fence.dispatcher_command(("git", "status")))
                    self.assertEqual(victim.read_text(encoding="utf-8"), "ssh-ed25519 operator\n")


class SnapshotMemberFilterTests(unittest.TestCase):
    """The diagnostics snapshot extracts a tree the unit committed."""

    def test_only_regular_files_and_directories_are_materialized(self) -> None:
        import io
        import tarfile

        from omh.coding.local_diagnostic_process import _snapshot_member

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            directory = tarfile.TarInfo("pkg")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            data = b"value = 1\n"
            regular = tarfile.TarInfo("pkg/seed.py")
            regular.size = len(data)
            archive.addfile(regular, io.BytesIO(data))
            for name, kind, target in (
                ("pkg/escape", tarfile.SYMTYPE, "../../outside"),
                ("pkg/inside-link", tarfile.SYMTYPE, "seed.py"),
                ("pkg/hard", tarfile.LNKTYPE, "pkg/seed.py"),
                ("pkg/device", tarfile.CHRTYPE, ""),
                ("pkg/pipe", tarfile.FIFOTYPE, ""),
            ):
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = target
                archive.addfile(member)
        buffer.seek(0)
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "checkout"
            destination.mkdir()
            with tarfile.open(fileobj=buffer, mode="r|") as stream:
                stream.extractall(destination, filter=_snapshot_member)
            found = sorted(str(path.relative_to(destination)) for path in destination.rglob("*"))
            self.assertEqual([part.replace(os.sep, "/") for part in found], ["pkg", "pkg/seed.py"])
            self.assertEqual((destination / "pkg" / "seed.py").read_bytes(), data)

    def test_a_python_without_the_data_filter_reports_the_provider_unavailable(self) -> None:
        import tarfile

        from omh.coding.local_diagnostic_process import LocalDiagnosticProviderRunner

        with mock.patch.object(tarfile, "data_filter", None):
            observation = LocalDiagnosticProviderRunner({"ruff": sys.executable}).run(
                "ruff", "/nonexistent/worktree", "0" * 40, ("seed.py",), 1_000, None,
            )
        self.assertEqual(observation.state, "unavailable")


if __name__ == "__main__":
    unittest.main()
