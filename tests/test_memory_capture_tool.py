"""The model's own write path into OMH memory: `omh_memory(action="capture")`.

The tool spawns the installed `omh` CLI (the bundle cannot import `omh`), the
default policy is auto-safe so no operator step sits in the loop, and a
duplicate names the record already held instead of leaving a candidate nobody
will approve.
"""

from __future__ import annotations

from contextlib import chdir
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package
from _platform_support import requires_posix

load_local_package()
from omh.paths import resolve_paths
from omh.plugin_bundle.omh.memory_provider import OmhMemoryProvider
from omh.plugin_bundle.omh.tools import memory_tool
from omh.plugin_bundle.omh.tools.memory_tool import MEMORY_ACTIONS, OMH_MEMORY_SCHEMA, omh_memory_handler
from omh.profiles.setup import write_setup_profile
from omh.workflows.memory import read_project_memory_policy
from project_identity_fixture import seed_project_identity


def _cli_payload(**overrides: object) -> dict:
    """The shape `omh memory capture` prints for an auto-safe approval."""
    payload: dict = {
        "schema_version": "project_memory_capture/v1",
        "captured": True,
        "auto_approved": True,
        "candidate": {"candidate_id": "cand_1", "status": "approved"},
        "record": {"record_id": "mem_1", "admission": {"state": "approved_auto_safe"}},
        "receipt_state": "replay_ready",
    }
    payload.update(overrides)
    return payload


class CaptureToolMappingTests(unittest.TestCase):
    """The CLI is replaced by a fake runner; the mapping is what is under test."""

    def setUp(self) -> None:
        temporary = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.repo = temporary / "repo"
        self.repo.mkdir()
        seed_project_identity(self.repo)
        self.outside = temporary / "outside"
        self.outside.mkdir()
        self.enterContext(patch.dict(os.environ, {"OMH_HOME": str(temporary / "omh"), "HERMES_HOME": str(temporary / "hermes")}))
        self.enterContext(patch.object(memory_tool, "_resolve_omh_executable", return_value="/fake/bin/omh"))
        self.run = self.enterContext(patch.object(memory_tool.subprocess, "run"))
        self.reply(_cli_payload())

    def reply(self, payload: dict, *, returncode: int = 0, stderr: str = "") -> None:
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], returncode, json.dumps(payload), stderr)

    def call(self, cwd: Path | None = None, **args) -> dict:
        return json.loads(omh_memory_handler({"action": "capture", **args}, cwd=str(cwd or self.repo)))

    def argv(self) -> list[str]:
        return list(self.run.call_args.args[0])

    def test_schema_advertises_capture_beside_the_four_reads(self) -> None:
        self.assertEqual(MEMORY_ACTIONS, ("status", "blocks", "read", "consolidation", "capture"))
        properties = OMH_MEMORY_SCHEMA["parameters"]["properties"]
        self.assertEqual(tuple(properties["action"]["enum"]), MEMORY_ACTIONS)
        self.assertEqual(properties["record_type"]["enum"], ["fact", "decision", "lesson", "procedure", "episode"])
        self.assertEqual(properties["scope"]["enum"], ["project", "user"])
        self.assertEqual(properties["retention_class"]["enum"], ["durable", "standard", "volatile"])
        description = OMH_MEMORY_SCHEMA["description"]
        for guidance in ("capture", "preference", "decision", "secrets", "raw logs", "transcripts", "task progress"):
            self.assertIn(guidance, description)

    def test_an_auto_safe_capture_is_remembered_with_the_receipt_the_cli_printed(self) -> None:
        result = self.call(summary="The user deploys from the release branch only.", tags=["deploy"])
        self.assertEqual(result["status"], "remembered")
        self.assertEqual(result["receipt_state"], "replay_ready")
        self.assertEqual(result["record_id"], "mem_1")
        self.assertEqual(result["candidate_id"], "cand_1")
        self.assertEqual(result["admission_state"], "approved_auto_safe")
        self.assertEqual(result["action"], "capture")
        self.assertIn("claim_boundary", result)
        argv = self.argv()
        self.assertEqual(argv[0], "/fake/bin/omh")
        self.assertEqual(argv[argv.index("--on-duplicate") + 1], "skip")
        self.assertEqual(argv[argv.index("--scope-kind") + 1], "project")
        self.assertEqual(argv[argv.index("--retention-class") + 1], "durable")
        self.assertEqual(argv[argv.index("--type") + 1], "fact")
        self.assertEqual(argv[argv.index("--omh-home") + 1], os.environ["OMH_HOME"])
        self.assertEqual(argv[-2:], ["--", "The user deploys from the release branch only."])
        self.assertIn("--tag=deploy", argv)
        kwargs = self.run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(Path(kwargs["cwd"]), self.repo)

    def test_a_receipt_short_of_replay_ready_is_never_upgraded(self) -> None:
        self.reply(_cli_payload(receipt_state="indexes_refreshed"))
        result = self.call(summary="A fact the evaluator did not clear.")
        self.assertEqual(result["status"], "remembered")
        self.assertEqual(result["receipt_state"], "indexes_refreshed")

    def test_a_held_candidate_is_pending_review_with_its_reason(self) -> None:
        for reason in ("unsafe_content", "relative_time_phrase", "duplicate", "policy_review_first"):
            with self.subTest(reason=reason):
                self.reply(_cli_payload(auto_approved=False, record={}, receipt_state="candidate_persisted", review_reason=reason))
                result = self.call(summary="Held for review.")
                self.assertEqual(result["status"], "pending_review")
                self.assertEqual(result["review_reason"], reason)
                self.assertEqual(result["receipt_state"], "candidate_persisted")
                self.assertEqual(result["admission_state"], "pending_review")
                self.assertIsNone(result["record_id"])

    def test_a_duplicate_is_already_remembered_and_names_the_record(self) -> None:
        self.reply({"captured": False, "auto_approved": False, "reason": "duplicate", "duplicate_of": "mem_old", "receipt_state": None})
        result = self.call(summary="Already held.")
        self.assertEqual(result["status"], "already_remembered")
        self.assertEqual(result["duplicate_of"], "mem_old")
        self.assertEqual(result["record_id"], "mem_old")
        self.assertIsNone(result["receipt_state"])

    def test_memory_turned_off_is_refused(self) -> None:
        self.reply({"captured": False, "auto_approved": False, "reason": "project_memory_disabled"})
        result = self.call(summary="Anything.")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "project_memory_disabled")

    def test_a_missing_cli_is_omh_cli_unavailable_with_a_next_action(self) -> None:
        with patch.object(memory_tool, "_resolve_omh_executable", return_value=None):
            result = self.call(summary="Cannot be saved.")
        self.assertEqual(result["status"], "omh_cli_unavailable")
        self.assertIn("next_action", result)
        self.run.assert_not_called()
        self.run.side_effect = FileNotFoundError("omh")
        self.assertEqual(self.call(summary="Vanished between lookup and spawn.")["status"], "omh_cli_unavailable")

    def test_a_timeout_is_an_error_not_a_save(self) -> None:
        self.run.side_effect = subprocess.TimeoutExpired(["omh"], 30)
        result = self.call(summary="Slow store.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "timeout")
        self.assertIsNone(result["receipt_state"])

    def test_a_failing_cli_is_an_error_carrying_its_last_stderr_line(self) -> None:
        self.reply({}, returncode=2, stderr="omh: memory capture requires a summary\n")
        result = self.call(summary="Broken.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "cli_exit_2")
        self.assertEqual(result["detail"], "omh: memory capture requires a summary")
        self.run.return_value = subprocess.CompletedProcess([], 0, "not json", "")
        self.assertEqual(self.call(summary="Garbled.")["reason"], "unparseable_cli_output")

    def test_scope_defaults_to_user_outside_a_repository_and_project_there_is_refused(self) -> None:
        self.call(cwd=self.outside, summary="Prefers terse replies.")
        self.assertEqual(self.argv()[self.argv().index("--scope-kind") + 1], "user-global")
        self.run.reset_mock()
        refused = self.call(cwd=self.outside, summary="Repo fact.", scope="project")
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reason"], "project_scope_unresolved")
        self.run.assert_not_called()

    def test_values_that_look_like_flags_stay_values(self) -> None:
        self.call(summary="--scope-kind thread is how it starts", tags=["--type"])
        argv = self.argv()
        self.assertIn("--tag=--type", argv)
        self.assertEqual(argv[-2:], ["--", "--scope-kind thread is how it starts"])

    def test_invalid_input_is_refused_before_anything_spawns(self) -> None:
        for args, reason in (
            ({}, "summary_required"),
            ({"summary": "x" * 501}, "summary_too_long"),
            ({"summary": "ok", "record_type": "rumor"}, "unsupported_record_type"),
            ({"summary": "ok", "retention_class": "forever"}, "unsupported_retention_class"),
            ({"summary": "ok", "tags": "deploy"}, "invalid_tags"),
            ({"summary": "ok", "scope": "thread"}, "unsupported_scope"),
        ):
            with self.subTest(reason=reason):
                result = self.call(**args)
                self.assertEqual((result["status"], result["reason"]), ("refused", reason))
        self.run.assert_not_called()

    def test_the_child_never_inherits_provider_credentials(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-not-for-the-child", "PATH": "/usr/bin"}):
            self.call(summary="Env check.")
        env = self.run.call_args.kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_the_four_read_actions_still_answer(self) -> None:
        for action in ("status", "blocks", "read", "consolidation"):
            with self.subTest(action=action):
                payload = json.loads(omh_memory_handler({"action": action}))
                self.assertEqual(payload["action"], action)
                self.assertNotEqual(payload["source_backend"], "omh_cli")
        self.run.assert_not_called()


@requires_posix
class ExecutableResolutionTests(unittest.TestCase):
    def test_the_managed_generation_is_found_when_path_has_no_omh(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            script = home / ".local" / "share" / "omh" / "current" / "venv" / "bin" / "omh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
            env = {"HOME": str(home), "PATH": str(home / "empty")}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(memory_tool._resolve_omh_executable(), str(script))
                script.unlink()
                self.assertIsNone(memory_tool._resolve_omh_executable())

    @requires_posix
    def test_the_managed_generation_wins_over_an_older_omh_on_path(self) -> None:
        # The `current` generation installed this bundle, so its CLI speaks
        # this bundle's flags; a checkout or pip `omh` earlier on PATH may not.
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            managed = home / ".local" / "share" / "omh" / "current" / "venv" / "bin" / "omh"
            on_path = home / "elsewhere" / "omh"
            for script in (managed, on_path):
                script.parent.mkdir(parents=True)
                script.write_text("#!/bin/sh\n", encoding="utf-8")
                script.chmod(0o755)
            env = {"HOME": str(home), "PATH": str(on_path.parent)}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(memory_tool._resolve_omh_executable(), str(managed))
                managed.unlink()
                self.assertEqual(memory_tool._resolve_omh_executable(), str(on_path), "PATH is the second choice")


class CaptureInputRefusalTests(unittest.TestCase):
    def test_a_nul_byte_is_refused_before_anything_is_spawned(self) -> None:
        # A NUL cannot travel in an argv: subprocess raises ValueError past
        # every handler, so the tool raised instead of answering.
        with patch.object(memory_tool, "_run_capture", side_effect=AssertionError("must not spawn")):
            for args in (
                {"action": "capture", "summary": "Use pnpm\x00 for installs."},
                {"action": "capture", "summary": "Use pnpm for installs.", "tags": ["tool\x00ing"]},
            ):
                result = json.loads(omh_memory_handler(args))
                self.assertEqual((result["status"], result["reason"]), ("refused", "control_character"), result)

    def test_an_unbound_home_is_an_error_result_not_a_raise(self) -> None:
        with patch.object(memory_tool, "_resolve_omh_executable", return_value="/usr/bin/true"), patch.object(
            memory_tool, "_home", side_effect=memory_tool.runtime_paths.RuntimeBindingError("no profile")
        ), patch.object(memory_tool, "_session_cwd", return_value=None):
            result = json.loads(omh_memory_handler({"action": "capture", "summary": "Use pnpm for installs.", "scope": "user"}))
            self.assertEqual((result["status"], result["reason"]), ("error", "RuntimeBindingError"), result)


class DefaultPolicyTests(unittest.TestCase):
    def _paths(self, root: Path):
        return resolve_paths(root / ".omh", root / ".hermes")

    def test_no_profile_is_auto_safe_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            policy = read_project_memory_policy(self._paths(Path(tmp)))
            self.assertEqual((policy["mode"], policy["mode_source"]), ("auto-safe", "default"))
            self.assertTrue(policy["auto_approve_safe"])

    def test_setup_without_a_mode_is_defaulted_and_with_one_is_explicit(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            profile = write_setup_profile(paths, [])
            self.assertEqual(profile["memory_policy"]["mode_source"], "default")
            self.assertEqual(read_project_memory_policy(paths)["mode"], "auto-safe")
            write_setup_profile(paths, [], memory_mode="review-first")
            policy = read_project_memory_policy(paths)
            self.assertEqual((policy["mode"], policy["mode_source"]), ("review-first", "explicit"))
            write_setup_profile(paths, [], memory_mode="off")
            policy = read_project_memory_policy(paths)
            self.assertEqual((policy["mode"], policy["capture_enabled"]), ("off", False))

    def test_a_rerun_without_a_mode_keeps_the_mode_the_operator_chose(self) -> None:
        # Setup rewrites the whole profile. A rerun that does not mention
        # memory must not turn an explicit review-first into the default and
        # relabel it `default`, which the legacy rule would then flip.
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            write_setup_profile(paths, [], memory_mode="review-first")
            profile = write_setup_profile(paths, [])
            self.assertEqual((profile["memory_policy"]["mode"], profile["memory_policy"]["mode_source"]), ("review-first", "explicit"))
            self.assertEqual(read_project_memory_policy(paths)["mode"], "review-first")
            # A new explicit choice still replaces it; a defaulted profile stays defaulted.
            write_setup_profile(paths, [], memory_mode="auto-safe")
            self.assertEqual(read_project_memory_policy(paths)["mode_source"], "explicit")
            paths.setup_profile_path.unlink()
            write_setup_profile(paths, [])
            self.assertEqual(write_setup_profile(paths, [])["memory_policy"]["mode_source"], "default")

    def test_a_profile_written_by_the_old_default_follows_the_new_one(self) -> None:
        # The owner machine's profile: review-first stored by the old default,
        # with no mode_source, because nobody passed --memory-mode.
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            profile = write_setup_profile(paths, [])
            for legacy_mode, expected in (("review-first", ("auto-safe", "default")), ("off", ("off", "legacy_explicit"))):
                with self.subTest(legacy_mode=legacy_mode):
                    policy = {key: value for key, value in profile["memory_policy"].items() if key != "mode_source"}
                    stored = {**profile, "memory_mode": legacy_mode, "memory_policy": {**policy, "mode": legacy_mode}}
                    paths.setup_profile_path.write_text(json.dumps(stored), encoding="utf-8")
                    policy = read_project_memory_policy(paths)
                    self.assertEqual((policy["mode"], policy["mode_source"]), expected)

    def test_memory_status_discloses_where_the_mode_came_from(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(Path(tmp) / ".hermes"), "memory", "status"])
            self.assertEqual(status, 0, stderr)
            policy = json.loads(stdout)["policy"]
            self.assertEqual((policy["mode"], policy["mode_source"]), ("auto-safe", "default"))


class OnDuplicateSkipTests(unittest.TestCase):
    def test_skip_persists_nothing_and_names_the_existing_record(self) -> None:
        with TemporaryDirectory() as tmp, chdir(tmp):
            root = Path(tmp)
            seed_project_identity(root)
            homes = ["--omh-home", str(root / "store"), "--hermes-home", str(root / "hermes")]
            status, stdout, stderr = run_cli([*homes, "memory", "capture", "Staging deploys run before production."])
            self.assertEqual(status, 0, stderr)
            first = json.loads(stdout)
            self.assertTrue(first["auto_approved"])
            record_id = first["record"]["record_id"]
            candidates = root / "store" / "memory" / "candidates"
            before = sorted(path.name for path in candidates.iterdir())

            status, stdout, stderr = run_cli([*homes, "memory", "capture", "--on-duplicate", "skip", "Staging  deploys run before PRODUCTION."])
            self.assertEqual(status, 0, stderr)
            skipped = json.loads(stdout)
            self.assertFalse(skipped["captured"])
            self.assertEqual((skipped["reason"], skipped["duplicate_of"]), ("duplicate", record_id))
            self.assertEqual(sorted(path.name for path in candidates.iterdir()), before)

            # The default keeps the reviewer-facing behaviour: a stamped candidate.
            status, stdout, stderr = run_cli([*homes, "memory", "capture", "Staging deploys run before production."])
            kept = json.loads(stdout)
            self.assertEqual(kept["candidate"]["duplicate_of"], record_id)
            self.assertEqual(kept["review_reason"], "duplicate")
            self.assertEqual(kept["receipt_state"], "candidate_persisted")
            self.assertEqual(len(list(candidates.iterdir())), len(before) + 1)

    def test_skip_only_counts_a_record_held_under_the_same_scope(self) -> None:
        # The same fact stated in another project is a different memory: a
        # record confined to project A is never recalled in project B, so
        # "already remembered" would drop it there for good. The reviewer
        # stamp (default mode) may still point across scopes.
        with TemporaryDirectory() as tmp, chdir(tmp):
            root = Path(tmp)
            seed_project_identity(root)
            homes = ["--omh-home", str(root / "store"), "--hermes-home", str(root / "hermes")]
            scope_a = ["--scope-kind", "project", "--scope-ref", "acme-project-a"]
            scope_b = ["--scope-kind", "project", "--scope-ref", "acme-project-b"]
            status, stdout, stderr = run_cli([*homes, "memory", "capture", *scope_a, "Use pnpm, not npm, for installs."])
            self.assertEqual(status, 0, stderr)
            held_in_a = json.loads(stdout)["record"]["record_id"]

            status, stdout, stderr = run_cli([*homes, "memory", "capture", "--on-duplicate", "skip", *scope_b, "Use pnpm, not npm, for installs."])
            self.assertEqual(status, 0, stderr)
            in_b = json.loads(stdout)
            self.assertTrue(in_b["captured"], in_b)
            self.assertTrue(in_b["auto_approved"], "a cross-scope match is not a duplicate here and must not block auto-safe")
            self.assertNotEqual(in_b["record"]["record_id"], held_in_a)
            self.assertNotIn("duplicate_of", in_b["candidate"])

            status, stdout, stderr = run_cli([*homes, "memory", "capture", "--on-duplicate", "skip", *scope_a, "Use pnpm, not npm, for installs."])
            self.assertEqual(status, 0, stderr)
            again_in_a = json.loads(stdout)
            self.assertFalse(again_in_a["captured"])
            self.assertEqual(again_in_a["duplicate_of"], held_in_a)

            # Default mode keeps the reviewer-facing cross-scope hint.
            status, stdout, stderr = run_cli([*homes, "memory", "capture", "--scope-kind", "project", "--scope-ref", "acme-project-c", "Use pnpm, not npm, for installs."])
            self.assertEqual(status, 0, stderr)
            self.assertIn(json.loads(stdout)["candidate"]["duplicate_of"], {held_in_a, in_b["record"]["record_id"]})


@requires_posix
class RealCliEndToEndTests(unittest.TestCase):
    """The tool drives the real CLI in a temp home through a console-script shim."""

    def test_capture_reaches_replay_ready_on_disk_and_the_next_prefetch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "repo"
            repo.mkdir()
            seed_project_identity(repo)
            shim = root / "bin" / "omh"
            shim.parent.mkdir()
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -P -m omh.cli "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
            store = root / "store"
            env = {"OMH_HOME": str(store), "HERMES_HOME": str(root / "hermes")}
            summary = "The operator ships releases only from the release branch."
            with patch.dict(os.environ, env), patch.object(memory_tool, "_resolve_omh_executable", return_value=str(shim)):
                result = json.loads(omh_memory_handler({"action": "capture", "summary": summary, "tags": ["release"]}, cwd=str(repo)))
                self.assertEqual(result["status"], "remembered", result)
                self.assertEqual(result["receipt_state"], "replay_ready")
                self.assertEqual(result["admission_state"], "approved_auto_safe")
                record_id = result["record_id"]
                memory_dir = store / "memory"
                self.assertTrue((memory_dir / "records" / f"{record_id}.json").is_file())
                self.assertEqual(len(list((memory_dir / "reviews").iterdir())), 1)
                index = json.loads((memory_dir / "index.json").read_text(encoding="utf-8"))
                self.assertIn(f"records/{record_id}.json", index["record_files"])
                candidates = sorted((memory_dir / "candidates").iterdir())

                again = json.loads(omh_memory_handler({"action": "capture", "summary": summary}, cwd=str(repo)))
                self.assertEqual((again["status"], again["duplicate_of"]), ("already_remembered", record_id))
                self.assertEqual(sorted((memory_dir / "candidates").iterdir()), candidates)

            provider = OmhMemoryProvider(store, hermes_home=root / "hermes")
            provider.initialize("s1", hermes_home=str(root / "hermes"), agent_context="primary", cwd=str(repo))
            self.assertIn(summary, provider.prefetch("release branch"))


if __name__ == "__main__":
    unittest.main()
