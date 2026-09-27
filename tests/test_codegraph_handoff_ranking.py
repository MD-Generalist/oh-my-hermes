"""#1696: the codegraph ranks by structure and reaches prepared coding handoffs.

Two contracts. The ranking is a personalized PageRank over the scanner's
internal import edges, seeded by task terms and files already in play, so a
module the task's files depend on outranks a leaf that merely shares a word
with the task. And a coding handoff built for a project that stores a codegraph
artifact carries a context pack derived from it, while a project without one
gets exactly the handoff it had.
"""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from omh.codegraph import build_codegraph, build_handoff_context, codegraph_artifact_path, write_codegraph_artifact
from omh.codegraph.schema import CODEGRAPH_CONTEXT_TRUTH_LEVEL
from omh.coding.codegraph_context import CODEGRAPH_CONTEXT_SOURCE, derived_codegraph_context_pack
from omh.coding.handoff_input_manifest import ManifestSelection, build_handoff_input_manifest
from omh.coding_delegation import build_coding_delegation_payload
from omh.memory import validate_handoff_context_pack
from omh.runtime.records import validate_handoff_context_pack_fields


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _hub_repo(root: Path) -> None:
    """Three widget modules import one store; a fourth widget module imports nothing.

    `pkg/store.py` shares no term with a widget task. `pkg/widget_leaf.py`
    matches the task exactly as well as the other three widget modules and is
    structurally a leaf: nothing imports it and it imports nothing.
    """
    _write(root / "pkg" / "store.py", "def persist(value):\n    return value\n")
    for name in ("widget_a", "widget_b", "widget_c"):
        _write(root / "pkg" / f"{name}.py", f"import pkg.store\n\ndef draw_{name}():\n    return pkg.store.persist(1)\n")
    _write(root / "pkg" / "widget_leaf.py", "def draw_widget_leaf():\n    return 0\n")


def _two_cluster_repo(root: Path) -> None:
    _write(root / "alpha" / "core.py", "def base():\n    return 1\n")
    _write(root / "alpha" / "entry.py", "import alpha.core\n\ndef go():\n    return alpha.core.base()\n")
    _write(root / "beta" / "core.py", "def base():\n    return 2\n")
    _write(root / "beta" / "entry.py", "import beta.core\n\ndef go():\n    return beta.core.base()\n")


def _paths(context: dict) -> list[str]:
    return [record["path"] for record in context["focus_files"]]


def _handoff_of(payload: dict) -> dict:
    for key in ("executor_handoff", "runtime_handoff", "prompt_handoff"):
        handoff = payload.get(key)
        if isinstance(handoff, dict):
            return handoff
    raise AssertionError(f"payload prepared no coding handoff: {sorted(payload)}")


class StructuralRankingTests(unittest.TestCase):
    def test_central_file_outranks_a_lexically_matching_leaf(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _hub_repo(repo)
            graph = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")
            context = build_handoff_context(graph, task="redraw the widget")

        order = _paths(context)
        self.assertEqual(order[0], "pkg/store.py", order)
        self.assertLess(order.index("pkg/store.py"), order.index("pkg/widget_leaf.py"), order)

    def test_same_graph_and_seed_give_the_same_order_whatever_the_input_order(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _hub_repo(repo)
            _two_cluster_repo(repo)
            first = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")
            second = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")

        shuffled = json.loads(json.dumps(second))
        rng = random.Random(1696)
        rng.shuffle(shuffled["files"])
        rng.shuffle(shuffled["edges"])
        for task in ("redraw the widget", "nothing matches here", "go base core"):
            with self.subTest(task=task):
                expected = build_handoff_context(first, task=task)
                self.assertEqual(expected, build_handoff_context(first, task=task))
                self.assertEqual(expected["focus_files"], build_handoff_context(second, task=task)["focus_files"])
                self.assertEqual(expected["focus_files"], build_handoff_context(shuffled, task=task)["focus_files"])

    def test_a_term_every_file_carries_seeds_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _hub_repo(repo)
            graph = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")
            # "pkg" is in every path; only "widget_leaf" names a file.
            context = build_handoff_context(graph, task="pkg widget_leaf")

        self.assertEqual(_paths(context), ["pkg/widget_leaf.py"])

    def test_equal_ranks_break_by_path(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _two_cluster_repo(repo)
            graph = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")
            # No term matches, so the restart is uniform and the two clusters
            # are mirror images: each pair ties exactly and path decides.
            context = build_handoff_context(graph, task="zzz")

        self.assertEqual(_paths(context), ["alpha/core.py", "beta/core.py", "alpha/entry.py", "beta/entry.py"])

    def test_changed_paths_seed_the_walk(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _two_cluster_repo(repo)
            graph = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")
            unseeded = build_handoff_context(graph, task="zzz")
            seeded = build_handoff_context(graph, task="zzz", changed_paths=["beta/entry.py"])

        self.assertEqual(len(_paths(unseeded)), 4)
        # Only the changed file's cluster is reachable from the seed.
        self.assertEqual(_paths(seeded), ["beta/entry.py", "beta/core.py"])
        self.assertEqual(seeded["changed_paths"], ["beta/entry.py"])

    def test_cli_changed_flag_resolves_paths_against_the_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _two_cluster_repo(repo)
            status, stdout, stderr = run_cli(
                [
                    "codegraph", "handoff", "--repo", str(repo), "--task", "zzz",
                    "--changed", str(repo / "beta" / "entry.py"), "--json",
                ],
                output_json=False,
            )

        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["changed_paths"], ["beta/entry.py"])
        self.assertEqual(_paths(payload), ["beta/entry.py", "beta/core.py"])


class HandoffWiringTests(unittest.TestCase):
    MESSAGE = "risky refactor of the widget drawing code"

    def _payload(self, repo: Path, **kwargs) -> dict:
        return build_coding_delegation_payload(
            self.MESSAGE, source="discord", executor_target="codex", project_root=repo, **kwargs
        )

    def test_handoff_with_a_stored_codegraph_carries_a_derived_pack(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _hub_repo(repo)
            graph = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")
            write_codegraph_artifact(graph)
            payload = self._payload(repo)
            expected = _paths(build_handoff_context(graph, task=self.MESSAGE))

        handoff = _handoff_of(payload)
        pack = handoff["context_pack"]
        self.assertEqual(validate_handoff_context_pack(pack, require_conflict_free=True), [])
        self.assertEqual(validate_handoff_context_pack_fields(handoff, "handoff"), [])
        self.assertEqual([item["key"] for item in pack["included_context"]], expected)
        self.assertEqual(pack["included_context"][0]["key"], "pkg/store.py")
        self.assertEqual({item["source"] for item in pack["included_context"]}, {CODEGRAPH_CONTEXT_SOURCE})
        self.assertEqual({item["truth_level"] for item in pack["included_context"]}, {CODEGRAPH_CONTEXT_TRUTH_LEVEL})
        self.assertNotIn(self.MESSAGE, json.dumps(pack), "the pack names files, never the task text")
        self.assertNotIn(str(repo), json.dumps(pack), "the pack carries no absolute path")
        self.assertNotIn("codegraph_context_not_attached", payload)
        # The derived pack is what the handoff's manifest enumerates.
        manifest_refs = [item["provenance"]["local_ref"] for item in handoff["input_manifest"]["items"]]
        self.assertEqual(manifest_refs, expected)

    def test_caller_supplied_pack_wins(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _hub_repo(repo)
            graph = build_codegraph(repo, generated_at="2026-09-28T00:00:00Z")
            write_codegraph_artifact(graph)
            supplied, _ = derived_codegraph_context_pack(repo, message="beta", executor_target="codex")
            assert supplied is not None
            supplied["included_context"] = supplied["included_context"][-1:]
            payload = self._payload(repo, context_pack=supplied)

        self.assertEqual(_handoff_of(payload)["context_pack"], supplied)

    def test_project_without_a_codegraph_keeps_its_handoff(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _hub_repo(repo)
            without = self._payload(repo)

        handoff = _handoff_of(without)
        self.assertNotIn("context_pack", handoff)
        self.assertNotIn("input_manifest", handoff)
        self.assertNotIn("codegraph_context_not_attached", without)

    def test_unusable_artifact_is_reported_not_read_as_absent(self) -> None:
        cases = {
            "artifact_unreadable": "{not json",
            "artifact_schema_mismatch": json.dumps({"schema_version": "omh_codegraph/v0", "files": []}),
        }
        for reason, content in cases.items():
            with self.subTest(reason=reason), TemporaryDirectory() as tmp:
                repo = Path(tmp)
                _hub_repo(repo)
                _write(codegraph_artifact_path(repo), content)
                payload = self._payload(repo)

                self.assertNotIn("context_pack", _handoff_of(payload))
                self.assertEqual(payload["codegraph_context_not_attached"]["reason"], reason)

    def test_files_named_in_the_input_manifest_seed_the_ranking(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _two_cluster_repo(repo)
            write_codegraph_artifact(build_codegraph(repo, generated_at="2026-09-28T00:00:00Z"))
            manifest = build_handoff_input_manifest(
                executor_target="codex",
                workspace_root=repo,
                selections=[ManifestSelection(item_kind="file", selector_kind="path", expression="beta/entry.py")],
            )
            pack, not_attached = derived_codegraph_context_pack(
                repo, message="zzz", executor_target="codex", input_manifest=manifest
            )

        self.assertIsNone(not_attached)
        assert pack is not None
        self.assertEqual([item["key"] for item in pack["included_context"]], ["beta/entry.py", "beta/core.py"])
        self.assertEqual(pack["metadata"]["codegraph_seed_changed_path_count"], 1)


if __name__ == "__main__":
    unittest.main()
