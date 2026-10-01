from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli
from omh.coding.hermes_model_config import (
    HermesModelConfigInspection,
    HermesModelConfigReceipt,
    ProviderPresence,
)
from omh.commands import setup as setup_commands
from omh.commands.main import build_parser


def _inspection(
    config_path: Path,
    *,
    aliases: dict[str, str] | None = None,
) -> HermesModelConfigInspection:
    return HermesModelConfigInspection(
        hermes="hermes",
        config_path=config_path,
        config_digest="before-digest",
        config_check_ok=True,
        model_aliases={},
        model_dot_aliases=aliases or {},
        providers=(
            ProviderPresence(
                provider_id="openai",
                auth_present=True,
                auth_status_ok=True,
                plugin_present=False,
            ),
            ProviderPresence(
                provider_id="google",
                auth_present=True,
                auth_status_ok=True,
                plugin_present=False,
            ),
            ProviderPresence(
                provider_id="openrouter",
                auth_present=True,
                auth_status_ok=True,
                plugin_present=False,
            ),
        ),
        commands=(),
    )


def _discovery(
    observations: list[dict[str, str]],
    *,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": "model_discovery/v1",
        "sources": {
            "codex": {
                "status": "truncated" if truncated else "observed_before",
                "truncated_reasons": ["record_count"] if truncated else [],
            }
        },
        "observations": observations,
        "observation_count": len(observations),
        "claim_boundary": "metadata only",
    }


class ModelSetupFlowTests(unittest.TestCase):
    def test_setup_parser_exposes_explicit_model_activation_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "setup",
                "--model-setup",
                "--confirm-model",
                "openai/gpt-5.6-sol",
                "--model-alias",
                "main=openai/gpt-5.6-sol",
                "--apply-model-config",
                "--model-config-digest",
                "digest",
                "--allow-model-alias-collision",
            ]
        )

        self.assertTrue(args.model_setup)
        self.assertEqual(args.confirm_model, ["openai/gpt-5.6-sol"])
        self.assertEqual(args.model_alias, ["main=openai/gpt-5.6-sol"])
        self.assertTrue(args.apply_model_config)
        self.assertEqual(args.model_config_digest, "digest")
        self.assertTrue(args.allow_model_alias_collision)

    def test_observed_before_is_not_confirmation_and_explicit_unavailable_freezes_choice(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            observations = [
                {
                    "source": "codex",
                    "provider": "openai",
                    "model_id": "gpt-5.6-sol",
                    "variant": "",
                    "timestamp": "",
                    "status": "observed_before",
                }
            ]
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery(observations),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=_inspection(config_path),
            ):
                status, stdout, stderr = run_cli(
                    base
                    + [
                        "setup",
                        "--model-setup",
                        "--model-alias",
                        "main=openai/gpt-5.6-sol",
                        "--json",
                    ],
                    output_json=False,
                )

        self.assertEqual((status, stderr), (0, ""))
        activation = json.loads(stdout)["steps"]["model_activation"]
        self.assertEqual(activation["status"], "choice_required")
        self.assertEqual(activation["candidates"][0]["status"], "observed_before")
        self.assertEqual(activation["recommendations"]["main"]["status"], "choice_required")
        self.assertIsNone(activation["preview"])
        self.assertEqual(activation["next_action"], "confirm_active_model")

    def test_no_confirmed_candidate_completes_with_owner_defaults_and_no_write(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            before = config_path.read_bytes()

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery([]),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=_inspection(config_path),
            ), patch.object(
                setup_commands,
                "apply_hermes_model_config",
            ) as apply_config:
                status, stdout, stderr = run_cli(
                    [
                        "--omh-home",
                        str(root / ".omh"),
                        "--hermes-home",
                        str(root / ".hermes"),
                        "setup",
                        "--dry-run",
                        "--skip-apply",
                        "--no-menubar",
                        "--model-setup",
                        "--no-interactive",
                        "--json",
                    ],
                    output_json=False,
                )

            activation = json.loads(stdout)["steps"]["model_activation"]
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(activation["status"], "defaulted")
            self.assertEqual(activation["next_action"], "model_setup_complete")
            self.assertEqual(
                activation["recommendations"]["main"]["status"],
                "owner_default",
            )
            self.assertTrue(
                all(
                    recommendation["status"] == "owner_default"
                    for recommendation in activation["recommendations"][
                        "hermes_native"
                    ]["categories"].values()
                )
            )
            self.assertTrue(
                all(
                    recommendation["status"] == "owner_default"
                    for recommendation in activation["recommendations"][
                        "maestro"
                    ]["categories"].values()
                )
            )
            self.assertIsNone(activation["preview"])
            self.assertEqual(activation["apply"]["status"], "not_requested")
            self.assertEqual(config_path.read_bytes(), before)
            apply_config.assert_not_called()

    def test_unmatched_confirmed_model_still_completes_with_owner_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            before = config_path.read_bytes()
            qwen = {
                "source": "hermes",
                "provider": "qwen-oauth",
                "model_id": "qwen3-coder",
                "variant": "",
                "timestamp": "",
                "status": "observed_before",
            }

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery([qwen]),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=_inspection(config_path),
            ), patch.object(
                setup_commands,
                "apply_hermes_model_config",
            ) as apply_config:
                status, stdout, stderr = run_cli(
                    [
                        "--omh-home",
                        str(root / ".omh"),
                        "--hermes-home",
                        str(root / ".hermes"),
                        "setup",
                        "--dry-run",
                        "--skip-apply",
                        "--no-menubar",
                        "--model-setup",
                        "--no-interactive",
                        "--confirm-model",
                        "qwen-oauth/qwen3-coder",
                        "--json",
                    ],
                    output_json=False,
                )

            activation = json.loads(stdout)["steps"]["model_activation"]
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(activation["status"], "defaulted")
            self.assertEqual(activation["next_action"], "model_setup_complete")
            self.assertEqual(activation["recommendations"]["main"]["status"], "owner_default")
            self.assertIsNone(activation["preview"])
            self.assertEqual(activation["apply"]["status"], "not_requested")
            self.assertEqual(config_path.read_bytes(), before)
            apply_config.assert_not_called()

    def test_confirmed_qwen_and_gemini_only_can_preview_without_writing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            observations = [
                {
                    "source": "codex",
                    "provider": provider,
                    "model_id": model,
                    "variant": "",
                    "timestamp": "",
                    "status": "observed_before",
                }
                for provider, model in (
                    ("openrouter", "qwen3-coder"),
                    ("google", "gemini-3.1-pro"),
                )
            ]
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery(observations),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=_inspection(config_path),
            ), patch.object(setup_commands, "apply_hermes_model_config") as apply_config:
                status, stdout, stderr = run_cli(
                    base
                    + [
                        "setup",
                        "--model-setup",
                        "--confirm-model",
                        "openrouter/qwen3-coder",
                        "--confirm-model",
                        "google/gemini-3.1-pro",
                        "--model-alias",
                        "main=openrouter/qwen3-coder",
                        "--json",
                    ],
                    output_json=False,
                )

            self.assertEqual((status, stderr), (0, ""))
            activation = json.loads(stdout)["steps"]["model_activation"]
            self.assertEqual(activation["status"], "preview_ready")
            self.assertEqual(
                {candidate["status"] for candidate in activation["candidates"]},
                {"confirmed_active"},
            )
            self.assertEqual(
                activation["preview"]["changes"],
                {"main": "openrouter/qwen3-coder"},
            )
            self.assertEqual(activation["apply"]["status"], "declined")
            self.assertEqual(activation["verification"]["status"], "not_run")
            self.assertNotIn("model:\n  aliases:", config_path.read_text(encoding="utf-8"))
            apply_config.assert_not_called()

    def test_rich_models_project_every_category_for_hermes_and_maestro(self) -> None:
        observations = [
            {
                "source": source,
                "provider": provider,
                "model_id": model,
                "variant": "",
                "timestamp": "",
                "status": "observed_before",
            }
            for provider, model, source in (
                ("openai", "gpt-6.1-sol", "codex"),
                ("openai", "gpt-5.6-terra", "codex"),
                ("apitopia", "kimi-k3", "omo"),
                ("anthropic", "claude-fable-5", "claude-code"),
                ("sglang", "glm-5.2", "omo"),
            )
        ]
        providers = tuple(
            ProviderPresence(provider, True, True, False)
            for provider in ("anthropic", "apitopia", "openai", "sglang")
        )
        inspection = HermesModelConfigInspection(
            hermes="hermes",
            config_path=Path("/tmp/config.yaml"),
            config_digest="before-digest",
            config_check_ok=True,
            model_aliases={},
            model_dot_aliases={},
            providers=providers,
            commands=(),
        )
        confirmed = [
            f"{observation['provider']}/{observation['model_id']}"
            for observation in observations
        ]
        with patch.object(
            setup_commands, "discover_local_models", return_value=_discovery(observations)
        ), patch.object(
            setup_commands, "inspect_hermes_model_config", return_value=inspection
        ):
            status, stdout, stderr = run_cli(
                [
                    "--omh-home", "/tmp/.omh", "--hermes-home", "/tmp/.hermes",
                    "setup", "--dry-run", "--skip-apply", "--no-menubar", "--model-setup",
                    *[item for model in confirmed for item in ("--confirm-model", model)],
                    "--json",
                ],
                output_json=False,
            )

        self.assertEqual((status, stderr), (0, ""))
        recommendations = json.loads(stdout)["steps"]["model_activation"]["recommendations"]
        expected = {
            "ultrabrain", "deep", "architect", "unspecified-high", "unspecified-low",
            "quick", "writing", "visual-engineering", "artistry",
            "capable", "simple-work", "deep-work",
        }
        self.assertEqual(set(recommendations["hermes_native"]["categories"]), expected)
        self.assertEqual(set(recommendations["maestro"]["categories"]), expected)
        self.assertEqual(
            recommendations["hermes_native"]["categories"]["ultrabrain"]["projection"]["kind"],
            "hermes_native_binding",
        )
        self.assertEqual(
            recommendations["maestro"]["categories"]["ultrabrain"]["projection"]["kind"],
            "maestro_ordered_chain",
        )

    def test_omo_import_and_qwen_gemini_next_actions_are_explicit(self) -> None:
        observations = [
            {
                "source": "omo",
                "provider": provider,
                "model_id": model,
                "variant": "",
                "timestamp": "",
                "status": "observed_before",
            }
            for provider, model in (("qwen", "qwen3-coder"), ("google", "gemini-3.1-pro"))
        ]
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".omo").mkdir()
            (home / ".omo" / "omo.json").write_text(
                json.dumps({"categories": {"deep": {
                    "model": "qwen/qwen3-coder",
                    "variant": "high",
                    "fallback_models": [{"model": "google/gemini-3.1-pro"}],
                }}}),
                encoding="utf-8",
            )
            inspection = HermesModelConfigInspection(
                hermes="hermes",
                config_path=home / "config.yaml",
                config_digest="before-digest",
                config_check_ok=True,
                model_aliases={},
                model_dot_aliases={},
                providers=(ProviderPresence("qwen", True, True, False),),
                commands=(),
            )
            with patch("omh.commands.model_setup_flow.Path.home", return_value=home), patch.object(
                setup_commands, "discover_local_models", return_value=_discovery(observations)
            ), patch.object(
                setup_commands, "inspect_hermes_model_config", return_value=inspection
            ):
                status, stdout, stderr = run_cli(
                    [
                        "--omh-home", str(home / ".omh"),
                        "--hermes-home", str(home / ".hermes"),
                        "setup", "--dry-run", "--skip-apply", "--no-menubar",
                        "--model-setup", "--import-omo-category-overrides",
                        "--confirm-model", "qwen/qwen3-coder",
                        "--confirm-model", "google/gemini-3.1-pro", "--json",
                    ],
                    output_json=False,
                )

        self.assertEqual((status, stderr), (0, ""))
        activation = json.loads(stdout)["steps"]["model_activation"]
        recommendations = activation["recommendations"]
        self.assertEqual(recommendations["omo_category_overrides"]["status"], "imported")
        self.assertEqual(
            recommendations["hermes_native"]["categories"]["deep"]["selected"]["model_alias"],
            "qwen3-coder",
        )
        self.assertEqual(
            activation["provider_next_actions"],
            [
                {"provider": "google", "status": "auth_missing",
                 "next_action": "hermes auth login google"},
                {"provider": "qwen", "status": "ready", "next_action": ""},
            ],
        )
        serialized = json.dumps(recommendations).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("session", serialized)

    def test_model_activation_flags_require_explicit_model_setup(self) -> None:
        status, _stdout, stderr = run_cli(
            ["setup", "--confirm-model", "openai/gpt-5.6-sol"],
            output_json=False,
        )

        self.assertEqual(status, 2)
        self.assertIn("require --model-setup", stderr)

    def test_alias_collision_requires_explicit_collision_choice(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
            command = [
                "setup",
                "--model-setup",
                "--confirm-model",
                "openai/gpt-5.6-sol",
                "--model-alias",
                "main=openai/gpt-5.6-sol",
                "--json",
            ]

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery([]),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=_inspection(config_path, aliases={"main": "anthropic/claude-opus-5"}),
            ):
                status, _stdout, stderr = run_cli(base + command, output_json=False)
                allowed_status, allowed_stdout, allowed_stderr = run_cli(
                    base + command[:-1] + ["--allow-model-alias-collision", "--json"],
                    output_json=False,
                )

        self.assertEqual(status, 2)
        self.assertIn("collision refused", stderr)
        self.assertEqual((allowed_status, allowed_stderr), (0, ""))
        preview = json.loads(allowed_stdout)["steps"]["model_activation"]["preview"]
        self.assertEqual(preview["changes"]["main"], "openai/gpt-5.6-sol")

    def test_apply_requires_digest_then_reports_adapter_verification(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            inspection = _inspection(config_path)
            receipt = HermesModelConfigReceipt(
                verified=True,
                before_digest="before-digest",
                after_digest="after-digest",
                commands=(("hermes", "config", "set", "model.aliases.main", "openai/gpt-5.6-sol"),),
                inspection=inspection,
            )
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
            command = [
                "setup",
                "--model-setup",
                "--confirm-model",
                "openai/gpt-5.6-sol",
                "--model-alias",
                "main=openai/gpt-5.6-sol",
                "--apply-model-config",
                "--json",
            ]

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery([]),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=inspection,
            ), patch.object(
                setup_commands,
                "apply_hermes_model_config",
                return_value=receipt,
            ) as apply_config:
                missing_status, _stdout, missing_stderr = run_cli(
                    base + command,
                    output_json=False,
                )
                status, stdout, stderr = run_cli(
                    base + command[:-1] + ["--model-config-digest", "before-digest", "--json"],
                    output_json=False,
                )

        self.assertEqual(missing_status, 2)
        self.assertIn("--model-config-digest", missing_stderr)
        self.assertEqual((status, stderr), (0, ""))
        activation = json.loads(stdout)["steps"]["model_activation"]
        self.assertEqual(activation["status"], "verified")
        self.assertEqual(activation["apply"]["status"], "applied")
        self.assertEqual(activation["verification"]["status"], "verified")
        apply_config.assert_called_once()

    def test_human_setup_output_shows_scan_truncation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery([], truncated=True),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=_inspection(config_path),
            ):
                status, stdout, stderr = run_cli(
                    base + ["setup", "--model-setup"],
                    output_json=False,
                )

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("Scanning local model metadata", stdout)
        self.assertIn("truncated: record_count", stdout)

    def test_interactive_shows_preview_before_apply_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".hermes" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("{}\n", encoding="utf-8")
            base = ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]
            events: list[str] = []

            with patch.object(
                setup_commands,
                "discover_local_models",
                return_value=_discovery([]),
            ), patch.object(
                setup_commands,
                "inspect_hermes_model_config",
                return_value=_inspection(config_path),
            ), patch.object(
                setup_commands,
                "_ask_yes_no",
                side_effect=lambda *args, **kwargs: events.append("confirm") or False,
            ), patch.object(
                setup_commands,
                "_print_model_preview_review",
                side_effect=lambda *args, **kwargs: events.append("preview"),
            ):
                status, _stdout, stderr = run_cli(
                    base
                    + [
                        "setup",
                        "--model-setup",
                        "--confirm-model",
                        "openai/gpt-5.6-sol",
                        "--model-alias",
                        "main=openai/gpt-5.6-sol",
                        "--interactive",
                        "--json",
                    ],
                    output_json=False,
                )

        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(events, ["preview", "confirm"])


if __name__ == "__main__":
    unittest.main()
