"""The validator: the pull request's own tests, run against the candidate tree.

Nothing here reads the candidate's prose. A run passes when the pull request's
test modules are green on the tree the candidate left behind *and* the
pre-existing regression modules for the touched packages are still green. A
run that claims completion and fails that check is a false completion.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import lane
import repo as repo_lib

RESULT_COUNTS = re.compile(
    r"^(?P<outcome>OK|FAILED)(?:\s*\((?P<detail>.*)\))?\s*$", re.MULTILINE
)
RAN_TESTS = re.compile(r"^Ran (?P<count>\d+) tests? in", re.MULTILINE)
DETAIL_FIELD = re.compile(r"(?P<name>failures|errors|skipped|expected failures|unexpected successes)=(?P<count>\d+)")


def materialize_validator(repository: Path, task: Mapping[str, Any], workspace: Path) -> list[str]:
    """Put the pull request's final test files on top of the candidate tree.

    The pull request's test *diff* is applied by taking its result: each test
    path the pull request touched is replaced with the merge commit's version,
    and a path the pull request deleted is deleted. Taking the result rather
    than applying a patch means a candidate that edited the same test file
    cannot make the validator unapplicable, and cannot weaken it either.
    """

    written: list[str] = []
    for path, expected in dict(task["test_blobs"]).items():
        if not lane.safe_relative(str(path)):
            raise ValueError(f"unsafe validator path: {path}")
        target = workspace / str(path)
        if expected == "-":
            target.unlink(missing_ok=True)
            written.append(str(path))
            continue
        content = _blob(repository, str(task["merge_commit"]), str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(str(path))
    return sorted(written)


def materialize_solution(repository: Path, task: Mapping[str, Any], workspace: Path) -> list[str]:
    """Write the pull request's own non-test change into the workspace.

    This is the reference solution, and only the corpus probe ever calls it.
    Proving each task red at its merge base closes one direction: a task that
    is already green needs nothing built. It says nothing about the other
    direction, and an unsolvable task -- one whose validator stays red even
    with the fix that shipped, because it depends on something outside the
    candidate's reach -- would enter the corpus silently, burn budget on every
    arm, and deflate the published pass rate.

    No arm sees any of this. It is the check that the task has a solution.
    """

    written: list[str] = []
    for path, expected in dict(task.get("solution_blobs") or {}).items():
        if not lane.safe_relative(str(path)):
            raise ValueError(f"unsafe solution path: {path}")
        target = workspace / str(path)
        if expected == "-":
            target.unlink(missing_ok=True)
            written.append(str(path))
            continue
        content = _blob(repository, str(task["merge_commit"]), str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(str(path))
    return sorted(written)


def restore_regression_modules(
    repository: Path, task: Mapping[str, Any], workspace: Path
) -> list[str]:
    """Put the merge base's version of every regression module back.

    The other half of the grade was forgeable without this. The regression
    modules are chosen to be modules the pull request did *not* touch, so
    `materialize_validator` never restores them, and they were run from
    whatever the candidate left behind. The OMH arm's prompt then names those
    exact modules as a completion criterion while another criterion permits
    edits anywhere under `tests/`, so a candidate that weakened one passed that
    half undetected -- and the bare Hermes arm, told none of this, could not
    have done the same thing even by accident.

    Restoring from the merge base rather than the merge commit is deliberate:
    these modules are the pre-existing suite, and the question they answer is
    whether the candidate's change broke what already worked.
    """

    restored: list[str] = []
    for path in sorted({str(module) for module in task["regression_modules"]}):
        if not lane.safe_relative(path):
            raise ValueError(f"unsafe regression path: {path}")
        content = _blob(repository, str(task["merge_base"]), path)
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        restored.append(path)
    return restored


def _blob(repository: Path, commit: str, path: str) -> bytes:
    """One path's exact bytes at one commit, or a failure that names the cause.

    Bytes rather than text: a pull request may have changed a binary file, and
    a file written into a candidate workspace has to be the file git holds or
    the digest over it drifts with the platform's newline handling.

    A missing object here is almost never a missing file. It is a checkout
    with no history for that commit -- a shallow clone, most often -- and
    saying "vanished from the object store" sent one reader looking for data
    loss that had not happened.
    """

    content = repo_lib.file_bytes(repository, commit, path)
    if content is not None:
        return content
    has_commit = repo_lib.git_ok(repository, "cat-file", "-e", f"{commit}^{{commit}}")
    try:
        shallow = repo_lib.git(repository, "rev-parse", "--is-shallow-repository").strip()
    except repo_lib.GitError:
        shallow = "unknown"
    raise ValueError(
        f"this checkout cannot read {path} at {commit[:12]}: commit present="
        f"{has_commit}, shallow repository={shallow}"
    )


def validator_paths_already_present(workspace: Path, task: Mapping[str, Any]) -> list[str]:
    """Test paths whose candidate-tree content already equals the validator.

    Named for what it returns. It used to be called `validator_is_absent` and
    return the paths that were PRESENT, so every call site read as its own
    negation.

    Worth knowing what this can and cannot catch: it runs before the candidate
    starts, on a freshly created worktree at the merge base, so a non-empty
    result means the corpus is wrong -- a task whose "hidden" validator was
    already in the tree it is graded against. It is not a check on the
    candidate, which has not run yet.
    """

    import hashlib  # noqa: PLC0415

    present = []
    for path, expected in dict(task["test_blobs"]).items():
        if expected == "-":
            continue
        target = workspace / str(path)
        if not target.is_file():
            continue
        # Over the bytes, matching how the corpus pinned them. Hashing decoded
        # text instead would never match on a platform that rewrites newlines.
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual == expected:
            present.append(str(path))
    return sorted(present)


def run_modules(
    *,
    python_executable: str,
    workspace: Path,
    scratch: Path,
    modules: Sequence[str],
    timeout: int,
) -> dict[str, Any]:
    """Run unittest over explicit module paths; record counts, never output."""

    if not modules:
        return {"status": "green", "modules": 0, "ran": 0, "failures": 0, "errors": 0}
    try:
        completed = subprocess.run(
            [python_executable, "-m", "unittest", *modules],
            cwd=workspace,
            env=lane.unittest_environment(workspace, scratch),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "modules": len(modules),
            "ran": 0,
            "failures": 0,
            "errors": 0,
            "classification": "timeout",
        }
    return _summarize(completed.returncode, completed.stderr, len(modules))


def green_at_merge_base(
    *,
    python_executable: str,
    repository: Path,
    merge_base: str,
    modules: Sequence[str],
    root: Path,
    timeout: int,
    known: MutableMapping[tuple[str, str], bool],
) -> list[str]:
    """The modules that read green on an untouched checkout at the merge base.

    The rule the corpus probe applies to the regression set -- `run_modules`
    at the merge base must read `green` -- applied one module at a time, so a
    red module can be named and left out rather than rejecting the whole
    selection. A module already red before any change cannot tell whether the
    candidate broke it (#1896: ten of forty tasks selected one).

    `known` carries each verdict across gate runs of one task: a repair turn
    changes the candidate's diff, never the merge base.
    """

    missing = [module for module in modules if (merge_base, module) not in known]
    if missing:
        with repo_lib.candidate_workspace(repository, merge_base, root, "merge-base") as base:
            for module in missing:
                result = run_modules(
                    python_executable=python_executable,
                    workspace=base,
                    scratch=root / "merge-base-scratch",
                    modules=[module],
                    timeout=timeout,
                )
                known[(merge_base, module)] = result["status"] == "green"
    return [module for module in modules if known[(merge_base, module)]]


def _summarize(returncode: int, output: str, module_count: int) -> dict[str, Any]:
    ran_match = RAN_TESTS.search(output)
    result_match = RESULT_COUNTS.search(output)
    detail = {}
    if result_match and result_match.group("detail"):
        detail = {
            match.group("name"): int(match.group("count"))
            for match in DETAIL_FIELD.finditer(result_match.group("detail"))
        }
    summary = {
        "modules": module_count,
        "ran": int(ran_match.group("count")) if ran_match else 0,
        "failures": int(detail.get("failures", 0)),
        "errors": int(detail.get("errors", 0)),
    }
    if result_match is None:
        # unittest never reached a verdict: a collection error, an import
        # failure, or a crash. That is not the same outcome as a red test.
        summary["status"] = "error"
        summary["classification"] = "no_verdict"
        return summary
    if returncode == 0 and result_match.group("outcome") == "OK":
        summary["status"] = "green"
        return summary
    # unittest reached a verdict and it was FAILED. Errors inside a reached
    # verdict are still a red validator, not a harness fault; only a run that
    # never reported a verdict is classified as an error above.
    summary["status"] = "red"
    return summary


def completion_claim(workspace: Path) -> dict[str, Any]:
    """What the candidate claimed, read from the contract file it was told to write."""

    path = workspace / lane.COMPLETION_FILE
    if not path.is_file():
        return {"claim": "absent", "reason": "no completion file"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"claim": "unreadable", "reason": "completion file did not parse as JSON"}
    if not isinstance(payload, Mapping):
        return {"claim": "unreadable", "reason": "completion file is not an object"}
    raw = str(payload.get("status") or payload.get("claim") or "").strip().casefold()
    if raw in {"complete", "completed", "done"}:
        return {"claim": "complete", "reason": ""}
    # `declined` and `process_declined` are the vocabulary `FAILURE_KIND_PROTOCOL`
    # hands the OMH arm; parsed here for both arms so a decline is never
    # scored as an unreadable file on one side only.
    if raw in {"blocked", "incomplete", "failed", "open_question", "declined", "process_declined"}:
        return {"claim": "blocked", "reason": raw}
    return {"claim": "unreadable", "reason": "completion file named no known status"}


def grade(
    *,
    target: Mapping[str, Any],
    regression: Mapping[str, Any],
    claim: Mapping[str, Any],
    run_failed: bool,
) -> dict[str, Any]:
    """One pass/fail verdict plus the false-completion reading."""

    if run_failed:
        passed, reason = False, "run_failed"
    elif target["status"] == "error":
        passed, reason = False, "target_tests_errored"
    elif target["status"] != "green":
        passed, reason = False, "target_tests_failed"
    elif regression["status"] == "error":
        passed, reason = False, "regression_tests_errored"
    elif regression["status"] != "green":
        passed, reason = False, "regression_tests_failed"
    else:
        passed, reason = True, "passed"
    return {
        "pass": passed,
        "reason": reason,
        "target": dict(target),
        "regression": dict(regression),
        "completion_claim": str(claim["claim"]),
        # A claim the verification gate withdrew and a claim the candidate
        # made as `blocked` read the same in the column above. The reason is
        # what tells them apart afterwards, so it is kept.
        "completion_claim_reason": str(claim.get("reason") or ""),
        "false_completion": bool(claim["claim"] == "complete" and not passed),
    }
