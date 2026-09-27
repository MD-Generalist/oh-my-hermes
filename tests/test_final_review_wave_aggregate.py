from __future__ import annotations

from concurrent.futures import CancelledError, TimeoutError
import threading
import unittest

from _local_package import load_local_package

load_local_package()
from omh.coding.final_review_wave import (
    LANE_ORDER,
    FinalReviewExecutionReservations,
    GlobalReviewReservation,
    ContextProvenance,
    ImmutableRevision,
    IntegrationReceipt,
    LaneBudgetReservationInput,
    LaneExecutionResult,
    LaneObservation,
    LaneState,
    ProviderReviewReservation,
    ReviewLens,
    ReviewReservation,
    WaveVerdict,
    execute_final_review_wave,
    prepare_final_review_wave,
    prepare_remediated_wave,
)


REVISION = ImmutableRevision("a" * 40)
FRESH = ContextProvenance.FRESH_FROM_DIFF


def _reservations() -> tuple[LaneBudgetReservationInput, ...]:
    return tuple(LaneBudgetReservationInput(lens, limit=1, reserved=0) for lens in LANE_ORDER)


def _limits(*, provider_limit: int = 4) -> FinalReviewExecutionReservations:
    return FinalReviewExecutionReservations(
        GlobalReviewReservation(limit=4, reserved=0),
        {"local": ProviderReviewReservation(limit=provider_limit, reserved=0)},
        ReviewReservation(limit=4, reserved=0),
    )


def _integrated_wave():
    return prepare_final_review_wave("wave-1", _reservations()).integrate(
        IntegrationReceipt(REVISION, completed=True)
    )


class FinalReviewWaveAggregateTests(unittest.TestCase):
    def test_at_least_three_lanes_overlap_within_typed_global_provider_and_review_limits(self) -> None:
        wave = _integrated_wave()
        started = {lens: threading.Event() for lens in LANE_ORDER}
        release = threading.Event()
        lock = threading.Lock()
        inflight = 0
        maximum = 0

        def run(lane):
            nonlocal inflight, maximum
            with lock:
                inflight += 1
                maximum = max(maximum, inflight)
            started[lane.lens].set()
            self.assertTrue(release.wait(timeout=5))
            with lock:
                inflight -= 1
            return LaneExecutionResult(LaneState.COMPLETED, REVISION)

        result: list[object] = []
        worker = threading.Thread(
            target=lambda: result.append(
                execute_final_review_wave(wave, run, _limits(), provider_for=lambda _: "local", context_provenance=FRESH)
            )
        )
        worker.start()

        self.assertTrue(all(event.wait(timeout=5) for event in started.values()))
        self.assertEqual(maximum, 4)
        release.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0].assess().verdict, WaveVerdict.PASS)

    def test_provider_limit_bounds_execution_even_when_global_and_review_allow_more(self) -> None:
        wave = _integrated_wave()
        first_started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        inflight = 0
        maximum = 0

        def run(_lane):
            nonlocal inflight, maximum
            with lock:
                inflight += 1
                maximum = max(maximum, inflight)
                first_started.set()
            self.assertTrue(release.wait(timeout=5))
            with lock:
                inflight -= 1
            return LaneExecutionResult(LaneState.COMPLETED, REVISION)

        result: list[object] = []
        worker = threading.Thread(
            target=lambda: result.append(
                execute_final_review_wave(wave, run, _limits(provider_limit=1), provider_for=lambda _: "local", context_provenance=FRESH)
            )
        )
        worker.start()

        self.assertTrue(first_started.wait(timeout=5))
        self.assertEqual(maximum, 1)
        release.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(maximum, 1)
        self.assertEqual(result[0].assess().verdict, WaveVerdict.PASS)

    def test_every_required_lane_reaches_a_terminal_state_after_one_fails(self) -> None:
        wave = _integrated_wave()
        calls: list[ReviewLens] = []
        lock = threading.Lock()

        def run(lane):
            with lock:
                calls.append(lane.lens)
            if lane.lens is ReviewLens.REQUIREMENT:
                raise RuntimeError("broken requirement lane")
            return LaneExecutionResult(LaneState.COMPLETED, REVISION)

        completed = execute_final_review_wave(wave, run, _limits(), provider_for=lambda _: "local", context_provenance=FRESH)

        self.assertEqual(set(calls), set(LANE_ORDER))
        self.assertEqual(completed.assess().verdict, WaveVerdict.BLOCK)
        self.assertEqual(completed.assess().blocking_lens, ReviewLens.REQUIREMENT)
        self.assertEqual(
            [lane.state for lane in completed.lanes],
            [LaneState.FAILED, LaneState.COMPLETED, LaneState.COMPLETED, LaneState.COMPLETED],
        )

    def test_terminal_failure_kinds_and_revision_mismatch_are_classified_exactly(self) -> None:
        wave = _integrated_wave()

        def run(lane):
            match lane.lens:
                case ReviewLens.REQUIREMENT:
                    raise TimeoutError()
                case ReviewLens.QUALITY:
                    raise CancelledError()
                case ReviewLens.SAFETY:
                    return None
                case ReviewLens.REAL_SURFACE:
                    return LaneExecutionResult(LaneState.COMPLETED, ImmutableRevision("b" * 40))
            raise AssertionError("unreachable")

        completed = execute_final_review_wave(wave, run, _limits(), provider_for=lambda _: "local", context_provenance=FRESH)

        self.assertEqual(
            [lane.state for lane in completed.lanes],
            [LaneState.TIMED_OUT, LaneState.CANCELLED, LaneState.MISSING, LaneState.STALE],
        )
        self.assertEqual(completed.assess().blocking_lens, ReviewLens.REQUIREMENT)

    def test_status_emits_required_running_and_terminal_human_states(self) -> None:
        statuses: list[tuple[str, ...]] = []

        completed = execute_final_review_wave(
            _integrated_wave(),
            lambda lane: (
                LaneExecutionResult(LaneState.FAILED, REVISION)
                if lane.lens is ReviewLens.REQUIREMENT
                else (
                    LaneExecutionResult(LaneState.MISSING, REVISION)
                    if lane.lens is ReviewLens.REAL_SURFACE
                    else LaneExecutionResult(LaneState.COMPLETED, REVISION)
                )
            ),
            _limits(),
            provider_for=lambda _: "local",
            context_provenance=FRESH,
            status_sink=lambda projection: statuses.append(tuple(item.status for item in projection.lanes)),
        )

        self.assertEqual(statuses[0], ("required",) * len(LANE_ORDER))
        self.assertIn("running", {status for batch in statuses for status in batch})
        self.assertEqual(statuses[-1], ("blocked", "completed", "completed", "missing"))
        self.assertEqual(completed.assess().verdict, WaveVerdict.BLOCK)

    def test_remediation_invalidates_a_completed_aggregate_and_requires_a_new_revision(self) -> None:
        completed = execute_final_review_wave(
            _integrated_wave(),
            lambda _lane: LaneExecutionResult(LaneState.COMPLETED, REVISION),
            _limits(),
            provider_for=lambda _: "local",
            context_provenance=FRESH,
        )
        invalidated = completed.invalidate_for_remediation()
        replacement = prepare_remediated_wave(invalidated, "wave-2", _reservations()).integrate(
            IntegrationReceipt(ImmutableRevision("b" * 40), completed=True)
        )

        self.assertEqual(completed.assess().verdict, WaveVerdict.PASS)
        self.assertEqual(invalidated.assess().verdict, WaveVerdict.BLOCK)
        self.assertEqual(replacement.assess().verdict, WaveVerdict.HOLD)
        self.assertNotEqual(replacement.integration, completed.integration)

    def test_prepared_or_reserved_lanes_are_not_execution_evidence(self) -> None:
        prepared = prepare_final_review_wave("wave-1", _reservations())

        self.assertEqual(prepared.assess().verdict, WaveVerdict.HOLD)
        self.assertEqual([lane.status for lane in prepared.project_status().lanes], ["required"] * 4)


    def test_runner_declared_inherited_context_overrides_the_executor_and_is_refused(self) -> None:
        completed = execute_final_review_wave(
            _integrated_wave(),
            lambda lane: LaneExecutionResult(
                LaneState.COMPLETED,
                REVISION,
                ContextProvenance.INHERITED_FROM_AUTHOR if lane.lens is ReviewLens.REAL_SURFACE else None,
            ),
            _limits(),
            provider_for=lambda _: "local",
            context_provenance=FRESH,
        )

        self.assertEqual(
            [lane.context_provenance for lane in completed.lanes],
            [FRESH, FRESH, FRESH, ContextProvenance.INHERITED_FROM_AUTHOR],
        )
        self.assertEqual(completed.assess().verdict, WaveVerdict.BLOCK)
        self.assertEqual(completed.assess().blocking_lens, ReviewLens.REAL_SURFACE)
        self.assertEqual(completed.assess().refused_field, "context_provenance")

    def test_executor_declared_inherited_context_cannot_pass(self) -> None:
        completed = execute_final_review_wave(
            _integrated_wave(),
            lambda _lane: LaneExecutionResult(LaneState.COMPLETED, REVISION),
            _limits(),
            provider_for=lambda _: "local",
            context_provenance=ContextProvenance.INHERITED_FROM_AUTHOR,
        )

        self.assertEqual(completed.assess().verdict, WaveVerdict.BLOCK)
        self.assertEqual(completed.assess().refused_field, "context_provenance")

    def test_runner_observation_keeps_its_declared_inherited_context(self) -> None:
        completed = execute_final_review_wave(
            _integrated_wave(),
            lambda lane: LaneObservation(
                lane.lens,
                LaneState.COMPLETED,
                REVISION,
                ContextProvenance.INHERITED_FROM_AUTHOR if lane.lens is ReviewLens.QUALITY else FRESH,
            ),
            _limits(),
            provider_for=lambda _: "local",
            context_provenance=FRESH,
        )

        self.assertIs(completed.lanes[1].context_provenance, ContextProvenance.INHERITED_FROM_AUTHOR)
        self.assertEqual(completed.assess().blocking_lens, ReviewLens.QUALITY)
        self.assertEqual(completed.assess().refused_field, "context_provenance")

if __name__ == "__main__":
    unittest.main()
