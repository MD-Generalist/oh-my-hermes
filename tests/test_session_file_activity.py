"""Session file activity: one Hermes session's file-tool calls, projected per workspace file.

Every fixture is a temporary ``state.db`` shaped like Hermes' own: assistant
rows carry ``tool_calls`` (the call id, tool name and JSON arguments) and tool
rows carry the result under the same ``tool_call_id``, with
``effect_disposition`` where Hermes sets it. Nothing here opens the real
store. Every expected value is a hand count of the rows written below.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from omh.quality.session_file_activity import (
    OMISSION_REASONS,
    OUTCOMES,
    SESSION_FILE_ACTIVITY_SCHEMA_VERSION,
    SessionFileActivityError,
    build_session_file_activity,
    format_session_file_activity_summary,
)


SESSION = "20260928_101500_a1b2c3"
READ_OK = json.dumps({"content": "1|x", "total_lines": 1, "is_binary": False, "is_image": False})
WRITE_OK = json.dumps({"bytes_written": 12, "files_modified": ["ignored"]})
WRITE_FAILED = json.dumps({"error": "write refused: file changed on disk since last read"})
PATCH_OK = json.dumps({"success": True, "diff": "--- a\n+++ b", "files_modified": ["ignored"]})
PATCH_FAILED = json.dumps({"success": False, "error": "old_string not found"})

_SESSIONS_DDL = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL, last_activity_at REAL, "
    "cwd TEXT, git_repo_root TEXT)"
)
_MESSAGES_DDL = (
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
    "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, effect_disposition TEXT, timestamp REAL)"
)


def _call(call_id: str, name: str, arguments: dict | str) -> dict:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "call_id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


class _Store:
    """Builds one fixture ``state.db`` row by row; ids and timestamps ascend."""

    def __init__(self, home: Path, *, messages_ddl: str = _MESSAGES_DDL) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self.path = home / "state.db"
        self._next_id = 1
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(_SESSIONS_DDL)
            connection.execute(messages_ddl)
        connection.close()
        self._has_disposition = "effect_disposition" in messages_ddl

    def session(self, session_id: str, *, cwd: str | None, repo_root: str | None, activity: float = 100.0) -> None:
        self._execute(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, cwd, git_repo_root) VALUES (?, 'tui', 1.0, ?, ?, ?)",
            (session_id, activity, cwd, repo_root),
        )

    def assistant(self, calls: list[dict], *, at: float, session_id: str = SESSION) -> None:
        self._execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, timestamp) VALUES (?, ?, 'assistant', '', ?, ?)",
            (self._take_id(), session_id, json.dumps(calls), at),
        )

    def result(
        self, call_id: str, name: str, content: str, *, at: float, disposition: str | None = None, session_id: str = SESSION
    ) -> None:
        if self._has_disposition:
            self._execute(
                "INSERT INTO messages (id, session_id, role, content, tool_call_id, tool_name, effect_disposition, timestamp) "
                "VALUES (?, ?, 'tool', ?, ?, ?, ?, ?)",
                (self._take_id(), session_id, content, call_id, name, disposition, at),
            )
        else:
            self._execute(
                "INSERT INTO messages (id, session_id, role, content, tool_call_id, tool_name, timestamp) "
                "VALUES (?, ?, 'tool', ?, ?, ?, ?)",
                (self._take_id(), session_id, content, call_id, name, at),
            )

    def _take_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _execute(self, sql: str, params: tuple) -> None:
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(sql, params)
        connection.close()


def _activity(payload: dict) -> dict[str, list[tuple[str, str, int]]]:
    return {
        entry["path"]: [(item["operation"], item["outcome"], item["calls"]) for item in entry["activity"]]
        for entry in payload["files"]
    }


class SessionFileActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / ".hermes"
        self.root = Path(self._tmp.name) / "repo"
        self.store = _Store(self.home)

    def _in_root(self, *parts: str) -> str:
        return str(self.root.joinpath(*parts))

    def test_single_file_calls_carry_operation_path_time_and_outcome(self) -> None:
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        self.store.assistant(
            [
                _call("c1", "read_file", {"path": self._in_root("src", "app.py")}),
                _call("c2", "write_file", {"path": "docs/notes.md", "content": "body never read"}),
            ],
            at=60.0,
        )
        self.store.result("c1", "read_file", READ_OK, at=61.0)
        self.store.result("c2", "write_file", WRITE_OK, at=62.0)
        self.store.assistant(
            [_call("c3", "patch", {"path": self._in_root("src", "app.py"), "old_string": "a", "new_string": "b"})],
            at=120.0,
        )
        self.store.result("c3", "patch", PATCH_OK, at=121.0)

        payload = build_session_file_activity(self.home, SESSION)

        self.assertEqual(payload["schema_version"], SESSION_FILE_ACTIVITY_SCHEMA_VERSION)
        self.assertEqual(payload["source"]["session_id"], SESSION)
        self.assertEqual(payload["workspace"], {"basis": "git_repo_root", "established": True})
        self.assertEqual(
            _activity(payload),
            {
                "src/app.py": [("read", "succeeded", 1), ("update", "succeeded", 1)],
                "docs/notes.md": [("write", "succeeded", 1)],
            },
        )
        # Files touched at the same moment order by path.
        self.assertEqual([entry["path"] for entry in payload["files"]], ["docs/notes.md", "src/app.py"])
        app = payload["files"][1]
        self.assertEqual((app["first_at"], app["last_at"]), ("1970-01-01T00:01:00Z", "1970-01-01T00:02:00Z"))
        self.assertEqual(
            [(item["first_at"], item["last_at"]) for item in app["activity"]],
            [("1970-01-01T00:01:00Z", "1970-01-01T00:01:00Z"), ("1970-01-01T00:02:00Z", "1970-01-01T00:02:00Z")],
        )
        self.assertEqual(payload["calls"]["by_tool"], {"read_file": 1, "write_file": 1, "patch": 1})
        self.assertEqual(payload["calls"]["by_outcome"], {"succeeded": 3, "failed": 0, "unknown": 0})
        self.assertTrue(payload["observed"])
        self.assertNotIn("body never read", json.dumps(payload))

    def test_a_multi_file_patch_yields_one_entry_per_declared_file(self) -> None:
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        patch = "\n".join(
            [
                "*** Begin Patch",
                "*** Update File: src/app.py",
                "@@ def main @@",
                "-old",
                "+new",
                "+*** Add File: src/not_a_marker.py",
                "*** Add File: src/new.py",
                "+print('hi')",
                "*** Delete File: src/old.py",
                "*** Move File: src/a.py -> src/b.py",
                "*** End Patch",
            ]
        )
        self.store.assistant([_call("c1", "patch", {"mode": "patch", "patch": patch})], at=60.0)
        self.store.result("c1", "patch", PATCH_OK, at=61.0)

        payload = build_session_file_activity(self.home, SESSION)

        self.assertEqual(
            _activity(payload),
            {
                "src/a.py": [("move_from", "succeeded", 1)],
                "src/app.py": [("update", "succeeded", 1)],
                "src/b.py": [("move_to", "succeeded", 1)],
                "src/new.py": [("add", "succeeded", 1)],
                "src/old.py": [("delete", "succeeded", 1)],
            },
        )
        self.assertEqual(payload["calls"]["total"], 1)

    def test_failed_and_unavailable_outcomes_are_never_successful_writes(self) -> None:
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        self.store.assistant(
            [
                _call("w1", "write_file", {"path": "a.txt", "content": "x"}),
                _call("w2", "write_file", {"path": "b.txt", "content": "x"}),
                _call("w3", "write_file", {"path": "c.txt", "content": "x"}),
                _call("w4", "write_file", {"path": "d.txt", "content": "x"}),
                _call("w5", "write_file", {"path": "e.txt", "content": "x"}),
                _call("w6", "write_file", {"path": "f.txt", "content": "x"}),
                _call("p1", "patch", {"path": "g.txt", "old_string": "a", "new_string": "b"}),
                _call("r1", "read_file", {"path": "h.txt"}),
            ],
            at=60.0,
        )
        self.store.result("w1", "write_file", WRITE_FAILED, at=61.0)
        # Blocked before it ran: Hermes records that the call had no effect.
        self.store.result("w2", "write_file", json.dumps({"bytes_written": 3}), at=61.0, disposition="none")
        # Timed out: the effect is unknown even though the text looks like success.
        self.store.result("w3", "write_file", json.dumps({"bytes_written": 3}), at=61.0, disposition="unknown")
        # w4 has no recorded result at all.
        self.store.result("w5", "write_file", "Wrote the file successfully.", at=61.0)
        self.store.result("w6", "write_file", json.dumps({"verified": True}), at=61.0)
        self.store.result("p1", "patch", PATCH_FAILED, at=61.0)
        self.store.result("r1", "read_file", json.dumps({"error": "File not found"}) + "\n\n[Tool loop warning]", at=61.0)

        payload = build_session_file_activity(self.home, SESSION)

        self.assertEqual(
            _activity(payload),
            {
                "a.txt": [("write", "failed", 1)],
                "b.txt": [("write", "failed", 1)],
                "c.txt": [("write", "unknown", 1)],
                "d.txt": [("write", "unknown", 1)],
                "e.txt": [("write", "unknown", 1)],
                "f.txt": [("write", "unknown", 1)],
                "g.txt": [("update", "failed", 1)],
                "h.txt": [("read", "failed", 1)],
            },
        )
        self.assertEqual(payload["calls"]["by_outcome"], {"succeeded": 0, "failed": 4, "unknown": 4})
        outcomes = {item["outcome"] for entry in payload["files"] for item in entry["activity"]}
        self.assertLessEqual(outcomes, set(OUTCOMES))
        self.assertEqual(OUTCOMES, ("succeeded", "failed", "unknown"))

    def test_unrelated_tools_create_no_entries(self) -> None:
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        self.store.assistant(
            [
                _call("t1", "terminal", {"command": f"cat {self._in_root('secret.txt')}"}),
                _call("t2", "search_files", {"pattern": "x", "path": "src"}),
                _call("t3", "web_extract", {"urls": ["https://example.com/a.py"]}),
                _call("t4", "mcp_fs_read", {"path": "src/app.py"}),
            ],
            at=60.0,
        )
        for call_id, name in (("t1", "terminal"), ("t2", "search_files"), ("t3", "web_extract"), ("t4", "mcp_fs_read")):
            self.store.result(call_id, name, '{"ok": true}', at=61.0)

        payload = build_session_file_activity(self.home, SESSION)

        self.assertEqual(payload["files"], [])
        self.assertEqual(payload["calls"]["total"], 0)
        self.assertEqual(payload["calls"]["results_without_call"], 0)
        self.assertEqual(payload["omitted_paths"]["count"], 0)

    def test_paths_outside_the_workspace_are_counted_and_never_shown(self) -> None:
        outside = str(Path(self._tmp.name) / "elsewhere" / "private.txt")
        sibling = str(Path(self._tmp.name) / "repo-sibling" / "x.py")
        unsafe = {
            "outside": outside,
            "sibling": sibling,
            "traversal": "../escape.txt",
            "home": "~/.ssh/config",
            "url": "https://example.com/a.py",
            "control": "src/evil\x1b[31m.py",
            "oversized": "a/" * 3000,
            "root_itself": str(self.root),
        }
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        calls = [_call(f"r-{name}", "read_file", {"path": value}) for name, value in unsafe.items()]
        calls.append(_call("r-nonstring", "read_file", {"path": 42}))
        calls.append(_call("r-noargs", "read_file", "{not json"))
        calls.append(_call("r-inside", "read_file", {"path": "src/../src/ok.py"}))
        self.store.assistant(calls, at=60.0)
        for call in calls:
            self.store.result(call["id"], "read_file", READ_OK, at=61.0)

        payload = build_session_file_activity(self.home, SESSION)

        self.assertEqual(_activity(payload), {"src/ok.py": [("read", "succeeded", 1)]})
        self.assertEqual(
            payload["omitted_paths"]["by_reason"],
            {
                "outside_workspace": 4,
                "workspace_unknown": 0,
                "relative_without_cwd": 0,
                "home_relative": 1,
                "url_like": 1,
                "control_characters": 1,
                "oversized": 1,
                "malformed": 2,
            },
        )
        self.assertEqual(payload["omitted_paths"]["count"], 10)
        self.assertEqual(tuple(payload["omitted_paths"]["by_reason"]), OMISSION_REASONS)
        rendered = json.dumps(payload) + format_session_file_activity_summary(payload)
        for leaked in ("elsewhere", "private.txt", "repo-sibling", "escape.txt", ".ssh", "example.com", "evil"):
            self.assertNotIn(leaked, rendered)
        self.assertNotIn(self._tmp.name, json.dumps(payload["files"]))

    def test_relative_paths_need_the_session_cwd_and_paths_need_a_workspace(self) -> None:
        self.store.session(SESSION, cwd=None, repo_root=str(self.root))
        self.store.session("no-workspace", cwd=None, repo_root=None, activity=50.0)
        self.store.assistant(
            [_call("c1", "read_file", {"path": "src/app.py"}), _call("c2", "read_file", {"path": self._in_root("x.py")})],
            at=60.0,
        )
        self.store.assistant([_call("c3", "read_file", {"path": self._in_root("x.py")})], at=60.0, session_id="no-workspace")

        rooted = build_session_file_activity(self.home, SESSION)
        unrooted = build_session_file_activity(self.home, "no-workspace")
        overridden = build_session_file_activity(self.home, "no-workspace", workspace=str(self.root))

        self.assertEqual(_activity(rooted), {"x.py": [("read", "unknown", 1)]})
        self.assertEqual(rooted["omitted_paths"]["by_reason"]["relative_without_cwd"], 1)
        self.assertEqual(unrooted["workspace"], {"basis": "unknown", "established": False})
        self.assertEqual(unrooted["files"], [])
        self.assertEqual(unrooted["omitted_paths"]["by_reason"]["workspace_unknown"], 1)
        self.assertEqual(overridden["workspace"], {"basis": "argument", "established": True})
        self.assertEqual(_activity(overridden), {"x.py": [("read", "unknown", 1)]})

    def test_the_file_list_is_bounded_and_says_so(self) -> None:
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        for index, name in enumerate(("d.py", "c.py", "b.py", "a.py")):
            self.store.assistant([_call(f"c{index}", "read_file", {"path": name})], at=60.0 + index)
            self.store.result(f"c{index}", "read_file", READ_OK, at=60.5 + index)

        payload = build_session_file_activity(self.home, SESSION, max_files=2)
        text = format_session_file_activity_summary(payload)

        # The earliest-touched files are kept, in the order they were first touched.
        self.assertEqual([entry["path"] for entry in payload["files"]], ["d.py", "c.py"])
        self.assertEqual(
            (payload["file_count"], payload["shown_file_count"], payload["max_files"], payload["truncated"], payload["omitted_file_count"]),
            (4, 2, 2, True, 2),
        )
        self.assertEqual(payload["calls"]["total"], 4)
        self.assertIn("truncated: showing 2 of 4 files (--max-files 2); 2 not listed", text)
        with self.assertRaisesRegex(SessionFileActivityError, "--max-files must be at least 1"):
            build_session_file_activity(self.home, SESSION, max_files=0)

    def test_an_unknown_session_and_an_empty_store_are_errors(self) -> None:
        with self.assertRaisesRegex(SessionFileActivityError, "the Hermes state database has no sessions"):
            build_session_file_activity(self.home, "latest")
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        with self.assertRaisesRegex(SessionFileActivityError, "no Hermes session nope"):
            build_session_file_activity(self.home, "nope")
        with self.assertRaisesRegex(SessionFileActivityError, "no Hermes state database"):
            build_session_file_activity(Path(self._tmp.name) / "missing", SESSION)

        # A real session that touched no file is an observation, not an error.
        payload = build_session_file_activity(self.home, "latest")
        self.assertEqual(payload["source"]["session_id"], SESSION)
        self.assertEqual((payload["files"], payload["calls"]["total"]), ([], 0))

    def test_compaction_duplicates_count_once_and_the_first_row_decides(self) -> None:
        self.store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        calls = [
            _call("c1", "write_file", {"path": "a.py", "content": "x"}),
            _call("c2", "patch", {"path": "a.py", "old_string": "a", "new_string": "b"}),
        ]
        self.store.assistant(calls, at=60.0)
        self.store.result("c1", "write_file", WRITE_OK, at=61.0)
        self.store.result("c2", "patch", PATCH_FAILED, at=61.0)
        # A compaction re-persists the assistant row and both results under new
        # ids, the results as placeholders; the originals still decide.
        self.store.assistant(calls, at=300.0)
        self.store.result("c1", "write_file", "[write_file] a.py (12 chars)", at=301.0)
        self.store.result("c2", "patch", PATCH_OK, at=301.0)
        # A result whose call row is gone cannot be attributed to a file.
        self.store.result("c9", "read_file", READ_OK, at=302.0)

        payload = build_session_file_activity(self.home, SESSION)

        self.assertEqual(_activity(payload), {"a.py": [("write", "succeeded", 1), ("update", "failed", 1)]})
        self.assertEqual(payload["files"][0]["last_at"], "1970-01-01T00:01:00Z")
        self.assertEqual(payload["calls"]["total"], 2)
        self.assertEqual(payload["calls"]["results_without_call"], 1)

    def test_a_store_without_effect_disposition_still_reads(self) -> None:
        home = Path(self._tmp.name) / "old-hermes"
        store = _Store(home, messages_ddl=_MESSAGES_DDL.replace(" effect_disposition TEXT,", ""))
        store.session(SESSION, cwd=str(self.root), repo_root=str(self.root))
        store.assistant([_call("c1", "write_file", {"path": "a.py", "content": "x"})], at=60.0)
        store.result("c1", "write_file", WRITE_OK, at=61.0)

        payload = build_session_file_activity(home, SESSION)

        self.assertEqual(_activity(payload), {"a.py": [("write", "succeeded", 1)]})


class SessionFileActivityCliTests(unittest.TestCase):
    def test_plain_text_by_default_json_on_request_and_the_store_is_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            root = Path(tmp) / "repo"
            store = _Store(home)
            store.session(SESSION, cwd=str(root), repo_root=str(root))
            store.assistant([_call("c1", "read_file", {"path": str(root / "src" / "app.py")})], at=60.0)
            store.result("c1", "read_file", READ_OK, at=61.0)
            before = store.path.read_bytes()
            common = ["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(home)]
            args = [*common, "quality-evidence", "file-activity", "--hermes-session", "latest"]

            text_status, text_stdout, text_stderr = run_cli(args, output_json=False)
            flag_status, flag_stdout, flag_stderr = run_cli([*args, "--json"], output_json=False)
            env_status, env_stdout, env_stderr = run_cli(args)
            missing_status, missing_stdout, missing_stderr = run_cli(
                [*common, "quality-evidence", "file-activity", "--hermes-session", "nope"]
            )

            self.assertEqual(store.path.read_bytes(), before)

        self.assertEqual((text_status, flag_status, env_status), (0, 0, 0), (text_stderr, flag_stderr, env_stderr))
        self.assertIn(f"OMH session file activity: session {SESSION}", text_stdout)
        self.assertIn("  src/app.py: read succeeded x1", text_stdout)
        self.assertIn("Boundary", text_stdout)
        self.assertFalse(text_stdout.lstrip().startswith("{"))
        payload = json.loads(flag_stdout)
        self.assertEqual(payload["schema_version"], SESSION_FILE_ACTIVITY_SCHEMA_VERSION)
        self.assertEqual(json.loads(env_stdout), payload)
        self.assertEqual(missing_status, 2)
        self.assertIn("no Hermes session nope", missing_stderr)
        self.assertEqual(missing_stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
