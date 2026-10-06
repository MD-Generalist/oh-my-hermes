"""One boundary matcher for the control plane and the bundle, with its documented cases."""

from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh import awareness, boundary_phrase
from omh.routing import executor_cues

PHRASES = ("pi", "claude-code", "codex", "omo runtime", "opencode")


class BoundaryPhraseTests(unittest.TestCase):
    def test_the_three_readers_share_one_function(self) -> None:
        self.assertIs(executor_cues.contains_boundary_phrase, boundary_phrase.contains_boundary_phrase)
        self.assertIs(awareness._contains_boundary_phrase, boundary_phrase.contains_boundary_phrase)

    def test_documented_mentions_and_non_mentions(self) -> None:
        mentions = ("ask pi: run it", "use claude-code.", "opencode로 해줘", "omo runtime으로", "pi status")
        non_mentions = ("api한테 물어봐", "promo runtime", "raspi status", "see claude-code.md", "codex-utils.py", "claudecode-notes")
        for text in mentions:
            with self.subTest(text=text):
                self.assertTrue(boundary_phrase.contains_boundary_phrase(text, PHRASES))
        for text in non_mentions:
            with self.subTest(text=text):
                self.assertFalse(boundary_phrase.contains_boundary_phrase(text, PHRASES))

    def test_the_bundle_has_no_second_copy(self) -> None:
        source = (boundary_phrase.__file__).replace("boundary_phrase.py", "awareness.py")
        with open(source, encoding="utf-8") as handle:
            self.assertNotIn("def _contains_boundary_phrase", handle.read())


if __name__ == "__main__":
    unittest.main()
