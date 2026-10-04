from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.coding.unit_prompt_protocol import (  # noqa: E402
    HIGH_EFFORT_CALIBRATIONS,
    MODEL_HIGH_EFFORT_CALIBRATIONS,
    PARENT_CLARIFICATION_PROTOCOL,
    shared_unit_preamble_lines,
)


BASE = Path(__file__).resolve().parents[1] / "benchmarks" / "live-model-tools" / "v1"
sys.path.insert(0, str(BASE / "lib"))

from runner import _snapshot  # noqa: E402
from validation import changed_paths  # noqa: E402


def _load_omh_live():
    path = BASE / "lib" / "omh_live.py"
    spec = importlib.util.spec_from_file_location("omh_live", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OmhLiveAdapterTests(unittest.TestCase):
    def test_route_argv_uses_omh_model_route_surface(self) -> None:
        module = _load_omh_live()
        argv = module.route_argv("omh", model="qwen3-coder-next", effort="high")
        self.assertEqual(
            argv,
            [
                "omh",
                "coding",
                "model-route",
                "--executor",
                "hermes",
                "--model",
                "qwen3-coder-next",
                "--effort",
                "high",
                "--role",
                "implementation",
                "--json",
            ],
        )

    def test_run_trial_requires_effect_boundary_confirmation_and_bounded_input(self) -> None:
        module = _load_omh_live()
        with self.assertRaisesRegex(ValueError, "explicit paid-live confirmation"):
            module.run_trial(
                omh_executable="omh",
                hermes_executable="hermes",
                workspace=Path("/tmp/workspace"),
                omh_home=Path("/tmp/omh-home"),
                task="task",
                model="glm-5",
                provider="zai",
                effort="high",
                condition="baseline",
                parent_run_id="parent",
                run_id="child",
                timeout=30,
                confirmed=False,
            )
        with self.assertRaisesRegex(ValueError, "prompt exceeds"):
            module.run_trial(
                omh_executable="omh",
                hermes_executable="hermes",
                workspace=Path("/tmp/workspace"),
                omh_home=Path("/tmp/omh-home"),
                task="x" * 131_073,
                model="glm-5",
                provider="zai",
                effort="high",
                condition="baseline",
                parent_run_id="parent",
                run_id="child",
                timeout=30,
                confirmed=True,
            )

    def test_dispatch_argv_uses_explicit_omh_hermes_child_surface(self) -> None:
        module = _load_omh_live()
        argv = module.dispatch_argv(
            "omh",
            hermes_executable="/tmp/fake-hermes",
            workspace=Path("/tmp/workspace"),
            route={
                "selected_model": "glm-5",
                "selected_reasoning_effort": "high",
                "model_family": "glm",
            },
            provider="zai",
            parent_run_id="parent",
            run_id="child",
            timeout=30,
        )
        self.assertIn("hermes-child", argv)
        self.assertIn("dispatch", argv)
        self.assertIn("--confirm-dispatch", argv)
        self.assertNotIn("omo", argv)

    def test_current_session_argv_uses_current_config_without_isolated_child_flags(self) -> None:
        module = _load_omh_live()
        argv = module.current_session_argv(
            "/tmp/fake-hermes",
            route={
                "selected_model": "glm-5",
                "selected_reasoning_effort": "high",
                "model_family": "glm",
            },
            provider="zai",
            usage_file=Path("/tmp/usage.json"),
            prompt="do the task",
            workspace=Path("/tmp/workspace"),
        )
        self.assertEqual(
            argv,
            [
                "/tmp/fake-hermes",
                "--oneshot",
                "do the task",
                "--in",
                # The harness hands the CLI OS-native paths (str(Path(...)));
                # on Windows these render with backslashes, so the expectation
                # must too.
                str(Path("/tmp/workspace")),
                "--provider",
                "zai",
                "--model",
                "glm-5",
                "--reasoning",
                "high",
                "--toolsets",
                "file,terminal",
                "--usage-file",
                str(Path("/tmp/usage.json")),
            ],
        )
        self.assertNotIn("--ignore-user-config", argv)
        self.assertNotIn("--safe-mode", argv)

    def test_current_session_argv_accepts_an_explicit_profile_provider_override(self) -> None:
        module = _load_omh_live()
        argv = module.current_session_argv(
            "/tmp/fake-hermes",
            route={
                "selected_model": "moonshotai/kimi-k3-ultrafast",
                "selected_reasoning_effort": "medium",
                "model_family": "kimi",
            },
            provider="kimi_k3",
            usage_file=Path("/tmp/usage.json"),
            prompt="do the task",
            workspace=Path("/tmp/workspace"),
        )
        self.assertEqual(argv[argv.index("--provider") + 1], "kimi_k3")
        self.assertNotIn("hermes-child", argv)

    def test_prompt_pair_changes_only_omh_calibration(self) -> None:
        module = _load_omh_live()
        task = "Read TARGET.txt and return its exact contents."
        route = {
            "selected_model": "deepseek-v4-pro",
            "selected_reasoning_effort": "high",
            "model_family": "deepseek",
        }
        baseline, optimized = module.prompt_pair(task, route)
        self.assertIn(task, baseline)
        self.assertIn(task, optimized)
        self.assertIn("MANDATORY BENCHMARK COMPLETION CONTRACT", baseline)
        self.assertIn("write `.omh-benchmark-answer.json` at the workspace root", baseline)
        self.assertIn("Verify the file exists and parses as JSON", baseline)
        self.assertIn(HIGH_EFFORT_CALIBRATIONS["deepseek"], optimized)
        self.assertEqual(optimized, f"{HIGH_EFFORT_CALIBRATIONS['deepseek']}\n\n{baseline}")
        self.assertEqual(module.task_digest(baseline), module.task_digest(optimized))

    def test_family_condition_sends_the_inherited_block_without_the_override(self) -> None:
        module = _load_omh_live()
        task = "Read TARGET.txt and return its exact contents."
        route = {
            "selected_model": "gpt-6-astra",
            "selected_reasoning_effort": "xhigh",
            "model_family": "gpt",
        }
        override = MODEL_HIGH_EFFORT_CALIBRATIONS["gpt-6-astra"]
        family = HIGH_EFFORT_CALIBRATIONS["gpt"]
        prompts = {name: module.prompt_for_condition(task, route, name) for name in ("baseline", "optimized", "family")}
        self.assertNotIn(override, prompts["baseline"])
        self.assertNotIn(family, prompts["baseline"])
        self.assertIn(override, prompts["optimized"])
        self.assertNotIn(family, prompts["optimized"])
        self.assertIn(family, prompts["family"])
        self.assertNotIn(override, prompts["family"])
        self.assertEqual(prompts["family"].split("\n\n", 1)[1], prompts["baseline"])
        self.assertEqual({module.task_digest(prompt) for prompt in prompts.values()}, {module.task_digest(prompts["baseline"])})
        self.assertEqual(module.prompt_for_condition(task, route, "unknown-condition"), prompts["baseline"])

    def test_unit_condition_sends_the_product_head_before_the_optimized_prompt(self) -> None:
        module = _load_omh_live()
        task = "Read TARGET.txt and return its exact contents."
        route = {
            "selected_model": "gpt-6-astra",
            "selected_reasoning_effort": "xhigh",
            "model_family": "gpt",
        }
        head = "\n".join(shared_unit_preamble_lines(module.UNIT_HEAD_GOAL))
        baseline = module.prompt_for_condition(task, route, "baseline")
        unit = module.prompt_for_condition(task, route, "unit")
        override = MODEL_HIGH_EFFORT_CALIBRATIONS["gpt-6-astra"]
        self.assertEqual(
            unit,
            "\n\n".join((head, override, module.UNIT_DELIVERABLE_PRECEDENCE, baseline)),
        )
        self.assertIn(PARENT_CLARIFICATION_PROTOCOL, unit)
        self.assertNotIn(task, head)
        self.assertEqual(module.task_digest(unit), module.task_digest(baseline))
        # Without a high-effort route there is no calibration paragraph, and the
        # head is still sent.
        low = {**route, "selected_reasoning_effort": "low"}
        self.assertEqual(
            module.prompt_for_condition(task, low, "unit"),
            "\n\n".join((head, module.UNIT_DELIVERABLE_PRECEDENCE, module.prompt_for_condition(task, low, "baseline"))),
        )

    def test_unit_head_blocks_reproduce_the_product_head_bytes(self) -> None:
        module = _load_omh_live()
        blocks = module.unit_head_blocks()
        self.assertEqual(
            "\n".join(blocks.values()).encode("utf-8"),
            "\n".join(shared_unit_preamble_lines(module.UNIT_HEAD_GOAL)).encode("utf-8"),
        )
        self.assertEqual(module.unit_head(), "\n".join(blocks.values()))
        self.assertTrue(set(module.UNIT_LEAN_OMITTED_BLOCKS) <= set(blocks))
        with self.assertRaisesRegex(ValueError, "unknown unit head blocks"):
            module.unit_head(("NOT_A_BLOCK",))

    def test_unit_lean_omits_exactly_the_declared_head_blocks(self) -> None:
        module = _load_omh_live()
        task = "Read TARGET.txt and return its exact contents."
        route = {
            "selected_model": "deepseek-v4-pro",
            "selected_reasoning_effort": "high",
            "model_family": "deepseek",
        }
        blocks = module.unit_head_blocks()
        omitted = module.UNIT_LEAN_OMITTED_BLOCKS
        self.assertEqual(omitted, ("PARENT_CLARIFICATION",))
        unit = module.prompt_for_condition(task, route, "unit")
        lean = module.prompt_for_condition(task, route, "unit_lean")
        expected = unit
        for name in omitted:
            expected = expected.replace("\n" + blocks[name], "", 1)
            self.assertNotIn(blocks[name], lean)
        self.assertEqual(lean, expected)
        for name, text in blocks.items():
            if name not in omitted:
                self.assertIn(text, lean, name)
        self.assertEqual(
            len(unit.encode("utf-8")) - len(lean.encode("utf-8")),
            sum(len(("\n" + blocks[name]).encode("utf-8")) for name in omitted),
        )
        self.assertNotIn("fanout_unit_result/v1\",\"unit_id\"", lean)
        self.assertEqual(module.task_digest(lean), module.task_digest(unit))
        self.assertEqual(
            {condition: module.head_omitted_blocks(condition) for condition in ("baseline", "optimized", "family", "unit", "unit_lean")},
            {"baseline": None, "optimized": None, "family": None, "unit": [], "unit_lean": list(omitted)},
        )

    def test_snapshot_and_changed_paths_use_canonical_posix_keys(self) -> None:
        with TemporaryDirectory() as root_text:
            root = Path(root_text)
            target = root / "nested" / "file.txt"
            target.parent.mkdir()
            target.write_text("before", encoding="utf-8")

            initial = _snapshot(root)
            self.assertEqual(initial, {"nested/file.txt": b"before"})
            self.assertEqual(changed_paths(root, initial), [])

            target.write_text("after", encoding="utf-8")
            self.assertEqual(changed_paths(root, initial), ["nested/file.txt"])

    def test_observation_metrics_never_estimate_missing_usage(self) -> None:
        module = _load_omh_live()
        observation = {
            "schema_version": "routing_observation/v1",
            "claim": "observed",
            "status": "completed",
            "category": None,
            "lane": None,
            "role": None,
            "selected_owner": None,
            "selected_provider": None,
            "selected_model": None,
            "selected_reasoning": None,
            "fallback_chain": [],
            "fallback_index": None,
            "reason": "runtime_completed",
            "provenance": "runtime",
            "routing_provenance": None,
            "turn": None,
            "tools": 3,
            "tokens": None,
            "cost_usd": None,
            "rate_tokens_per_second": None,
            "current_action": None,
            "parent_session_id": None,
            "child_session_id": None,
            "run_id": None,
            "dispatch_id": None,
        }
        metrics = module.observation_metrics(observation)
        self.assertEqual(metrics["tools"], 3)
        self.assertIsNone(metrics["tokens"])
        self.assertIsNone(metrics["cost_usd"])

    def test_manifest_declares_omh_harness_without_omo(self) -> None:
        manifest = json.loads((BASE / "manifest.json").read_text())
        self.assertEqual(manifest["live_harness"]["kind"], "omh_hermes_child")
        serialized = json.dumps(manifest).lower()
        self.assertNotIn('"omo"', serialized)

    def test_current_session_trial_uses_fake_authenticated_cli_and_labels_execution(self) -> None:
        module = _load_omh_live()
        with TemporaryDirectory() as root_text:
            root = Path(root_text)
            omh = root / "omh.py"
            hermes = root / "hermes.py"
            workspace = root / "workspace"
            workspace.mkdir()
            omh.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
                "from omh.cli import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            hermes.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "args=sys.argv[1:]\n"
                "import os\n"
                "prompt=args[args.index('--oneshot')+1]\n"
                "assert os.environ.get('TERMINAL_CWD') == args[args.index('--in')+1]\n"
                "assert prompt\n"
                "assert 'MANDATORY BENCHMARK COMPLETION CONTRACT' in prompt\n"
                "assert 'write `.omh-benchmark-answer.json` at the workspace root' in prompt\n"
                "assert args[args.index('--toolsets')+1] == 'file,terminal'\n"
                "assert '--ignore-user-config' not in args\n"
                "usage=Path(args[args.index('--usage-file')+1])\n"
                "usage.write_text(json.dumps({'provider':'fake','model':'glm-5','tool_calls':3,'total_tokens':41,'estimated_cost_usd':0.25}),encoding='utf-8')\n",
                encoding="utf-8",
            )
            executable = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            omh.chmod(omh.stat().st_mode | executable)
            hermes.chmod(hermes.stat().st_mode | executable)
            trial = module.run_current_session_trial(
                omh_executable=str(omh),
                hermes_executable=str(hermes),
                workspace=workspace,
                task="Read TARGET.txt and return its exact contents.",
                model="glm-5",
                provider="zai",
                effort="high",
                condition="optimized",
                parent_run_id="parent-current",
                run_id="current-run",
                timeout=30,
                confirmed=True,
            )
        self.assertEqual(trial["execution_path"], "hermes_current_session")
        self.assertEqual(trial["observation"], {"status": "completed", "tools": 3, "tokens": 41, "cost_usd": 0.25})

    def test_current_session_trial_retains_a_redacted_failure_receipt(self) -> None:
        module = _load_omh_live()
        with TemporaryDirectory() as root_text:
            root = Path(root_text)
            omh = root / "omh.py"
            hermes = root / "hermes.py"
            workspace = root / "workspace"
            workspace.mkdir()
            omh.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
                "from omh.cli import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            hermes.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stderr.write('authentication failed: sk-secret-value-123456789\\n')\n"
                "raise SystemExit(23)\n",
                encoding="utf-8",
            )
            executable = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            omh.chmod(omh.stat().st_mode | executable)
            hermes.chmod(hermes.stat().st_mode | executable)
            trial = module.run_current_session_trial(
                omh_executable=str(omh),
                hermes_executable=str(hermes),
                workspace=workspace,
                task="Read TARGET.txt and return its exact contents.",
                model="glm-5",
                provider="zai",
                effort="high",
                condition="baseline",
                parent_run_id="parent-current",
                run_id="current-run",
                timeout=30,
                confirmed=True,
            )
        self.assertEqual(trial["observation"], {"status": "failed", "tools": None, "tokens": None, "cost_usd": None})
        self.assertEqual(trial["failure_receipt"], {"classification": "authentication_failed", "kind": "process_exit", "exit_code": 23})
        self.assertNotIn("secret", json.dumps(trial))

    def test_bench_passes_current_session_provider_override_to_current_session(self) -> None:
        path = BASE / "bench.py"
        spec = importlib.util.spec_from_file_location("benchmark_cli", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        captured: dict[str, object] = {}

        def fake_execute(*args: object, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"ok": True}

        with patch.object(module, "execute_one", side_effect=fake_execute):
            self.assertEqual(module.main([
                "smoke", "--harness", "hermes_current_session", "--model", "moonshotai/kimi-k3-ultrafast",
                "--current-session-provider", "kimi_k3", "--allow-paid-live", "--max-paid-calls", "1",
            ]), 0)
        self.assertEqual(captured["current_session_provider"], "kimi_k3")

    def test_bench_rejects_current_session_provider_override_for_isolated_omh(self) -> None:
        path = BASE / "bench.py"
        spec = importlib.util.spec_from_file_location("benchmark_cli", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaisesRegex(SystemExit, "2"):
            module.main([
                "smoke", "--harness", "omh", "--current-session-provider", "kimi_k3",
                "--allow-paid-live", "--max-paid-calls", "1",
            ])

    def test_baseline_and_optimized_run_through_real_omh_observation_surface(self) -> None:
        module = _load_omh_live()
        with TemporaryDirectory() as root_text:
            root = Path(root_text)
            omh = root / "omh.py"
            hermes = root / "hermes.py"
            capture = root / "capture"
            capture.mkdir()
            omh.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
                "from omh.cli import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            # Mirrors the installed Hermes CLI: only `chat --query-file -`
            # reads stdin (#1824). This fake writes no state.db into its
            # disposable home, so the child has no usage ledger to read and
            # reports no tokens or cost (#1831).
            hermes.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "from pathlib import Path\n"
                "args=sys.argv[1:]\n"
                "if args[:3] != ['chat', '--query-file', '-']:\n"
                "    raise SystemExit('no Hermes stdin transport in argv')\n"
                "prompt=sys.stdin.read()\n"
                "capture=Path(sys.argv[0]).resolve().parent/'capture'\n"
                "index=len(list(capture.glob('prompt-*.txt')))\n"
                "(capture/f'prompt-{index}.txt').write_text(prompt,encoding='utf-8')\n"
                "print('done')\n",
                encoding="utf-8",
            )
            executable = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            omh.chmod(omh.stat().st_mode | executable)
            hermes.chmod(hermes.stat().st_mode | executable)
            trials = []
            for condition in ("baseline", "optimized"):
                workspace = root / condition
                workspace.mkdir()
                trials.append(
                    module.run_trial(
                        omh_executable=str(omh),
                        hermes_executable=str(hermes),
                        workspace=workspace,
                        omh_home=root / f"omh-{condition}",
                        task="Read TARGET.txt and return its exact contents.",
                        model="qwen3-coder-next",
                        provider="qwen",
                        effort="high",
                        condition=condition,
                        parent_run_id=f"parent-{condition}",
                            run_id=f"child-{condition}",
                            timeout=30,
                            confirmed=True,
                        )
                )
            self.assertEqual(trials[0]["task_digest"], trials[1]["task_digest"])
            self.assertEqual(trials[1]["route"]["model_family"], "qwen")
            self.assertEqual(trials[1]["observation"]["status"], "completed")
            # Absent, never zero: a home without a usage ledger reads as none.
            self.assertEqual(
                (trials[1]["observation"]["tools"], trials[1]["observation"]["tokens"], trials[1]["observation"]["cost_usd"]),
                (None, None, None),
            )
            baseline = (capture / "prompt-0.txt").read_text(encoding="utf-8")
            optimized = (capture / "prompt-1.txt").read_text(encoding="utf-8")
            self.assertNotIn(HIGH_EFFORT_CALIBRATIONS["qwen"], baseline)
            self.assertIn(HIGH_EFFORT_CALIBRATIONS["qwen"], optimized)


if __name__ == "__main__":
    unittest.main()
