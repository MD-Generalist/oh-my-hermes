"""A staged self-update keeps the skills the active generation already serves.

`omh update` on an installer-managed command renders the workflow pack into a
fresh, empty candidate generation (`OMH_SELF_UPDATE_GENERATION`) and then
switches the shared `current` pointer to it. `install_skill_pack` decides what
to refresh from what is already on disk, so for the candidate it saw an empty
directory and wrote only `CORE_PROFILE_SKILLS` whenever the recorded profile
was `core`.

Every full-only skill the operator had kept on a core install (for example the
nine `ulw-*` engines) therefore disappeared on the next update, although the
contract is that an update never removes skills and only
`omh skill-profile reconcile --to core` narrows an install.

The fix reads the refresh set from both the candidate directory and the active
generation's pack behind the `current` pointer.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.installer import installed_skill_names
from omh.skill_pack import CORE_PROFILE_SKILLS

# Canonical names of the nine ULW engines (directories `ulw-*`).
ULW_LABELS = {
    "context",
    "deep-interview",
    "loop",
    "maestro",
    "ralplan",
    "research",
    "ultraperf",
    "ultraqa",
    "ultrawork",
}


def _staged_reentry(candidate: Path) -> dict[str, str]:
    """The environment `omh update` hands the candidate's own `omh update`."""
    return {
        "OMH_SELF_UPDATE_GENERATION": str(candidate),
        "OMH_UPDATE_COMMAND_PACKAGE_REENTERED": "1",
    }


class StagedUpdateKeepsInstalledSkillsTests(unittest.TestCase):
    def _base(self, root: Path) -> list[str]:
        return ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]

    def _managed_layout(self, root: Path) -> tuple[Path, Path]:
        """An installer-managed command root with an active generation."""
        command_root = root / "omh-command"
        active = command_root / "generations" / "active"
        (active / "skills").mkdir(parents=True)
        (command_root / "venv").mkdir(parents=True)
        current = command_root / "current"
        try:
            current.symlink_to(active, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable on this host")
        return command_root, active

    def _core_install_with_ulw(self, root: Path, active: Path) -> None:
        """A core install that also kept the ULW engines, like a user who
        installed full, stashed `ultrawork`, reconciled to core, and put it back."""
        env = {"OMH_SELF_UPDATE_GENERATION": str(active)}
        with mock.patch.dict(os.environ, env):
            status, _, stderr = run_cli(self._base(root) + ["install", "--full"])
            self.assertEqual(status, 0, stderr)
            keep = {name for name in installed_skill_names(active / "skills") if name in ULW_LABELS}
            self.assertEqual(keep, ULW_LABELS)
            status, _, stderr = run_cli(self._base(root) + ["install", "--core"])
            self.assertEqual(status, 0, stderr)
        manifest = json.loads((root / ".omh" / "manifest.json").read_text(encoding="utf-8"))
        manifest["skill_profile"] = "core"
        (root / ".omh" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_staged_update_keeps_full_only_skills_of_the_active_generation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            command_root, active = self._managed_layout(root)
            with mock.patch.dict(os.environ, {"OMH_VENV_DIR": str(command_root / "venv")}):
                self._core_install_with_ulw(root, active)
                before = set(installed_skill_names(active / "skills"))
                self.assertTrue(ULW_LABELS <= before)

                candidate = command_root / "generations" / "candidate"
                with mock.patch.dict(os.environ, _staged_reentry(candidate)):
                    status, _, stderr = run_cli(self._base(root) + ["update"])
                self.assertEqual(status, 0, stderr)

                after = set(installed_skill_names(candidate / "skills"))
                self.assertTrue(ULW_LABELS <= after, sorted(ULW_LABELS - after))
                self.assertEqual(after, before)

    def test_staged_update_of_a_plain_core_install_stays_core(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            command_root, active = self._managed_layout(root)
            with mock.patch.dict(os.environ, {"OMH_VENV_DIR": str(command_root / "venv")}):
                with mock.patch.dict(os.environ, {"OMH_SELF_UPDATE_GENERATION": str(active)}):
                    status, _, stderr = run_cli(self._base(root) + ["install", "--core"])
                    self.assertEqual(status, 0, stderr)

                candidate = command_root / "generations" / "candidate"
                with mock.patch.dict(os.environ, _staged_reentry(candidate)):
                    status, _, stderr = run_cli(self._base(root) + ["update"])
                self.assertEqual(status, 0, stderr)

                after = installed_skill_names(candidate / "skills")
                self.assertEqual(len(after), len(CORE_PROFILE_SKILLS))
                self.assertFalse(ULW_LABELS & set(after))


if __name__ == "__main__":
    unittest.main()
