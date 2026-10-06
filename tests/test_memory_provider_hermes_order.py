"""The provider serves memory in the order Hermes actually calls its hooks.

Hermes (`agent/turn_context.py`) calls `on_turn_start(turn, message)` and then
`prefetch_all(message)` inside the SAME turn; `queue_prefetch_all` runs only
after the turn ends (`run_agent.py`). Every other provider test here inserts
`queue_prefetch` between the two, an order Hermes never uses, which is how a
pack blanked by `on_turn_start` passed the suite while live sessions received
an empty string on every turn from 2026-09-12 (commit 2ab7da2ef) until the
hook order was checked against the installed host.

These cases drive the provider in the host's order and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from project_identity_fixture import memory_paths as resolve_paths  # noqa: E402
from test_memory_prefetch_canonical import approve, provider, rendered_ids  # noqa: E402
from omh.plugin_bundle.omh.memory_provider import RecallStatus  # noqa: E402
from omh.workflows.memory import build_project_memory_status  # noqa: E402


def hermes_turn(live, turn: int, message: str, **kwargs) -> str:
    """One Hermes turn: on_turn_start, then prefetch, same message, no queue."""
    live.on_turn_start(turn, message, **kwargs)
    return live.prefetch(message, **kwargs)


class HermesHookOrderTests(unittest.TestCase):
    def test_the_first_turn_serves_the_record_without_a_queued_render(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes").mkdir()
            record = approve(root, "OMH uses deterministic token recall for memory packs")
            live = provider(root)
            pack = hermes_turn(live, 1, "how does omh recall memory packs")
            self.assertEqual(rendered_ids(pack), [record["record_id"]])
            self.assertEqual(live.recall_status(), RecallStatus(provider_label="OMH", count=1))
            self.assertIsNotNone(live.latest_prefetch_receipt(), "a served pack leaves its receipt")

    def test_every_later_turn_serves_too_after_the_host_queues_the_finished_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes").mkdir()
            record = approve(root, "OMH uses deterministic token recall for memory packs")
            live = provider(root)
            hermes_turn(live, 1, "how does omh recall memory packs")
            live.queue_prefetch("how does omh recall memory packs")  # end of turn 1, as Hermes does
            pack = hermes_turn(live, 2, "and the memory packs again")
            self.assertEqual(rendered_ids(pack), [record["record_id"]])
            self.assertEqual(live.recall_status(), RecallStatus(provider_label="OMH", count=1))

    def test_the_pack_is_ranked_for_the_current_message_not_the_previous_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes").mkdir()
            packs = approve(root, "OMH uses deterministic token recall for memory packs")
            deploy = approve(root, "Production deploys run from the release branch only")
            live = provider(root)
            first = hermes_turn(live, 1, "how do we deploy to production")
            self.assertEqual(rendered_ids(first), [deploy["record_id"]])
            live.queue_prefetch("how do we deploy to production")
            # Hermes queued the finished turn's text; the next message must
            # still be what ranks the next pack.
            second = hermes_turn(live, 2, "how does omh recall memory packs")
            self.assertEqual(rendered_ids(second), [packs["record_id"]])

    def test_a_due_consolidation_brief_reaches_the_next_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            memories = root / ".hermes" / "memories"
            memories.mkdir(parents=True)
            (memories / "MEMORY.md").write_text("- one fact\n", encoding="utf-8")
            live = provider(root)
            hermes_turn(live, 1, "hi")
            live.on_session_end()  # one unconsolidated turn is enough at a session end
            self.assertTrue((root / ".omh" / "memory" / "consolidation.json").exists())
            # The next session's first turn, in Hermes order.
            resumed = provider(root)
            pack = hermes_turn(resumed, 1, "what did we leave unfinished")
            self.assertIn("<memory_consolidation", pack)
            self.assertIsNone(resumed.recall_status(), "a brief is a request, not recalled memory")

    def test_a_principal_handed_to_prefetch_gets_its_own_render_not_a_blank(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes").mkdir()
            record = approve(root, "OMH uses deterministic token recall for memory packs")
            live = provider(root)
            live.on_turn_start(1, "how does omh recall memory packs")
            other = {
                "schema_version": "memory_principal_context/v1",
                "principal": "principal:v1:" + "a" * 64,
                "profile_ref": "",
                "surface_ref": "fixture",
                "session_ref": "session-a",
                "turn_ref": "turn_1",
                "actor_kind": "human",
                "identity_evidence_refs": ["evidence:fixture"],
                "binding_state": "validated_local",
            }
            pack = live.prefetch("how does omh recall memory packs", principal_context=other)
            self.assertEqual(rendered_ids(pack), [record["record_id"]], "an unscoped record is visible to the arriving lens")
            self.assertEqual(live.recall_status(), RecallStatus(provider_label="OMH", count=1))

    def test_status_says_whether_a_pack_was_ever_served(self) -> None:
        # The store counts say what could be recalled; `last_prefetch` says
        # what the provider actually handed the host. For a month the two
        # read identically ("nothing") while the defect above blanked every
        # pack, so `never_served` has to be a distinct, loud state.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes").mkdir()
            approve(root, "OMH uses deterministic token recall for memory packs")
            paths = resolve_paths(root / ".omh", root / ".hermes")
            before = build_project_memory_status(paths)["last_prefetch"]
            self.assertEqual(before["state"], "never_served")
            self.assertIsNone(before["served_at"])
            self.assertEqual(Path(before["receipt_path"]).resolve(), (root / ".omh" / "memory" / "prefetch_receipt.json").resolve())
            live = provider(root)
            hermes_turn(live, 1, "how does omh recall memory packs")
            after = build_project_memory_status(paths)["last_prefetch"]
            self.assertEqual(after["state"], "returned_to_host")
            self.assertEqual(after["rendered_record_count"], 1)
            self.assertEqual(after["rendered_block_count"], 0)
            self.assertEqual(after["session_id"], "session-a")
            self.assertTrue(str(after["served_at"]).endswith("Z"))
            self.assertLess(float(after["age_hours"]), 1.0)

    def test_an_empty_store_still_serves_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hermes").mkdir()
            live = provider(root)
            self.assertEqual(hermes_turn(live, 1, "anything at all"), "")
            self.assertIsNone(live.recall_status())


if __name__ == "__main__":
    unittest.main()
