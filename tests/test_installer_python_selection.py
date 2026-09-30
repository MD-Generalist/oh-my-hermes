"""`install.sh` must pick an interpreter the wheel can actually be installed into.

The reported failure, on a macOS machine with Homebrew Python installed:
`/usr/bin/python3` is 3.9 and stays first on PATH, install.sh built the venv
out of it without looking at its version, and pip refused several steps later
with `Package 'oh-my-hermes' requires a different Python: 3.9.6 not in
'>=3.11'`. The venv was left behind and no `omh` command existed.

Every interpreter here is a stub script, so these cases are about the
SELECTION and nothing else: no venv is created by CPython, no wheel is
downloaded, and no network is reachable from them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_ROOT / "install.sh"

# The only system commands the installer and the stubs are given. Putting
# `/usr/bin` itself on PATH hands the search every interpreter the host ships:
# `install.sh` probes `python3.14` by name before `python3.12`, so on a host
# with `/usr/bin/python3.14` (Fedora 44, reported in #1955) that one was chosen
# ahead of the stubs and the case measured the machine instead of the
# selection. `uv` is left out for the same reason: a host `uv` lists its own
# interpreters as candidates.
SYSTEM_TOOLS = (
    "sh", "awk", "sed", "grep", "tr", "tail", "uname", "dirname", "readlink",
    "ln", "mkdir", "cp", "chmod", "curl", "wget",
)

# Answers the version probe and nothing else: a 3.9 that would be chosen by
# presence alone and must not be.
STUB_OLD = """#!/bin/sh
case "$*" in
  *"import sys; print"*) echo "3.9"; exit 0 ;;
esac
echo "old python called with: $*" >&2
exit 1
"""

# A supported interpreter that also stands in for `-m venv` and `-m pip`, so
# the run reaches the end and the chosen interpreter is observable from what
# it was asked to build.
STUB_NEW = """#!/bin/sh
case "$*" in
  *"import sys; print"*) echo "3.12"; exit 0 ;;
esac
case "$2" in
  venv)
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    printf 'stub\\n' > "$3/bin/omh"
    chmod +x "$3/bin/omh"
    printf '%s\\n' "$0" > "$3/built-by"
    exit 0
    ;;
  pip) exit 0 ;;
esac
exit 0
"""


@unittest.skipIf(sys.platform == "win32", "install.sh is the POSIX installer; install.ps1 covers Windows")
class InstallerPythonSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in SYSTEM_TOOLS:
            found = shutil.which(name, path="/usr/bin:/bin")
            if found is not None:
                (self.tools / name).symlink_to(found)

    def _stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _run(self, **environment: str) -> subprocess.CompletedProcess[str]:
        # `env -i`-style: the installer must not inherit this suite's own
        # interpreter or PATH, or the case under test is not the one running.
        env = {
            "HOME": str(self.root),
            "PATH": f"{self.bin}:{self.tools}",
            # A local path, so no release lookup and no network.
            "OMH_PACKAGE_URL": str(self.root / "oh_my_hermes-0-py3-none-any.whl"),
            "OMH_LINK_COMMAND": "0",
            "OMH_RUN_SETUP": "0",
            "NO_COLOR": "1",
            **environment,
        }
        return subprocess.run(
            ["sh", str(INSTALLER)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_a_python3_older_than_the_wheel_floor_is_not_the_one_used(self) -> None:
        self._stub("python3", STUB_OLD)
        chosen = self._stub("python3.12", STUB_NEW)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        built_by = self.root / ".local" / "share" / "omh" / "venv" / "built-by"
        self.assertTrue(built_by.is_file(), result.stdout)
        self.assertEqual(built_by.read_text(encoding="utf-8").strip(), str(chosen))
        # The run says which interpreter it used, so the next report of this
        # failure arrives with the answer already in it.
        self.assertIn("Python: ", result.stdout)
        self.assertIn("3.12", result.stdout)

    def test_an_interpreter_the_operator_named_is_the_only_candidate(self) -> None:
        """A search that quietly overruled OMH_PYTHON would defeat the escape hatch."""
        old = self._stub("python3", STUB_OLD)
        self._stub("python3.12", STUB_NEW)

        result = self._run(OMH_PYTHON=str(old))

        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a usable Python 3.11+", result.stdout)
        self.assertIn("is Python 3.9", result.stdout)
        self.assertFalse((self.root / ".local" / "share" / "omh" / "venv").exists())

    def test_the_refusal_names_the_version_it_found_rather_than_only_the_rule(self) -> None:
        old = self._stub("python3", STUB_OLD)

        result = self._run(OMH_PYTHON=str(old))

        self.assertIn(f"  {old} is Python 3.9", result.stdout)
        self.assertIn("https://www.python.org/downloads/", result.stdout)

    def test_a_stub_that_prints_a_banner_before_the_version_is_refused(self) -> None:
        """The probe is anchored on a whole line, not searched for a number."""
        self._stub(
            "python3",
            '#!/bin/sh\ncase "$*" in *"import sys; print"*) echo "warning: banner"; echo "3.12";; esac\nexit 0\n',
        )

        result = self._run(OMH_PYTHON=str(self.bin / "python3"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a usable Python 3.11+", result.stdout)

    def test_a_named_interpreter_that_is_not_there_is_reported_as_missing(self) -> None:
        """A typo is the likeliest way OMH_PYTHON goes wrong, and "not a usable
        Python 3.11+" would send the person to look at the version of a file
        that does not exist."""
        result = self._run(OMH_PYTHON=str(self.bin / "pyhton3.12"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("was not found", result.stdout)
        self.assertNotIn("is not a usable", result.stdout)

    def test_the_interpreter_is_resolved_before_any_release_lookup(self) -> None:
        """Otherwise an unreachable GitHub hides the diagnosis this exists for."""
        self._stub("python3", STUB_OLD)

        result = self._run(
            OMH_PYTHON=str(self.bin / "python3"),
            OMH_PACKAGE_URL="",
            OMH_REPO_LATEST_URL="http://127.0.0.1:1/nope",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a usable Python 3.11+", result.stdout)
        self.assertNotIn("could not resolve the latest release", result.stdout)

    def test_provisioning_can_be_declined_without_naming_an_interpreter(self) -> None:
        """`OMH_PROVISION_PYTHON=0` is the opt-out; the alternative opt-out is
        setting OMH_PYTHON, which requires already having what is missing."""
        self._stub("python3", STUB_OLD)
        # A `uv` that would succeed loudly if it were ever reached.
        self._stub("uv", '#!/bin/sh\necho "uv was invoked" >&2\nexit 0\n')

        result = self._run(OMH_PROVISION_PYTHON="0", OMH_PYTHON=str(self.bin / "python3"))

        self.assertNotIn("uv was invoked", result.stdout + result.stderr)
        self.assertNotIn("Asking uv", result.stdout)

    def test_the_floor_matches_the_one_the_wheel_declares(self) -> None:
        """Two places state it; a drift between them is the failure this fixes."""
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.11"', pyproject)
        self.assertIn("OMH_MIN_PYTHON_MINOR=11", INSTALLER.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
