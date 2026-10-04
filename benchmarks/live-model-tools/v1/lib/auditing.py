from __future__ import annotations

from pathlib import Path
from typing import Any

from common import CONDITIONS, artifact_is_safe, digest, load_object


def audit(
    manifest_path: Path,
    report_path: Path,
    require_signoff: bool = False,
    signoff_path: Path | None = None,
) -> dict[str, Any]:
    manifest = load_object(manifest_path)
    report = load_object(report_path)
    checks = {
        "manifest_schema": (
            manifest.get("schema_version") == "omh_live_model_tool_benchmark/v1"
        ),
        "analysis_schema": (
            report.get("schema_version") == "omh_live_model_tool_analysis/v1"
        ),
        "development_evaluation_separated": (
            manifest.get("corpus", {}).get("development_seeds")
            != manifest.get("corpus", {}).get("evaluation_seeds")
        ),
        "digest_pinning_declared": (
            manifest.get("integrity", {}).get("algorithm") == "sha256"
        ),
        "no_secrets_or_absolute_paths": artifact_is_safe(report),
        "claim_boundary_matches_manifest": (
            report.get("claim_boundary") == manifest.get("claim_boundary")
        ),
        # Both compared arms must be declared benchmark conditions; a report
        # naming anything else was not produced by this benchmark's arms.
        "conditions_declared": (
            isinstance(report.get("conditions"), dict)
            and set(report["conditions"]) == {"baseline", "optimized"}
            and all(value in CONDITIONS for value in report["conditions"].values())
        ),
    }
    signoff = False
    if signoff_path and signoff_path.is_file():
        raw = load_object(signoff_path)
        signoff = bool(
            raw.get("reviewer") and raw.get("report_digest") == digest(report)
        )
    checks["independent_signoff"] = signoff if require_signoff else True

    gates: dict[str, bool] = {}
    for model, value in report.get("models", {}).items():
        interval = value.get("bootstrap_ci95", [-1.0, -1.0])
        holm_result = report.get("holm", {}).get(model, {})
        improvement = (
            value.get("delta", 0) >= 0.05
            and isinstance(interval, list)
            and len(interval) == 2
            and interval[0] > 0
        ) or (
            value.get("delta", 0) >= 0.10
            and holm_result.get("rejected", False)
        )
        gates[model] = bool(
            value.get("n", 0) >= 30
            and improvement
            and isinstance(interval, list)
            and len(interval) == 2
            and interval[0] >= -0.02
            and signoff
        )

    return {
        "schema_version": "omh_live_model_tool_audit/v1",
        "ok": all(checks.values()),
        "checks": checks,
        "claim_permitted": bool(gates) and all(gates.values()),
        "model_claim_gates": gates,
        "required_public_statement": (
            "A reproducible benchmark is available; current results are preliminary "
            "and are not a superiority claim."
        ),
    }
