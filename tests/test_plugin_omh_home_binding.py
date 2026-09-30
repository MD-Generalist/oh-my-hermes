"""`omh setup --omh-home X` binds the plugin loaded in the Hermes home to X (#1960).

The plugin resolves its store from its home's
`plugins.entries.omh.settings.omh_home`, then the Hermes process's `OMH_HOME`,
then `~/.omh`. Setup wrote none of the first, so an install at any other store
had a plugin reading `~/.omh` unless Hermes was started with `OMH_HOME`
exported. Every test here runs with `HOME` pointed at the temporary root and
`OMH_HOME` unset, so `~/.omh` is a directory the test owns.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _cli_harness import run_cli
from omh.config_adapter import ensure_plugin_omh_home, plugin_omh_home_setting, remove_plugin_omh_home
from omh.plugin_bundle.omh import runtime_paths


class _IsolatedHome(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        environ = patch.dict(os.environ, {"HOME": str(self.root)})
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("OMH_HOME", None)
        os.environ.pop("HERMES_HOME", None)
        self.hermes_home = self.root / "hermes"
        self.config_path = self.hermes_home / "config.yaml"

    def setup_at(self, omh_home: Path, *extra: str) -> dict:
        status, stdout, stderr = run_cli(
            ["--omh-home", str(omh_home), "--hermes-home", str(self.hermes_home), "setup", "--json", *extra],
            output_json=False,
        )
        self.assertEqual(status, 0, stderr)
        return json.loads(stdout)

    def plugin_binds(self) -> Path:
        """The store the plugin's own resolver picks for this Hermes home."""
        return runtime_paths.resolve_homes(hermes_home=self.hermes_home)[0]

    def config(self) -> str:
        return self.config_path.read_text(encoding="utf-8")


class SetupBindsPluginHomeTests(_IsolatedHome):
    def test_a_non_default_store_is_recorded_and_the_plugin_binds_it(self) -> None:
        store = self.root / "isolated-omh"

        self.setup_at(store)

        self.assertEqual(plugin_omh_home_setting(self.config()), store.as_posix())
        self.assertEqual(self.plugin_binds(), store)

    def test_the_default_store_leaves_the_config_without_a_setting(self) -> None:
        default = self.root / ".omh"
        self.assertEqual(runtime_paths.unset_launch_omh_home(self.hermes_home), default)

        self.setup_at(default)

        # Byte-identical to a default install before #1960: the plugin reaches
        # this store with nothing named, so nothing is written.
        self.assertNotIn("entries:", self.config())
        self.assertNotIn("omh_home", self.config())
        self.assertEqual(self.plugin_binds(), default)

    def test_a_setting_already_in_the_config_is_never_replaced(self) -> None:
        self.hermes_home.mkdir(parents=True)
        chosen = self.root / "chosen-store"
        self.config_path.write_text(
            f"plugins:\n  entries:\n    omh:\n      settings:\n        omh_home: {chosen.as_posix()}\n",
            encoding="utf-8",
        )

        self.setup_at(self.root / "isolated-omh")

        self.assertEqual(plugin_omh_home_setting(self.config()), chosen.as_posix())

    def test_update_records_the_setting_on_an_install_made_before_it(self) -> None:
        store = self.root / "isolated-omh"
        self.setup_at(store)
        # An install from before #1960: registered, and no setting.
        before = remove_plugin_omh_home(self.config(), store.as_posix())
        self.assertTrue(before.changed, before.message)
        self.config_path.write_text(before.text, encoding="utf-8")
        self.assertEqual(self.plugin_binds(), self.root / ".omh")

        status, _stdout, stderr = run_cli(
            ["--omh-home", str(store), "--hermes-home", str(self.hermes_home), "update", "--json"],
            output_json=False,
        )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.plugin_binds(), store)

    def test_uninstall_takes_back_the_setting_it_recorded(self) -> None:
        store = self.root / "isolated-omh"
        self.hermes_home.mkdir(parents=True)
        self.config_path.write_text("version: 1\n", encoding="utf-8")
        self.setup_at(store, "--with-plugin", "--yes")
        self.assertEqual(plugin_omh_home_setting(self.config()), store.as_posix())

        status, stdout, stderr = run_cli(
            ["--omh-home", str(store), "--hermes-home", str(self.hermes_home), "uninstall"],
        )

        self.assertEqual(status, 0, stderr)
        rows = {row["key"]: row["status"] for row in json.loads(stdout)["config_keys"]}
        self.assertEqual(rows["plugins.entries.omh.settings.omh_home"], "reversed")
        self.assertEqual(self.config(), "version: 1\n")

    def test_uninstall_keeps_a_setting_it_did_not_write(self) -> None:
        store = self.root / "isolated-omh"
        self.hermes_home.mkdir(parents=True)
        self.config_path.write_text(
            f"plugins:\n  entries:\n    omh:\n      settings:\n        omh_home: {store.as_posix()}\n",
            encoding="utf-8",
        )
        self.setup_at(store, "--with-plugin", "--yes")

        status, stdout, stderr = run_cli(
            ["--omh-home", str(store), "--hermes-home", str(self.hermes_home), "uninstall"],
        )

        self.assertEqual(status, 0, stderr)
        rows = {row["key"]: row["status"] for row in json.loads(stdout)["config_keys"]}
        self.assertEqual(rows["plugins.entries.omh.settings.omh_home"], "unrecorded")
        self.assertEqual(plugin_omh_home_setting(self.config()), store.as_posix())


class ProfileStoreChoiceTests(_IsolatedHome):
    """A bot profile's `settings.omh_home` is its dispatch store (#1679)."""

    def test_setup_keeps_a_profile_setting_and_writes_none_where_there_is_none(self) -> None:
        own = self.hermes_home / "profiles" / "own"
        bare = self.hermes_home / "profiles" / "bare"
        own.mkdir(parents=True)
        bare.mkdir(parents=True)
        profile_store = self.root / "bot-store"
        own_config = f"plugins:\n  entries:\n    omh:\n      settings:\n        omh_home: {profile_store.as_posix()}\n"
        (own / "config.yaml").write_text(own_config, encoding="utf-8")

        store = self.root / "isolated-omh"
        self.setup_at(store)

        self.assertEqual(plugin_omh_home_setting(self.config()), store.as_posix())
        own_after = (own / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(plugin_omh_home_setting(own_after), profile_store.as_posix())
        self.assertIn("omh", own_after.split("enabled:", 1)[-1])
        # A profile with no setting may bind through its own `.env`
        # `OMH_HOME`, which a written setting would outrank.
        self.assertNotIn("omh_home", (bare / "config.yaml").read_text(encoding="utf-8"))


class DoctorBindingTests(_IsolatedHome):
    def doctor_row(self, store: Path) -> dict:
        _status, stdout, _stderr = run_cli(
            ["--omh-home", str(store), "--hermes-home", str(self.hermes_home), "doctor", "--json"],
        )
        rows = [row for row in json.loads(stdout)["checks"] if row["name"] == "plugin_omh_home_binding"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_a_bound_pair_passes(self) -> None:
        store = self.root / "isolated-omh"
        self.setup_at(store, "--with-plugin")

        row = self.doctor_row(store)

        self.assertEqual(row["severity"], "ok", row)

    def test_an_unbound_pair_warns_without_blocking(self) -> None:
        store = self.root / "isolated-omh"
        self.setup_at(store, "--with-plugin")
        unbound = remove_plugin_omh_home(self.config(), store.as_posix())
        self.config_path.write_text(unbound.text, encoding="utf-8")

        row = self.doctor_row(store)

        self.assertTrue(row["ok"], row)
        self.assertEqual(row["severity"], "warning", row)
        self.assertIn(str(self.root / ".omh"), row["message"])
        self.assertIn(str(store), row["next_action"])

    def test_a_setting_naming_another_store_warns(self) -> None:
        store = self.root / "isolated-omh"
        self.setup_at(store, "--with-plugin")
        elsewhere = self.root / "elsewhere"
        self.config_path.write_text(
            self.config().replace(f"'{store.as_posix()}'", f"'{elsewhere.as_posix()}'"), encoding="utf-8"
        )

        row = self.doctor_row(store)

        self.assertEqual(row["severity"], "warning", row)
        self.assertIn(str(elsewhere), row["message"])


class EnsurePluginOmhHomeTests(unittest.TestCase):
    def test_existing_entries_keep_their_indent_and_siblings(self) -> None:
        text = "plugins:\n  enabled:\n    - omh\n  entries:\n    other:\n      enabled: true\nmemory:\n  provider: omh\n"

        change = ensure_plugin_omh_home(text, "/stores/x")

        self.assertTrue(change.changed, change.message)
        self.assertEqual(
            change.text,
            "plugins:\n  enabled:\n    - omh\n  entries:\n    other:\n      enabled: true\n"
            "    omh:\n      settings:\n        omh_home: '/stores/x'\nmemory:\n  provider: omh\n",
        )
        self.assertEqual(remove_plugin_omh_home(change.text, "/stores/x").text, text)

    def test_shapes_the_plugin_scan_would_not_follow_are_left_alone(self) -> None:
        for text in (
            "plugins:\n  entries: {}\n",
            "plugins:\n  entries:\n    omh: ~\n",
            "plugins: &p\n  enabled:\n    - omh\n",
            "plugins:\n  enabled:\n    - omh\n  entries:\n    omh:\n      settings:\n        omh_home: /y\n",
        ):
            with self.subTest(text=text):
                change = ensure_plugin_omh_home(text, "/stores/x")
                self.assertFalse(change.changed, change.message)
                self.assertEqual(change.text, text)

    def test_a_path_with_a_single_quote_is_not_written(self) -> None:
        change = ensure_plugin_omh_home("plugins:\n  enabled:\n    - omh\n", "/stores/it's")

        self.assertFalse(change.changed)

    def test_removal_leaves_a_value_somebody_changed(self) -> None:
        text = ensure_plugin_omh_home("plugins:\n  enabled:\n    - omh\n", "/stores/x").text.replace("/stores/x", "/mine")

        self.assertFalse(remove_plugin_omh_home(text, "/stores/x").changed)


if __name__ == "__main__":
    unittest.main()
