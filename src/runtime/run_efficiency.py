from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from ..plugin_bundle.omh.run_records import RUN_CONTEXT_BUDGET_SCHEMA_VERSION
from ..local_store import atomic_write_json, ensure_dir
from ..paths import OmhPaths
from ..system.metadata_safety import require_opaque_metadata_ref


RUN_EFFICIENCY_INPUT_SCHEMA_VERSION = "run_efficiency_input/v1"
RUN_EFFICIENCY_REPORT_SCHEMA_VERSION = "run_efficiency_report/v1"
_INPUT_KEYS = {"schema_version", "run_id", "context_budget", "observations"}
_OBSERVATION_KEYS = {"metric", "value", "source_ref"}
_METRICS = frozenset({"wall_clock_duration_ms", "tool_duration_ms", "context_bytes"})
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass(frozen=True)
class RunEfficiencyBudget:
    run_id: str
    budget_bytes: int
    emitted_bytes: int
    remaining_bytes: int
    surface_counts: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RUN_CONTEXT_BUDGET_SCHEMA_VERSION,
            "run_id": self.run_id,
            "budget_bytes": self.budget_bytes,
            "emitted_bytes": self.emitted_bytes,
            "remaining_bytes": self.remaining_bytes,
        }


@dataclass(frozen=True)
class RunEfficiencyObservation:
    metric: str
    value: int
    source_ref: str

    def to_dict(self) -> dict[str, object]:
        return {"metric": self.metric, "value": self.value, "source_ref": self.source_ref}


@dataclass(frozen=True)
class RunEfficiencyInput:
    run_id: str
    context_budget: RunEfficiencyBudget
    observations: tuple[RunEfficiencyObservation, ...]


def parse_run_efficiency_input(raw: object) -> RunEfficiencyInput:
    if not isinstance(raw, dict) or set(raw) != _INPUT_KEYS:
        raise ValueError("run efficiency input must use the exact run_efficiency_input/v1 fields")
    if raw.get("schema_version") != RUN_EFFICIENCY_INPUT_SCHEMA_VERSION:
        raise ValueError("unsupported run efficiency input schema")
    run_id = _required_run_id(raw.get("run_id"))
    budget = _parse_budget(raw.get("context_budget"), run_id)
    observations = _parse_observations(raw.get("observations"))
    return RunEfficiencyInput(run_id, budget, observations)


def build_run_efficiency_report(value: RunEfficiencyInput) -> dict[str, object]:
    budget = value.context_budget
    return {
        "schema_version": RUN_EFFICIENCY_REPORT_SCHEMA_VERSION,
        "run_id": value.run_id,
        "context_budget": budget.to_dict(),
        "context_utilization_ratio": f"{budget.emitted_bytes / budget.budget_bytes:.6f}",
        "surface_counts": dict(budget.surface_counts),
        "observations": [observation.to_dict() for observation in value.observations],
        "not_observed": {
            "provider": {"status": "not_observed"},
            "billing": {"status": "not_observed"},
            "cron": {"status": "not_observed"},
            "host": {"status": "not_observed"},
        },
        "claim_boundary": (
            "Run-efficiency reports are OMH-local metadata prepared from supplied observations, not provider, "
            "billing, cron, or host evidence. They do not intercept tools, collect sessions, or infer costs."
        ),
    }


def write_run_efficiency_report(paths: OmhPaths, report: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"))
    report_id = "efficiency_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    directory = _managed_efficiency_reports_dir(paths)
    path = directory / f"{report_id}.json"
    ensure_dir(directory, private=True)
    atomic_write_json(path, {**report, "report_id": report_id}, private=True)
    return {"written": True, "report_id": report_id, "path": str(path)}


def _parse_budget(raw: object, run_id: str) -> RunEfficiencyBudget:
    if not isinstance(raw, dict) or raw.get("schema_version") != RUN_CONTEXT_BUDGET_SCHEMA_VERSION:
        raise ValueError("context_budget must be an omh_run_context_budget/v1 object")
    if _required_run_id(raw.get("run_id")) != run_id:
        raise ValueError("context_budget.run_id must match run_id")
    budget_bytes = _nonnegative_int(raw.get("budget_bytes"), "budget_bytes")
    emitted_bytes = _nonnegative_int(raw.get("emitted_bytes"), "emitted_bytes")
    remaining_bytes = _nonnegative_int(raw.get("remaining_bytes"), "remaining_bytes")
    if budget_bytes <= 0:
        raise ValueError("context_budget.budget_bytes must be positive")
    if emitted_bytes > budget_bytes:
        raise ValueError("context_budget.emitted_bytes cannot exceed budget_bytes")
    if remaining_bytes != budget_bytes - emitted_bytes:
        raise ValueError("context_budget.remaining_bytes must equal budget_bytes minus emitted_bytes")
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, dict):
        raise ValueError("context_budget.surfaces must be an object")
    if len(surfaces) > 32:
        raise ValueError("context_budget.surfaces must contain at most 32 entries")
    counts: list[tuple[str, int]] = []
    for surface, count in surfaces.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("context_budget.surfaces must map names to nonnegative integer counts")
        counts.append((require_opaque_metadata_ref(surface, field="context_budget surface"), count))
    return RunEfficiencyBudget(run_id, budget_bytes, emitted_bytes, remaining_bytes, tuple(sorted(counts)))


def _parse_observations(raw: object) -> tuple[RunEfficiencyObservation, ...]:
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("run efficiency observations must contain at most 20 items")
    observations: list[RunEfficiencyObservation] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != _OBSERVATION_KEYS:
            raise ValueError("run efficiency observation must use metric, value, and source_ref")
        metric = item.get("metric")
        value = item.get("value")
        source_ref = item.get("source_ref")
        if metric not in _METRICS:
            raise ValueError("run efficiency observation metric is unsupported")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("run efficiency observation value must be a nonnegative integer")
        source_ref = require_opaque_metadata_ref(source_ref, field="run efficiency observation source_ref")
        observations.append(RunEfficiencyObservation(metric, value, source_ref))
    return tuple(observations)


def _required_run_id(value: object) -> str:
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise ValueError("run_id must contain 1 to 120 safe identifier characters")
    return require_opaque_metadata_ref(value, field="run_id")


def _nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"context_budget.{field} must be a nonnegative integer")
    return value


def _managed_efficiency_reports_dir(paths: OmhPaths) -> Path:
    root = paths.runtime_efficiency_reports_dir
    if root.is_symlink():
        raise ValueError("run efficiency report storage must not be a symlink")
    if not root.resolve(strict=False).is_relative_to(paths.omh_home.resolve(strict=False)):
        raise ValueError("run efficiency report storage must resolve under OMH home")
    return root
