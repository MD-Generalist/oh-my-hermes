from __future__ import annotations

from dataclasses import replace
import itertools
import unittest

from _local_package import load_local_package

load_local_package()
from omh.coding.final_review_wave import (
    LANE_ORDER,
    ContextProvenance,
    FinalReviewWave,
    ImmutableRevision,
    IntegrationReceipt,
    LaneBudgetReservationInput,
    LaneObservation,
    LaneState,
    ReviewLens,
    WaveAssessment,
    WaveVerdict,
    prepare_final_review_wave,
    prepare_remediated_wave,
)


FRESH = ContextProvenance.FRESH_FROM_DIFF


def _reservations(*, unavailable: ReviewLens | None = None) -> tuple[LaneBudgetReservationInput, ...]:
    return tuple(
        LaneBudgetReservationInput(lens, limit=1, reserved=1 if lens == unavailable else 0)
        for lens in LANE_ORDER
    )


def _integrated_wave() -> FinalReviewWave:
    return prepare_final_review_wave("wave-1", _reservations()).integrate(
        IntegrationReceipt(ImmutableRevision("a" * 40), completed=True)
    )


class FinalReviewWaveTests(unittest.TestCase):
    def test_at_least_three_read_only_lanes_are_eligible_concurrently_only_after_integration(self) -> None:
        prepared = prepare_final_review_wave("wave-1", _reservations())

        self.assertEqual(prepared.eligible_lanes(), ())

        integrated = prepared.integrate(IntegrationReceipt(ImmutableRevision("a" * 40), completed=True))

        self.assertEqual(integrated.eligible_lanes(), LANE_ORDER)
        self.assertTrue(all(not lane.read_only.allows_mutation for lane in integrated.lanes))
        self.assertTrue(all(lane.bound_revision == ImmutableRevision("a" * 40) for lane in integrated.lanes))

    def test_revision_mismatch_marks_the_exact_lane_stale_and_blocks(self) -> None:
        wave = _integrated_wave().observe(
            LaneObservation(ReviewLens.QUALITY, LaneState.COMPLETED, ImmutableRevision("b" * 40), FRESH)
        )

        quality = next(lane for lane in wave.lanes if lane.lens == ReviewLens.QUALITY)
        self.assertEqual(quality.state, LaneState.STALE)
        self.assertEqual(wave.assess().verdict, WaveVerdict.BLOCK)
        self.assertEqual(wave.assess().blocking_lens, ReviewLens.QUALITY)

    def test_missing_real_surface_blocks_with_its_exact_lens(self) -> None:
        wave = _integrated_wave().observe(
            LaneObservation(ReviewLens.REAL_SURFACE, LaneState.MISSING, ImmutableRevision("a" * 40), FRESH)
        )

        self.assertEqual(wave.assess().verdict, WaveVerdict.BLOCK)
        self.assertEqual(wave.assess().blocking_lens, ReviewLens.REAL_SURFACE)

    def test_completion_permutations_have_the_same_pass_assessment(self) -> None:
        observations = tuple(
            LaneObservation(lens, LaneState.COMPLETED, ImmutableRevision("a" * 40), FRESH)
            for lens in LANE_ORDER
        )

        verdicts = set()
        for order in itertools.permutations(observations):
            wave = _integrated_wave()
            for observation in order:
                wave = wave.observe(observation)
            verdicts.add(wave.assess())

        self.assertEqual(verdicts, {WaveAssessment(WaveVerdict.PASS, None)})

    def test_remediation_invalidates_old_wave_and_requires_a_new_revision_and_wave(self) -> None:
        prior = _integrated_wave().invalidate_for_remediation()
        replacement = prepare_remediated_wave(prior, "wave-2", _reservations()).integrate(
            IntegrationReceipt(ImmutableRevision("b" * 40), completed=True)
        )

        self.assertEqual(prior.assess().verdict, WaveVerdict.BLOCK)
        self.assertNotEqual(prior.wave_id, replacement.wave_id)
        self.assertNotEqual(prior.integration, replacement.integration)

    def test_prepared_lane_status_never_claims_execution(self) -> None:
        projection = _integrated_wave().project_status()

        self.assertEqual(
            [lane.execution_status for lane in projection.lanes],
            ["prepared_not_executed"] * len(LANE_ORDER),
        )

    def test_exhausted_typed_reservation_blocks_its_exact_lane(self) -> None:
        wave = prepare_final_review_wave("wave-1", _reservations(unavailable=ReviewLens.SAFETY)).integrate(
            IntegrationReceipt(ImmutableRevision("a" * 40), completed=True)
        )

        self.assertEqual(wave.assess().verdict, WaveVerdict.BLOCK)
        self.assertEqual(wave.assess().blocking_lens, ReviewLens.SAFETY)


class ContextProvenanceTests(unittest.TestCase):
    """#1698: a lane records whether it saw the author's context."""

    def _observed(self, provenance_for_safety: ContextProvenance) -> FinalReviewWave:
        wave = _integrated_wave()
        for lens in LANE_ORDER:
            provenance = provenance_for_safety if lens is ReviewLens.SAFETY else FRESH
            wave = wave.observe(LaneObservation(lens, LaneState.COMPLETED, ImmutableRevision("a" * 40), provenance))
        return wave

    def test_observation_without_context_provenance_fails_validation(self) -> None:
        revision = ImmutableRevision("a" * 40)
        with self.assertRaises(TypeError):
            LaneObservation(ReviewLens.SAFETY, LaneState.COMPLETED, revision)  # type: ignore[call-arg]
        for value in (None, "", "fresh", "independent"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "context_provenance"):
                LaneObservation(ReviewLens.SAFETY, LaneState.COMPLETED, revision, value)  # type: ignore[arg-type]

    def test_serialized_value_is_read_back_as_the_enum(self) -> None:
        observation = LaneObservation(
            ReviewLens.SAFETY, LaneState.COMPLETED, ImmutableRevision("a" * 40), "inherited_from_author"  # type: ignore[arg-type]
        )

        self.assertIs(observation.context_provenance, ContextProvenance.INHERITED_FROM_AUTHOR)

    def test_inherited_lane_is_refused_as_independent_and_the_refusal_names_the_field(self) -> None:
        assessment = self._observed(ContextProvenance.INHERITED_FROM_AUTHOR).assess()

        self.assertEqual(
            assessment,
            WaveAssessment(WaveVerdict.BLOCK, ReviewLens.SAFETY, "context_provenance"),
        )

    def test_inherited_lane_is_refused_before_it_finishes(self) -> None:
        wave = _integrated_wave().observe(
            LaneObservation(
                ReviewLens.QUALITY, LaneState.RUNNING, ImmutableRevision("a" * 40), ContextProvenance.INHERITED_FROM_AUTHOR
            )
        )

        self.assertEqual(
            wave.assess(),
            WaveAssessment(WaveVerdict.BLOCK, ReviewLens.QUALITY, "context_provenance"),
        )

    def test_fresh_lanes_are_accepted(self) -> None:
        wave = self._observed(FRESH)

        self.assertEqual(wave.assess(), WaveAssessment(WaveVerdict.PASS, None, None))
        self.assertTrue(all(lane.context_provenance is FRESH for lane in wave.lanes))

    def test_completed_lane_without_recorded_provenance_never_counts_as_independent(self) -> None:
        # The pre-#1698 lane shape: completed, with no provenance recorded.
        wave = self._observed(FRESH)
        legacy = replace(wave.lanes[2], context_provenance=None)
        wave = replace(wave, lanes=wave.lanes[:2] + (legacy,) + wave.lanes[3:])

        self.assertEqual(
            wave.assess(),
            WaveAssessment(WaveVerdict.BLOCK, ReviewLens.SAFETY, "context_provenance"),
        )

    def test_declaring_provenance_is_not_evidence_that_the_review_ran(self) -> None:
        wave = _integrated_wave()
        for lens in LANE_ORDER:
            wave = wave.observe(LaneObservation(lens, LaneState.PREPARED, ImmutableRevision("a" * 40), FRESH))

        self.assertEqual(wave.assess(), WaveAssessment(WaveVerdict.HOLD, ReviewLens.REQUIREMENT))
        self.assertEqual(
            [lane.execution_status for lane in wave.project_status().lanes],
            ["prepared_not_executed"] * len(LANE_ORDER),
        )
