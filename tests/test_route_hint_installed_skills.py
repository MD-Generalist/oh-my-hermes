"""The per-turn route hint names only skills this home installed (#1954).

A `--core` install holds ten of the catalog's skills, and the route hint and
the skill-candidate line rank the whole catalog. These tests install a real
core and full pack with the installer, drive `pre_llm_call` against them, and
pin the four outcomes: a core home emits only installed names, a full home
emits the same bytes as a home with no manifest (today's behaviour), an
unreadable manifest falls back to the whole catalog, and `omh doctor` warns
when the names the hint can emit are not on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
import unittest

from omh.installer import install_skill_pack
from omh.maintenance.doctor import run_doctor
from omh.paths import OmhPaths
from omh.plugin_bundle.omh import skill_shortlist
from omh.plugin_bundle.omh.awareness import (
    awareness_route_hint,
    awareness_route_hint_context_from_payload,
    route_hint_for_installed_skills,
)
from omh.plugin_bundle.omh.hooks import llm_hooks
from omh.plugin_bundle.omh.installed_skills import installed_skill_names, reset_installed_skill_cache
from omh.quality.route_hint_alignment import route_hint_alignment_cases
from omh.skill_pack import CORE_PROFILE_SKILLS, builtin_skill_templates
from omh.skills.catalog import omh_skill_display_name

# Selects `workflow-learning` with `doctor` second: the shape the report
# quoted, a full-only selected workflow on a core install.
WORKFLOW_LEARNING_MESSAGE = "how does workflow learning work in omh and why did it pick doctor"
# Ranks full-only skills (`omh-lifecycle-growth` first) for the candidate line.
CANDIDATE_MESSAGE = (
    "Activation fell after we changed the signup flow and new users churn in their first week. "
    "Where does retention break?"
)
_CANDIDATE_LINE = re.compile(r"Skills that may fit this request: (.*?)\. If one matches")
_SELECTED = re.compile(r"selected=([A-Za-z0-9_-]+)")
_ADJACENT = re.compile(r"adjacent_workflows=([^\n]*)\.$", re.MULTILINE)


def _catalog_names() -> frozenset[str]:
    names = {template.name for template in builtin_skill_templates()}
    return frozenset(names | {omh_skill_display_name(name) for name in names})


def _core_names() -> frozenset[str]:
    return frozenset(set(CORE_PROFILE_SKILLS) | {omh_skill_display_name(name) for name in CORE_PROFILE_SKILLS})


def _emitted_skill_names(context: str) -> set[str]:
    """Every catalog skill name the injected context puts in front of the model."""
    names = set(_SELECTED.findall(context))
    for match in _ADJACENT.findall(context):
        names.update(item.strip() for item in match.split(",") if item.strip())
    for match in _CANDIDATE_LINE.findall(context):
        names.update(option.split(" (", 1)[0] for option in match.split("; "))
    return names & _catalog_names()


def _paths(root: Path) -> OmhPaths:
    return OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes")


def _context(paths: OmhPaths, message: str, session: str) -> str:
    result = llm_hooks.pre_llm_call(
        user_message=message,
        session_id=session,
        omh_home=str(paths.omh_home),
        hermes_home=str(paths.hermes_home),
        is_first_turn=False,
    )
    return str((result or {}).get("context", ""))


class _Reset(unittest.TestCase):
    def setUp(self) -> None:
        reset_installed_skill_cache()
        skill_shortlist.reset_candidate_line_state()
        self.addCleanup(reset_installed_skill_cache)
        self.addCleanup(skill_shortlist.reset_candidate_line_state)


class PreLlmCallTests(_Reset):
    def test_a_core_install_names_only_installed_skills(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            install_skill_pack(paths, profile="core")

            route = _context(paths, WORKFLOW_LEARNING_MESSAGE, "s-route")
            candidates = _context(paths, CANDIDATE_MESSAGE, "s-candidates")

            self.assertIn("[OMH Route Hint]", route)
            emitted = _emitted_skill_names(route) | _emitted_skill_names(candidates)
            self.assertTrue(emitted, route)
            self.assertEqual(sorted(emitted - _core_names()), [])
            # The full-only selection fell through to the next ranked hint.
            self.assertNotIn("workflow-learning", route)
            self.assertIn("selected=doctor", route)
            self.assertNotIn("omh-lifecycle-growth", candidates)

    def test_a_full_install_emits_what_a_home_without_a_manifest_emits(self) -> None:
        with TemporaryDirectory() as tmp:
            full = _paths(Path(tmp) / "full")
            bare = _paths(Path(tmp) / "bare")
            install_skill_pack(full, profile="full")

            for index, message in enumerate((WORKFLOW_LEARNING_MESSAGE, CANDIDATE_MESSAGE)):
                skill_shortlist.reset_candidate_line_state()
                installed = _context(full, message, f"s-{index}")
                skill_shortlist.reset_candidate_line_state()
                unfiltered = _context(bare, message, f"s-{index}")
                self.assertTrue(unfiltered)
                self.assertEqual(installed, unfiltered)
            self.assertIn("workflow-learning", _context(full, WORKFLOW_LEARNING_MESSAGE, "s-again"))

    def test_an_unreadable_manifest_falls_back_to_the_whole_catalog(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            install_skill_pack(paths, profile="core")
            paths.manifest_path.write_text("{not json", encoding="utf-8")

            route = _context(paths, WORKFLOW_LEARNING_MESSAGE, "s-route")

            self.assertIn("selected=workflow-learning", route)


class InstalledSetTests(_Reset):
    def test_the_manifest_yields_canonical_names_and_labels(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            install_skill_pack(paths, profile="core")

            self.assertEqual(installed_skill_names(paths.omh_home), _core_names())

    def test_an_unknown_set_is_none(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIsNone(installed_skill_names(home))
            for text in ("{not json", "[]", '{"skills": {}}', '{"skills": []}'):
                (home / "manifest.json").write_text(text, encoding="utf-8")
                reset_installed_skill_cache()
                self.assertIsNone(installed_skill_names(home), text)

    def test_a_rewritten_manifest_is_read_again(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            manifest = home / "manifest.json"
            manifest.write_text(json.dumps({"skills": [{"name": "plan", "path": "omh/omh-plan/SKILL.md"}]}))
            self.assertEqual(installed_skill_names(home), frozenset({"plan", "omh-plan"}))
            manifest.write_text(json.dumps({"skills": [{"name": "doctor", "path": "omh/omh-doctor/SKILL.md"}]}))
            stat = manifest.stat()
            os.utime(manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            self.assertEqual(installed_skill_names(home), frozenset({"doctor", "omh-doctor"}))


class FilterTests(unittest.TestCase):
    def test_every_alignment_case_emits_only_core_names_on_a_core_set(self) -> None:
        core = _core_names()
        checked = 0
        for case in route_hint_alignment_cases():
            payload = awareness_route_hint(case.message)
            context = awareness_route_hint_context_from_payload(route_hint_for_installed_skills(payload, core))
            candidates = skill_shortlist.skill_candidates_for_turn(
                case.message, route_hint_payload=payload, installed=core
            )
            context += "\n" + skill_shortlist.skill_candidate_line(candidates)
            self.assertEqual(sorted(_emitted_skill_names(context) - core), [], case.message)
            checked += bool(payload.get("status") == "hinted")
        self.assertGreater(checked, 0)

    def test_the_whole_catalog_changes_nothing(self) -> None:
        catalog = _catalog_names()
        for case in route_hint_alignment_cases():
            payload = awareness_route_hint(case.message)
            self.assertIs(route_hint_for_installed_skills(payload, catalog), payload)
            self.assertIs(route_hint_for_installed_skills(payload, None), payload)
            self.assertEqual(
                skill_shortlist.skill_candidates_for_turn(case.message, route_hint_payload=payload, installed=catalog),
                skill_shortlist.skill_candidates_for_turn(case.message, route_hint_payload=payload),
            )

    def test_a_selection_with_nothing_installed_behind_it_renders_no_block(self) -> None:
        payload = awareness_route_hint(WORKFLOW_LEARNING_MESSAGE)
        filtered = route_hint_for_installed_skills(payload, frozenset({"plan", "omh-plan"}))

        self.assertEqual(filtered["status"], "no_hint")
        self.assertEqual(filtered["selected_workflow"], "")
        self.assertEqual(awareness_route_hint_context_from_payload(filtered), "")
        # The cached payload the builder returned is not mutated.
        self.assertEqual(payload["selected_workflow"], "workflow-learning")

    def test_names_outside_the_catalog_are_left_alone(self) -> None:
        payload = {
            "status": "hinted",
            "selected_workflow": "coding handoff",
            "adjacent_workflows": ["coding handoff"],
            "hints": [{"workflow": "coding handoff", "adjacent_workflows": ["coding handoff"]}],
        }
        self.assertIs(route_hint_for_installed_skills(payload, frozenset({"plan"})), payload)


class DoctorTests(_Reset):
    def _check(self, paths: OmhPaths):
        reset_installed_skill_cache()
        return next(check for check in run_doctor(paths) if check.name == "route_hint_skills")

    def test_a_consistent_install_passes(self) -> None:
        for profile in ("core", "full"):
            with TemporaryDirectory() as tmp:
                paths = _paths(Path(tmp))
                install_skill_pack(paths, profile=profile)
                check = self._check(paths)
                self.assertEqual((check.ok, check.severity), (True, "ok"), check.message)

    def test_a_missing_manifest_beside_a_core_install_warns_with_the_names(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            install_skill_pack(paths, profile="core")
            paths.manifest_path.unlink()

            check = self._check(paths)

            self.assertTrue(check.ok)
            self.assertEqual(check.severity, "warning")
            self.assertIn("whole catalog", check.message)
            missing = check.detail["not_installed"]
            self.assertIn("omh-workflow-learning", missing)
            self.assertEqual(len(missing), len(builtin_skill_templates()) - len(CORE_PROFILE_SKILLS))
            self.assertIn("omh update", check.next_action)

    def test_a_manifest_naming_a_deleted_skill_warns_with_its_name(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            install_skill_pack(paths, profile="full")
            gone = paths.skills_dir / next(
                (entry for entry in paths.skills_dir.rglob("omh-localization-review") if entry.is_dir())
            ).relative_to(paths.skills_dir)
            shutil.rmtree(gone)

            check = self._check(paths)

            self.assertEqual(check.severity, "warning")
            self.assertEqual(check.detail["not_installed"], ["omh-localization-review"])
            self.assertIn("omh-localization-review", check.message)

    def test_an_empty_home_is_not_observed(self) -> None:
        with TemporaryDirectory() as tmp:
            check = self._check(_paths(Path(tmp)))
            self.assertEqual((check.ok, check.observed), (True, False))


if __name__ == "__main__":
    unittest.main()
