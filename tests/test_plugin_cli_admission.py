"""Cold CLI imports must stay usable when an unsupported Hermes is importable."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PluginCliAdmissionTests(unittest.TestCase):
    def run_with_host(self, version: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory(prefix="omh-cli-host-admission-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            (root / "hermes_cli.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
            environment = {
                "PATH": os.defpath,
                "HOME": str(home),
                "USERPROFILE": str(home),
                "TMPDIR": str(root),
                "TEMP": str(root),
                "TMP": str(root),
                "PYTHONPATH": os.pathsep.join((str(root), str(ROOT / "src"))),
                "PYTHONDONTWRITEBYTECODE": "1",
                "OMH_HOME": str(home / ".omh"),
                "HERMES_HOME": str(home / ".hermes"),
            }
            if "SYSTEMROOT" in os.environ:
                environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            return subprocess.run(
                [sys.executable, *arguments], cwd=ROOT, env=environment,
                text=True, capture_output=True, timeout=30,
            )

    def test_maintenance_cli_starts_with_supported_and_unsupported_hosts(self) -> None:
        for version in ("0.21.0", "0.22.0", "0.21.1", "0.30.0"):
            for command in (["--help"], ["doctor"], ["update", "--help"]):
                with self.subTest(version=version, command=command):
                    result = self.run_with_host(version, ["-m", "omh.cli", *command])
                    self.assertEqual(result.stderr, "", result.stderr)
                    if command == ["doctor"]:
                        # An unconfigured profile needs attention, but returns diagnostics.
                        self.assertEqual(result.returncode, 1, result.stdout)
                        self.assertTrue(result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout)
                        self.assertTrue(result.stdout)

    def test_cold_imports_succeed_but_register_refuses_before_context_access(self) -> None:
        script = """
import json
from omh.plugin_bundle.omh import register
from omh.plugin_bundle.omh.domain_signals import SPECIALIST_DOMAIN_TRIGGERS
from omh.plugin_bundle.omh.host_compat import parse_range
from omh.plugin_bundle.omh.metadata import PROVIDED_TOOLS

assert SPECIALIST_DOMAIN_TRIGGERS and PROVIDED_TOOLS
assert parse_range(">=0.21.1")

class UntouchedContext:
    def __getattr__(self, name):
        raise AssertionError("context accessed before admission: " + name)

try:
    register(UntouchedContext())
except RuntimeError:
    print(json.dumps({"imported": True, "refused": True}))
else:
    raise AssertionError("unsupported host registered")
"""
        for version in ("0.21.0", "0.20.9"):
            with self.subTest(version=version):
                result = self.run_with_host(version, ["-c", script])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout), {"imported": True, "refused": True})

    def run_register_with_stamped_host(self, stamp_version: str, release_version: str) -> subprocess.CompletedProcess[str]:
        """A host whose `__version__` is the install-stamp value and whose
        `version_info` resolves the release, like hermes-agent main since the pm store."""
        with TemporaryDirectory(prefix="omh-stamped-host-") as directory:
            root = Path(directory)
            package = root / "hermes_cli"
            package.mkdir()
            (package / "__init__.py").write_text(f"__version__ = {stamp_version!r}\n", encoding="utf-8")
            (package / "version_info.py").write_text(
                "class _Info:\n"
                f"    base_version = {release_version!r}\n"
                "def get_version_info():\n"
                "    return _Info()\n",
                encoding="utf-8",
            )
            script = """
import json
from omh.plugin_bundle.omh import _admit_host
try:
    _admit_host()
except RuntimeError as exc:
    print(json.dumps({"admitted": False, "reason": str(exc)}))
else:
    print(json.dumps({"admitted": True}))
"""
            environment = {
                "PATH": os.defpath,
                "PYTHONPATH": os.pathsep.join((str(root), str(ROOT / "src"))),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            if "SYSTEMROOT" in os.environ:
                environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            return subprocess.run(
                [sys.executable, "-c", script], cwd=ROOT, env=environment,
                text=True, capture_output=True, timeout=30,
            )

    def test_admission_reads_the_release_a_stamp_less_checkout_resolves(self) -> None:
        # The "0.0.0" placeholder is what a git checkout without an install
        # stamp reports as `__version__`; the release comes from version_info.
        for release in ("0.21.5", "0.22.0", "0.30.0"):
            with self.subTest(release=release):
                result = self.run_register_with_stamped_host("0.0.0", release)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"admitted": True})

    def test_admission_still_refuses_an_unsupported_release_behind_a_stamp(self) -> None:
        for stamp, release in (("0.21.5", "0.21.0"), ("0.0.0", "0.21.0"), ("0.0.0", "unknown")):
            with self.subTest(stamp=stamp, release=release):
                result = self.run_register_with_stamped_host(stamp, release)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["admitted"], payload)
