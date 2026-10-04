"""The assembled fanout unit prompt, path by path.

`tests/fixtures/unit_prompt_golden_digests.json` holds the sha256 and byte
size of the exact prompt every dispatch path sends for a matrix of units. It
was captured from the string-mutating composer that `_dispatch_unit` used
before the prompt became a value, so a refactor of the assembly proves itself
byte-identical against it. An intentional prompt-text change moves digests;
regenerate with `_render_golden_fixture()` (see `GoldenDigestTests`) and check
that the cases named in the failure are the ones you meant to change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout_clarification_dispatch import parent_decision_prompt  # noqa: E402
from omh.coding.fanout_dispatch import build_unit_prompt  # noqa: E402
from omh.coding.fanout_repair import repair_brief_prompt  # noqa: E402

GOLDEN_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "unit_prompt_golden_digests.json"

GOAL = "Ship the bounded exporter flag across the dashboard units"

OWNERS = ("codex", "claude-code")
ROLES = ("implementation", "review")
EFFORTS = ("low", "high", "max")
# (selected_model, recorded model_family). kimi-k3 has no exact-model override,
# so it is the family-only case; the rest are every exact override plus the
# `-pro` projection of 6.1 Sol and the claude family's shipped head.
MODELS = (
    ("kimi-k3", "kimi"),
    ("gpt-6-astra", "gpt"),
    ("gpt-6.1-sol", "gpt"),
    ("gpt-6.1-sol-pro", "gpt"),
    ("deepseek-v4.1-flash", "deepseek"),
    ("claude-sonnet-5-5", "claude"),
    ("claude-fable-5-1", "claude"),
)
# The live variants `_dispatch_unit` can produce, plus the two it never pairs
# (repair without a sidecar contract, and repair with a parent decision) so a
# composer that only handled the live pairs could not pass by accident.
VARIANTS = (
    "bare",
    "sidecar",
    "sidecar+discovery",
    "sidecar+repair",
    "sidecar+parent",
    "sidecar+repair+retry",
    "sidecar+parent+retry",
    "repair",
    "sidecar+repair+parent",
)

DISCOVERIES = {
    "claude-code": {
        "schema_version": "executor_skill_discovery/v1",
        "executor_profile": "claude-code",
        "skills": [
            {"name": "omc-plan", "invocation": "/omc-plan", "role": "brain", "role_score": 5, "source": "claude_user_skills"},
            {"name": "ultrawork", "invocation": "/ultrawork", "role": "implementation", "role_score": 3, "source": "claude_user_skills"},
            {"name": "code-reviewer", "invocation": "/code-reviewer", "role": "review", "role_score": 4, "source": "claude_user_skills"},
        ],
    },
    "codex": {
        "schema_version": "executor_skill_discovery/v1",
        "executor_profile": "codex",
        "skills": [
            {"name": "review-pr", "invocation": "$review-pr", "role": "review", "role_score": 4, "source": "codex_skills"},
            {"name": "implement", "invocation": "$implement", "role": "implementation", "role_score": 3, "source": "codex_skills"},
        ],
    },
}

SIDECAR_PATH = "/tmp/omh-fanout-intake-golden/11111111-1111-4111-8111-111111111111.json"
RETRY_SIDECAR_PATH = "/tmp/omh-fanout-intake-golden/22222222-2222-4222-8222-222222222222.json"
REPAIR = {
    "attempt": 1,
    "max_repair_attempts": 3,
    "failing_checks": [
        {"command": "python -m unittest tests/test_exporter.py", "exit_code": 1, "failure_kind": "exit_nonzero"},
    ],
}
PARENT_DECISION = {
    "answer": {"decision_id": "decision-d1", "answer": "json"},
    "goal_attempt_id": "attempt-0001",
}


def golden_unit(owner: str, role: str, effort: str, model: str, family: str) -> dict[str, object]:
    """A hand-built contract unit, so the goldens never move with chain data."""
    unit: dict[str, object] = {
        "unit_id": "exporter",
        "run_ref": "fanout-0123456789ab-exporter",
        "title": "Add the bounded exporter flag",
        "boundary": {"file_scope": ["src/exporter/", "tests/test_exporter.py"], "do_not_touch": ["docs/"]},
        "branch_suggestion": "agent/exporter",
        "integration_checks": ["python -m unittest tests/test_exporter.py passes"],
        "handoff": {
            "executor_target": owner,
            "model_route": {
                "role": role,
                "selected_model": model,
                "selected_reasoning_effort": effort,
                "model_family": family,
            },
        },
    }
    if role == "review":
        unit["domain"] = "research"
    if owner == "codex" and role == "implementation":
        unit["input_budget"] = {
            "chars": 24000,
            "tokens": 6000,
            "source_ranges": [
                {"source": "src/exporter/core.py", "span": "lines 1-200", "offset": 0, "limit": 200, "end_line": 200, "estimated_chars": 8000},
            ],
        }
    return unit


def golden_cases():
    """Yield (case_id, path, unit, variant) for every golden case."""
    for owner in OWNERS:
        for role in ROLES:
            for effort in EFFORTS:
                for model, family in MODELS:
                    unit = golden_unit(owner, role, effort, model, family)
                    stem = f"{owner}|{role}|{effort}|{model}"
                    for variant in VARIANTS:
                        yield f"dispatch|{stem}|{variant}", "dispatch", unit, variant
                    yield f"recovery|{stem}", "recovery", unit, ""


def _sidecar_contract(unit: dict[str, object]) -> dict[str, str]:
    return {
        "path": SIDECAR_PATH,
        "unit_id": str(unit["unit_id"]),
        "run_id": str(unit["run_ref"]),
        "fanout_id": "fanout-0123456789ab",
        "base_sha": "a" * 40,
    }


def compose(path: str, unit: dict[str, object], variant: str) -> str:
    """The prompt the named path sends, composed the way `_dispatch_unit` did."""
    if path == "recovery":
        return build_unit_prompt(unit, GOAL)
    flags = set(variant.split("+"))
    owner = str(unit["handoff"]["executor_target"])  # type: ignore[index]
    discovery = DISCOVERIES[owner] if "discovery" in flags else None
    prompt = build_unit_prompt(
        unit,
        GOAL,
        discovery,
        unit_result_contract=_sidecar_contract(unit) if "sidecar" in flags else None,
    )
    if "repair" in flags:
        prompt += repair_brief_prompt(
            attempt=int(REPAIR["attempt"]),
            max_repair_attempts=int(REPAIR["max_repair_attempts"]),
            failing_checks=list(REPAIR["failing_checks"]),
        )
    if "parent" in flags:
        prompt += parent_decision_prompt(PARENT_DECISION)  # type: ignore[arg-type]
    if "retry" in flags:
        prompt = prompt.replace(SIDECAR_PATH, RETRY_SIDECAR_PATH)
    return prompt


def _render_golden_fixture() -> dict[str, object]:
    cases = {}
    for case_id, path, unit, variant in golden_cases():
        data = compose(path, unit, variant).encode("utf-8")
        cases[case_id] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return {"schema_version": "omh_unit_prompt_golden/v1", "goal": GOAL, "cases": cases}


class GoldenDigestTests(unittest.TestCase):
    def test_every_path_reproduces_its_golden_bytes(self) -> None:
        expected = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
        actual = _render_golden_fixture()
        self.assertEqual(sorted(actual["cases"]), sorted(expected["cases"]))
        moved = sorted(case for case, row in actual["cases"].items() if row != expected["cases"][case])
        self.assertEqual(moved, [], f"{len(moved)} golden prompts moved, first: {moved[:5]}")


if __name__ == "__main__":
    unittest.main()
