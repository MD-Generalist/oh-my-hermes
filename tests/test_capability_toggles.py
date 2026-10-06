"""Contract tests for per-family capability enable/disable.

The routing cases here are the point of the feature, not decoration. Before it
existed, "메모리 기능 꺼줘" dispatched into `memory-sync` -- the workflow that
WRITES memory -- at score 28. A confident wrong dispatch is worse than a miss,
so both the positive routes and the overroute guards are locked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omh.capabilities.toggles import (
    CAPABILITY_POLICY_SCHEMA_VERSION,
    CapabilityPolicyError,
    build_capability_policy,
    disabled_family_ids,
    disabled_family_workflows,
    enabled_workflow_names,
    family_is_enabled,
    normalize_family_id,
    read_capability_policy,
    retained_core_skills,
    toggleable_family_ids,
    write_capability_policy,
)
from omh.capabilities.families import capability_family_projection
from omh.paths import resolve_paths
from omh.profiles.setup import write_setup_profile
from omh.routing.chat import route_chat_message
from omh.skills.catalog import CORE_SKILLS, installable_skill_names
from omh.workflows.memory import read_project_memory_policy
from _route_owner import route_owner


class _PathsMixin:
    def _paths(self, root: Path):
        paths = resolve_paths(root / ".omh", root / ".hermes")
        paths.setup_profile_path.parent.mkdir(parents=True, exist_ok=True)
        return paths


class CapabilityFamilyIdentityTests(unittest.TestCase):
    def test_the_six_ids_are_derived_from_the_family_table(self) -> None:
        projection = capability_family_projection()
        self.assertEqual(list(toggleable_family_ids()), list(projection["family_order"]))

    def test_labels_and_short_aliases_resolve_to_canonical_ids(self) -> None:
        self.assertEqual(normalize_family_id("retain_knowledge"), "retain_knowledge")
        self.assertEqual(normalize_family_id("Retain knowledge"), "retain_knowledge")
        self.assertEqual(normalize_family_id("memory"), "retain_knowledge")
        self.assertEqual(normalize_family_id("coding"), "delegate_coding_and_ship")
        self.assertEqual(normalize_family_id("retain-knowledge"), "retain_knowledge")

    def test_an_unknown_family_names_every_valid_id_instead_of_guessing(self) -> None:
        with self.assertRaises(CapabilityPolicyError) as caught:
            normalize_family_id("memries")
        message = str(caught.exception)
        for family_id in toggleable_family_ids():
            self.assertIn(family_id, message)

    def test_an_empty_family_is_refused(self) -> None:
        with self.assertRaises(CapabilityPolicyError):
            normalize_family_id("")


class CapabilityPolicyShapeTests(unittest.TestCase):
    def test_absent_policy_means_every_family_is_offered(self) -> None:
        policy = build_capability_policy()
        self.assertEqual(policy["schema_version"], CAPABILITY_POLICY_SCHEMA_VERSION)
        self.assertEqual(policy["disabled_families"], [])
        self.assertEqual(sorted(policy["enabled_families"]), sorted(toggleable_family_ids()))

    def test_every_persisted_value_is_a_scalar_or_a_list_of_scalars(self) -> None:
        # A nested dict or a float here is dropped by the setup-profile
        # correct/restore path, which would silently lose the disable list.
        for key, value in build_capability_policy(["memory"]).items():
            with self.subTest(key=key):
                if isinstance(value, list):
                    self.assertTrue(all(isinstance(item, str) for item in value))
                else:
                    self.assertIsInstance(value, (str, bool, int))
                    self.assertNotIsInstance(value, float)

    def test_the_policy_is_deterministic_and_order_independent(self) -> None:
        first = build_capability_policy(["memory", "coding"])
        second = build_capability_policy(["coding", "memory"])
        self.assertEqual(first, second)
        self.assertEqual(first, build_capability_policy(["memory", "coding", "memory"]))

    def test_disabling_names_the_family_and_leaves_the_rest_enabled(self) -> None:
        policy = build_capability_policy(["memory"])
        self.assertEqual(disabled_family_ids(policy), ("retain_knowledge",))
        self.assertFalse(family_is_enabled(policy, "retain_knowledge"))
        self.assertTrue(family_is_enabled(policy, "delegate_coding_and_ship"))


class CapabilityPolicyEnforcementTests(unittest.TestCase):
    def test_disabling_memory_withholds_exactly_the_memory_workflows(self) -> None:
        policy = build_capability_policy(["retain_knowledge"])
        self.assertEqual(
            disabled_family_workflows(policy),
            ("decision-recall", "memory-new", "memory-sync", "wiki"),
        )

    def test_core_skills_survive_every_possible_disable(self) -> None:
        policy = build_capability_policy(toggleable_family_ids())
        enabled = set(enabled_workflow_names(policy))
        for skill in CORE_SKILLS:
            with self.subTest(skill=skill):
                self.assertIn(skill, enabled)
        self.assertEqual(set(retained_core_skills()), set(CORE_SKILLS))

    def test_disabling_nothing_offers_the_whole_catalog(self) -> None:
        self.assertEqual(
            set(enabled_workflow_names(build_capability_policy())),
            set(installable_skill_names()),
        )

    def test_a_withheld_workflow_is_never_also_offered(self) -> None:
        policy = build_capability_policy(["retain_knowledge"])
        offered = set(enabled_workflow_names(policy))
        for name in disabled_family_workflows(policy):
            with self.subTest(workflow=name):
                self.assertNotIn(name, offered)


class CapabilityPolicyPersistenceTests(_PathsMixin, unittest.TestCase):
    def test_round_trip_through_the_setup_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            write_setup_profile(paths)
            self.assertEqual(disabled_family_ids(read_capability_policy(paths)), ())
            write_capability_policy(paths, ["memory"])
            self.assertEqual(disabled_family_ids(read_capability_policy(paths)), ("retain_knowledge",))

    def test_writing_the_policy_preserves_every_other_profile_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before = write_setup_profile(paths)
            write_capability_policy(paths, ["memory"])
            after = json.loads(paths.setup_profile_path.read_text(encoding="utf-8"))
            for key, value in before.items():
                if key == "capability_policy":
                    continue
                with self.subTest(key=key):
                    self.assertEqual(after[key], value)

    def test_a_missing_profile_reads_as_all_enabled_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self.assertEqual(disabled_family_ids(read_capability_policy(paths)), ())

    def test_a_hand_edited_contradiction_resolves_in_favour_of_the_disable_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            write_setup_profile(paths)
            profile = json.loads(paths.setup_profile_path.read_text(encoding="utf-8"))
            profile["capability_policy"] = {
                "schema_version": CAPABILITY_POLICY_SCHEMA_VERSION,
                "disabled_families": ["retain_knowledge"],
                # Contradicts the disable list on purpose.
                "enabled_families": list(toggleable_family_ids()),
            }
            paths.setup_profile_path.write_text(json.dumps(profile), encoding="utf-8")
            policy = read_capability_policy(paths)
            self.assertEqual(disabled_family_ids(policy), ("retain_knowledge",))
            self.assertNotIn("retain_knowledge", policy["enabled_families"])

    def test_an_unknown_family_in_a_hand_edited_file_is_ignored(self) -> None:
        self.assertEqual(disabled_family_ids({"disabled_families": ["not_a_family"]}), ())


class MemoryFamilyEnforcementTests(_PathsMixin, unittest.TestCase):
    """Disabling `retain_knowledge` must stop memory, not just hide it."""

    def test_disabling_the_family_turns_capture_and_recall_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            write_setup_profile(paths)
            enabled = read_project_memory_policy(paths)
            self.assertTrue(enabled["capture_enabled"])
            self.assertTrue(enabled["recall_enabled"])

            write_capability_policy(paths, ["retain_knowledge"])
            disabled = read_project_memory_policy(paths)
            self.assertEqual(disabled["mode"], "off")
            self.assertFalse(disabled["capture_enabled"])
            self.assertFalse(disabled["recall_enabled"])

    def test_re_enabling_restores_the_previous_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            write_setup_profile(paths)
            write_capability_policy(paths, ["retain_knowledge"])
            write_capability_policy(paths, [])
            restored = read_project_memory_policy(paths)
            # The profile named no mode, so re-enabling returns to the default.
            self.assertEqual(restored["mode"], "auto-safe")
            self.assertTrue(restored["capture_enabled"])

    def test_disabling_a_different_family_leaves_memory_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            write_setup_profile(paths)
            write_capability_policy(paths, ["create_materials_and_visuals"])
            self.assertTrue(read_project_memory_policy(paths)["capture_enabled"])


class CapabilityToggleRoutingTests(unittest.TestCase):
    def test_turning_a_family_off_reaches_the_toggle_skill(self) -> None:
        for message in (
            "메모리 기능 꺼줘",
            "메모리 관리 비활성화해줘",
            "메모리 비활성화",
            "turn off memory",
            "disable memory",
            "disable coding orchestration",
            "코딩 오케스트레이션 비활성화",
            "turn off research",
        ):
            with self.subTest(message=message):
                self.assertEqual(route_chat_message(message)["selected_skill"], "capability-toggle")

    def test_turning_a_family_back_on_reaches_the_toggle_skill(self) -> None:
        for message in ("turn on memory", "enable memory", "메모리 기능 켜줘", "메모리 활성화"):
            with self.subTest(message=message):
                self.assertEqual(route_chat_message(message)["selected_skill"], "capability-toggle")

    def test_a_disable_request_is_not_read_as_an_enable_request(self) -> None:
        # `비활성화` contains `활성화`; without the containment fix both states
        # matched and the contradiction guard refused a perfectly clear message.
        route = route_chat_message("메모리 관리 비활성화해줘")
        self.assertEqual(route["selected_skill"], "capability-toggle")
        self.assertIn("disable", route["reason"])

    def test_the_memory_write_workflows_keep_their_own_requests(self) -> None:
        # The regression this feature exists to prevent, in both directions.
        # Shortlist-first: this asks, with memory-new leading the shortlist.
        self.assertEqual(route_owner(route_chat_message("capture this decision to memory"), allow_clarify=True), "memory-new")
        self.assertEqual(route_chat_message("memory-sync")["selected_skill"], "memory-sync")

    def test_a_toggle_in_the_users_own_product_is_not_a_capability_change(self) -> None:
        for message in (
            "add a dark mode toggle to my app",
            "내 앱에 다크모드 토글 추가해줘",
            "disable the feature flag in my product",
        ):
            with self.subTest(message=message):
                self.assertNotEqual(route_chat_message(message)["selected_skill"], "capability-toggle")

    def test_turning_off_something_that_is_not_a_capability_family_is_not_claimed(self) -> None:
        # These shared their scoring tokens with a "turn off memory" trigger and
        # dispatched here until that trigger was removed.
        for message in (
            "turn off the lights",
            "turn off my laptop",
            "turn off notifications",
            "turn off wifi",
        ):
            with self.subTest(message=message):
                self.assertNotEqual(route_chat_message(message)["selected_skill"], "capability-toggle")

    def test_a_bare_product_token_opens_the_product_itself(self) -> None:
        # This asserted `workflow-learning` while triggers matched in both
        # directions. That skill's only `omh` triggers are negative-usage
        # phrases - "did not use OMH", "OMH was not used", "OMH 안 썼어" - and the
        # bare word is a substring of all three, so it collected +6 apiece and
        # typing the product's own name opened "record a missed-route
        # improvement candidate". `oh-my-hermes` carries a real bare `omh`
        # trigger and is where the word belongs.
        self.assertEqual(route_chat_message("omh")["selected_skill"], "oh-my-hermes")


if __name__ == "__main__":
    unittest.main()
