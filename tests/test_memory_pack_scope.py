"""`omh memory pack` scopes reviewed records the way the recall pack does.

The reviewed-records snapshot is labelled project/default while each item
carries the scope its record was captured under. Before this, the scope
filter matched labels only, so `--scope-kind thread --scope-ref <session>`
and `--scope-kind project --scope-ref <identity>` both came back empty, and
the default project pack carried every session's thread records.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from project_identity_fixture import PROJECT_IDENTITY, memory_paths as resolve_paths

from omh.workflows import memory
from omh.workflows.memory import build_handoff_context_pack

CAPTURED_AT = "2026-09-01T00:00:00Z"
SESSION = "session-a"
OTHER_SESSION = "session-b"


def approve(root: Path, summary: str, **capture: Any) -> dict[str, Any]:
    paths = resolve_paths(root / ".omh", root / ".hermes")
    with patch.object(memory, "utc_now", return_value=CAPTURED_AT):
        candidate = memory.capture_project_memory_candidate(paths, summary, **capture)["candidate"]
        assert isinstance(candidate, dict)
        record = memory.approve_project_memory_candidate(paths, candidate["candidate_id"], approved_by="user")["record"]
        assert isinstance(record, dict)
        return record


def _memory_items(pack: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [item for item in pack[key] if isinstance(item, dict) and item.get("source") == "omh_memory"]


class HandoffPackScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = resolve_paths(self.root / ".omh", self.root / ".hermes")
        self.project = approve(self.root, "the project deploys from the main branch")
        self.own_thread = approve(self.root, "this session prefers the staging bucket", scope_kind="thread", scope_ref=SESSION)
        self.other_thread = approve(self.root, "another session prefers the canary bucket", scope_kind="thread", scope_ref=OTHER_SESSION)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def included_ids(self, pack: dict[str, Any]) -> set[str]:
        return {str(item["item_id"]) for item in _memory_items(pack, "included_context")}

    def excluded_reasons(self, pack: dict[str, Any]) -> dict[str, str]:
        return {str(item["item_id"]): str(item["reason"]) for item in _memory_items(pack, "excluded_context")}

    def test_a_thread_pack_holds_that_sessions_records_only(self) -> None:
        pack = build_handoff_context_pack(self.paths, scope_kind="thread", scope_ref=SESSION)
        self.assertEqual(pack["scope"], {"kind": "thread", "ref": SESSION})
        self.assertEqual(self.included_ids(pack), {self.own_thread["record_id"]})
        self.assertNotIn(self.project["summary"], str(pack))
        self.assertNotIn(self.other_thread["summary"], str(pack))

    def test_a_project_pack_by_identity_holds_the_project_records(self) -> None:
        pack = build_handoff_context_pack(self.paths, scope_kind="project", scope_ref=PROJECT_IDENTITY)
        self.assertEqual(self.included_ids(pack), {self.project["record_id"]})
        self.assertNotIn(self.own_thread["summary"], str(pack))

    def test_the_default_pack_carries_only_its_own_sessions_thread_records(self) -> None:
        pack = build_handoff_context_pack(self.paths, session_id=SESSION)
        self.assertEqual(pack["scope"], {"kind": "project", "ref": PROJECT_IDENTITY})
        self.assertEqual(self.included_ids(pack), {self.project["record_id"], self.own_thread["record_id"]})
        # Listed, not dropped: this surface enumerates its exclusions.
        self.assertEqual(self.excluded_reasons(pack), {self.other_thread["record_id"]: "scope_mismatch"})

    def test_a_pack_without_a_session_carries_no_thread_records(self) -> None:
        pack = build_handoff_context_pack(self.paths)
        self.assertEqual(self.included_ids(pack), {self.project["record_id"]})
        self.assertEqual(
            self.excluded_reasons(pack),
            {self.own_thread["record_id"]: "scope_mismatch", self.other_thread["record_id"]: "scope_mismatch"},
        )

    def test_a_label_only_snapshot_still_matches_on_its_label(self) -> None:
        # Snapshots whose items carry no scope of their own (setup, runtime
        # state) keep matching by label, so a project/default pack is unchanged.
        snapshots = [
            {"source": "setup", "scope": {"kind": "project", "ref": "default"}, "items": [{"item_id": "s1", "key": "k", "summary": "x"}]},
            {"source": "setup", "scope": {"kind": "target", "ref": "codex"}, "items": [{"item_id": "s2", "key": "k", "summary": "y"}]},
        ]
        kept = memory._filter_snapshots_by_scope(snapshots, scope_kind="project", scope_ref="default")
        self.assertEqual([snapshot["items"][0]["item_id"] for snapshot in kept], ["s1"])
        self.assertEqual(memory._filter_snapshots_by_scope(snapshots, scope_kind="thread", scope_ref=SESSION), [])


if __name__ == "__main__":
    unittest.main()
