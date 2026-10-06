"""The provider's own state survives concurrent sessions, and its skips are counted.

Three defects, one per class below. The dreaming counters were a plain
read-modify-write, so a CLI session and a gateway session on one home erased
each other's increments. A corrupt record file was skipped with nothing saying
so, which made a damaged store indistinguishable from an empty one. And
`_safely` swallowed every failed write without a trace. Each test drives the
real provider or the real reader; nothing here is a model of them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()
from test_memory_prefetch_canonical import approve, provider, serve
from omh.plugin_bundle.omh import memory_provider as provider_module
from omh.plugin_bundle.omh import memory_state_files
from omh.plugin_bundle.omh.memory_dreaming import (
    dreaming_state_path,
    read_dreaming_state,
    record_turn,
    update_dreaming_state,
    write_dreaming_state,
)
from omh.plugin_bundle.omh.memory_prefetch_receipt import (
    mark_prefetch_receipt_returned,
    prefetch_receipt_path,
    validate_prefetch_receipt,
)
from omh.plugin_bundle.omh.memory_provider import OmhMemoryProvider
from omh.plugin_bundle.omh.memory_records import read_record_store_snapshot


THREADS = 8
INCREMENTS = 25


def _leftover_temporaries(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".tmp"))


class ConcurrentStateTests(unittest.TestCase):
    def test_concurrent_sessions_lose_no_turn_increment(self) -> None:
        # Given one OMH home and several providers on it, one per session.
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".omh"
            sessions = [OmhMemoryProvider(home) for _ in range(THREADS)]
            barrier = threading.Barrier(THREADS)

            def turns(live: OmhMemoryProvider) -> None:
                barrier.wait()
                for _ in range(INCREMENTS):
                    live._mutate_state(record_turn)

            # When every session counts its turns at the same time.
            workers = [threading.Thread(target=turns, args=(live,)) for live in sessions]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            # Then every increment is on disk and nothing was swallowed.
            failures = [failure for live in sessions for failure in live._write_failures]
            self.assertEqual(failures, [])
            self.assertEqual(read_dreaming_state(home)["turns_since_consolidation"], THREADS * INCREMENTS)
            self.assertEqual(_leftover_temporaries(home / "memory"), [])

    def test_a_mutate_that_raises_leaves_the_previous_counters_whole(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".omh"
            write_dreaming_state(home, record_turn(read_dreaming_state(home)))
            before = dreaming_state_path(home).read_bytes()

            def broken(state: dict[str, object]) -> dict[str, object]:
                raise RuntimeError("mutation failed half-way")

            with self.assertRaises(RuntimeError):
                update_dreaming_state(home, broken)
            self.assertEqual(dreaming_state_path(home).read_bytes(), before)
            self.assertEqual(_leftover_temporaries(home / "memory"), [])

    def test_a_replace_that_fails_leaves_the_previous_file_and_no_temporary(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".omh"
            write_dreaming_state(home, record_turn(read_dreaming_state(home)))
            before = dreaming_state_path(home).read_bytes()
            with patch.object(memory_state_files.os, "replace", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    update_dreaming_state(home, record_turn)
            self.assertEqual(dreaming_state_path(home).read_bytes(), before)
            self.assertEqual(_leftover_temporaries(home / "memory"), [])

    def test_state_is_written_with_lf_only(self) -> None:
        # Windows text mode would write CRLF; the journals are line-split.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory" / "write_journal.jsonl"
            provider_module._append_bounded_json_line(path, {"action": "add"})
            provider_module._append_bounded_json_line(path, {"action": "remove"})
            self.assertNotIn(b"\r", path.read_bytes())
            self.assertEqual(len(path.read_bytes().splitlines()), 2)


class UnreadableStoreTests(unittest.TestCase):
    def test_a_corrupt_record_is_named_and_the_valid_one_still_served(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            approve(root, "current project release checklist")
            (root / ".omh" / "memory" / "records" / "garbage.json").write_text("{not json", encoding="utf-8")

            snapshot = read_record_store_snapshot((root / ".omh",))
            self.assertEqual(snapshot.unreadable, ("garbage.json",))
            self.assertFalse(snapshot.store_read_error)
            self.assertEqual(len(snapshot.records), 1)

            live = provider(root)
            pack = serve(live, "release checklist")
            self.assertIn("current project release checklist", pack)
            receipt = live.latest_prefetch_receipt()
            assert receipt is not None
            self.assertEqual(receipt["store"]["unreadable_count"], 1)
            self.assertEqual(receipt["store"]["unreadable"], ["garbage.json"])
            self.assertFalse(receipt["store"]["store_read_error"])
            self.assertEqual(validate_prefetch_receipt(receipt), [])

    def test_a_clean_store_reports_nothing_unreadable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            approve(root, "current project release checklist")
            live = provider(root)
            serve(live, "release checklist")
            receipt = live.latest_prefetch_receipt()
            assert receipt is not None
            self.assertEqual(
                {key: receipt["store"][key] for key in ("unreadable_count", "unreadable", "store_read_error")},
                {"unreadable_count": 0, "unreadable": [], "store_read_error": False},
            )

    @unittest.skipIf(sys.platform == "win32" or os.geteuid() == 0, "needs POSIX permissions enforced on this user")
    def test_an_unlistable_records_directory_is_not_an_empty_store(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            approve(root, "current project release checklist")
            records = root / ".omh" / "memory" / "records"
            records.chmod(0)
            try:
                snapshot = read_record_store_snapshot((root / ".omh",))
            finally:
                records.chmod(0o700)
            self.assertEqual(snapshot.records, ())
            self.assertEqual(snapshot.unreadable, ("memory/records",))
            self.assertTrue(snapshot.store_read_error)


class SwallowedWriteFailureTests(unittest.TestCase):
    def test_a_failed_receipt_write_keeps_the_pack_and_reaches_the_next_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            approve(root, "current project release checklist")
            live = provider(root)
            receipt_path = prefetch_receipt_path(root / ".omh")
            real_write = provider_module._write_text

            def refuse_receipt(path: Path, text: str) -> None:
                # By name: the provider resolves its home (/var -> /private/var on macOS).
                if path.name == receipt_path.name:
                    raise PermissionError("read-only receipt")
                real_write(path, text)

            with patch.object(provider_module, "_write_text", side_effect=refuse_receipt):
                pack = serve(live, "release checklist")
            self.assertIn("current project release checklist", pack)
            first = live.latest_prefetch_receipt()
            assert first is not None
            # The failure happened writing this receipt, after it was served.
            self.assertEqual(first["write_failures_count"], 0)

            live.prefetch("release checklist")
            served = live.latest_prefetch_receipt()
            assert served is not None
            self.assertEqual(served["write_failures_count"], 1)
            self.assertEqual(served["last_write_failure"], {"op": "prefetch_receipt", "error": "PermissionError"})
            self.assertEqual(validate_prefetch_receipt(served), [])
            persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["write_failures_count"], 1)
            # Metadata only: the exception message never leaves the provider.
            self.assertNotIn("read-only receipt", json.dumps(live._write_failures))

    def test_a_lock_held_past_its_deadline_is_counted_not_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".omh"
            live = OmhMemoryProvider(home)
            with patch.object(memory_state_files, "STATE_LOCK_TIMEOUT_SECONDS", 0.05):
                with memory_state_files.state_file_lock(dreaming_state_path(home)):
                    live._mutate_state(record_turn)
            self.assertEqual(live._write_failures, [{"op": "dreaming_state", "path": "dreaming.json", "error": "TimeoutError"}])
            self.assertEqual(read_dreaming_state(home)["turns_since_consolidation"], 0)

    def test_the_failure_list_is_bounded_and_the_count_is_not(self) -> None:
        with TemporaryDirectory() as tmp:
            live = OmhMemoryProvider(Path(tmp) / ".omh")
            for _ in range(provider_module.WRITE_FAILURE_LIMIT + 5):
                live._safely("write_journal", Path("write_journal.jsonl"), _raise_os_error)
            self.assertEqual(live._write_failure_count, provider_module.WRITE_FAILURE_LIMIT + 5)
            self.assertEqual(len(live._write_failures), provider_module.WRITE_FAILURE_LIMIT)

    def test_a_tampered_failure_field_fails_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            approve(root, "current project release checklist")
            live = provider(root)
            serve(live, "release checklist")
            receipt = live.latest_prefetch_receipt()
            assert receipt is not None
            leaked = mark_prefetch_receipt_returned(receipt, write_failures_count=1, last_write_failure={"op": "x", "error": "OSError"})
            leaked["last_write_failure"] = {"op": "x", "error": "OSError", "message": "/home/user/secret"}
            self.assertIn("write_failures", validate_prefetch_receipt(leaked))


def _raise_os_error() -> None:
    raise OSError("disk full")


if __name__ == "__main__":
    unittest.main()
