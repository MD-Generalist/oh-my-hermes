from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()
from omh.context_safety import (
    CODING_PROGRESS_REPORTABLE_EVENTS,
    build_progress_event as build_chat_progress_event,
)
from omh.executor_progress import (
    CLOSING_EVENT_TYPES,
    DEFAULT_MINIMUM_REPEAT_INTERVAL_SECONDS,
    PROGRESS_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    build_progress_binding,
    build_progress_event,
    build_safe_progress_signal,
    infer_progress_event_type,
    reported_event_types,
    should_report_event,
    update_binding_reporter_state,
)


CLAIM_MISMATCH_EVENT_TYPES = ("reported_change_not_observed",)


def _binding(**overrides: object) -> dict[str, object]:
    binding = build_progress_binding(
        target_type="run",
        target_id="run-quality-floor",
        executor_profile="codex",
        codex_session_ref="codex-session-1",
        now="2026-07-25T00:00:00Z",
    )
    binding.update(overrides)
    return binding


def _event(
    binding: dict[str, object],
    event_type: str,
    *,
    observed_at: str,
    summary: str = "",
) -> dict[str, object]:
    # `transition_fingerprint` hashes the summary, so a distinct summary is what
    # separates "the same state observed again" (duplicate_transition) from "the
    # same kind of thing happened again" (repeat_interval).
    return build_progress_event(
        binding,
        event_type=event_type,
        summary=summary or f"observed {event_type}",
        observed_at=observed_at,
    )


class SuppressionExemptFloorTests(unittest.TestCase):
    """The floor that volume control must never cut through.

    A long delegated coding session drowns its own signal: the observed incident
    ran twelve fix/test cycles and only surfaced "4 file(s) were NOT modified
    this turn despite any wording above that may suggest otherwise" at the very
    end. Claim/observation mismatches and first occurrences have to survive any
    suppression added for volume.
    """

    def test_claim_mismatch_types_are_registered_as_progress_events(self) -> None:
        for event_type in CLAIM_MISMATCH_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, PROGRESS_EVENT_TYPES)

    def test_claim_mismatch_types_are_suppression_exempt(self) -> None:
        for event_type in CLAIM_MISMATCH_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, TERMINAL_EVENT_TYPES)

    def test_claim_mismatch_reports_even_when_the_same_type_just_reported(self) -> None:
        for event_type in CLAIM_MISMATCH_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                binding = _binding(
                    last_reported_event_type=event_type,
                    last_reported_at="2026-07-25T00:00:00Z",
                    report_count=9,
                    reported_event_types=[event_type],
                )
                event = _event(binding, event_type, observed_at="2026-07-25T00:00:01Z")
                should_report, reason = should_report_event(binding, event, now="2026-07-25T00:00:01Z")
                self.assertTrue(should_report, reason)
                self.assertEqual(reason, "terminal_or_blocker")

    def test_claim_mismatch_survives_the_chat_facing_event_builder(self) -> None:
        for event_type in CLAIM_MISMATCH_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                event = build_chat_progress_event(event_type, "patch reported applied; no file changed")
                self.assertEqual(
                    event["event_type"],
                    event_type,
                    "unregistered types normalize to status_update, erasing the mismatch signal",
                )

    def test_claim_mismatch_types_are_declared_reportable_in_the_progress_policy(self) -> None:
        for event_type in CLAIM_MISMATCH_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, CODING_PROGRESS_REPORTABLE_EVENTS)


class BindingClosureTests(unittest.TestCase):
    """A binding must still close when the run ends.

    The closing set used to be hardcoded separately from TERMINAL_EVENT_TYPES.
    Adding a mismatch type that intercepted observations previously classified
    as `executor_failed` therefore left the binding `active`, and
    `project_active_executor_status` reported a dead executor as running until
    freshness aged it out 15 minutes later.
    """

    def _closed_state(self, event_type: str) -> str:
        binding = _binding()
        event = _event(binding, event_type, observed_at="2026-07-25T00:00:01Z")
        updated = update_binding_reporter_state(
            binding, event, reported=True, reported_at="2026-07-25T00:00:01Z"
        )
        return str(updated["state"])

    def test_every_closing_event_type_closes_the_binding(self) -> None:
        for event_type in CLOSING_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                self.assertEqual(self._closed_state(event_type), "closed")

    def test_a_claim_mismatch_closes_the_binding(self) -> None:
        for event_type in CLAIM_MISMATCH_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, CLOSING_EVENT_TYPES)
                self.assertEqual(self._closed_state(event_type), "closed")

    def test_test_results_are_reportable_but_do_not_close(self) -> None:
        """The executor may keep working after a test run reports."""
        for event_type in ("tests_passed", "tests_failed"):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, TERMINAL_EVENT_TYPES)
                self.assertNotIn(event_type, CLOSING_EVENT_TYPES)
                self.assertEqual(self._closed_state(event_type), "active")


class FirstOccurrenceGuaranteeTests(unittest.TestCase):
    def test_migrating_a_legacy_binding_does_not_un_suppress_everything(self) -> None:
        """Seeding the new field with an empty list discards real history.

        A legacy binding that reported `repo_exploration` forty times would,
        after one unrelated report, treat `repo_exploration` as never seen and
        report it again -- a burst of un-suppression on a run already in flight.
        """
        binding = _binding(
            last_reported_event_type="repo_exploration",
            last_reported_at="2026-07-25T00:00:00Z",
            report_count=40,
        )
        binding.pop("reported_event_types")

        closing = _event(binding, "executor_completed", observed_at="2026-07-25T00:00:01Z")
        binding = update_binding_reporter_state(
            binding, closing, reported=True, reported_at="2026-07-25T00:00:01Z"
        )
        self.assertEqual(reported_event_types(binding), ["repo_exploration", "executor_completed"])

        repeat = _event(
            binding,
            "repo_exploration",
            observed_at="2026-07-25T00:00:05Z",
            summary="observed repo_exploration again",
        )
        _should_report, reason = should_report_event(binding, repeat, now="2026-07-25T00:00:05Z")
        self.assertNotEqual(
            reason,
            "first_occurrence",
            "a type the legacy binding already reported must not read as new",
        )
        # It still reports, as `meaningful_transition` -- the pre-existing
        # interval rule only guards a repeat of the immediately previous type,
        # and an intervening `executor_completed` breaks that run. The point of
        # the seed is that the reason is not `first_occurrence`, which would
        # bypass the interval rule outright for every remaining type.
        self.assertEqual(reason, "meaningful_transition")

    def test_a_legacy_binding_with_no_reports_still_treats_types_as_new(self) -> None:
        binding = _binding(report_count=0)
        binding.pop("reported_event_types")
        event = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:01Z")
        _should_report, reason = should_report_event(binding, event, now="2026-07-25T00:00:01Z")
        self.assertEqual(reason, "first_occurrence")


    def test_first_occurrence_of_a_non_terminal_type_always_reports(self) -> None:
        binding = _binding()
        event = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:01Z")
        should_report, reason = should_report_event(binding, event, now="2026-07-25T00:00:01Z")
        self.assertTrue(should_report, reason)
        self.assertEqual(reason, "first_occurrence")

    def test_repeat_inside_the_interval_is_still_suppressed(self) -> None:
        binding = _binding()
        first = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:01Z")
        binding = update_binding_reporter_state(
            binding, first, reported=True, reported_at="2026-07-25T00:00:01Z"
        )
        self.assertEqual(reported_event_types(binding), ["repo_exploration"])

        repeat = _event(
            binding,
            "repo_exploration",
            observed_at="2026-07-25T00:00:30Z",
            summary="observed repo_exploration in another directory",
        )
        should_report, reason = should_report_event(binding, repeat, now="2026-07-25T00:00:30Z")
        self.assertFalse(should_report)
        self.assertEqual(reason, "repeat_interval")

    def test_identical_state_observed_twice_is_suppressed_as_a_duplicate(self) -> None:
        binding = _binding()
        first = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:01Z")
        binding = update_binding_reporter_state(
            binding, first, reported=True, reported_at="2026-07-25T00:00:01Z"
        )
        same = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:30Z")
        should_report, reason = should_report_event(binding, same, now="2026-07-25T00:00:30Z")
        self.assertFalse(should_report)
        self.assertEqual(reason, "duplicate_transition")

    def test_reported_types_accumulate_without_duplicates(self) -> None:
        binding = _binding()
        for index, event_type in enumerate(("repo_exploration", "diff_started", "repo_exploration"), start=1):
            event = _event(binding, event_type, observed_at=f"2026-07-25T00:0{index}:00Z")
            binding = update_binding_reporter_state(
                binding, event, reported=True, reported_at=f"2026-07-25T00:0{index}:00Z"
            )
        self.assertEqual(reported_event_types(binding), ["repo_exploration", "diff_started"])

    def test_a_suppressed_event_does_not_consume_its_first_occurrence(self) -> None:
        binding = _binding()
        event = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:01Z")
        binding = update_binding_reporter_state(
            binding, event, reported=False, reported_at="2026-07-25T00:00:01Z"
        )
        self.assertEqual(reported_event_types(binding), [])

        retry = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:02Z")
        should_report, reason = should_report_event(binding, retry, now="2026-07-25T00:00:02Z")
        self.assertTrue(should_report, reason)
        self.assertEqual(reason, "first_occurrence")

    def test_legacy_bindings_without_the_field_keep_interval_behaviour(self) -> None:
        """A binding written before the field existed must not be re-opened.

        Treating an unknown history as "nothing reported yet" would un-suppress
        every type on a run already in flight.
        """
        binding = _binding(
            last_reported_event_type="repo_exploration",
            last_reported_at="2026-07-25T00:00:00Z",
            report_count=4,
        )
        binding.pop("reported_event_types")
        event = _event(binding, "repo_exploration", observed_at="2026-07-25T00:00:30Z")
        should_report, reason = should_report_event(binding, event, now="2026-07-25T00:00:30Z")
        self.assertFalse(should_report)
        self.assertEqual(reason, "repeat_interval")

    def test_repeat_after_the_interval_reports_again(self) -> None:
        binding = _binding(reported_event_types=["repo_exploration"], last_reported_event_type="repo_exploration")
        binding["last_reported_at"] = "2026-07-25T00:00:00Z"
        later = f"2026-07-25T00:{DEFAULT_MINIMUM_REPEAT_INTERVAL_SECONDS // 60 + 1:02d}:00Z"
        event = _event(binding, "repo_exploration", observed_at=later)
        should_report, reason = should_report_event(binding, event, now=later)
        self.assertTrue(should_report, reason)
        self.assertEqual(reason, "meaningful_transition")


class ClaimMismatchDetectionTests(unittest.TestCase):
    """The exemption list is worthless if nothing ever emits the exempt types."""

    def _signal(self, **overrides: object) -> dict[str, object]:
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            profile_progress_summary={
                "status": "running",
                "observable_activity": ["Codex changed files."],
            },
        )
        signal.update(overrides)
        return signal

    def test_a_confirmed_edit_with_clean_git_is_flagged(self) -> None:
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="completed",
            profile_progress_summary={"observable_activity": ["Codex applied a file change."]},
            git_status_short="",
            git_diff_stat="",
        )
        self.assertTrue(signal["git_observed"])
        self.assertEqual(infer_progress_event_type(signal), "reported_change_not_observed")

    def test_the_broad_edit_bucket_alone_is_not_a_claim(self) -> None:
        """"Codex changed files." also fires on a line that merely mentions a diff.

        Over-matching is free for the benign `diff_started` label and expensive
        for an accusation, so only the confirmed label contradicts anything.
        """
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="completed",
            profile_progress_summary={"observable_activity": ["Codex changed files."]},
            git_status_short="",
            git_diff_stat="",
        )
        self.assertNotEqual(infer_progress_event_type(signal), "reported_change_not_observed")

    def test_reported_change_with_a_real_diff_is_not_flagged(self) -> None:
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="completed",
            profile_progress_summary={"observable_activity": ["Codex applied a file change."]},
            git_status_short=" M src/coding/executor_progress.py",
            git_diff_stat="1 file changed, 2 insertions(+)",
        )
        self.assertEqual(infer_progress_event_type(signal), "diff_started")

    def test_no_git_observation_never_claims_a_mismatch(self) -> None:
        """Callers that do not collect git state must not trip the detector."""
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="completed",
            profile_progress_summary={"observable_activity": ["Codex changed files."]},
        )
        self.assertFalse(signal["git_observed"])
        self.assertNotEqual(infer_progress_event_type(signal), "reported_change_not_observed")

    def test_clean_git_without_any_change_claim_is_not_a_mismatch(self) -> None:
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            profile_progress_summary={"observable_activity": ["Codex inspected the repo."]},
            git_status_short="",
        )
        self.assertEqual(infer_progress_event_type(signal), "repo_exploration")

    def test_clean_exit_with_observed_failure_stays_executor_failed(self) -> None:
        """A clean exit contradicted by an observed failure is already covered.

        This was briefly its own `reported_success_contradicted` type. The
        existing ladder classifies it as `executor_failed`, which is more
        actionable and -- unlike a mismatch label -- closes the binding, so the
        separate type only stole that behaviour.
        """
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="exited_zero",
            profile_progress_summary={"status": "failed_or_error_observed"},
        )
        self.assertEqual(infer_progress_event_type(signal), "executor_failed")

    def test_clean_exit_without_observed_failure_stays_completed(self) -> None:
        signal = build_safe_progress_signal(executor_profile="claude_code", process_status="exited_zero")
        self.assertEqual(infer_progress_event_type(signal), "executor_completed")

    def test_a_mid_flight_run_mentioning_a_diff_is_not_accused(self) -> None:
        """The upstream change claim is a coarse bucket.

        Codex sets it from any log line containing patch/write/edit/diff, so
        "let me run git diff to see the state" reads as a change claim against a
        tree that is legitimately still clean. Only a finished run can
        contradict itself.
        """
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="running",
            profile_progress_summary={"observable_activity": ["Codex changed files."]},
            git_status_short="",
            git_diff_stat="",
        )
        self.assertTrue(signal["git_observed"])
        self.assertNotEqual(infer_progress_event_type(signal), "reported_change_not_observed")

    def test_a_blocker_outranks_the_mismatch(self) -> None:
        """Blocked text that also mentions a patch is a blocker, not a lie."""
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="completed",
            profile_progress_summary={
                "status": "blocked",
                "observable_activity": ["Codex changed files."],
            },
            git_status_short="",
        )
        self.assertEqual(infer_progress_event_type(signal), "executor_blocked")

    def test_a_failure_outranks_the_mismatch(self) -> None:
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="completed",
            profile_progress_summary={
                "status": "failed_or_error_observed",
                "observable_activity": ["Codex changed files."],
            },
            git_status_short="",
        )
        self.assertEqual(infer_progress_event_type(signal), "executor_failed")

    def test_an_explicit_event_type_still_wins(self) -> None:
        signal = build_safe_progress_signal(
            executor_profile="claude_code",
            process_status="completed",
            explicit_event_type="tests_passed",
            profile_progress_summary={"observable_activity": ["Codex changed files."]},
            git_status_short="",
        )
        self.assertEqual(infer_progress_event_type(signal), "tests_passed")


if __name__ == "__main__":
    unittest.main()
