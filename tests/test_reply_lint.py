"""The reply lint: the QA evidence for OMH's reply rules.

The rules ship as prompt text (skill tails, the rail's Reply Language And Host
Voice and Turn Ending sections, the awareness primers). These tests pin what
the lint reports on the replies the owner actually read, the negatives that
keep everyday words out of it, the carve-out for a user who names a term,
and the read-only Hermes session source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from omh.quality import reply_lint
from omh.quality.reply_lint import (
    FINDING_KINDS,
    REPLY_LINT_SCHEMA_VERSION,
    build_reply_lint,
    closing_has_question,
    closing_paragraph,
    format_reply_lint_summary,
    record_term_vocabulary,
    summarize_reply_lints,
)
from omh.quality.reply_lint_source import ReplySourceError, hermes_session_replies


# The reply the owner pasted on 2026-09-23: a stop declared as a refusal.
OWNER_REFUSAL_REPLY = (
    "재발 방지 기준을 정리했습니다.\n\n"
    "나는 여기서 추가 수정·force-push·merge를 하지 않을게. (2/2)"
)

# The same stop, ended the way the Turn Ending rule asks.
OWNER_REFUSAL_REPLY_REPAIRED = (
    "재발 방지 기준을 정리했습니다.\n\n"
    "추가 수정과 force-push, merge는 아직 안 했습니다. 지금 revert PR을 열까요, 아니면 기여자 수정을 기다릴까요?"
)

CLEAN_ENGLISH_REPLY = (
    "Merged #1829 and closed the issue. The revert PR is prepared, not run yet; "
    "the child report is not checked.\n\n"
    "Shall I open the revert PR, or wait for the contributor's fix?"
)


def _kinds(payload: dict) -> list[tuple[str, str]]:
    return [(item["kind"], item["match"]) for item in payload["findings"]]


class ClosingTests(unittest.TestCase):
    def test_owner_refusal_reply_is_a_refusal_closer_without_a_question(self) -> None:
        payload = build_reply_lint(OWNER_REFUSAL_REPLY)

        self.assertFalse(payload["ok"])
        self.assertEqual(_kinds(payload), [("refusal_closer", "하지 않을게")])
        self.assertEqual(payload["findings"][0]["line"], 3)
        self.assertFalse(payload["closing"]["has_question"])
        self.assertEqual(payload["closing"]["kind"], "refusal_closer")

    def test_the_repaired_owner_reply_is_clean(self) -> None:
        payload = build_reply_lint(OWNER_REFUSAL_REPLY_REPAIRED)

        self.assertTrue(payload["ok"], payload["findings"])
        self.assertTrue(payload["closing"]["has_question"])

    def test_english_refusal_closer(self) -> None:
        payload = build_reply_lint("The revert PR is ready.\n\nI will not merge or force-push here.")

        self.assertEqual(_kinds(payload), [("refusal_closer", "I will not")])

    def test_a_refusal_followed_by_the_question_is_not_a_closer(self) -> None:
        payload = build_reply_lint(
            "I won't merge this myself.\n\nShall I open the revert PR, or wait for the contributor's fix?"
        )

        self.assertTrue(payload["ok"], payload["findings"])

    def test_a_refusal_earlier_in_the_reply_is_not_the_closing(self) -> None:
        # Only the closing paragraph decides; a refusal in the body followed by a
        # closing question is the rule's own shape ("stated as the option it leaves open").
        payload = build_reply_lint(
            "I will not force-push over the contributor's branch.\n\nDo you want the revert PR opened now?"
        )

        self.assertTrue(payload["ok"], payload["findings"])

    def test_live_korean_closers_observed_in_the_owner_sessions(self) -> None:
        # Shapes read out of ~/.hermes/state.db on 2026-09-23, trimmed.
        for reply, match in (
            (
                "이번 알림은 실제 종료를 확인해 주지 못했습니다.\n\n"
                "실제 종료를 확인하기 전까지 복구 완료로 처리하지 않겠습니다.",
                "하지 않겠",
            ),
            (
                "정리:\n\n- 새 조사나 통합 작업은 시작하지 않음\n- 현재 실행 중 subagent: 없음\n- 설정에는 영향 없음",
                "하지 않음",
            ),
        ):
            with self.subTest(match=match):
                self.assertEqual(_kinds(build_reply_lint(reply)), [("refusal_closer", match)])

    def test_a_decision_left_without_a_question(self) -> None:
        payload = build_reply_lint("Both options are viable.\n\nThis needs your approval before I continue.")

        self.assertEqual(_kinds(payload), [("decision_without_question", "needs your approval")])
        self.assertEqual(payload["closing"]["kind"], "decision_without_question")

    def test_a_korean_question_ending_counts_without_a_question_mark(self) -> None:
        self.assertTrue(closing_has_question("어느 쪽으로 진행할까요"))
        self.assertTrue(closing_has_question("revert PR을 열까요?"))
        self.assertFalse(closing_has_question("여기서 멈추겠습니다."))

    def test_closing_paragraph_is_the_last_block_and_a_list_stays_one_block(self) -> None:
        text = "first\n\nsecond\n\n- a\n- b\n- c"
        self.assertEqual(closing_paragraph(text), "- a\n- b\n- c")
        self.assertEqual(closing_paragraph("   "), "")


class RecordTermTests(unittest.TestCase):
    def test_record_terms_in_an_english_reply(self) -> None:
        payload = build_reply_lint(
            "This is an evidence-bounded surface; the child report is prepared_not_observed, so the handoff waits."
        )

        self.assertEqual(
            _kinds(payload),
            [
                ("record_term_leak", "evidence-bounded"),
                ("record_term_leak", "prepared_not_observed"),
                ("record_term_leak", "handoff"),
            ],
        )

    def test_the_longest_term_wins_so_one_token_is_one_finding(self) -> None:
        payload = build_reply_lint("status: prepared_not_observed")

        self.assertEqual(_kinds(payload), [("record_term_leak", "prepared_not_observed")])
        self.assertEqual(payload["counts"]["record_term_leak"], 1)

    def test_korean_renderings_read_in_live_replies(self) -> None:
        payload = build_reply_lint("한 파일이 두 표면을 서빙합니다. 레인을 열었습니다.")

        self.assertEqual(_kinds(payload), [("record_term_leak", "표면"), ("record_term_leak", "레인")])

    def test_everyday_words_that_contain_a_term_do_not_match(self) -> None:
        for text in (
            "표면적으로는 같아 보입니다.",  # superficially
            "브레인스토밍을 먼저 했습니다.",  # brain
            "The function surfaces the error to the caller.",
            "Take the left lane after the bridge.",
            "the wrapper function returns early",
            "the run was not observed by anyone",  # plain words, not the token
        ):
            with self.subTest(text=text):
                self.assertEqual(build_reply_lint(text)["findings"], [])

    def test_the_plain_substitutes_from_the_rail_are_clean(self) -> None:
        payload = build_reply_lint(CLEAN_ENGLISH_REPLY)

        self.assertTrue(payload["ok"], payload["findings"])
        self.assertEqual(payload["finding_count"], 0)

    def test_a_term_the_user_named_is_carved_out_not_counted(self) -> None:
        payload = build_reply_lint(
            "prepared_not_observed means the handoff was prepared and not run yet.",
            user_text="what does prepared_not_observed mean here?",
        )

        self.assertEqual(payload["carved_out_terms"], ["prepared_not_observed"])
        # The user did not name `handoff`, so that one still counts.
        self.assertEqual(_kinds(payload), [("record_term_leak", "handoff")])

    def test_the_vocabulary_is_the_rail_list_plus_the_observed_korean_forms(self) -> None:
        vocabulary = record_term_vocabulary()
        for term in ("prepared_not_observed", "not_observed", "evidence boundary", "handoff", "표면", "레인"):
            self.assertIn(term, vocabulary)
        self.assertNotIn("surface", vocabulary)
        self.assertNotIn("lane", vocabulary)
        self.assertNotIn("wrapper", vocabulary)


class AwarenessLineTests(unittest.TestCase):
    def test_quoted_awareness_and_boundary_lines(self) -> None:
        payload = build_reply_lint(
            "[OMH Awareness] plan-first lane is active.\nBoundary: this shows X and not Y.\n\n다음으로 넘어갈까요?"
        )

        self.assertEqual(
            _kinds(payload),
            [("awareness_line_quoted", "[OMH Awareness]"), ("awareness_line_quoted", "Boundary:")],
        )
        self.assertEqual([item["line"] for item in payload["findings"]], [1, 2])

    def test_the_word_boundary_mid_sentence_is_not_a_quoted_line(self) -> None:
        self.assertEqual(build_reply_lint("The boundary: nothing here was merged.")["findings"], [])


_SRC = Path(__file__).resolve().parents[1] / "src"
_LINT_SOURCE = _SRC / "quality" / "reply_lint.py"
# `[OMH ...]` is a placeholder in the bundle's own comments, not a head it writes.
_TAG_PLACEHOLDERS = frozenset({"[OMH ...]"})


def _source_texts(root: Path) -> list[str]:
    return [
        path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if path != _LINT_SOURCE and "__pycache__" not in path.parts
    ]


def _derived_tags() -> set[str]:
    tags: set[str] = set()
    for text in _source_texts(_SRC):
        tags.update(re.findall(r"\[OMH(?: [^\]\n\"'{\\]*)?\]", text))
    return tags - _TAG_PLACEHOLDERS


def _derived_tag_prefixes() -> set[str]:
    prefixes: set[str] = set()
    for text in _source_texts(_SRC):
        prefixes.update(re.findall(r"\[OMH [A-Za-z]+:(?= ?\{)", text))
    return prefixes


_SCHEMA_ID = re.compile(r"(?<![A-Za-z0-9_/.-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+/v[0-9]+)(?![0-9])")


def _derived_schema_ids() -> set[str]:
    """The lint's declared scope rule, re-derived: `omh_` ids the bundle spells,
    plus every id a shipped plugin tool schema names."""
    from omh.plugin_bundle.omh.tools import builtin_tool_schemas

    bundle: set[str] = set()
    for text in _source_texts(_SRC / "plugin_bundle"):
        bundle.update(_SCHEMA_ID.findall(text))
    in_schemas = set(_SCHEMA_ID.findall(json.dumps(builtin_tool_schemas())))
    return {schema_id for schema_id in bundle if schema_id.startswith("omh_")} | in_schemas


class OmhHeadTests(unittest.TestCase):
    """Every `[OMH ...]` head OMH writes is caught, and nothing that merely looks like one."""

    def test_the_tag_list_is_every_head_the_source_writes(self) -> None:
        derived = _derived_tags()
        listed = set(reply_lint._AWARENESS_TAGS)
        self.assertEqual(
            sorted(derived - listed), [], "add these heads to _AWARENESS_TAGS in src/quality/reply_lint.py"
        )
        self.assertEqual(sorted(listed - derived), [], "no source writes these heads any more; remove them")
        self.assertEqual(set(reply_lint._AWARENESS_TAG_PREFIXES), _derived_tag_prefixes())

    def test_each_head_quoted_into_a_reply_is_flagged(self) -> None:
        for tag in reply_lint._AWARENESS_TAGS:
            with self.subTest(tag=tag):
                payload = build_reply_lint(f"Done.\n\n{tag} something for the model.")
                self.assertEqual(_kinds(payload), [("awareness_line_quoted", tag)])
        payload = build_reply_lint("[OMH Role: reviewer] Review the diff.")
        self.assertEqual(_kinds(payload), [("awareness_line_quoted", "[OMH Role:]")])

    def test_the_old_honest_stop_sentence_is_flagged(self) -> None:
        old = (
            "[OMH] This turn started remote work and armed nothing to wake the session "
            "when it finishes, so the session has stopped here: it resumes only when "
            "someone sends it a message."
        )
        self.assertEqual(_kinds(build_reply_lint(f"Pushed.\n\n{old}")), [("awareness_line_quoted", "[OMH]")])

    def test_things_that_look_like_a_head_are_not_one(self) -> None:
        for text in (
            "See the [OMH README](https://github.com/rlaope/oh-my-hermes#readme) for install steps.",
            "Fixed in [OMH 2.0.2].",
            "The [OMH Awareness](https://example.com/awareness) page explains it.",
            "[OMH](https://github.com/rlaope/oh-my-hermes) is installed.",
        ):
            with self.subTest(text=text):
                self.assertEqual(build_reply_lint(text)["findings"], [])

    def test_a_head_the_user_asked_about_is_still_counted(self) -> None:
        # Hermes stores OMH's injected context inside the user turn, so a
        # carve-out on the user text would excuse every head the model saw.
        payload = build_reply_lint(
            "[OMH plan todo] is the plan line.", user_text="what does [OMH plan todo] mean?"
        )
        self.assertEqual(_kinds(payload), [("awareness_line_quoted", "[OMH plan todo]")])


class SchemaIdTests(unittest.TestCase):
    """OMH schema ids are exact literals under a declared scope rule, never a shape."""

    def test_the_id_list_is_the_scope_rule_re_derived(self) -> None:
        derived = _derived_schema_ids()
        listed = set(reply_lint._SCHEMA_ID_TERMS)
        self.assertEqual(sorted(derived - listed), [], "add these ids to _SCHEMA_ID_TERMS in src/quality/reply_lint.py")
        self.assertEqual(sorted(listed - derived), [], "these ids left the scope rule; remove them")
        self.assertEqual(len(listed), len(reply_lint._SCHEMA_ID_TERMS), "duplicate id")

    def test_each_id_in_a_reply_is_flagged(self) -> None:
        for schema_id in reply_lint._SCHEMA_ID_TERMS:
            with self.subTest(schema_id=schema_id):
                payload = build_reply_lint(f"The result came back as {schema_id} with no error.")
                self.assertEqual(_kinds(payload), [("record_term_leak", schema_id)])

    def test_the_ids_measured_in_live_replies_are_in_scope(self) -> None:
        for schema_id in ("omh_todo_result/v1", "omh_run_summary/v1", "route_question/v1"):
            self.assertIn(schema_id, reply_lint._SCHEMA_ID_TERMS)

    def test_an_id_is_exact(self) -> None:
        self.assertEqual(build_reply_lint("omh_todo/v12 and xomh_todo/v1")["findings"], [])
        self.assertEqual(
            _kinds(build_reply_lint("stored as omh_todo_result/v1.")),
            [("record_term_leak", "omh_todo_result/v1")],
        )

    def test_ordinary_version_references_are_clean(self) -> None:
        for text in (
            "Call https://api.openai.com/v1/chat/completions with the key.",
            "GET /api/v2/users returns the list.",
            "apiVersion: apps/v1",
            "The governance/v2 policy applies here.",
            "Upgrade to version v2 first.",
        ):
            with self.subTest(text=text):
                self.assertEqual(build_reply_lint(text)["findings"], [])

    def test_a_user_who_names_an_id_gets_it_explained(self) -> None:
        payload = build_reply_lint(
            "omh_todo_result/v1 is the shape of a todo result.", user_text="what is omh_todo_result/v1?"
        )
        self.assertTrue(payload["ok"], payload["findings"])
        self.assertEqual(payload["carved_out_terms"], ["omh_todo_result/v1"])


class AuditTermTests(unittest.TestCase):
    """The 2026-09-28 audit's terms: qualified compounds in, everyday words out."""

    def test_the_qualified_terms_are_flagged(self) -> None:
        for term in ("route_question", "closure receipt", "fanout dispatch"):
            with self.subTest(term=term):
                self.assertEqual(
                    _kinds(build_reply_lint(f"Next I will check the {term} for this change.")),
                    [("record_term_leak", term)],
                )

    def test_the_everyday_words_inside_them_are_clean(self) -> None:
        for text in (
            "I read the contract before signing it.",
            "Keep the store receipt for the refund.",
            "Merge into the traffic lane after the exit.",
            "The fan-out of this function is three callers.",
            "Update the goal ledger in the finance sheet.",
            "The route question is whether to go north.",
        ):
            with self.subTest(text=text):
                self.assertEqual(build_reply_lint(text)["findings"], [])


def _rail_text() -> str:
    from omh.skills.packaging import builtin_skill_reference_templates

    for template in builtin_skill_reference_templates():
        if template.relative_path.endswith("skill-common-rail.md"):
            return template.content
    raise AssertionError("no skill-common-rail.md reference template")


def _rail_vocabulary() -> list[str]:
    line = next(
        line for line in _rail_text().splitlines() if "record vocabulary is for records" in line
    )
    listed = line.partition("the sentence the user reads:")[2].partition(". In the reply")[0]
    return re.findall(r"`([^`]+)`", listed)


def _whole_word(needle: str, haystack: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(needle.lower()) + r"(?![A-Za-z0-9_])", haystack.lower()) is not None


class RailVocabularySyncTests(unittest.TestCase):
    """The rail tells the model which words stay in records; the lint checks the same words."""

    def test_every_rail_word_is_something_the_lint_checks(self) -> None:
        english = reply_lint._ENGLISH_RECORD_TERMS
        for word in _rail_vocabulary():
            with self.subTest(word=word):
                if word in reply_lint._SCHEMA_ID_TERMS:
                    continue
                self.assertTrue(
                    any(_whole_word(word, term) for term in english),
                    f"the rail lists `{word}` but no _ENGLISH_RECORD_TERMS entry contains it",
                )

    def test_every_lint_term_is_named_in_the_rail(self) -> None:
        rail = [word for word in _rail_vocabulary() if word not in reply_lint._SCHEMA_ID_TERMS]
        for term in reply_lint._ENGLISH_RECORD_TERMS:
            with self.subTest(term=term):
                self.assertTrue(
                    any(_whole_word(word, term) for word in rail),
                    f"the lint checks `{term}` but the rail's vocabulary line names no word in it",
                )

    def test_the_rail_names_a_schema_id_the_lint_checks_and_the_say_relay(self) -> None:
        self.assertTrue(any(word in reply_lint._SCHEMA_ID_TERMS for word in _rail_vocabulary()))
        rail = _rail_text()
        self.assertIn("`say` field", rail)
        self.assertIn("in their language and your own words", rail)


class PayloadTests(unittest.TestCase):
    def test_payload_shape_and_determinism(self) -> None:
        first = build_reply_lint(OWNER_REFUSAL_REPLY)
        second = build_reply_lint(OWNER_REFUSAL_REPLY)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], REPLY_LINT_SCHEMA_VERSION)
        self.assertEqual(set(first["counts"]), set(FINDING_KINDS))
        self.assertIn("not execution, review, CI, or merge evidence", first["claim_boundary"])
        for finding in first["findings"]:
            self.assertEqual(set(finding), {"kind", "match", "line", "column", "excerpt"})

    def test_summary_folds_counts_and_keeps_the_boundary(self) -> None:
        payload = summarize_reply_lints(
            [build_reply_lint(OWNER_REFUSAL_REPLY), build_reply_lint(CLEAN_ENGLISH_REPLY)],
            source={"kind": "text_file"},
        )

        self.assertEqual(payload["reply_count"], 2)
        self.assertEqual(payload["finding_count"], 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["counts"]["refusal_closer"], 1)
        self.assertEqual(payload["source"], {"kind": "text_file"})
        text = format_reply_lint_summary(payload)
        self.assertIn("1 finding across 2 replies", text)
        self.assertIn("refusal_closer `하지 않을게`", text)
        self.assertIn("Reply 2: clean", text)
        self.assertIn("Boundary", text)

    def test_an_empty_batch_is_clean_and_says_so(self) -> None:
        payload = summarize_reply_lints([])

        self.assertTrue(payload["ok"])
        self.assertIn("clean across 0 replies", format_reply_lint_summary(payload))


# A skill_view result carries the SKILL.md frontmatter; OMH's own skills are
# the ones whose description starts with the catalog's `[omh] ` prefix, which
# includes the `ulw-*` display names.
OMH_PLAN_SKILL_VIEW = json.dumps(
    {"name": "omh-plan", "description": "[omh] Hermes Plan workflow: turn a goal into a plan.", "success": True}
)
ULW_WORK_SKILL_VIEW = json.dumps(
    {"name": "ulw-work", "description": "[omh] Ultrawork: carry a goal to done.", "success": True}
)
OTHER_SKILL_VIEW = json.dumps({"name": "reviewer", "description": "Review code for defects.", "success": True})
OMH_FILE_SKILL_VIEW = json.dumps({"name": "omh-plan", "file": "references/plan.md", "content": "notes", "success": True})
# What a compaction leaves in place of a skill_view result: the name, no description.
OMH_PLACEHOLDER_SKILL_VIEW = "[skill_view] name=omh-routing (12,243 chars) [SKILL_PRUNED]"


def _write_state_db(home: Path, *, session_id: str = "20260923_150513_4286a5") -> Path:
    """A five-session store shaped like Hermes' own: the reply-lint rows (ids 1-8)
    plus the tool rows the session-usage reader counts (ids 9 and up). The
    sessions table carries the host's `ended_at`, `archived` and `hidden`
    columns so an archived, hidden session can be shown to count."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state.db"
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL, last_activity_at REAL, "
            "ended_at REAL, archived INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
            "tool_name TEXT, tool_call_id TEXT, timestamp REAL)"
        )
        sessions = [
            (session_id, "tui", 1.0, 5.0, None, 0, 0),
            ("older", "cli", 0.5, 2.0, None, 0, 0),
            ("desk", "desktop", 3.0, 4.0, None, 0, 0),
            ("untagged", None, 1.5, None, None, 0, 0),
            ("archived", "cli", 0.2, 0.3, 0.3, 1, 1),  # archived and hidden after using OMH
        ]
        connection.executemany(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, ended_at, archived, hidden) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            sessions,
        )
        rows = [
            (1, session_id, "user", "what does prepared_not_observed mean?", None, None),
            (2, session_id, "assistant", "prepared_not_observed means prepared, not run yet.", None, None),
            (3, session_id, "tool", '{"ok": true}', "read_file", None),  # no tool_call_id: counts by row
            (4, session_id, "user", "merge it", None, None),
            (5, session_id, "assistant", "[PRIOR CONTEXT — for reference only] old summary", None, None),
            (6, session_id, "assistant", OWNER_REFUSAL_REPLY, None, None),
            (7, session_id, "assistant", OWNER_REFUSAL_REPLY, None, None),  # compaction re-record
            (8, "older", "assistant", "unrelated", None, None),
            (9, session_id, "tool", '{"ok": true}', "omh_status", "c1"),
            (10, session_id, "tool", '{"ok": true}', "omh_status", "c1"),  # compaction re-persists the row
            (11, session_id, "tool", '{"ok": true}', "omh_recommend", "c2"),
            (12, session_id, "tool", OMH_PLAN_SKILL_VIEW, "skill_view", "c3"),
            (13, session_id, "tool", OTHER_SKILL_VIEW, "skill_view", "c4"),
            (14, session_id, "tool", OMH_FILE_SKILL_VIEW, "skill_view", "c5"),  # file shape: no description
            (15, "older", "tool", '{"ok": true}', "omh_todo", "c6"),
            (16, "desk", "tool", "loaded [omh] guidance as text", "skill_view", "c7"),  # non-JSON fallback
            (17, "desk", "tool", "plain text", "skill_view", "c8"),
            (18, "untagged", "tool", '{"ok": true}', "terminal", "c9"),
            (19, session_id, "tool", ULW_WORK_SKILL_VIEW, "skill_view", "c10"),
            (20, "archived", "tool", '{"ok": true}', "omh_status", "c11"),
            (21, "desk", "tool", '{"ok": true}', "omh_todo", ""),  # empty tool_call_id: counts by row, like NULL
            (22, "desk", "tool", '{"ok": true}', "omh_hud", ""),
            (23, "desk", "tool", OMH_PLACEHOLDER_SKILL_VIEW, "skill_view", "c12"),  # name-only: catalog decides
            (24, "untagged", "assistant", "untagged reply", None, None),
        ]
        connection.executemany(
            "INSERT INTO messages (id, session_id, role, content, tool_name, tool_call_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, 0.0)",
            rows,
        )
    connection.close()
    return path


class HermesSessionSourceTests(unittest.TestCase):
    def test_replies_pair_with_the_user_message_and_skip_prior_context_and_duplicates(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            read = hermes_session_replies(home, "20260923_150513_4286a5", last=5)

        self.assertEqual(read["session_id"], "20260923_150513_4286a5")
        self.assertEqual(
            [(item["message_id"], item["user_text"]) for item in read["replies"]],
            [(2, "what does prepared_not_observed mean?"), (6, "merge it")],
        )

    def test_latest_resolves_to_the_most_recently_active_session_and_last_bounds_the_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            read = hermes_session_replies(home, "latest", last=1)

        self.assertEqual(read["session_id"], "20260923_150513_4286a5")
        self.assertEqual([item["message_id"] for item in read["replies"]], [6])

    def test_missing_database_unknown_session_and_bad_last_are_named_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            with self.assertRaisesRegex(ReplySourceError, "no Hermes state database"):
                hermes_session_replies(home, "latest")
            _write_state_db(home)
            with self.assertRaisesRegex(ReplySourceError, "has no user or assistant messages"):
                hermes_session_replies(home, "nope")
            with self.assertRaisesRegex(ReplySourceError, "--last"):
                hermes_session_replies(home, "latest", last=0)

    def test_the_database_is_not_written(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            before = path.read_bytes()

            hermes_session_replies(home, "latest", last=3)

            self.assertEqual(path.read_bytes(), before)

    def test_source_scopes_latest_to_that_surface_and_checks_an_explicit_id(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)

            scoped = hermes_session_replies(home, "latest", source="cli")
            explicit = hermes_session_replies(home, "20260923_150513_4286a5", source="tui")
            with self.assertRaisesRegex(ReplySourceError, "session 20260923_150513_4286a5 has source tui, not cli"):
                hermes_session_replies(home, "20260923_150513_4286a5", source="cli")
            with self.assertRaisesRegex(ReplySourceError, "no Hermes session with source slack"):
                hermes_session_replies(home, "latest", source="slack")

        self.assertEqual(scoped["session_id"], "older")
        self.assertEqual([item["message_id"] for item in scoped["replies"]], [8])
        self.assertEqual(explicit["session_id"], "20260923_150513_4286a5")

    def test_source_none_selects_untagged_sessions_and_an_orphan_id_cannot_be_checked(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            connection = sqlite3.connect(path)
            with connection:
                # Messages whose session row is gone (a deleted session) are readable
                # without a filter and refused with one: there is no tag to check.
                connection.execute(
                    "INSERT INTO messages (id, session_id, role, content, timestamp) "
                    "VALUES (40, 'orphan', 'assistant', 'orphan reply', 0.0)"
                )
            connection.close()

            latest_untagged = hermes_session_replies(home, "latest", source="(none)")
            explicit_untagged = hermes_session_replies(home, "untagged", source="(none)")
            orphan = hermes_session_replies(home, "orphan")
            with self.assertRaisesRegex(ReplySourceError, r"session untagged has source \(none\), not cli"):
                hermes_session_replies(home, "untagged", source="cli")
            with self.assertRaisesRegex(ReplySourceError, "no Hermes session orphan to check --source cli against"):
                hermes_session_replies(home, "orphan", source="cli")

        self.assertEqual(latest_untagged["session_id"], "untagged")
        self.assertEqual([item["message_id"] for item in latest_untagged["replies"]], [24])
        self.assertEqual(explicit_untagged["session_id"], "untagged")
        self.assertEqual([item["message_id"] for item in orphan["replies"]], [40])


class ReplyLintCliTests(unittest.TestCase):
    def test_text_file_json_payload_and_a_finding_exits_one(self) -> None:
        with TemporaryDirectory() as tmp:
            reply = Path(tmp) / "reply.txt"
            reply.write_text(OWNER_REFUSAL_REPLY, encoding="utf-8")

            status, stdout, stderr = run_cli(["quality-evidence", "reply-lint", "--text-file", str(reply)])

        self.assertEqual(status, 1, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], REPLY_LINT_SCHEMA_VERSION)
        self.assertEqual(payload["source"], {"kind": "text_file", "path": str(reply)})
        self.assertEqual(payload["counts"]["refusal_closer"], 1)
        self.assertFalse(payload["ok"])

    def test_stdin_with_the_user_text_carve_out_exits_zero_when_clean(self) -> None:
        with TemporaryDirectory() as tmp:
            asked = Path(tmp) / "user.txt"
            asked.write_text("what does prepared_not_observed mean?", encoding="utf-8")

            status, stdout, stderr = run_cli(
                ["quality-evidence", "reply-lint", "--stdin", "--user-text-file", str(asked)],
                stdin_text="prepared_not_observed means prepared, not run yet. Shall I run it?",
            )

        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["replies"][0]["carved_out_terms"], ["prepared_not_observed"])

    def test_hermes_session_source_is_read_only_and_plain_text_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            path = _write_state_db(home)
            before = path.read_bytes()

            status, stdout, stderr = run_cli(
                [
                    "--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(home),
                    "quality-evidence", "reply-lint", "--hermes-session", "latest", "--last", "2",
                ],
                output_json=False,
            )

            self.assertEqual(path.read_bytes(), before)

        self.assertEqual(status, 1, stderr)
        self.assertFalse(stdout.lstrip().startswith("{"))
        self.assertIn("OMH reply lint: 1 finding across 2 replies", stdout)
        self.assertIn("named by the user, not counted: prepared_not_observed", stdout)
        self.assertIn("not execution, review, CI, or merge evidence", stdout)

    def test_source_filter_is_recorded_and_applies_only_to_a_hermes_session(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            _write_state_db(home)
            common = ["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(home)]

            status, stdout, stderr = run_cli(
                [*common, "quality-evidence", "reply-lint", "--hermes-session", "latest", "--source", "cli"]
            )
            stdin_status, stdin_stdout, stdin_stderr = run_cli(
                [*common, "quality-evidence", "reply-lint", "--stdin", "--source", "cli"], stdin_text="fine?"
            )

        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(
            payload["source"],
            {"kind": "hermes_session", "session_id": "older", "last": 1, "source_filter": "cli"},
        )
        self.assertEqual(stdin_status, 2)
        self.assertIn("--source applies only to --hermes-session", stdin_stderr)
        self.assertEqual(stdin_stdout.strip(), "")

    def test_a_missing_database_is_an_error_not_a_clean_result(self) -> None:
        with TemporaryDirectory() as tmp:
            status, stdout, stderr = run_cli(
                [
                    "--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(Path(tmp) / ".hermes"),
                    "quality-evidence", "reply-lint", "--hermes-session", "latest",
                ]
            )

        self.assertNotEqual(status, 0)
        self.assertIn("no Hermes state database", stderr)
        self.assertEqual(stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
