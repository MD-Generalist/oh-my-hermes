"""#815: a contested OMH name names every source, its owner, and the safe repair.

Three acceptance criteria are pinned here.

AC1 -- diagnosis identifies all visible competing sources. Covered per kind:
a skill directory, a plugin-declared command name, and a plugin-declared hook
name, each asserting that *both* sides of the contest appear with an ownership
label rather than only the foreign one.

AC2 -- unknown precedence becomes a warning or a blocker. The rule lives in
`EXCLUSIVE_NAME_KINDS` in the module under test and both branches are exercised
against the same unknown precedence, so the split is the stated rule and not the
kind of conflict that happened to be built first.

AC3 -- no user-owned asset is rewritten or removed. `NoWriteTests` snapshots
every byte and mtime under a tree holding a user-owned colliding asset, runs the
diagnosis, and compares.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.commands import setup as setup_commands
from omh.config_adapter import ensure_external_dir, read_config, write_config
from omh.install.identity_conflicts import (
    EXCLUSIVE_NAME_KINDS,
    IDENTITY_CONFLICT_REPORT_KEYS,
    IDENTITY_CONFLICT_REPORT_SCHEMA_VERSION,
    OWNERSHIPS,
    PRECEDENCE_STATES,
    SOURCE_KINDS,
    build_identity_conflict_report,
    validate_identity_conflict_report,
)
from omh.local_store import atomic_write_text
from omh.maintenance.doctor import run_doctor
from omh.manifest import new_manifest, skill_records, write_manifest
from omh.paths import resolve_paths
from omh.plugin_bundle.omh.metadata import OPTIONAL_HOOKS, PROVIDED_TOOLS, REQUIRED_HOOKS
from omh.plugin_pack import install_plugin_bundle
from omh.skill_pack import CORE_SKILLS, builtin_skill_templates
from omh.skills.catalog import omh_skill_display_name

_DEFAULT_DIRS = object()

CONTESTED_SKILL = CORE_SKILLS[1]
CONTESTED_COMMAND = PROVIDED_TOOLS[0]
CONTESTED_REQUIRED_HOOK = REQUIRED_HOOKS[0]
CONTESTED_OPTIONAL_HOOK = OPTIONAL_HOOKS[0]


def _install_managed_skills(skills_dir: Path) -> dict[str, object]:
    """Render and manifest OMH's core skills the way the installer does.

    `atomic_write_text` rather than `Path.write_text`: it writes with
    `newline=""`, so a `\\n` in a template stays a `\\n` on disk. Default text
    mode would translate to CRLF on Windows and change bytes this suite later
    compares for the AC3 no-write proof.
    """
    wanted = frozenset(CORE_SKILLS)
    for template in builtin_skill_templates():
        if template.name not in wanted:
            continue
        path = skills_dir / omh_skill_display_name(template.name) / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, template.content)
    return new_manifest("builtin", skills_dir, skill_records(skills_dir, "builtin"))


def _write_foreign_plugin(plugins_dir: Path, name: str, *, tools: tuple[str, ...] = (), hooks: tuple[str, ...] = ()) -> Path:
    """A local Hermes plugin OMH did not install, declaring the given names."""
    directory = plugins_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", 'description: "a local plugin OMH did not install"']
    if tools:
        lines.append("provides_tools:")
        lines.extend(f"  - {tool}" for tool in tools)
    if hooks:
        lines.append("provides_hooks:")
        lines.extend(f"  - {hook}" for hook in hooks)
    atomic_write_text(directory / "plugin.yaml", "\n".join(lines) + "\n")
    return directory


class _Fixture:
    """A home with OMH's skills and bridge installed, plus whatever a test adds."""

    def __init__(self, root: Path) -> None:
        self.paths = resolve_paths(root / ".omh", root / ".hermes")
        self.manifest = _install_managed_skills(self.paths.skills_dir)
        write_manifest(self.paths.manifest_path, self.manifest)
        self.paths.hermes_home.mkdir(parents=True, exist_ok=True)
        write_config(self.paths.hermes_config_path, "version: 1\n")
        install_plugin_bundle(self.paths)
        self.configured_dirs: list[str] = [self.paths.skills_dir.as_posix()]

    def register_skill_dir(self, directory: Path) -> None:
        write_config(
            self.paths.hermes_config_path,
            ensure_external_dir(read_config(self.paths.hermes_config_path), directory).text,
        )
        self.configured_dirs.append(directory.as_posix())

    def report(self, *, configured_skill_dirs: list[str] | None | object = _DEFAULT_DIRS) -> dict[str, object]:
        # A sentinel default, because `None` is a meaningful argument here: it
        # is how a caller says the Hermes config could not be read at all.
        dirs = self.configured_dirs if configured_skill_dirs is _DEFAULT_DIRS else configured_skill_dirs
        return build_identity_conflict_report(
            skills_dir=self.paths.skills_dir,
            manifest=self.manifest,
            plugins_dir=self.paths.hermes_plugins_dir,
            configured_skill_dirs=dirs,  # type: ignore[arg-type]
        )


def _conflict(report: dict[str, object], kind: str, name: str) -> dict[str, object]:
    matches = [
        item
        for item in report["conflicts"]  # type: ignore[index]
        if item["kind"] == kind and item["name"] == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {kind} conflict on {name!r}, got {matches}")
    return matches[0]


def _ownership_map(conflict: dict[str, object]) -> dict[str, str]:
    return {str(source["location"]): str(source["ownership"]) for source in conflict["sources"]}  # type: ignore[index]


class ContractShapeTests(unittest.TestCase):
    def test_a_clean_report_validates_and_carries_the_declared_vocabulary(self) -> None:
        with TemporaryDirectory() as tmp:
            report = _Fixture(Path(tmp)).report()

        self.assertEqual(validate_identity_conflict_report(report), [])
        self.assertEqual(sorted(report), sorted(IDENTITY_CONFLICT_REPORT_KEYS))
        self.assertEqual(report["schema_version"], IDENTITY_CONFLICT_REPORT_SCHEMA_VERSION)
        self.assertEqual(report["precedence"], "uncontested")
        self.assertEqual(report["severity"], "ok")
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["unreadable"], [])
        self.assertEqual(report["scanned"], {"plugin_dirs": 1, "skill_dirs": 0})
        self.assertIn("Hermes runtime precedence is not observed", str(report["claim_boundary"]))

    def test_the_validator_rejects_an_unknown_key_and_an_unknown_vocabulary_value(self) -> None:
        with TemporaryDirectory() as tmp:
            report = _Fixture(Path(tmp)).report()

        extra = {**report, "resolved_winner": "omh"}
        self.assertIn(
            "identity_conflict_report has unsupported keys: ['resolved_winner']",
            validate_identity_conflict_report(extra),
        )
        # A resolved order is not in the vocabulary at all: OMH cannot observe
        # Hermes precedence, so there is no value for it to report.
        self.assertNotIn("resolved", PRECEDENCE_STATES)
        decided = {**report, "precedence": "omh_wins"}
        self.assertTrue(
            any("precedence must be one of" in error for error in validate_identity_conflict_report(decided))
        )

    def test_the_validator_rejects_a_conflict_that_names_only_one_side(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "notes", tools=(CONTESTED_COMMAND,))
            report = fixture.report()

        conflict = _conflict(report, "command", CONTESTED_COMMAND)
        halved = {
            **report,
            "conflicts": [{**conflict, "sources": conflict["sources"][:1]}],  # type: ignore[index]
        }
        self.assertTrue(
            any("at least two sources" in error for error in validate_identity_conflict_report(halved))
        )

    def test_the_report_is_deterministic_across_repeated_scans(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(
                fixture.paths.hermes_plugins_dir,
                "notes",
                tools=(CONTESTED_COMMAND,),
                hooks=(CONTESTED_REQUIRED_HOOK,),
            )
            foreign = Path(tmp) / "foreign-skills"
            (foreign / CONTESTED_SKILL).mkdir(parents=True)
            fixture.register_skill_dir(foreign)

            first = fixture.report()
            second = fixture.report()

        self.assertEqual(first, second)


class VisibleCompetingSourceTests(unittest.TestCase):
    """AC1: every visible competing source is named, with its owner."""

    def test_a_contested_skill_directory_names_the_managed_and_the_user_owned_side(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            foreign = Path(tmp) / "foreign-skills"
            user_owned = foreign / CONTESTED_SKILL
            user_owned.mkdir(parents=True)
            fixture.register_skill_dir(foreign)

            report = fixture.report()
            conflict = _conflict(report, "skill", CONTESTED_SKILL)
            managed = fixture.paths.skills_dir / omh_skill_display_name(CONTESTED_SKILL)

        self.assertEqual(validate_identity_conflict_report(report), [])
        # The foreign side is reported at its resolved location, which is what
        # the configured-directory scan compared against the managed one.
        self.assertEqual(
            _ownership_map(conflict),
            {str(managed): "omh_managed", str(user_owned.resolve()): "user_owned"},
        )
        self.assertEqual(report["precedence"], "unknown")

    def test_a_contested_command_name_names_both_declaring_plugins(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            foreign = _write_foreign_plugin(
                fixture.paths.hermes_plugins_dir, "notes", tools=(CONTESTED_COMMAND, "notes_search")
            )

            report = fixture.report()
            conflict = _conflict(report, "command", CONTESTED_COMMAND)

        self.assertEqual(validate_identity_conflict_report(report), [])
        self.assertEqual(
            _ownership_map(conflict),
            {str(fixture.paths.hermes_plugin_dir): "omh_managed", str(foreign): "user_owned"},
        )
        # A name only the foreign plugin declares is not OMH's contest to report.
        self.assertEqual(
            [item for item in report["conflicts"] if item["name"] == "notes_search"],  # type: ignore[index]
            [],
        )

    def test_a_contested_hook_name_names_both_subscribing_plugins(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            foreign = _write_foreign_plugin(
                fixture.paths.hermes_plugins_dir, "notes", hooks=(CONTESTED_REQUIRED_HOOK,)
            )

            report = fixture.report()
            conflict = _conflict(report, "hook", CONTESTED_REQUIRED_HOOK)

        self.assertEqual(validate_identity_conflict_report(report), [])
        self.assertEqual(
            _ownership_map(conflict),
            {str(fixture.paths.hermes_plugin_dir): "omh_managed", str(foreign): "user_owned"},
        )

    def test_three_plugins_claiming_one_command_all_appear(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            first = _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "alpha", tools=(CONTESTED_COMMAND,))
            second = _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "beta", tools=(CONTESTED_COMMAND,))

            conflict = _conflict(fixture.report(), "command", CONTESTED_COMMAND)

        self.assertEqual(
            _ownership_map(conflict),
            {
                str(first): "user_owned",
                str(second): "user_owned",
                str(fixture.paths.hermes_plugin_dir): "omh_managed",
            },
        )

    def test_every_covered_kind_can_be_reported_at_once(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(
                fixture.paths.hermes_plugins_dir,
                "notes",
                tools=(CONTESTED_COMMAND,),
                hooks=(CONTESTED_REQUIRED_HOOK,),
            )
            foreign = Path(tmp) / "foreign-skills"
            (foreign / CONTESTED_SKILL).mkdir(parents=True)
            fixture.register_skill_dir(foreign)

            report = fixture.report()

        self.assertEqual(validate_identity_conflict_report(report), [])
        self.assertEqual({item["kind"] for item in report["conflicts"]}, set(SOURCE_KINDS))  # type: ignore[index]


class PrecedenceSeverityTests(unittest.TestCase):
    """AC2: the same unknown precedence is a warning or a blocker, by one rule.

    The rule: a contested name blocks when Hermes can only award it to a single
    owner, so one side becomes unreachable and OMH cannot say which. Only
    `command` is such a name today, which is what `EXCLUSIVE_NAME_KINDS` says.
    """

    def test_the_rule_is_stated_as_data_and_not_as_the_kind_that_came_first(self) -> None:
        self.assertEqual(EXCLUSIVE_NAME_KINDS, frozenset({"command"}))
        self.assertTrue(EXCLUSIVE_NAME_KINDS.issubset(set(SOURCE_KINDS)))

    def test_an_exclusive_command_name_makes_unknown_precedence_a_blocker(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "notes", tools=(CONTESTED_COMMAND,))

            report = fixture.report()

        self.assertEqual(report["precedence"], "unknown")
        self.assertEqual(report["severity"], "blocking")
        self.assertEqual(_conflict(report, "command", CONTESTED_COMMAND)["severity"], "blocking")
        self.assertIn("OMH will not edit or remove a plugin it does not own", str(report["next_action"]))

    def test_a_shared_hook_name_keeps_unknown_precedence_a_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "notes", hooks=(CONTESTED_OPTIONAL_HOOK,))

            report = fixture.report()

        # Same precedence verdict as the blocking case above; only the
        # reachability of the OMH side differs.
        self.assertEqual(report["precedence"], "unknown")
        self.assertEqual(report["severity"], "warning")
        self.assertEqual(_conflict(report, "hook", CONTESTED_OPTIONAL_HOOK)["severity"], "warning")
        # The warning names no repair: a shared hook is a second subscriber.
        self.assertIn("nothing needs to change", str(report["next_action"]))

    def test_a_blocking_conflict_outranks_a_warning_one_in_the_report_severity(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(
                fixture.paths.hermes_plugins_dir,
                "notes",
                tools=(CONTESTED_COMMAND,),
                hooks=(CONTESTED_OPTIONAL_HOOK,),
            )

            report = fixture.report()

        self.assertEqual(report["severity"], "blocking")
        self.assertEqual(
            {item["kind"]: item["severity"] for item in report["conflicts"]},  # type: ignore[index]
            {"command": "blocking", "hook": "warning"},
        )


class NextActionByKindTests(unittest.TestCase):
    """#1902: the repair a report names follows the kinds it found.

    A tool name is exclusive and a skill directory can be renamed, but a hook
    name is the lifecycle event itself and Hermes lets every plugin subscribe
    to it. Telling an operator to rename or unregister one sends them to break
    a working plugin to silence a warning.
    """

    def _report(self, *, tools: tuple[str, ...] = (), hooks: tuple[str, ...] = (), skill: bool = False):
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            if tools or hooks:
                _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "notes", tools=tools, hooks=hooks)
            if skill:
                foreign = Path(tmp) / "foreign-skills"
                (foreign / CONTESTED_SKILL).mkdir(parents=True)
                fixture.register_skill_dir(foreign)
            report = fixture.report()
        self.assertEqual(validate_identity_conflict_report(report), [])
        return report, str(report["next_action"])

    def test_a_hook_only_overlap_names_no_rename_and_no_unregister(self) -> None:
        # Every hook OMH subscribes to, the way #1902's reproduction shared three.
        report, action = self._report(hooks=(CONTESTED_REQUIRED_HOOK, CONTESTED_OPTIONAL_HOOK))

        self.assertEqual({item["kind"] for item in report["conflicts"]}, {"hook"})  # type: ignore[index]
        self.assertEqual(report["severity"], "warning")
        self.assertEqual(report["precedence"], "unknown")
        self.assertIn("subscribe to the same lifecycle event", action)
        self.assertIn("both rewrite the same tool call or both inject context", action)
        for word in ("rename", "unregister", "uninstall", "delete"):
            self.assertNotIn(word, action.lower())

    def test_a_command_conflict_keeps_the_blocking_rename_action(self) -> None:
        report, action = self._report(tools=(CONTESTED_COMMAND,))

        self.assertEqual(report["severity"], "blocking")
        self.assertTrue(action.startswith("Rename or uninstall the competing tool name"))
        self.assertNotIn("lifecycle event", action)

    def test_a_skill_only_conflict_names_the_skill_directory_action(self) -> None:
        report, action = self._report(skill=True)

        self.assertEqual(report["severity"], "warning")
        self.assertTrue(action.startswith("Rename or unregister whichever competing skill directory you own"))
        self.assertNotIn("lifecycle event", action)
        self.assertNotIn("tool name", action)

    def test_a_command_and_hook_mix_leads_with_the_blocker_and_keeps_the_hook_wording(self) -> None:
        report, action = self._report(tools=(CONTESTED_COMMAND,), hooks=(CONTESTED_OPTIONAL_HOOK,))

        self.assertEqual(report["severity"], "blocking")
        command_at = action.index("Rename or uninstall the competing tool name")
        hook_at = action.index("subscribe to the same lifecycle event")
        self.assertLess(command_at, hook_at)
        # The only rename in the text is the one written for the tool name.
        self.assertEqual(action.lower().count("rename"), 1)
        self.assertNotIn("rename", action[hook_at:].lower())


class AttributionGuardTests(unittest.TestCase):
    def test_omhs_own_bridge_is_not_a_conflict_with_itself(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            report = fixture.report()

        # The managed bridge declares every name in PROVIDED_TOOLS; on its own
        # that is an install, not a contest.
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["severity"], "ok")

    def test_omhs_own_managed_skill_directory_is_not_a_conflict_with_itself(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            # The managed directory is also a configured directory, and a second
            # spelling of it must not turn OMH's own install into a competitor.
            report = fixture.report(
                configured_skill_dirs=[
                    fixture.paths.skills_dir.as_posix(),
                    (fixture.paths.skills_dir / ".." / "skills").as_posix(),
                ]
            )

        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["scanned"], {"plugin_dirs": 1, "skill_dirs": 0})

    def test_ownership_comes_from_the_manifest_and_not_from_the_directory_name(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            # A plugin directory literally named `omh`, minus OMH's install
            # manifest. Path-shaped attribution would call this OMH's own and
            # report nothing; manifest-shaped attribution sees a stranger.
            impostor = fixture.paths.hermes_plugins_dir / "omh"
            for entry in sorted(impostor.glob(".omh-plugin-manifest.json")):
                entry.unlink()

            report = fixture.report()

        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["severity"], "ok")
        # With no manifest-backed OMH source left, there is no OMH side to
        # contest -- reporting a conflict here would invent a competitor for a
        # bridge that is not installed.
        self.assertEqual(report["scanned"], {"plugin_dirs": 1, "skill_dirs": 0})

    def test_an_unmanaged_plugin_named_omh_beside_the_managed_one_is_user_owned(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            impostor = _write_foreign_plugin(
                fixture.paths.hermes_plugins_dir, "omh-extra", tools=(CONTESTED_COMMAND,)
            )

            conflict = _conflict(fixture.report(), "command", CONTESTED_COMMAND)

        self.assertEqual(_ownership_map(conflict)[str(impostor)], "user_owned")
        self.assertEqual(set(_ownership_map(conflict).values()), set(OWNERSHIPS))

    def test_a_plugin_declaring_nothing_omh_facing_is_scanned_but_never_contested(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "notes", tools=("notes_search",))

            report = fixture.report()

        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["scanned"], {"plugin_dirs": 2, "skill_dirs": 0})


class IncompleteScanTests(unittest.TestCase):
    def test_an_unread_hermes_config_is_unknown_and_never_no_conflicts(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            report = fixture.report(configured_skill_dirs=None)

        self.assertEqual(validate_identity_conflict_report(report), [])
        self.assertEqual(report["precedence"], "unknown")
        self.assertEqual(report["severity"], "warning")
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(
            report["unreadable"],
            ["Hermes config was not read, so configured skill directories could not be enumerated"],
        )

    def test_an_empty_configured_list_is_an_answer_and_may_reach_uncontested(self) -> None:
        with TemporaryDirectory() as tmp:
            report = _Fixture(Path(tmp)).report(configured_skill_dirs=[])

        self.assertEqual(report["precedence"], "uncontested")
        self.assertEqual(report["unreadable"], [])

    def test_an_unreadable_configured_directory_keeps_precedence_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            missing = Path(tmp) / "gone"
            missing.mkdir()
            report = build_identity_conflict_report(
                skills_dir=fixture.paths.skills_dir,
                manifest=fixture.manifest,
                plugins_dir=fixture.paths.hermes_plugins_dir,
                configured_skill_dirs=[str(missing)],
            )
            missing.rmdir()
            after_removal = build_identity_conflict_report(
                skills_dir=fixture.paths.skills_dir,
                manifest=fixture.manifest,
                plugins_dir=fixture.paths.hermes_plugins_dir,
                configured_skill_dirs=[str(missing)],
            )

        self.assertEqual(report["precedence"], "uncontested")
        self.assertEqual(after_removal["precedence"], "unknown")
        self.assertEqual(after_removal["severity"], "warning")
        # The report records the resolved directory, so the assertion has to
        # resolve too. Matching the raw path passed on macOS only because
        # `/private/var/...` happens to contain `/var/...` as a substring; on
        # Windows the temp directory resolves out of its 8.3 short form and the
        # raw spelling is not a substring of the long one.
        resolved = str(Path(missing).resolve(strict=False))
        self.assertTrue(
            any(resolved in item for item in after_removal["unreadable"]),
            after_removal["unreadable"],
        )


class NoWriteTests(unittest.TestCase):
    """AC3: the diagnosis reads a user-owned colliding asset and leaves it alone."""

    def test_a_user_owned_colliding_asset_survives_the_diagnosis_byte_for_byte(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _Fixture(root)
            foreign_skills = root / "foreign-skills"
            colliding_skill = foreign_skills / CONTESTED_SKILL
            colliding_skill.mkdir(parents=True)
            # `atomic_write_text`, not `Path.write_text`: the bytes below are
            # hashed and compared, and default text mode would write CRLF on
            # Windows and fail the comparison for the wrong reason.
            atomic_write_text(colliding_skill / "SKILL.md", "# a workflow the user wrote\nkeep me\n")
            fixture.register_skill_dir(foreign_skills)
            foreign_plugin = _write_foreign_plugin(
                fixture.paths.hermes_plugins_dir,
                "notes",
                tools=(CONTESTED_COMMAND,),
                hooks=(CONTESTED_REQUIRED_HOOK,),
            )

            watched = [foreign_skills, foreign_plugin]
            before = {
                str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for parent in watched
                for path in sorted(parent.rglob("*"))
                if path.is_file()
            }
            self.assertTrue(before)

            report = fixture.report()
            # Repeat: a scan that only wrote on first sight would still pass a
            # single-pass comparison.
            fixture.report()

            after = {
                str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for parent in watched
                for path in sorted(parent.rglob("*"))
                if path.is_file()
            }

            self.assertEqual(before, after)
            self.assertTrue(colliding_skill.is_dir())
            self.assertTrue((colliding_skill / "SKILL.md").is_file())
            self.assertTrue(foreign_plugin.is_dir())
            self.assertEqual(report["severity"], "blocking")
            self.assertNotIn("delete", str(report["next_action"]).lower().replace("does not rename or delete", ""))


class DoctorSurfaceTests(unittest.TestCase):
    """The surface an operator actually reaches: `omh doctor`."""

    def _check(self, paths, name: str):
        return next(item for item in run_doctor(paths) if item.name == name)

    def test_the_check_reports_conflicts_and_lands_in_a_named_group(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "notes", tools=(CONTESTED_COMMAND,))

            checks = run_doctor(fixture.paths)
            check = next(item for item in checks if item.name == "identity_conflicts")
            summary = setup_commands._doctor_operator_summary(checks)
            group = next(item for item in summary["groups"] if item["name"] == "hermes_registration")

        self.assertFalse(check.ok)
        self.assertEqual(check.severity, "blocking")
        self.assertIn(f"command name {CONTESTED_COMMAND} (blocking) claimed by", check.message)
        self.assertIn("omh_managed at", check.message)
        self.assertIn("user_owned at", check.message)
        self.assertIn("identity_conflicts", group["failed"])

    def test_a_warning_conflict_does_not_fail_doctor(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            _write_foreign_plugin(fixture.paths.hermes_plugins_dir, "notes", hooks=(CONTESTED_OPTIONAL_HOOK,))

            check = self._check(fixture.paths, "identity_conflicts")

        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "warning")
        # A shared hook reads as a second subscriber, never as a claim one side
        # lost, and the claim boundary says there is no winner to resolve.
        self.assertIn(f"hook name {CONTESTED_OPTIONAL_HOOK} (warning) subscribed to by", check.message)
        self.assertNotIn("claimed by", check.message)
        self.assertIn("A shared lifecycle hook has no winner to resolve", check.message)
        self.assertNotIn("rename", check.next_action.lower())

    def test_a_clean_installed_home_reports_uncontested(self) -> None:
        with TemporaryDirectory() as tmp:
            fixture = _Fixture(Path(tmp))
            check = self._check(fixture.paths, "identity_conflicts")

        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "ok")
        self.assertIn("precedence=uncontested", check.message)


if __name__ == "__main__":
    unittest.main()
