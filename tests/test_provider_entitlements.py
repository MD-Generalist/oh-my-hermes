"""Provider entitlements: the setup interview's answer and how it reshapes chains.

Chains stay provider-neutral alias lists; the entitlement document is the one
place a machine's own accounts are described. Shaping reorders, never drops.
"""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.coding.model_recommendations import SHIPPED_MODEL_RECOMMENDATIONS  # noqa: E402
from omh.commands.common import _wants_json  # noqa: E402
from omh.commands.language import LANGUAGE_CODES, MESSAGES, tr  # noqa: E402
from omh.coding.model_routing import CLAUDE_FRONTIER_CHAIN_MODELS  # noqa: E402
from omh.commands import setup as setup_module  # noqa: E402
from omh.config_adapter import configured_provider_ids  # noqa: E402
from omh.plugin_bundle.omh.provider_detection import env_key_names  # noqa: E402
from omh.plugin_bundle.omh.hermes_delegation import (  # noqa: E402
    HERMES_MIXTURE_ALIAS_PROVIDER_FAMILIES,
    HERMES_MIXTURE_CATEGORY_CHAINS,
    PROVIDER_ENTITLEMENTS_SCHEMA_VERSION,
    PROVIDER_FAMILY_VOCABULARY,
    alias_is_served,
    effective_mixture_category_chains,
    effective_provider_entitlements,
    entitlement_shaped_chain,
    load_provider_entitlements,
    parse_provider_entitlements,
    provider_entitlements_path,
)


def _entitlements(providers: dict[str, str], clis: list[str] | None = None, excluded: list[str] | None = None) -> dict[str, object]:
    return {"providers": providers, "subscription_clis": list(clis or []), "excluded_providers": list(excluded or [])}


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


class ParityTests(unittest.TestCase):
    """The plugin bundle cannot import src/coding, so it embeds two mirrors."""

    # Aliases the router recognizes but no shipped chain recommends. Claude
    # Mythos 5.1 is Claude Fable 5.1 served only to Project Glasswing-approved
    # accounts, so it left the chains (owner decision, 2026-09-06) while
    # staying routable for a user who names it — and that user is exactly who
    # needs "which of my providers serves this" answered rather than unknown.
    # The exception is a named list, not a loosened comparison: an alias may
    # sit here only while it is genuinely absent from every shipped chain, so
    # a chain entry that silently disappears still fails this gate.
    # The retired generations (owner decision, 2026-09-11) sit here for the
    # same reason: a machine-level chain override may still name them.
    _RECOGNITION_ONLY_ALIAS_FAMILIES: dict[str, tuple[str, ...]] = {
        "claude-mythos-5-1": ("ccapi", "anthropic", "openrouter"),
        # The versioned spelling of the shipped `deepseek-flash` pointer;
        # gateways serve it under this id.
        "deepseek-v4.1-flash": ("deepseek", "openrouter", "opencode"),
        "claude-fable-5": ("ccapi", "anthropic", "openrouter"),
        "deepseek-v3.2": ("deepseek", "openrouter", "opencode"),
        "glm-5.2": ("zai", "openrouter", "opencode"),
        "glm-5.2-ultrafast": ("zai", "openrouter", "opencode"),
        # Retired 2026-09-23 when Opus 5.5 and GPT-6 Luna took their slots.
        "claude-opus-5": ("ccapi", "anthropic", "openrouter"),
        "gpt-5.6-luna": ("openai-codex", "openai"),
        # Retired 2026-09-23 when GPT-6 Sol took both GPT-5.6 tiers' slots.
        "gpt-5.6-sol": ("openai-codex", "openai"),
        "gpt-5.6-terra": ("openai-codex", "openai"),
        # Retired 2026-10-01: xAI serves the alias as Grok Build 0.1.
        "grok-code-fast": ("xai", "openrouter"),
        # Retired 2026-10-01 when GPT-6.1 Sol took every GPT-6 Sol slot.
        "gpt-6-sol": ("openai-codex", "openai"),
    }

    def _catalog_families(self) -> dict[str, tuple[str, ...]]:
        families: dict[str, tuple[str, ...]] = {}
        for section in ("categories", "role_suggestions", "domain_affinities", "last_resort"):
            for chain in SHIPPED_MODEL_RECOMMENDATIONS[section].values():
                for candidate in chain:
                    alias = str(candidate["model_alias"])
                    listed = tuple(candidate["preferred_provider_families"])
                    self.assertEqual(families.get(alias, listed), listed, alias)
                    families[alias] = listed
        return families

    def test_alias_families_mirror_the_catalog(self) -> None:
        catalog = self._catalog_families()
        for alias in self._RECOGNITION_ONLY_ALIAS_FAMILIES:
            self.assertNotIn(alias, catalog, alias)
        self.assertEqual(
            HERMES_MIXTURE_ALIAS_PROVIDER_FAMILIES,
            {**catalog, **self._RECOGNITION_ONLY_ALIAS_FAMILIES},
        )

    def test_family_vocabulary_is_the_catalog_union(self) -> None:
        union = sorted({family for families in self._catalog_families().values() for family in families})
        self.assertEqual(list(PROVIDER_FAMILY_VOCABULARY), union)
        # A recognition-only alias may only name families the catalog already
        # describes; it never widens the vocabulary the setup interview knows.
        for alias, families in self._RECOGNITION_ONLY_ALIAS_FAMILIES.items():
            self.assertEqual(sorted(set(families) - set(union)), [], alias)


class ParseTests(unittest.TestCase):
    def test_valid_document(self) -> None:
        parsed, status = parse_provider_entitlements(
            {
                "schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION,
                "providers": {"og": "gateway", "zai": "zai"},
                "subscription_clis": ["claude-code", "claude-code"],
            }
        )
        self.assertEqual(status, "applied")
        self.assertEqual(parsed, _entitlements({"og": "gateway", "zai": "zai"}, ["claude-code"]))
        # The optional exclusion list: linked ids the operator cleared.
        parsed, status = parse_provider_entitlements(
            {
                "schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION,
                "providers": {"zai": "zai"},
                "excluded_providers": ["anthropic", "anthropic"],
            }
        )
        self.assertEqual(status, "applied")
        self.assertEqual(parsed, _entitlements({"zai": "zai"}, [], ["anthropic"]))

    def test_invalid_documents_yield_none(self) -> None:
        cases = [
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "excluded_providers": "zai"}, "must be a list"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "excluded_providers": ["bad id"]}, "plain identifier"),
            (
                {"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"zai": "zai"}, "excluded_providers": ["zai"]},
                "both recorded and excluded",
            ),
            ([], "must be a JSON object"),
            ({"schema_version": "nope"}, "schema_version"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "extra": 1}, "unsupported fields"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": []}, "providers must be"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"bad id": "zai"}}, "plain identifier"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"og": "warp"}}, "kind must be one of"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "subscription_clis": "claude-code"}, "must be a list"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "subscription_clis": ["cursor"]}, "must be one of"),
            # A Codex login is a Hermes provider (openai-codex), not a Maestro-only subscription.
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "subscription_clis": ["codex"]}, "must be one of"),
        ]
        for raw, fragment in cases:
            with self.subTest(raw=raw):
                parsed, status = parse_provider_entitlements(raw)
                self.assertIsNone(parsed)
                self.assertIn(fragment, status)

    def test_loader_reports_absent_and_unreadable(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(load_provider_entitlements(tmp), (None, "absent"))
            provider_entitlements_path(tmp).parent.mkdir(parents=True)
            provider_entitlements_path(tmp).write_text("{", encoding="utf-8")
            self.assertEqual(load_provider_entitlements(tmp), (None, "invalid: unreadable JSON"))


class ServingRuleTests(unittest.TestCase):
    def test_vendor_provider_serves_only_its_families(self) -> None:
        zai = _entitlements({"zai": "zai"})
        self.assertTrue(alias_is_served("glm-5.3", zai))
        self.assertFalse(alias_is_served("claude-fable-5-1", zai))
        self.assertFalse(alias_is_served("kimi-k3", zai))

    def test_gateway_and_unknown_serve_everything(self) -> None:
        for kind in ("gateway", "unknown", "openrouter", "opencode"):
            with self.subTest(kind=kind):
                self.assertTrue(alias_is_served("claude-fable-5-1", _entitlements({"p": kind})))

    def test_fail_open_without_providers_or_catalog_knowledge(self) -> None:
        self.assertTrue(alias_is_served("claude-fable-5-1", _entitlements({})))
        self.assertTrue(alias_is_served("some-new-model", _entitlements({"zai": "zai"})))

    def test_explicit_route_decides_before_families(self) -> None:
        routes = {"claude-fable-5": ("og", "anthropic/claude-fable-5")}
        # Routed to a confirmed provider: served even for a vendor-only account.
        self.assertTrue(alias_is_served("claude-fable-5", _entitlements({"og": "zai"}), routes))
        # Routed to a provider the operator did not confirm: unserved even
        # though a gateway is present.
        self.assertFalse(alias_is_served("claude-fable-5", _entitlements({"other": "gateway"}), routes))

    def test_shaping_is_a_stable_partition(self) -> None:
        # A deepseek-only machine serves the second entry of
        # `unspecified-low`; shaping moves it to the front and keeps the
        # unserved entries in shipped order behind it — a no-op would fail.
        deepseek = _entitlements({"deepseek": "deepseek"})
        chain = HERMES_MIXTURE_CATEGORY_CHAINS["unspecified-low"]
        self.assertEqual(chain[1][0], "deepseek-flash")
        shaped = entitlement_shaped_chain(chain, deepseek)
        aliases = [alias for alias, _ in shaped]
        self.assertEqual(aliases[:1], ["deepseek-flash"])
        self.assertEqual(sorted(aliases), sorted(alias for alias, _ in chain))
        unserved = [alias for alias, _ in chain if alias != "deepseek-flash"]
        self.assertEqual(aliases[1:], unserved)

    def test_effective_chains_apply_entitlements_after_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            _write(
                Path(tmp) / "routing" / "model-chains.json",
                {
                    "schema_version": "mixture_chain_overrides/v1",
                    "categories": {"quick": [{"model": "claude-opus-5", "reasoning_effort": "low"}, {"model": "glm-5.3", "reasoning_effort": "low"}]},
                },
            )
            _write(
                provider_entitlements_path(tmp),
                {"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"zai": "zai"}, "subscription_clis": []},
            )
            chains = effective_mixture_category_chains(tmp, Path(tmp) / "hermes")
            self.assertEqual(chains["quick"], (("glm-5.3", "low"), ("claude-opus-5", "low")))
            # An untouched category is shaped from the shipped default.
            self.assertEqual(chains["unspecified-low"][0][0], "glm-5.3")

    def test_absent_document_leaves_chains_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(effective_mixture_category_chains(tmp, Path(tmp) / "hermes"), dict(HERMES_MIXTURE_CATEGORY_CHAINS))


class ChainSurfaceConsistencyTests(unittest.TestCase):
    """Every chain reader sees the same head: show, the route tool, the HUD projection."""

    def _home(self, tmp: str) -> Path:
        home = Path(tmp) / ".omh"
        _write(
            provider_entitlements_path(home),
            {"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"openai-codex": "openai-codex"}, "subscription_clis": []},
        )
        return home

    def test_model_chains_show_marks_reordered_chains_in_text_and_json(self) -> None:
        import io
        from contextlib import redirect_stdout

        from omh.commands.model_chains import _print_state, _state

        with TemporaryDirectory() as tmp:
            home = self._home(tmp)
            # A Hermes home with nothing linked, so the record alone decides.
            state = _state(home, Path(tmp) / ".hermes")
            self.assertEqual(state["entitlements_status"], "applied")
            self.assertEqual(state["entitlements_path"], str(provider_entitlements_path(home)))
            architect = next(row for row in state["categories"] if row["category"] == "architect")
            self.assertTrue(architect["entitlement_shaped"])
            # The one GPT entry is served by the confirmed openai-codex
            # provider and leads; the Claude and Kimi entries follow as
            # unserved in shipped order.
            self.assertEqual(architect["chain"][0]["model"], "gpt-6-astra")
            self.assertEqual(architect["chain"][1]["model"], "claude-fable-5-1")
            self.assertTrue(architect["chain"][0]["served"])
            self.assertFalse(architect["chain"][1]["served"])
            self.assertFalse(architect["chain"][2]["served"])
            out = io.StringIO()
            with redirect_stdout(out):
                _print_state(state)
            self.assertIn("architect: gpt-6-astra:xhigh, claude-fable-5-1:xhigh, kimi-k3:xhigh", out.getvalue())
            self.assertIn("(reordered by this machine's providers)", out.getvalue())
            self.assertIn("Linked Hermes providers: none found", out.getvalue())
            self.assertIn(f"Provider entitlements: {provider_entitlements_path(home)} [applied]", out.getvalue())

    def test_route_tool_and_show_agree_on_the_head_and_the_fallback_walk(self) -> None:
        from omh.plugin_bundle.omh.tools.delegate_route_tool import omh_delegate_route_handler

        with TemporaryDirectory() as tmp:
            home = self._home(tmp)
            hermes_home = Path(tmp) / ".hermes"
            hermes_home.mkdir()
            (hermes_home / "config.yaml").write_text("model:\n  provider: openai-codex\n", encoding="utf-8")
            common = {"omh_home": str(home), "hermes_home": str(hermes_home)}
            routed = json.loads(omh_delegate_route_handler({"action": "set", "category": "architect", **common}))
            self.assertEqual(routed["status"], "routed", routed)
            self.assertEqual(routed["applied"]["alias"], "gpt-6-astra")
            fallback = json.loads(omh_delegate_route_handler({"action": "fallback", "category": "architect", **common}))
            self.assertEqual(fallback["applied"]["alias"], "claude-fable-5-1")
            fallback = json.loads(omh_delegate_route_handler({"action": "fallback", "category": "architect", **common}))
            self.assertEqual(fallback["applied"]["alias"], "kimi-k3")
            status = json.loads(omh_delegate_route_handler({"action": "status", **common}))
            self.assertEqual(status["categories"]["architect"][0]["alias"], "gpt-6-astra")


class ConfigReaderTests(unittest.TestCase):
    def test_reads_provider_keys_and_default_provider(self) -> None:
        config = (
            "model:\n"
            "  provider: openai-codex\n"
            "  default: gpt-5.6-sol\n"
            "providers:\n"
            "  og:\n"
            "    base_url: https://example.invalid/v1\n"
            "  # commented: out\n"
            "  zai:\n"
            "    api_mode: chat_completions\n"
            "plugins:\n"
            "  enabled: [omh]\n"
        )
        self.assertEqual(configured_provider_ids(config), ["openai-codex", "og", "zai"])

    def test_empty_and_absent_sections(self) -> None:
        self.assertEqual(configured_provider_ids(""), [])
        self.assertEqual(configured_provider_ids("providers:\n  og:\n    base_url: x\nmodel:\n  provider: og\n"), ["og"])


class FreeTextPromptTests(unittest.TestCase):
    """`_ask`, the primitive under every free-text setup prompt.

    The arrow keys that drive the menu right above it keep being pressed into
    the free-text prompt that follows, and `input()` returns their escape
    bytes as if they had been typed. Stripping them is `_ask`'s job because
    every call site would otherwise have to remember to do it, and because a
    caller that echoes the answer back would write the control bytes into the
    terminal.
    """

    def _ask(self, typed: str) -> str:
        with patch.object(setup_module.sys, "stdin", io.StringIO(typed)), redirect_stdout(io.StringIO()):
            return setup_module._ask("Add another provider by name", default="", use_color=False)

    def test_a_line_of_arrow_keys_reads_as_enter(self) -> None:
        # The observed line: down, up, down, then a truncated sequence the
        # person's Enter cut short. Nothing here was typed, so nothing is
        # answered -- and "" is what every call site already treats as Enter.
        self.assertEqual(self._ask("\x1b[B\x1b[A\x1b[B\x1b[\n"), "")

    def test_ordinary_input_still_answers_and_is_still_trimmed(self) -> None:
        self.assertEqual(self._ask("  gpt5  \n"), "gpt5")
        self.assertEqual(self._ask("work-relay\n"), "work-relay")

    def test_every_control_form_is_removed_not_only_the_escape_byte(self) -> None:
        # A CSI stripped as a bare ESC would leave `[B` behind as text, which
        # reads as a typed answer rather than as nothing.
        self.assertEqual(self._ask("\x1b[1;5Dog\x1b[3~\n"), "og")
        # SS3, which is what the arrows send in application-cursor mode.
        self.assertEqual(self._ask("\x1bOA\x1bOBog\n"), "og")
        # A sequence the submitting Enter cut short. Its ESC alone is not
        # enough: `[` left behind reads as a typed answer.
        self.assertEqual(self._ask("og\x1b[\n"), "og")
        self.assertEqual(self._ask("og\x1b[1;\n"), "og")
        self.assertEqual(self._ask("og\x1bO\n"), "og")
        # A bare ESC, a stray C0, DEL, and a C1 byte.
        self.assertEqual(self._ask("\x1b\x07o\x7fg\x9b\n"), "og")
        # Nothing a person can legitimately type is touched.
        self.assertEqual(self._ask("한국어-provider.v2/x\n"), "한국어-provider.v2/x")

    def test_answer_carries_no_control_byte_for_a_caller_to_echo(self) -> None:
        answer = self._ask("\x1b[Bmy\x1bOAprovider\x1b[A\n")
        self.assertEqual(answer, "myprovider")
        self.assertFalse(any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in answer))

    def test_end_of_input_is_still_an_empty_answer(self) -> None:
        self.assertEqual(self._ask(""), "")


class MultiChoicePromptTests(unittest.TestCase):
    """The multi-select primitive: same shape as `_ask_single_choice`, many answers."""

    rendered = ""
    defaults: list[str] = []
    OPTIONS = [
        {"choice": "1", "value": "og", "label": "og", "description": "found in your Hermes config"},
        {"choice": "2", "value": "zai", "label": "Z.ai (GLM)", "description": ""},
        {"choice": "3", "value": "openrouter", "label": "OpenRouter", "description": "relays every model family"},
    ]

    def _typed(self, answers: list[str], selected: list[str], *, exclusive: set[str] | None = None) -> list[str]:
        replies = iter(answers)
        self.defaults = []
        out = io.StringIO()

        def ask(_prompt, *, default, **_kwargs):
            self.defaults.append(default)
            return next(replies)

        with patch.object(setup_module, "_keyboard_menu_available", return_value=False), patch.object(
            setup_module, "_ask", side_effect=ask
        ), redirect_stdout(out):
            chosen = setup_module._ask_multi_choice(
                "Which?",
                ["pick some"],
                self.OPTIONS,
                selected=selected,
                exclusive=exclusive,
                use_color=False,
                language="en",
            )
        self.rendered = out.getvalue()
        return chosen

    def test_empty_input_keeps_the_preselected_set(self) -> None:
        self.assertEqual(self._typed([""], ["og", "openrouter"]), ["og", "openrouter"])
        # The ticked rows are shown ticked, and the prompt's default names them
        # so "Enter" is visibly the ticked list rather than a blind accept.
        self.assertIn("1) [x] og", self.rendered)
        self.assertIn("2) [ ] Z.ai (GLM)", self.rendered)
        self.assertEqual(self.defaults, ["1,3"])
        # With nothing ticked the default is empty -- there is no token to learn.
        self.assertEqual(self._typed([""], []), [])
        self.assertEqual(self.defaults, [""])

    def test_numbers_replace_the_selection_and_can_clear_a_preselected_row(self) -> None:
        self.assertEqual(self._typed(["2, 3"], ["og"]), ["zai", "openrouter"])
        self.assertEqual(self._typed(["2"], ["og", "zai", "openrouter"]), ["zai"])

    def test_there_is_no_magic_none_token(self) -> None:
        """"None of these" is a row a caller adds, not a number to know."""
        self.assertEqual(self._typed(["0", "2"], ["og"]), ["zai"])
        self.assertEqual(self._typed(["none", "2"], ["og"]), ["zai"])

    def test_an_exclusive_value_may_only_be_picked_alone(self) -> None:
        exclusive = {"openrouter"}
        self.assertEqual(self._typed(["3"], ["og"], exclusive=exclusive), ["openrouter"])
        # Picked beside another row it is contradictory, so the prompt re-asks.
        self.assertEqual(self._typed(["1, 3", "2"], ["og"], exclusive=exclusive), ["zai"])
        self.assertIn("cannot be combined", self.rendered)
        # An empty answer that would confirm a contradictory pre-selection is
        # caught by the same rule rather than slipping through.
        self.assertEqual(self._typed(["2"], ["og", "openrouter"], exclusive=exclusive), ["zai"])

    def test_keyboard_path_refuses_a_contradictory_tick_set_and_keeps_the_menu(self) -> None:
        # Row 1 ticked; tick row 3 (exclusive) too, press Enter -> refused;
        # untick row 1, Enter -> accepted.
        keys = iter(["3", "\r", "1", "\r"])
        out = io.StringIO()
        with patch.object(setup_module, "_keyboard_menu_available", return_value=True), patch.object(
            setup_module, "_read_tui_key", side_effect=lambda: next(keys)
        ), redirect_stdout(out):
            chosen = setup_module._ask_multi_choice(
                "Which?",
                ["pick some"],
                self.OPTIONS,
                selected=["og"],
                exclusive={"openrouter"},
                use_color=False,
                language="en",
            )
        self.assertEqual(chosen, ["openrouter"])
        self.assertIn("cannot be combined", out.getvalue())

    def test_values_are_accepted_and_an_unknown_token_re_asks(self) -> None:
        self.assertEqual(self._typed(["zai"], []), ["zai"])
        self.assertEqual(self._typed(["9", "1"], ["zai"]), ["og"])

    def test_keyboard_path_toggles_with_space_and_confirms_with_enter(self) -> None:
        keys = iter([" ", "\x1b[B", " ", "\r"])  # untick 1, move down, tick 2, confirm
        out = io.StringIO()
        with patch.object(setup_module, "_keyboard_menu_available", return_value=True), patch.object(
            setup_module, "_read_tui_key", side_effect=lambda: next(keys)
        ), redirect_stdout(out):
            chosen = setup_module._ask_multi_choice(
                "Which?", ["pick some"], self.OPTIONS, selected=["og"], use_color=False, language="en"
            )
        self.assertEqual(chosen, ["zai"])
        rendered = out.getvalue()
        self.assertIn("[x] og", rendered)  # the first render, before the untick
        self.assertIn("[x] Z.ai (GLM)", rendered)  # after Space on row 2

    def test_every_language_carries_the_new_prompt_keys(self) -> None:
        keys = (
            "menu_hint_multi",
            "select_multi",
            "provider_select_title",
            "provider_select_intro_1",
            "provider_select_intro_2",
            "provider_detected_config",
            "provider_detected_env",
            "provider_detected_login",
            "provider_recorded_before",
            "provider_opengateway_desc",
            "provider_skip_label",
            "provider_skip_desc",
            "provider_entitlements_skipped",
            "exclusive_selection",
        )
        for code in LANGUAGE_CODES:
            for key in keys:
                with self.subTest(language=code, key=key):
                    self.assertIn(key, MESSAGES[code])
                    self.assertTrue(tr(code, key, name="X", label="Y").strip())
        # The retired per-provider question and the retired gate question are
        # gone from every catalog.
        for code in LANGUAGE_CODES:
            self.assertNotIn("provider_hold_prompt", MESSAGES[code])
            self.assertNotIn("provider_entitlements_prompt", MESSAGES[code])

    def test_the_interview_copy_does_not_claim_the_built_in_order_survives_a_skip(self) -> None:
        """Linked providers reorder chains whether or not the interview is answered.

        Before detection the three strings were true: nothing counted until
        the operator answered. Now the record only corrects what is already
        counted, so every language has to say that skipping keeps the linked
        providers counting, and that the record sits on top of them.
        """
        retired = ("built-in model order", "기본 모델 순서", "組み込みのモデル順序", "内置的模型顺序", "内置模型列表")
        for code in LANGUAGE_CODES:
            for key in ("provider_entitlements_note", "provider_skip_desc", "provider_entitlements_skipped"):
                with self.subTest(language=code, key=key):
                    text = tr(code, key)
                    for phrase in retired:
                        self.assertNotIn(phrase, text)
        self.assertIn("count on their own and arrive pre-ticked", tr("en", "provider_entitlements_note"))
        # The interview also offers a key exported only in the shell,
        # pre-ticked; routing reads `.env`, not the process environment, so
        # that row counts only once it is recorded, and the copy says so.
        self.assertIn("a key exported only in this shell is offered pre-ticked too, but counts only once you record it", tr("en", "provider_entitlements_note"))
        for code in ("ko", "ja", "zh"):
            self.assertNotEqual(tr(code, "provider_entitlements_note"), tr("en", "provider_entitlements_note"))
        self.assertIn("on top of that", tr("en", "provider_entitlements_note"))
        self.assertIn("keep counting", tr("en", "provider_skip_desc"))
        self.assertIn("an earlier record stays as it is", tr("en", "provider_skip_desc"))
        self.assertIn("keep counting on their own", tr("en", "provider_entitlements_skipped"))
        for code in ("ko", "ja", "zh"):
            self.assertIn("Hermes", tr(code, "provider_skip_desc"))
            self.assertIn("Hermes", tr(code, "provider_entitlements_skipped"))
            self.assertIn("providers.json", tr(code, "provider_entitlements_note"))


class SetupInterviewTests(unittest.TestCase):
    def setUp(self) -> None:
        # The interview offers the shell's key names on purpose (a person
        # confirms each row), so a developer's exported keys would otherwise
        # decide these tests. Only the fixture's `.env` is read here; a test
        # that wants the shell patches the seam itself.
        patcher = patch.object(setup_module, "_env_key_names", side_effect=lambda paths: set(env_key_names(paths.hermes_home)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _paths(self, root: Path):
        args = argparse.Namespace(omh_home=str(root / ".omh"), hermes_home=str(root / ".hermes"), scope=None)
        return setup_module._paths(args)

    def _config(self, paths, text: str) -> None:
        paths.hermes_config_path.parent.mkdir(parents=True, exist_ok=True)
        paths.hermes_config_path.write_text(text, encoding="utf-8")

    def test_interview_records_answers_and_seeds_claude_code(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "model:\n  provider: og\nproviders:\n  og:\n    base_url: x\n  zai:\n    base_url: y\n")
            args = argparse.Namespace()
            detected = {
                "claude-code": {"binary_present": True, "login_marker": "present"},
                "codex": {"binary_present": True, "login_marker": "absent"},
            }
            # One list (og ticked, zai unticked), then claude-code? yes.
            # There is no gate question any more, and Codex is never asked: a
            # Codex login is a Hermes provider, not a Maestro-only
            # subscription.
            answers = iter([True])
            with patch.object(setup_module, "_detect_external_cli_profiles", return_value=detected), patch.object(
                setup_module, "_ask_yes_no", side_effect=lambda *a, **k: next(answers)
            ), patch.object(setup_module, "_ask_multi_choice", return_value=["og"]), patch.object(
                setup_module, "_ask", return_value=""
            ), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(args, paths, "en")
            with self.assertRaises(StopIteration):
                next(answers)

            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(
                document,
                {
                    # `zai` was found in the config and left unticked: the
                    # clearing is recorded so routing stops counting it.
                    "excluded_providers": ["zai"],
                    "providers": {"og": "gateway"},
                    "schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION,
                    "subscription_clis": ["claude-code"],
                },
            )
            prefs = json.loads((paths.omh_home / "routing" / "dispatch-models.json").read_text(encoding="utf-8"))
            self.assertEqual(prefs["profiles"], {"claude-code": CLAUDE_FRONTIER_CHAIN_MODELS[0]})
            # A second call in the same run asks nothing more.
            with patch.object(setup_module, "_ask_yes_no") as yes_no:
                setup_module._ask_provider_entitlements(args, paths, "en")
            yes_no.assert_not_called()

    def test_rerun_defaults_come_from_the_existing_document(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n  zai:\n    base_url: y\n")
            _write(
                provider_entitlements_path(paths.omh_home),
                {"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"zai": "zai"}, "subscription_clis": []},
            )
            args = argparse.Namespace()
            rows: dict[str, object] = {}

            def multi_choice(_title, _intro, options, *, selected, **_kwargs):
                rows["options"] = options
                rows["selected"] = selected
                return list(selected)

            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no") as yes_no, patch.object(
                setup_module, "_ask_multi_choice", side_effect=multi_choice
            ), patch.object(setup_module, "_ask", return_value=""), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(args, paths, "en")

            # No yes/no survives here: the gate is gone and no subscription CLI
            # is detected, so the list is the whole question.
            yes_no.assert_not_called()
            # The previously recorded provider is pre-ticked, and so is the
            # configured one: routing already counts a linked provider, and a
            # record written before exclusions existed says nothing about it.
            values = [option["value"] for option in rows["options"]]
            self.assertEqual(values[:2], ["og", "zai"])
            self.assertEqual(rows["selected"], ["og", "zai"])
            # The recorded kind survives the re-run rather than reverting;
            # the found row keeps the kind detection gave it.
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(document["providers"], {"og": "gateway", "zai": "zai"})
            self.assertNotIn("excluded_providers", document)

    def test_env_key_names_surface_builtin_providers_without_reading_values(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "model:\n  provider: auto\n")
            paths.hermes_home.mkdir(parents=True, exist_ok=True)
            (paths.hermes_home / ".env").write_text(
                "# keys\nexport ANTHROPIC_API_KEY=sk-secret-value\nUNRELATED=1\n", encoding="utf-8"
            )
            with patch.dict("os.environ", {}, clear=False):
                import os

                os.environ.pop("ANTHROPIC_API_KEY", None)
                candidates = setup_module._provider_candidates(paths)
            # `auto` is Hermes' resolution mode, not an account, and is never asked.
            # The third element is where the row was found, and it is the
            # variable NAME -- the value is never read.
            self.assertEqual(candidates, [("anthropic", "anthropic", "ANTHROPIC_API_KEY")])
            args = argparse.Namespace()
            rows: dict[str, object] = {}

            def multi_choice(_title, _intro, options, *, selected, **_kwargs):
                rows["options"] = options
                return list(selected)

            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no", return_value=True), patch.object(
                setup_module, "_ask_multi_choice", side_effect=multi_choice
            ), patch.object(setup_module, "_ask", return_value=""), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(args, paths, "en")
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(document["providers"], {"anthropic": "anthropic"})
            # The row names the variable it was found by; no value reaches the
            # menu or the document.
            self.assertEqual(rows["options"][0]["description"], "found: ANTHROPIC_API_KEY is set here")
            self.assertNotIn("sk-secret-value", json.dumps(rows["options"]))
            self.assertNotIn("sk-secret-value", json.dumps(document))

    def test_a_key_name_recorded_under_its_family_id_is_not_offered_twice(self) -> None:
        # A document from before detection named OPENAI_API_KEY's account
        # `openai`; detection now names it `openai-api`. One row, ticked.
        options, preselected, kinds = setup_module._provider_entitlement_options(
            [("openai-api", "openai", "OPENAI_API_KEY"), ("openai-codex", "openai-codex", "login")],
            {"openai": "openai"},
            "en",
        )
        values = [option["value"] for option in options]
        self.assertNotIn("openai-api", values)
        self.assertEqual(values[:2], ["openai-codex", "openai"])
        self.assertEqual(preselected, ["openai-codex", "openai"])
        self.assertEqual(kinds["openai"], "openai")

    def test_a_hermes_auth_login_is_offered_as_a_pre_ticked_row(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "model:\n  provider: auto\n")
            (paths.hermes_home / "auth.json").write_text(
                json.dumps({"version": 1, "providers": {"openai-codex": {"access_token": "tok-secret-value"}}}),
                encoding="utf-8",
            )
            with patch.object(setup_module, "_env_key_names", return_value=set()):
                candidates = setup_module._provider_candidates(paths)
                # The login is the row; its kind is the registry family and
                # the evidence is the login itself, not a key name.
                self.assertEqual(candidates, [("openai-codex", "openai-codex", "login")])
                args = argparse.Namespace()
                rows: dict[str, object] = {}

                def multi_choice(_title, _intro, options, *, selected, **_kwargs):
                    rows["options"] = options
                    rows["selected"] = list(selected)
                    return list(selected)

                with patch.object(
                    setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
                ), patch.object(setup_module, "_ask_multi_choice", side_effect=multi_choice), patch.object(
                    setup_module, "_ask", return_value=""
                ), patch.object(setup_module, "_use_color", return_value=False):
                    setup_module._ask_provider_entitlements(args, paths, "en")
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(document["providers"], {"openai-codex": "openai-codex"})
            self.assertEqual(rows["selected"], ["openai-codex"])
            self.assertEqual(rows["options"][0]["label"], "OpenAI Codex")
            self.assertEqual(rows["options"][0]["description"], "found: logged in through hermes auth")
            self.assertNotIn("tok-secret-value", json.dumps(rows["options"]))
            self.assertNotIn("tok-secret-value", json.dumps(document))

    def test_add_loop_records_extra_providers_and_rejects_bad_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  <<: *shared\n  og:\n    base_url: x\n")
            args = argparse.Namespace()
            extra = iter(["my provider", "og", "work-relay", ""])
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no", return_value=True), patch.object(
                setup_module, "_ask_multi_choice", return_value=["og"]
            ), patch.object(setup_module, "_ask_single_choice", return_value="openrouter"), patch.object(
                setup_module, "_ask", side_effect=lambda *a, **k: next(extra)
            ), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(args, paths, "en")
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            # A provider outside the family vocabulary is still reachable by
            # name after the list, and still gets its kind menu. The YAML merge
            # key `<<` is never offered; the bad id and the duplicate are skipped.
            self.assertEqual(document["providers"], {"og": "gateway", "work-relay": "openrouter"})
            parsed, status = load_provider_entitlements(paths.omh_home)
            self.assertEqual(status, "applied")
            self.assertIsNotNone(parsed)

    def test_the_two_rejection_reasons_are_told_apart(self) -> None:
        # One message that ORed the reasons left the operator guessing which
        # of the two happened, and only one of them is worth retyping.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            args = argparse.Namespace()
            extra = iter(["og", "my provider", ""])
            out = io.StringIO()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_multi_choice", return_value=["og"]), patch.object(
                setup_module, "_ask", side_effect=lambda *a, **k: next(extra)
            ), patch.object(setup_module, "_use_color", return_value=False), redirect_stdout(out):
                setup_module._ask_provider_entitlements(args, paths, "en")
            printed = out.getvalue()
            self.assertIn(tr("en", "provider_add_duplicate", provider="og"), printed)
            self.assertIn(tr("en", "provider_add_rejected", provider="my provider"), printed)
            self.assertNotIn("already recorded or is not", printed)

    def test_arrow_keys_into_the_add_prompt_leave_the_loop_without_a_rejection(self) -> None:
        # The real `_ask` this time: the arrow keys the operator carried over
        # from the multi-select above have to die inside it, or they become a
        # provider name, a rejection, and raw escape bytes echoed back into
        # the terminal.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            args = argparse.Namespace()
            out = io.StringIO()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_multi_choice", return_value=["og"]), patch.object(
                setup_module, "_use_color", return_value=False
            ), patch.object(setup_module.sys, "stdin", io.StringIO("\x1b[B\x1b[A\x1b[B\x1b[\n")), redirect_stdout(out):
                setup_module._ask_provider_entitlements(args, paths, "en")
            printed = out.getvalue()
            self.assertNotIn("skipped.", printed)
            self.assertNotIn("\x1b[B", printed)
            self.assertNotIn("\x1b[A", printed)
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(document["providers"], {"og": "gateway"})

    def test_invalid_existing_document_is_announced_and_replaced_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            _write(provider_entitlements_path(paths.omh_home), {"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"og": "warp"}})
            args = argparse.Namespace()
            out = io.StringIO()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_multi_choice", return_value=["og"]), patch.object(
                setup_module, "_ask", return_value=""
            ), patch.object(setup_module, "_use_color", return_value=False), redirect_stdout(out):
                setup_module._ask_provider_entitlements(args, paths, "en")
            # The warning names why the old document is not in force, and
            # answering replaces it wholesale.
            self.assertIn("not applied", out.getvalue())
            self.assertIn("kind must be one of", out.getvalue())
            _parsed, status = load_provider_entitlements(paths.omh_home)
            self.assertEqual(status, "applied")

    def test_unreadable_dispatch_document_is_reported_not_silently_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "")
            path = paths.omh_home / "routing" / "dispatch-models.json"
            path.parent.mkdir(parents=True)
            path.write_text("{", encoding="utf-8")
            args = argparse.Namespace()
            out = io.StringIO()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": True}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no", return_value=True), patch.object(
                setup_module, "_ask", return_value=""
            ), patch.object(setup_module, "_use_color", return_value=False), redirect_stdout(out):
                setup_module._ask_provider_entitlements(args, paths, "en")
            self.assertIn("Could not seed", out.getvalue())
            self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_the_skip_row_writes_nothing_and_keeps_the_shipped_order(self) -> None:
        """Skip is the whole "leave it alone" contract: no document, no seed.

        It has to hold even when a Claude Code subscription would otherwise be
        asked about and seeded -- picking skip ends the question there.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            args = argparse.Namespace()
            out = io.StringIO()
            with patch.object(
                setup_module,
                "_detect_external_cli_profiles",
                return_value={"claude-code": {"binary_present": True, "login_marker": "present"}, "codex": {"binary_present": False}},
            ), patch.object(
                setup_module, "_ask_multi_choice", return_value=[setup_module._PROVIDER_SKIP_CHOICE]
            ), patch.object(setup_module, "_ask_yes_no") as yes_no, patch.object(
                setup_module, "_ask"
            ) as free_form, patch.object(setup_module, "_use_color", return_value=False), redirect_stdout(out):
                setup_module._ask_provider_entitlements(args, paths, "en")
            self.assertIsNone(args._provider_entitlements)
            self.assertFalse(provider_entitlements_path(paths.omh_home).exists())
            self.assertFalse((paths.omh_home / "routing" / "dispatch-models.json").exists())
            # Skip ends the question: neither the add loop nor the subscription
            # yes/no runs after it.
            free_form.assert_not_called()
            yes_no.assert_not_called()
            # Skip leaves the linked providers counting; it does not restore
            # the built-in order, and the copy must not say it does.
            self.assertIn("providers linked to Hermes keep counting", out.getvalue())
            self.assertNotIn("built-in", out.getvalue())

    def test_ticking_nothing_records_an_empty_document_which_skip_does_not(self) -> None:
        """The two ways of answering "no" are distinct, and both keep the order.

        Skip writes nothing. Ticking nothing records "I hold none of these" --
        a document whose empty `providers` makes `alias_is_served` fail open,
        so the chain order is identical either way. The difference that earns
        the distinction is that an operator who recorded providers before can
        clear them by unticking; skip would leave the old answers standing.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            _write(
                provider_entitlements_path(paths.omh_home),
                {"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"og": "zai"}, "subscription_clis": []},
            )
            args = argparse.Namespace()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_multi_choice", return_value=[]), patch.object(
                setup_module, "_ask", return_value=""
            ), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(args, paths, "en")
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(document["providers"], {})
            self.assertEqual(
                entitlement_shaped_chain(HERMES_MIXTURE_CATEGORY_CHAINS["quick"], _entitlements({})),
                HERMES_MIXTURE_CATEGORY_CHAINS["quick"],
            )

    def test_the_skip_row_is_last_exclusive_and_never_pre_ticked(self) -> None:
        options, preselected, kinds = setup_module._provider_entitlement_options(
            [("og", "gateway", "config")], {"og": "gateway"}, "en"
        )
        self.assertEqual(options[-1]["value"], setup_module._PROVIDER_SKIP_CHOICE)
        self.assertNotIn(setup_module._PROVIDER_SKIP_CHOICE, preselected)
        # It is not a provider id and carries no kind, so it can never be
        # written into the document by accident.
        self.assertNotIn(setup_module._PROVIDER_SKIP_CHOICE, kinds)
        from omh.plugin_bundle.omh.hermes_delegation import is_provider_id_token

        self.assertFalse(is_provider_id_token(setup_module._PROVIDER_SKIP_CHOICE))

    def test_opengateway_is_always_offered_and_never_duplicated(self) -> None:
        """OMH's own gateway is discoverable before its key is configured.

        Offering it only once OPENGATEWAY_API_KEY exists hides it from everyone
        who has the service but has not set the key up yet.
        """
        from omh.plugin_bundle.omh.hermes_delegation import PROVIDER_FAMILY_VOCABULARY

        self.assertNotIn("opengateway", PROVIDER_FAMILY_VOCABULARY)
        options, preselected, kinds = setup_module._provider_entitlement_options([], {}, "en")
        values = [option["value"] for option in options]
        # Last real row, immediately before skip, and not pre-ticked when the
        # env key is absent.
        self.assertEqual(values[-2:], ["opengateway", setup_module._PROVIDER_SKIP_CHOICE])
        self.assertEqual(kinds["opengateway"], "gateway")
        self.assertEqual(preselected, [])
        # Detected through its env-key name it becomes a normal pre-ticked
        # candidate row and the standing row is not repeated.
        options, preselected, kinds = setup_module._provider_entitlement_options(
            [("opengateway", "gateway", "OPENGATEWAY_API_KEY")], {}, "en"
        )
        values = [option["value"] for option in options]
        self.assertEqual(values.count("opengateway"), 1)
        self.assertEqual(values[0], "opengateway")
        self.assertEqual(preselected, ["opengateway"])
        self.assertEqual(options[0]["description"], "found: OPENGATEWAY_API_KEY is set here")

    def test_a_detected_row_is_a_default_the_operator_can_clear(self) -> None:
        """Detection pre-ticks a row; it never decides one.

        EXECUTOR_AUTH_SIGNALS_CLAIM_BOUNDARY applies to the config key and the
        variable name too: neither is an account. Unticking a pre-ticked row
        must keep it out of the document.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n  zai:\n    base_url: y\n")
            args = argparse.Namespace()
            seen: dict[str, object] = {}

            def multi_choice(_title, _intro, options, *, selected, **_kwargs):
                seen["selected"] = list(selected)
                # The operator unticks `og` and leaves `zai`.
                return [value for value in selected if value != "og"]

            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no", return_value=True), patch.object(
                setup_module, "_ask_multi_choice", side_effect=multi_choice
            ), patch.object(setup_module, "_ask", return_value=""), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(args, paths, "en")
            self.assertEqual(seen["selected"], ["og", "zai"])
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            # A `providers.zai` block is Hermes' own Z.ai provider, so the row
            # carries that family; only an id the registry does not know
            # (`og`) defaults to a gateway.
            self.assertEqual(document["providers"], {"zai": "zai"})
            # The cleared row is linked to Hermes and would count on its own;
            # the clearing is what stops it, here and in every chain reader.
            self.assertEqual(document["excluded_providers"], ["og"])
            entitlements, status, providers = effective_provider_entitlements(paths.omh_home, paths.hermes_home)
            self.assertEqual(status, "applied")
            self.assertEqual(entitlements["providers"], {"zai": "zai"})
            self.assertEqual([row["id"] for row in providers], ["zai"])
            # On the next run the cleared row is offered again, unticked, and
            # Enter keeps it cleared rather than re-adopting it.
            rerun: dict[str, object] = {}

            def rerun_choice(_title, _intro, options, *, selected, **_kwargs):
                rerun["selected"] = list(selected)
                return list(selected)

            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_multi_choice", side_effect=rerun_choice), patch.object(
                setup_module, "_ask", return_value=""
            ), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(argparse.Namespace(), paths, "en")
            self.assertEqual(rerun["selected"], ["zai"])
            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(document["excluded_providers"], ["og"])

    def test_recorded_document_keeps_its_serialized_shape(self) -> None:
        """The question shape changed; the `provider_entitlements/v1` bytes did not."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            args = argparse.Namespace()
            answers = iter([True])
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": True, "login_marker": "present"}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no", side_effect=lambda *a, **k: next(answers)), patch.object(
                setup_module, "_ask_multi_choice", return_value=["og"]
            ), patch.object(setup_module, "_ask", return_value=""), patch.object(setup_module, "_use_color", return_value=False):
                setup_module._ask_provider_entitlements(args, paths, "en")
            self.assertEqual(
                provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"),
                '{\n'
                '  "providers": {\n'
                '    "og": "gateway"\n'
                '  },\n'
                '  "schema_version": "provider_entitlements/v1",\n'
                '  "subscription_clis": [\n'
                '    "claude-code"\n'
                '  ]\n'
                '}\n',
            )
            _parsed, status = load_provider_entitlements(paths.omh_home)
            self.assertEqual(status, "applied")

    def test_every_setup_suppressor_still_closes_the_only_door(self) -> None:
        """Re-derive the suppressor list from the gate rather than restating it.

        `_ask_provider_entitlements` is reachable only from `_run_setup_wizard`,
        which runs only when `_setup_should_interact` says so. A new suppressor
        added to the gate without a case here fails this test.
        """
        import inspect
        import re as _re

        from omh.commands.main import build_parser

        gate = inspect.getsource(setup_module._setup_should_interact) + inspect.getsource(_wants_json)
        read = set(_re.findall(r'getattr\(args, "([a-z_]+)"', gate)) | set(_re.findall(r"args\.([a-z_]+)", gate))
        suppressors = {
            "json": True,
            "dry_run": True,
            "yes": True,
            "no_interactive": True,
            "profile": ["safety-first"],
            "default_executor": "codex",
            "profile_pack": ["team"],
            "with_mcp": True,
            "memory_mode": "auto",
            "with_menubar": True,
            "no_menubar": True,
            "skip_apply": True,
            "scope": "user",
            "model_setup": True,
        }
        self.assertEqual(read - {"interactive"}, set(suppressors))
        parser = build_parser()
        for name, value in suppressors.items():
            with self.subTest(suppressor=name):
                args = parser.parse_args(["setup"])
                self.assertIn(name, vars(args))
                # `vars()`, not `setattr`: the static shard planner rejects a
                # `setattr` whose name is not a literal.
                vars(args)[name] = value
                self.assertFalse(setup_module._setup_should_interact(args))
        # `OMH_OUTPUT=json` is the same hard suppressor without a flag.
        with patch.dict("os.environ", {"OMH_OUTPUT": "json"}):
            self.assertFalse(setup_module._setup_should_interact(parser.parse_args(["setup", "--interactive"])))
        # `--interactive` is the one flag that forces the question on.
        forced = parser.parse_args(["setup", "--interactive"])
        self.assertTrue(setup_module._setup_should_interact(forced))

    def test_yes_run_writes_no_entitlement_document(self) -> None:
        """`--yes` is consent for the display choice, never for an entitlement answer."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            self.assertTrue(setup_module._provider_candidates(paths))
            status, _stdout, stderr = run_cli(
                ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home), "setup", "--yes"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            self.assertFalse(provider_entitlements_path(paths.omh_home).exists())

    def test_nothing_detected_asks_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            args = argparse.Namespace()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no") as yes_no:
                setup_module._ask_provider_entitlements(args, paths, "en")
            yes_no.assert_not_called()
            self.assertIsNone(args._provider_entitlements)

    def test_claude_code_seed_refuses_a_document_the_reader_would_ignore(self) -> None:
        from omh.coding.fanout_dispatch import _dispatch_model_preference

        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            path = paths.omh_home / "routing" / "dispatch-models.json"
            for document in ({"schema_version": "omh_dispatch_model_preferences/v0", "profiles": {}}, {"profiles": {}}, {"schema_version": "omh_dispatch_model_preferences/v1", "profiles": []}):
                with self.subTest(document=document):
                    _write(path, document)
                    self.assertEqual(setup_module._seed_claude_code_dispatch_head(paths)["status"], "unreadable")
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
            path.unlink()
            self.assertEqual(setup_module._seed_claude_code_dispatch_head(paths)["status"], "seeded")
            # What the seed wrote is what the dispatch reader accepts.
            self.assertEqual(_dispatch_model_preference(paths, "claude-code"), CLAUDE_FRONTIER_CHAIN_MODELS[0])

    def test_claude_code_seed_never_overwrites_an_existing_value(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            path = paths.omh_home / "routing" / "dispatch-models.json"
            _write(path, {"schema_version": "omh_dispatch_model_preferences/v1", "profiles": {"claude-code": "opus"}})
            self.assertEqual(setup_module._seed_claude_code_dispatch_head(paths)["status"], "already_present")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["profiles"], {"claude-code": "opus"})
            _write(path, {"schema_version": "omh_dispatch_model_preferences/v1", "profiles": {"codex": "gpt-5.6-sol"}})
            self.assertEqual(setup_module._seed_claude_code_dispatch_head(paths)["status"], "seeded")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["profiles"],
                {"claude-code": CLAUDE_FRONTIER_CHAIN_MODELS[0], "codex": "gpt-5.6-sol"},
            )


if __name__ == "__main__":
    unittest.main()
