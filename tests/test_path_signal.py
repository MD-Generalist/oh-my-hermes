from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.routing.path_signal import matching_path_glob, path_mentions
from omh.routing.recommend import recommend_skills
from omh.skills.catalog import builtin_definitions
from omh.wrapper.contract import build_chat_interaction_payload

IAC = "iac-change"


def _route(message: str) -> dict[str, object]:
    return build_chat_interaction_payload(message, source="discord")["route"]


def _matched(message: str, skill: str) -> tuple[str, ...]:
    row = next((row for row in recommend_skills(message, limit=10) if row["skill"] == skill), None)
    return tuple(row["matched"]) if row else ()


class PathGlobMatchingTests(unittest.TestCase):
    """#1716: a glob matches a named path or any tail of it."""

    def test_mentions_are_words_with_a_slash_or_an_extension(self) -> None:
        self.assertEqual(
            path_mentions(normalized_phrase("review `infra/main.tf`, then values.yaml and the plan.")),
            ("infra/main.tf", "values.yaml"),
        )

    def test_a_glob_matches_the_path_or_a_tail_after_a_slash(self) -> None:
        globs = ("*.tf", "charts/**", "k8s/**")
        for message, expected in (
            ("change infra/network/main.tf", "*.tf"),
            ("change deploy/charts/api/values.yaml", "charts/**"),
            ("change k8s/base/deployment.yaml", "k8s/**"),
            ("change mycharts/values.yaml", ""),
            ("change main.tfstate.backup", ""),
            ("change the chart", ""),
        ):
            with self.subTest(message=message):
                self.assertEqual(matching_path_glob(normalized_phrase(message), globs), expected)


class PathSignalRoutingTests(unittest.TestCase):
    def test_a_terraform_path_reaches_iac_change_without_the_word(self) -> None:
        for message in (
            "review the change to infra/network/main.tf before we apply it",
            "apply the change in charts/payments/values.yaml to staging first",
        ):
            with self.subTest(message=message):
                self.assertNotIn("terraform", message)
                route = _route(message)
                self.assertEqual(route["action"], "dispatch")
                self.assertEqual(route["selected_skill"], IAC)
                self.assertTrue(any(label.startswith("path:") for label in _matched(message, IAC)))

    def test_a_path_mentioned_in_passing_adds_nothing(self) -> None:
        for message in (
            "I saved my grocery list as main.tf, what should I cook tonight",
            "why is my laptop fan loud while main.tf is open in the editor",
        ):
            with self.subTest(message=message):
                self.assertNotEqual(_route(message)["selected_skill"], IAC)
                self.assertFalse(any(label.startswith("path:") for label in _matched(message, IAC)))

    def test_only_iac_change_declares_globs(self) -> None:
        """Additive: every other skill routes exactly as it did."""

        declared = {definition.name for definition in builtin_definitions() if definition.path_globs}
        self.assertEqual(declared, {IAC})


if __name__ == "__main__":
    unittest.main()
