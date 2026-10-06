"""Plain-file repository fixtures; no resolver mocks or git subprocesses."""
from __future__ import annotations

import json
from pathlib import Path

from _local_package import load_local_package

load_local_package()
from omh.paths import resolve_paths as _resolve_paths
from omh.profiles.setup import write_setup_profile
from omh.plugin_bundle.omh.project_identity import resolve_project_identity

PROJECT_IDENTITY = "prj:" + "a" * 64


def seed_project_identity(root: Path, identity: str = PROJECT_IDENTITY) -> str:
    path = root / ".omh" / "project-identity.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "project_identity_file/v1", "identity": identity,
                                   "created_at": "2026-01-01T00:00:00Z", "resolver_version": "project_identity/v2"}), encoding="utf-8")
        path.chmod(0o600)
    return resolve_project_identity(root).identity


def seed_repository_remote(root: Path) -> str:
    gitdir = root / ".git"
    gitdir.mkdir(parents=True, exist_ok=True)
    (gitdir / "config").write_text(f'[remote "origin"]\n url = https://example.invalid/{root.name}.git\n', encoding="utf-8")
    return resolve_project_identity(root).identity


def memory_paths(omh_home, hermes_home=None, **kwargs):
    """Give a memory test's named checkout explicit repository evidence.

    It also pins the review-first policy when no profile exists. These tests
    exercise the capture -> review -> approve path, which stopped being the
    default when auto-safe became it; a test of the default writes no profile
    and resolves its paths without this fixture.
    """
    paths = _resolve_paths(omh_home, hermes_home, **kwargs)
    if resolve_project_identity(paths.omh_home.parent).state != "resolved":
        seed_project_identity(paths.omh_home.parent)
    pin_review_first(paths)
    return paths


def pin_review_first(paths) -> None:
    """Write an explicit review-first profile unless one is already there."""
    if not paths.setup_profile_path.exists():
        write_setup_profile(paths, [], memory_mode="review-first")


def project_identity(root):
    return resolve_project_identity(root).identity
