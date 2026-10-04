from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import random
from typing import Any

from common import CONDITIONS, SCHEMA


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
            raise ValueError(f"invalid run record at line {number}")
        rows.append(value)
    return rows


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def macro(rows: list[dict[str, Any]]) -> float:
    return sum(bool(row["grade"]["pass"]) for row in rows) / len(rows) if rows else math.nan


def exact_mcnemar(
    baseline: list[dict[str, Any]], optimized: list[dict[str, Any]]
) -> dict[str, Any]:
    baseline_only = sum(
        bool(base["grade"]["pass"]) and not bool(opt["grade"]["pass"])
        for base, opt in zip(baseline, optimized, strict=True)
    )
    optimized_only = sum(
        not bool(base["grade"]["pass"]) and bool(opt["grade"]["pass"])
        for base, opt in zip(baseline, optimized, strict=True)
    )
    discordant = baseline_only + optimized_only
    if discordant == 0:
        pvalue = 1.0
    else:
        smaller = min(baseline_only, optimized_only)
        tail = sum(math.comb(discordant, index) for index in range(smaller + 1)) / (
            2**discordant
        )
        pvalue = min(1.0, 2 * tail)
    return {
        "baseline_only": baseline_only,
        "optimized_only": optimized_only,
        "discordant": discordant,
        "exact_two_sided_p": pvalue,
    }


def holm(
    pvalues: dict[str, float], alpha: float = 0.05
) -> dict[str, dict[str, Any]]:
    ordered = sorted(pvalues.items(), key=lambda item: item[1])
    result: dict[str, dict[str, Any]] = {}
    running = 0.0
    still_rejecting = True
    for index, (name, pvalue) in enumerate(ordered):
        multiplier = len(ordered) - index
        running = max(running, min(1.0, multiplier * pvalue))
        threshold = alpha / multiplier
        rejected = still_rejecting and pvalue <= threshold
        still_rejecting = still_rejecting and rejected
        result[name] = {
            "raw_p": pvalue,
            "adjusted_p": running,
            "rejected": rejected,
        }
    return result


def analyze(
    baseline_path: Path,
    optimized_path: Path,
    repetitions: int,
    seed: int,
    manifest: dict[str, Any],
    *,
    baseline_condition: str = "baseline",
    optimized_condition: str = "optimized",
) -> dict[str, Any]:
    # The two files are compared positionally; the condition each must carry
    # is a parameter so an exact-model override (`optimized`) can be paired
    # against the family block it replaced (`family`) as well as against the
    # bare contract (`baseline`).
    for side in (baseline_condition, optimized_condition):
        if side not in CONDITIONS:
            raise ValueError(f"unknown benchmark condition: {side}")
    baseline = _indexed(read_jsonl(baseline_path), baseline_condition)
    optimized = _indexed(read_jsonl(optimized_path), optimized_condition)
    if set(baseline) != set(optimized):
        raise ValueError("baseline and optimized coverage differ")
    for key in baseline:
        if baseline[key]["task_digest"] != optimized[key]["task_digest"]:
            raise ValueError(f"baseline and optimized task digests differ: {key}")
    _require_manifest_evaluation_matrix(baseline, manifest)
    _require_manifest_evaluation_matrix(optimized, manifest)

    rng = random.Random(seed)
    by_model: dict[
        tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]
    ] = defaultdict(list)
    for key in sorted(baseline):
        by_model[key[:2]].append((baseline[key], optimized[key]))

    models: dict[str, Any] = {}
    pvalues: dict[str, float] = {}
    for (provider, model_id), pairs in by_model.items():
        baseline_model = [pair[0] for pair in pairs]
        optimized_model = [pair[1] for pair in pairs]
        deltas = []
        for _ in range(repetitions):
            sample = [rng.choice(pairs) for _ in pairs]
            deltas.append(
                macro([pair[1] for pair in sample])
                - macro([pair[0] for pair in sample])
            )
        label = f"{provider}/{model_id}"
        mcnemar = exact_mcnemar(baseline_model, optimized_model)
        pvalues[label] = mcnemar["exact_two_sided_p"]
        models[label] = {
            "provider": provider,
            "model": model_id,
            "n": len(pairs),
            "baseline_pass_rate": macro(baseline_model),
            "optimized_pass_rate": macro(optimized_model),
            "delta": macro(optimized_model) - macro(baseline_model),
            "bootstrap_ci95": [
                percentile(deltas, 0.025),
                percentile(deltas, 0.975),
            ],
            "mcnemar": mcnemar,
            "observation": {
                field: {
                    condition: _observed_mean(
                        [pair[index]["observation"].get(field) for pair in pairs]
                    )
                    for condition, index in (("baseline", 0), ("optimized", 1))
                }
                for field in ("tools", "tokens", "cost_usd")
            },
            "per_instance": {
                pair[0]["instance_id"]: {
                    "baseline_pass": bool(pair[0]["grade"]["pass"]),
                    "optimized_pass": bool(pair[1]["grade"]["pass"]),
                }
                for pair in pairs
            },
        }
    return {
        "schema_version": "omh_live_model_tool_analysis/v1",
        "analysis_seed": seed,
        "conditions": {"baseline": baseline_condition, "optimized": optimized_condition},
        "models": models,
        "holm": holm(pvalues),
        "claim_boundary": manifest["claim_boundary"],
    }


def _indexed(
    rows: list[dict[str, Any]], condition: str
) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("condition") != condition:
            raise ValueError(f"{condition} file contains wrong condition")
        model = row.get("model")
        if isinstance(model, dict):
            provider = str(model.get("provider", "unknown"))
            model_id = str(model.get("id", "unknown"))
        else:
            provider, model_id = "offline", "fake-model"
        key = provider, model_id, str(row["instance_id"])
        if key in result:
            raise ValueError(f"duplicate run record: {key}")
        result[key] = row
    return result


def _observed_mean(values: list[object]) -> float | None:
    observed = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return sum(observed) / len(observed) if observed else None


def _require_manifest_evaluation_matrix(
    records: dict[tuple[str, str, str], dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    configured = {
        (str(model["provider"]), str(model["id"]))
        for model in manifest["models"]
        if model.get("live")
    }
    observed = {key[:2] for key in records}
    if observed == {("offline", "fake-model")}:
        return
    if observed != configured:
        raise ValueError("analysis model coverage does not match the live manifest matrix")
    expected_instances = {
        f"E-{template}-{seed}"
        for template, _task_class in __import__("corpus").TEMPLATES
        for seed in __import__("corpus").EVALUATION_SEEDS
    }
    for key, row in records.items():
        if row.get("split") != "evaluation":
            raise ValueError("claim analysis accepts evaluation split records only")
        if key[2] not in expected_instances:
            raise ValueError(f"unscheduled evaluation instance: {key[2]}")
    for identity in configured:
        actual = {key[2] for key in records if key[:2] == identity}
        if actual != expected_instances:
            raise ValueError(
                f"incomplete evaluation matrix for {identity[0]}/{identity[1]}"
            )
