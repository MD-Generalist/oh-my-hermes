from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

SCHEMA = "omh_live_model_tool_run/v1"
# Every benchmark arm, in one place: bench.py, analyze.py, the runner, the
# run-record schema, and the audit all read this tuple, so an arm added here
# is either wired everywhere or fails a test that compares them.
CONDITIONS = ("baseline", "optimized", "family", "unit", "unit_lean")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|bearer\s+\S+|api[_-]?key|password|credential)",
    re.IGNORECASE,
)
CREDENTIAL_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL", "API_KEY", "OPENAI_",
    "ANTHROPIC_", "AWS_", "AZURE_", "GOOGLE_", "GITHUB_", "CLAUDE_", "CODEX_",
)
FAILURE_CODES = {
    "model_unavailable", "authentication_failed", "provider_error", "rate_limited",
    "adapter_protocol_error", "invalid_final_json", "timeout", "process_crash",
    "cleanup_failed", "output_limit_exceeded", "no_tool_use", "wrong_tool",
    "mutation_outside_scope", "first_attempt_failed", "tests_failed",
    "semantic_validator_failed", "missing_fact", "unsupported_fact", "invalid_citation",
    "search_false_positive", "search_false_negative", "lsp_not_used", "lsp_wrong_location",
    "diagnostics_not_clean", "routing_wrong_field", "silent_explicit_substitution",
    "usage_unavailable",
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: Path) -> str:
    entries = []
    excluded_parts = {".git", "__pycache__", ".venv", ".pytest_cache"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if excluded_parts.intersection(relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"workspace symlink is not allowed: {relative}")
        if not path.is_file():
            continue
        entries.append((path.relative_to(root).as_posix(), file_digest(path)))
    return digest(entries)


def load_object(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value
    raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain an object")
    return raw


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _open_regular_output(path, append=False)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _open_regular_output(path, append=True)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        output.write(canonical(value).decode() + "\n")


def _open_regular_output(path: Path, *, append: bool) -> int:
    if path.is_symlink():
        raise OSError(f"benchmark output path must not be a symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if append:
        flags |= os.O_APPEND
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, linked):
            raise OSError("benchmark output must remain the opened regular file")
        if not append:
            os.ftruncate(descriptor, 0)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def artifact_is_safe(value: Any) -> bool:
    pending = [value]
    forbidden_keys = {"prompt", "prompt_body", "credential", "credentials", "api_key", "password", "secret"}
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if key.casefold() in forbidden_keys:
                    return False
                pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str):
            if SECRET.search(current) or current.startswith(("/Users/", "/home/")):
                return False
    return True


def redact_bytes(raw: bytes) -> bytes:
    text=raw.decode("utf-8","replace")
    return SECRET.sub("[REDACTED]",text).encode("utf-8")


def sanitized_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    if extra:
        for key, value in extra.items():
            upper = key.upper()
            if any(marker in upper for marker in CREDENTIAL_MARKERS) or SECRET.search(value):
                raise ValueError("credential environment rejected")
            env[key] = value
    return env
