from __future__ import annotations

import json
import unittest
from unittest import mock

from _cli_harness import run_cli

from omh.mcp import bridge
from omh.routing import chat as chat_module
from omh.routing import recommend as recommend_module
from omh.plugin_bundle.omh.tools import recommend_tool
from omh.routing.chat import public_chat_route_payload
from omh.routing.intent import scrub_diagnostic_status_text
from omh.routing.localization import normalized_phrase, prepare_routing_text
from omh.routing.policy import everyday_sense_phrase_unanchored
from omh.routing.query import RoutingQuery
from omh.routing.recommend import (
    _SKILL_OFFERS_ITSELF,
    _strip_path_like_fragments,
    _tokens,
    everyday_sense_withheld,
    offers_itself_withheld,
    recommend_skills,
)
from omh.routing.reference_regions import executable_routing_text


MESSAGES = (
    "plan a risky refactor of the billing module",
    "Can you review this PR for correctness bugs?",
    "이 문서를 요약해줘",
    "로그인 페이지 디자인을 바꿔줘",
    "see src/foo/bar.py and fix the off-by-one",
    "FAILED tests/test_cli.py::test_recommend - AssertionError",
    "$ralplan migrate the auth service to the new token store",
    "/omh deep-interview what should we build next",
    "> quoted: run ralph on this\nwhat does the quote mean?",
    "`src/a/b.py` `src/c/d.py`",
    "what's the weather like today?",
    "set up a weekly cron that posts the sales pipeline review",
    "the login page looks off on mobile, change the colors",
    "we had a silent failure in the payment webhook worker",
    "[omh route hint] ralplan selected\nplease plan the billing migration",
    "```\nrun ralph on everything\n```\nwhat does this snippet do?",
)


def _hand_written_chain(message: str) -> dict[str, object]:
    # The chain as the call sites wrote it before `RoutingQuery` existed.
    executable = executable_routing_text(message)
    scrubbed = scrub_diagnostic_status_text(executable)
    routing_text = prepare_routing_text(_strip_path_like_fragments(scrubbed))
    normalized = normalized_phrase(routing_text.scoring_text)
    return {
        "executable": executable,
        "scrubbed": scrubbed,
        "routing_text": routing_text,
        "normalized": normalized,
        "tokens": _tokens(normalized),
    }


class RoutingQueryStageTests(unittest.TestCase):
    def test_every_stage_equals_the_hand_written_chain(self) -> None:
        for message in MESSAGES:
            with self.subTest(message=message):
                query = RoutingQuery.from_message(message)
                expected = _hand_written_chain(message)
                self.assertEqual(query.raw, message)
                self.assertEqual(query.executable, expected["executable"])
                self.assertEqual(query.scrubbed, expected["scrubbed"])
                self.assertEqual(query.routing_text, expected["routing_text"])
                self.assertEqual(query.normalized, expected["normalized"])
                self.assertEqual(query.tokens, expected["tokens"])

    def test_the_corpus_reaches_the_stages_it_claims_to(self) -> None:
        # Guard the corpus itself: without these the stage test could pass on
        # messages that never exercise a stage.
        by_message = {message: RoutingQuery.from_message(message) for message in MESSAGES}
        path_query = by_message["see src/foo/bar.py and fix the off-by-one"]
        self.assertIn("src/foo/bar.py", path_query.scrubbed)
        self.assertNotIn("src/foo/bar.py", path_query.routing_text.original)
        self.assertEqual(by_message["`src/a/b.py` `src/c/d.py`"].normalized, "")
        fenced = by_message["```\nrun ralph on everything\n```\nwhat does this snippet do?"]
        self.assertNotIn("ralph", fenced.executable)
        status = by_message["[omh route hint] ralplan selected\nplease plan the billing migration"]
        self.assertNotEqual(status.scrubbed, status.executable)

    def test_coerce_returns_a_query_unchanged_and_builds_one_from_text(self) -> None:
        query = RoutingQuery.from_message(MESSAGES[0])
        self.assertIs(RoutingQuery.coerce(query), query)
        self.assertEqual(RoutingQuery.coerce(MESSAGES[0]), query)


class RoutingQueryPredicateTests(unittest.TestCase):
    def test_offers_itself_withheld_matches_the_original_body(self) -> None:
        self.assertTrue(_SKILL_OFFERS_ITSELF)
        for message in MESSAGES:
            expected_chain = _hand_written_chain(message)
            query = RoutingQuery.from_message(message)
            for skill, offers_itself in _SKILL_OFFERS_ITSELF.items():
                with self.subTest(message=message, skill=skill):
                    expected = not offers_itself(expected_chain["normalized"], expected_chain["tokens"])
                    self.assertEqual(query.offers_itself_withheld(skill), expected)
                    self.assertEqual(offers_itself_withheld(message, skill), expected)
                    self.assertEqual(offers_itself_withheld(query, skill), expected)
        self.assertFalse(RoutingQuery.from_message(MESSAGES[0]).offers_itself_withheld("no-such-skill"))

    def test_everyday_sense_withheld_matches_the_original_body(self) -> None:
        for message in MESSAGES:
            normalized = _hand_written_chain(message)["normalized"]
            query = RoutingQuery.from_message(message)
            for skill in ("failure-signal-audit", "ops-review", "release-cut"):
                with self.subTest(message=message, skill=skill):
                    expected = everyday_sense_phrase_unanchored(skill, normalized)
                    self.assertEqual(query.everyday_sense_withheld(skill), expected)
                    self.assertEqual(everyday_sense_withheld(message, skill), expected)
                    self.assertEqual(everyday_sense_withheld(query, skill), expected)


class RecommendSkillsAcceptsQueryTests(unittest.TestCase):
    def test_text_and_query_rank_identically(self) -> None:
        for message in MESSAGES:
            with self.subTest(message=message):
                query = RoutingQuery.from_message(message)
                self.assertEqual(recommend_skills(message), recommend_skills(query))
                self.assertEqual(
                    recommend_skills(message, limit=10, apply_guardrails=False),
                    recommend_skills(query, limit=10, apply_guardrails=False),
                )


class ChatPathBuildsTheQueryOnceTests(unittest.TestCase):
    def test_an_uncached_message_runs_the_prep_chain_once(self) -> None:
        # `_strip_path_like_fragments` has one caller, the prep chain inside
        # `RoutingQuery.from_message`, so its call count is the number of times
        # the chain ran. Before `RoutingQuery` was threaded through, this
        # message ran it twice (scorer plus a helper) on the chat path.
        message = "review the code-review skill for the auth module"
        for cached in (
            chat_module._public_chat_route_payload_cached,
            chat_module._route_chat_message_cached,
            recommend_module._recommend_skills_cached,
        ):
            cached.cache_clear()
        with (
            mock.patch.object(
                recommend_module,
                "_strip_path_like_fragments",
                wraps=recommend_module._strip_path_like_fragments,
            ) as chain,
            mock.patch.object(
                recommend_module,
                "_scored_field",
                wraps=recommend_module._scored_field,
            ) as scorer,
        ):
            public_chat_route_payload(message)
        self.assertEqual(scorer.call_count, 1, "the message must reach recommend_skills")
        self.assertEqual(chain.call_count, 1)

    def test_a_query_handed_to_recommend_skills_builds_no_stage(self) -> None:
        query = RoutingQuery.from_message("plan the zebra-stripe billing migration for the ledger team")
        recommend_module._recommend_skills_cached.cache_clear()
        with mock.patch.object(
            recommend_module,
            "_strip_path_like_fragments",
            wraps=recommend_module._strip_path_like_fragments,
        ) as chain:
            first = recommend_skills(query)
            second = recommend_skills(query.raw)
        self.assertEqual(chain.call_count, 0)
        self.assertEqual(first, second)
        self.assertEqual(recommend_module._recommend_skills_cached.cache_info().hits, 1)


def _route_fields(message: str) -> dict[str, object]:
    route = public_chat_route_payload(message)
    return {key: route[key] for key in ("action", "selected_skill", "candidate_skill", "confidence")}


SURFACE_MESSAGES = (
    "plan a risky refactor of the billing module",
    "로그인 페이지 디자인을 바꿔줘",
    "what's the weather like today?",
)


class RecommendSurfacesCarryRouteTests(unittest.TestCase):
    def test_cli_json_adds_route_and_keeps_recommendations(self) -> None:
        for message in SURFACE_MESSAGES:
            with self.subTest(message=message):
                status, stdout, stderr = run_cli(["recommend", message, "--limit", "3"])
                self.assertEqual((status, stderr), (0, ""))
                payload = json.loads(stdout)
                self.assertEqual(payload["route"], _route_fields(message))
                self.assertEqual(payload["query"], message)
                self.assertEqual(payload["recommendations"], recommend_skills(message, limit=8)[:3])
                self.assertLessEqual(set(payload), {"query", "recommendations", "workflow_route_plan", "route"})

    def test_cli_text_prints_one_route_line(self) -> None:
        message = SURFACE_MESSAGES[0]
        route = _route_fields(message)
        status, stdout, _ = run_cli(["recommend", message, "--limit", "3"], output_json=False)
        self.assertEqual(status, 0)
        route_lines = [line for line in stdout.splitlines() if line.startswith("route: ")]
        expected_skill = route["selected_skill"] or route["candidate_skill"] or "none"
        self.assertEqual(route_lines, [f"route: {route['action']} -> {expected_skill}"])

    def test_plugin_tool_adds_route_and_keeps_recommendations(self) -> None:
        for message in SURFACE_MESSAGES:
            with self.subTest(message=message):
                payload = json.loads(recommend_tool.omh_recommend_handler({"message": message, "limit": 3}))
                self.assertEqual(payload["schema_version"], "omh_recommend_result/v1")
                self.assertEqual(payload["source"], "package_recommend")
                self.assertEqual(payload["route"], _route_fields(message))
                expected = [recommend_tool._redacted_recommendation(item) for item in recommend_skills(message, limit=3)]
                self.assertEqual(payload["recommendations"], expected)

    def test_mcp_bridge_adds_route_and_keeps_recommendations(self) -> None:
        for message in SURFACE_MESSAGES:
            with self.subTest(message=message):
                result = bridge._call_tool(None, "omh_recommend", {"message": message, "limit": 3})
                self.assertEqual(result["result_schema_version"], "omh_recommend_result/v1")
                payload = result["payload"]
                self.assertEqual(payload["route"], _route_fields(message))
                self.assertEqual(payload["recommendations"], recommend_skills(message, limit=3))
                self.assertEqual(set(payload), {"message_summary", "recommendations", "route"})


if __name__ == "__main__":
    unittest.main()
