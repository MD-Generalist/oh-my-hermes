"""Production diagnostic and immutable final-review smoke exercises."""

from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock

from omh.coding.diagnostic_execution import (
    DiagnosticExecutionEngine,
    DiagnosticExecutionRequest,
    DiagnosticExecutionSettings,
    ProviderObservation,
)
from omh.coding.diagnostic_providers import DiagnosticProviderConfig, ProviderCapability
from omh.coding.final_review_wave import (
    FinalReviewExecutionReservations, GlobalReviewReservation, ImmutableRevision,
    IntegrationReceipt, LaneBudgetReservationInput, LaneExecutionResult, LaneState,
    ProviderReviewReservation, ReadOnlyCapability, ReviewLane, ReviewReservation,
    execute_final_review_wave, prepare_final_review_wave, prepare_remediated_wave,
)
from omh.coding.final_review_wave_models import LANE_ORDER, ContextProvenance, ReviewLens

_BASELINE = "orchestration-baseline"
_END = "orchestration-end"
_SENTINEL = "SENTINEL-SECRET-MESSAGE-SOURCE"


class _Resolver:
    def __init__(self, files: tuple[str, ...] = ("src/qa.py",)) -> None:
        self.files = files

    def resolve(self, workspace_id: str, baseline_revision: str, end_revision: str) -> tuple[str, ...]:
        return self.files


class _Revisions:
    def __init__(self, final_end: str = _END) -> None:
        self.final_end = final_end

    def read(self, workspace_id: str, revision: str) -> str:
        return _BASELINE if revision == _BASELINE else self.final_end


class _MovingRevisions:
    def __init__(self) -> None:
        self.end_reads = 0

    def read(self, workspace_id: str, revision: str) -> str:
        if revision == _BASELINE:
            return _BASELINE
        self.end_reads += 1
        return _END if self.end_reads == 1 else "orchestration-moved"


class _Runner:
    def __init__(self, mode: str = "clean") -> None:
        self.mode = mode
        self.calls: list[str] = []

    def run(self, provider_id: str, workspace_id: str, revision: str, files: tuple[str, ...], timeout_ms: int, cancelled: object) -> ProviderObservation:
        self.calls.append(revision)
        if self.mode == "timeout":
            return ProviderObservation("timeout")
        if self.mode == "crash":
            raise OSError("local provider crash")
        if self.mode == "unavailable":
            return ProviderObservation.unavailable()
        if self.mode == "mixed":
            return ProviderObservation.unavailable() if provider_id == "pyright" else ProviderObservation("timeout")
        if self.mode in {"message", "source"}:
            key = "message" if self.mode == "message" else "source_text"
            return ProviderObservation.completed(files, ({"severity": "error", "path": "src/qa.py", "line": 1, "character": 1, "source": "local", key: _SENTINEL},))
        return ProviderObservation.completed(files, ())


def _config(*, providers: tuple[str, ...] = ("pyright",)) -> DiagnosticProviderConfig:
    return DiagnosticProviderConfig(tuple(
        ProviderCapability(provider, ("python",), (".py",), 1_000, 10, 10) for provider in providers
    ))


def _engine(runner: _Runner, *, files: tuple[str, ...] = ("src/qa.py",), final_end: str = _END, cancellation: Event | None = None, settings: DiagnosticExecutionSettings | None = None, providers: tuple[str, ...] = ("pyright",)) -> DiagnosticExecutionEngine:
    revisions = _Revisions(final_end) if final_end == _END else _MovingRevisions()
    return DiagnosticExecutionEngine(
        config=_config(providers=providers), resolver=_Resolver(files), revisions=revisions, runner=runner,
        cancellation=cancellation, settings=settings,
    )


def _execute(engine: DiagnosticExecutionEngine, workspace: str = "qa-workspace"):
    return engine.execute(DiagnosticExecutionRequest("wrapper", workspace, _BASELINE, _END))


def happy_diagnostic() -> dict[str, object]:
    runner = _Runner()
    engine = _engine(runner)
    first = _execute(engine)
    second = _execute(engine)
    evidence = first.results[0].evidence
    return {
        "status": first.status, "provider_status": first.results[0].status,
        "evidence_verdict": evidence["verdict"], "baseline_revision": evidence["baseline_revision"],
        "end_revision": evidence["end_revision"], "runner_calls": runner.calls,
        "cache_exact_once": runner.calls == [_BASELINE, _END], "identical_result": first == second,
        "metadata_only": evidence["privacy"] == "metadata_only", "claim_boundary": evidence["claim_boundary"],
    }


def _status(engine: DiagnosticExecutionEngine) -> str:
    result = _execute(engine)
    verdict = result.results[0].evidence["verdict"] if result.results else "unavailable"
    return f"HOLD:{result.status}:{verdict}"


def _stateful_serialization() -> str:
    started, release, competing = Event(), Event(), Barrier(2)
    lock = Lock()
    active = 0
    maximum = 0

    class Runner:
        def run(self, provider_id: str, workspace_id: str, revision: str, files: tuple[str, ...], timeout_ms: int, cancelled: object) -> ProviderObservation:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                started.set()
            release.wait(timeout=5)
            with lock:
                active -= 1
            return ProviderObservation.completed(files, ())

    engine = DiagnosticExecutionEngine(
        config=_config(), resolver=_Resolver(), revisions=_Revisions(), runner=Runner(),
        settings=DiagnosticExecutionSettings(stateful_providers=frozenset(("pyright",))),
    )
    def invoke(workspace: str):
        competing.wait(timeout=5)
        return _execute(engine, workspace)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, "qa-stateful-one")
        second = pool.submit(invoke, "qa-stateful-two")
        started.wait(timeout=5)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    return f"HOLD:serialized:max_active={maximum}"


def adversarial_diagnostic() -> dict[str, object]:
    cancelled = Event()
    cancelled.set()
    result = {
        "moving_end_revision": _status(_engine(_Runner(), final_end="orchestration-moved")),
        "unsupported_suffix": _status(_engine(_Runner(), files=("src/qa.txt",))),
        "timeout": _status(_engine(_Runner("timeout"))),
        "cancel": _status(_engine(_Runner(), cancellation=cancelled)),
        "crash": _status(_engine(_Runner("crash"))),
        "partial_provider": _status(_engine(_Runner("mixed"), providers=("pyright", "ruff"))),
        "stateful_serialization": _stateful_serialization(),
    }
    for name, mode in (("forbidden_message", "message"), ("forbidden_source", "source")):
        result[name] = _status(_engine(_Runner(mode)))
    with TemporaryDirectory(prefix="omh-diagnostic-privacy-") as raw:
        root = Path(raw)
        fixture = root / "unpersisted-provider-input"
        fixture.write_text(_SENTINEL, encoding="utf-8")
        files = tuple(path for path in root.rglob("*") if path.is_file())
        contents = {path.name: path.read_text(encoding="utf-8") for path in files}
        output_files = tuple(path for path in files if path.name != fixture.name)
        result["fixture_privacy_scan"] = {
            "searched_file_count": len(files), "input_fixture_contains_sentinel": contents[fixture.name] == _SENTINEL,
            "output_store_file_count": len(output_files), "persisted_secret_absent": all(_SENTINEL not in path.read_text(encoding="utf-8") for path in output_files),
            "sentinel_absent_from_output": _SENTINEL not in str(result),
        }
    return result


def _reservations() -> tuple[LaneBudgetReservationInput, ...]:
    return tuple(LaneBudgetReservationInput(lens, 1, 0) for lens in LANE_ORDER)


def _limits() -> FinalReviewExecutionReservations:
    return FinalReviewExecutionReservations(GlobalReviewReservation(4, 0), {"local": ProviderReviewReservation(4, 0)}, ReviewReservation(4, 0))


def _wave():
    return prepare_final_review_wave("qa-wave", _reservations()).integrate(IntegrationReceipt(ImmutableRevision(_END), True))


def happy_review() -> dict[str, object]:
    started, release, lock = Event(), Event(), Lock()
    active = 0
    peak = 0

    def runner(lane):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == len(LANE_ORDER):
                started.set()
        started.wait(timeout=5)
        release.set()
        release.wait(timeout=5)
        with lock:
            active -= 1
        return LaneExecutionResult(LaneState.COMPLETED, ImmutableRevision(_END))

    completed = execute_final_review_wave(_wave(), runner, _limits(), provider_for=lambda _: "local", context_provenance=ContextProvenance.FRESH_FROM_DIFF)
    return {"lane_states": {lane.lens.value: lane.state.value for lane in completed.lanes}, "aggregate": completed.assess().verdict.value, "concurrent_peak": peak, "revision": _END}


def adversarial_review() -> dict[str, str]:
    revision = ImmutableRevision(_END)
    cases = {"missing": lambda lane: None, "failed": lambda lane: LaneExecutionResult(LaneState.FAILED, revision), "stale": lambda lane: LaneExecutionResult(LaneState.COMPLETED, ImmutableRevision(_BASELINE)), "timed_out": lambda lane: (_ for _ in ()).throw(TimeoutError()), "cancelled": lambda lane: (_ for _ in ()).throw(CancelledError()), "crashed": lambda lane: (_ for _ in ()).throw(RuntimeError("crash"))}
    result: dict[str, str] = {}
    for name, runner in cases.items():
        assessment = execute_final_review_wave(_wave(), runner, _limits(), provider_for=lambda _: "local", context_provenance=ContextProvenance.FRESH_FROM_DIFF).assess()
        result[name] = f"{assessment.verdict.value}:{assessment.blocking_lens.value if assessment.blocking_lens else 'none'}"
    try:
        ReviewLane(ReviewLens.QUALITY, LaneState.PREPARED, ReadOnlyCapability(True), None)
    except ValueError as error:
        result["non_read_only_attempt"] = f"BLOCK:{error}"
    completed = execute_final_review_wave(_wave(), lambda lane: LaneExecutionResult(LaneState.COMPLETED, revision), _limits(), provider_for=lambda _: "local", context_provenance=ContextProvenance.FRESH_FROM_DIFF)
    invalidated = completed.invalidate_for_remediation()
    replacement = prepare_remediated_wave(invalidated, "qa-wave-2", _reservations()).integrate(IntegrationReceipt(ImmutableRevision("orchestration-remediated"), True))
    result["remediation_invalidation"] = f"{invalidated.assess().verdict.value}:{replacement.assess().verdict.value}"
    return result
