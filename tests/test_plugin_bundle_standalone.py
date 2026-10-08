"""The plugin bundle must import with no ``omh`` package anywhere in sight.

Hermes copies the bundle into ``$HERMES_HOME/plugins/omh`` and loads it with
its own interpreter. The documented installs (`uv tool install`,
`pip install --user`) put the `omh` package in OMH's environment, not Hermes',
so a bundle module that imports `omh.*` at module scope does not merely lose a
feature: Hermes' memory-provider loader execs every top-level file of the
bundle eagerly and keeps the half-initialized module in `sys.modules` when one
raises. The next lazy import of that module then fails on the NAME, with an
`ImportError` carrying the bundle's own dotted path -- which is how one dead
bridge became a logged hook warning on every single tool call (#1623, reported
with a reproduction by @tonalenar).

Two gates, because the fault had two halves. The bundle-wide load is the one
that would have caught it; the cached-stub cases pin the hooks against the
same shape arriving from anywhere else.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest

from _local_package import load_local_package
from _standalone_bundle import (
    bundle_dir,
    load_standalone_bundle,
    standalone_bundle_import_failures,
    standalone_bundle_module_names,
)

load_local_package()

# The name Hermes' memory-provider lane gives the bundle is synthetic
# (`_hermes_user_memory.omh__source_<digest>`), and that name is what the
# ImportError from a cached stub carries. Modelling it with a foreign name is
# the whole point: under this repo's own package name the error reads
# `omh.plugin_bundle.omh.agent_board_bridge`, which starts with `omh.`, so a
# guard that decides by name passes every local run and re-raises on the only
# host it was written for.
LANE_PACKAGE = "_hermes_user_memory_omh__source_test1623"


@contextmanager
def hermes_memory_lane_bundle() -> Iterator[ModuleType]:
    """Load the bundle with a bridge module Hermes could not exec.

    `plugin_loader.py::_exec` keeps the module in `sys.modules` when
    `exec_module` raises, and the parent package never gains the attribute, so
    what the next import resolves to is a module object holding none of the
    names the caller asked for. That is the state rebuilt here.
    """
    _forget_lane_modules()
    directory = bundle_dir()
    spec = importlib.util.spec_from_file_location(
        LANE_PACKAGE, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load the plugin bundle under a lane name")
    package = importlib.util.module_from_spec(spec)
    sys.modules[LANE_PACKAGE] = package
    spec.loader.exec_module(package)
    stub_name = f"{LANE_PACKAGE}.agent_board_bridge"
    sys.modules[stub_name] = ModuleType(stub_name)
    try:
        yield package
    finally:
        _forget_lane_modules()


def _forget_lane_modules() -> None:
    for name in list(sys.modules):
        if name == LANE_PACKAGE or name.startswith(f"{LANE_PACKAGE}."):
            sys.modules.pop(name, None)


class StandaloneBundleImportTests(unittest.TestCase):
    def test_every_bundle_module_imports_without_the_omh_package(self) -> None:
        failures = standalone_bundle_import_failures()
        self.assertEqual(
            failures,
            {},
            "every module under src/plugin_bundle/omh/ must import on a host with no "
            "`omh` package, because Hermes execs all of them and keeps a module that "
            f"raised: {json.dumps(failures, indent=2, sort_keys=True)}\n"
            "Fix the module, do not narrow this gate. Either vendor what it needs into "
            "the bundle, or guard the import (`try: from omh... except ImportError:`) "
            "and have the feature report its absence at its own entry point -- see "
            "`agent_board_bridge._BOARD_CORE_AVAILABLE` and `activity_observer` for "
            "both halves of that shape.",
        )

    def test_the_module_list_is_derived_from_the_directory(self) -> None:
        """A module added to the bundle joins the gate without anyone listing it."""
        names = standalone_bundle_module_names()
        self.assertIn("agent_board_bridge", names)
        self.assertIn("hooks.tool_hooks", names)
        self.assertIn("tools.agent_board_tool", names)
        self.assertNotIn("__init__", names)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue((self._bundle_path(name)).is_file())

    @staticmethod
    def _bundle_path(name: str) -> Path:
        direct = bundle_dir().joinpath(*name.split("."))
        return direct.with_suffix(".py") if direct.with_suffix(".py").is_file() else direct / "__init__.py"


class StandaloneBundleDegradationTests(unittest.TestCase):
    """Importing is half of it; the features must then say they are absent."""

    def test_the_board_bridge_admits_no_action_and_refuses_none(self) -> None:
        bridge = load_standalone_bundle(("agent_board_bridge",))["agent_board_bridge"]
        kanban_call = {
            "tool_name": "kanban_create",
            "args": {"board": "qa"},
            "session_id": "s",
            "task_id": "t",
            "tool_call_id": "c",
        }
        # No engine means nothing here ever prepared a request, so a native
        # Kanban call OMH has no claim over must not be blocked.
        self.assertIsNone(bridge.pre_agent_board(kanban_call))
        self.assertIsNone(bridge.post_agent_board(kanban_call))
        with self.assertRaises(bridge.BoardCoreUnavailable):
            bridge.installed_bridge("qa")
        with self.assertRaises(bridge.BoardCoreUnavailable):
            bridge.installed_status("request-1")

    def test_the_activity_observer_reports_the_missing_store_at_construction(self) -> None:
        modules = load_standalone_bundle(("activity_observer", "native_activity_observer"))
        with self.assertRaises(ImportError):
            modules["activity_observer"].ActivityObserver(None, "sha256:" + "0" * 64)
        with self.assertRaises(ImportError):
            modules["native_activity_observer"].register(object())

    def test_the_agent_board_tool_reports_the_missing_core(self) -> None:
        # The bridge loads in the same pass on purpose: the handler imports it
        # lazily, and an import that happens after the block is lifted would
        # reach the installed package instead of the host being modelled.
        tool = load_standalone_bundle(("agent_board_bridge", "tools.agent_board_tool"))["tools.agent_board_tool"]
        result = json.loads(tool.omh_agent_board_handler({"action": "prepare", "request_id": "r", "board": "qa"}))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "omh_agent_board_core_unavailable")


class CachedStubBridgeTests(unittest.TestCase):
    """The defence in depth: a broken bridge, however it came to be broken."""

    def test_the_tool_call_hooks_return_instead_of_raising(self) -> None:
        call = {"tool_name": "kanban_create", "args": {"board": "qa"}, "session_id": "s",
                "task_id": "t", "tool_call_id": "c"}
        with hermes_memory_lane_bundle(), TemporaryDirectory() as home:
            hooks = importlib.import_module(f"{LANE_PACKAGE}.hooks.tool_hooks")
            self.assertIsNone(hooks.pre_tool_call(omh_home=home, hermes_home=home, **call))
            self.assertIsNone(hooks.post_tool_call(omh_home=home, hermes_home=home, **call))

    def test_the_agent_board_tool_reports_unavailable_instead_of_raising(self) -> None:
        with hermes_memory_lane_bundle():
            tool = importlib.import_module(f"{LANE_PACKAGE}.tools.agent_board_tool")
            result = json.loads(tool.omh_agent_board_handler({"action": "status", "request_id": "r"}))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "omh_agent_board_core_unavailable")

    def test_the_stub_carries_a_name_no_omh_prefixed_check_can_match(self) -> None:
        """Why the guard cannot decide by name, measured rather than asserted.

        `exec` because the failure exists only in the `from X import Y` form --
        `import_module` hands back the stub without complaint -- and the
        package name has to be the lane's rather than a literal.
        """
        with hermes_memory_lane_bundle():
            with self.assertRaises(ImportError) as raised:
                exec(f"from {LANE_PACKAGE}.agent_board_bridge import pre_agent_board")
        error = raised.exception
        self.assertNotIsInstance(error, ModuleNotFoundError)
        self.assertEqual(error.name, f"{LANE_PACKAGE}.agent_board_bridge")
        self.assertFalse(str(error.name) == "omh" or str(error.name).startswith("omh."))


# Hermes' directory loader names the bundle `hermes_plugins.<slug>`; a foreign
# name keeps the case from passing on this repo's own `omh.` package path.
EVICTED_PACKAGE = "hermes_plugins_test1979.omh"


class EvictedBundleModulesTests(unittest.TestCase):
    """The board after Hermes dropped the bundle's modules from `sys.modules`.

    Hermes evicts `hermes_plugins.<slug>` and every submodule on a reload or a
    failed load (`_evict_modules` in hermes_cli/plugins_loader.py) while the
    handlers and hooks it registered stay callable. #1979 reported that state
    from a running agent -- no `hermes_plugins`, no module under the bundle --
    with the board tool answering `omh_agent_board_core_unavailable` on a host
    whose `omh` package imported fine, while the other tools kept working.
    """

    def setUp(self) -> None:
        self.addCleanup(self._forget)
        self._forget()
        # Hermes registers the bare namespace parent before the plugin.
        parent = ModuleType(EVICTED_PACKAGE.split(".")[0])
        parent.__path__ = []
        sys.modules[parent.__name__] = parent
        directory = bundle_dir()
        spec = importlib.util.spec_from_file_location(
            EVICTED_PACKAGE, directory / "__init__.py", submodule_search_locations=[str(directory)]
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("failed to load the plugin bundle under a host name")
        package = importlib.util.module_from_spec(spec)
        sys.modules[EVICTED_PACKAGE] = package
        spec.loader.exec_module(package)
        # What `register()` imports when Hermes loads the plugin.
        self.tool = importlib.import_module(f"{EVICTED_PACKAGE}.tools.agent_board_tool")
        self.hooks = importlib.import_module(f"{EVICTED_PACKAGE}.hooks.tool_hooks")
        self.team = importlib.import_module(f"{EVICTED_PACKAGE}.tools.team_tool")
        self.jev = importlib.import_module(f"{EVICTED_PACKAGE}.tools.jev_ask_tool")
        self.bridge = importlib.import_module(f"{EVICTED_PACKAGE}.agent_board_bridge")
        self._forget()  # the eviction

    @staticmethod
    def _forget() -> None:
        parent = EVICTED_PACKAGE.split(".")[0]
        for name in list(sys.modules):
            if name == parent or name.startswith(f"{parent}."):
                sys.modules.pop(name, None)

    def test_the_board_tool_still_reaches_its_core(self) -> None:
        from unittest.mock import patch

        self.assertNotIn(EVICTED_PACKAGE, sys.modules)
        with TemporaryDirectory() as home, patch.object(self.bridge, "default_omh_home", return_value=Path(home)):
            result = json.loads(self.tool.omh_agent_board_handler({"action": "status", "request_id": "r"}))
        # An empty store has no such request: the engine answered, so the core
        # is present. Before #1979 this read `omh_agent_board_core_unavailable`.
        self.assertEqual(result["reason"], "invalid_request_or_board_store")

    def test_the_tool_call_hooks_still_reach_the_bridge(self) -> None:
        self.assertIs(self.hooks._agent_board_bridge(), self.bridge)

    def test_the_team_tool_still_reaches_its_siblings(self) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=False) as environ:
            environ.pop(self.team.KANBAN_TASK_ENV, None)
            result = json.loads(self.team.omh_team_handler({"action": "team_status", "team_id": "t"}))
        # No host session in the call: the handler got past every sibling it
        # reads and refused on the request itself.
        self.assertEqual(result["reason"], "session_required", result)

    def test_the_jev_ask_check_still_reaches_its_store(self) -> None:
        from unittest.mock import patch

        with TemporaryDirectory() as home, patch.object(self.jev, "default_omh_home", return_value=Path(home)):
            self.assertIs(self.jev.jev_ask_available(), False)


# Functions that run only while Hermes holds the package in `sys.modules`, or
# never inside Hermes at all, with the reason each call-time import is safe.
CALL_TIME_IMPORT_EXEMPTIONS = {
    ("__init__.py", "register"): "runs inside Hermes' load, while the package is in sys.modules",
    ("tools/__init__.py", "builtin_tool_schemas"): "called by the OMH CLI (quality/schema_overlap), never registered",
}


def call_time_bundle_imports() -> list[str]:
    """Every relative import of a bundle sibling made inside a function body.

    Read off the directory, so a new tool or hook joins without a list. An
    import that climbs above the bundle root (`awareness.py`'s `...routing`)
    reaches the OMH package, not a sibling, and is out of scope here; a
    `TYPE_CHECKING` block never runs.
    """
    bundle = bundle_dir()
    found: list[str] = []
    for path in sorted(bundle.rglob("*.py")):
        rel = path.relative_to(bundle).as_posix()
        depth = len(Path(rel).parts) - 1  # package depth below the bundle root
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (rel, function.name) in CALL_TIME_IMPORT_EXEMPTIONS:
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom) and 1 <= node.level <= depth + 1:
                    found.append(f"{rel}:{node.lineno} {function.name}: from {'.' * node.level}{node.module or ''}")
    return sorted(set(found))


class CallTimeBundleImportGateTests(unittest.TestCase):
    def test_no_bundle_function_resolves_a_sibling_through_sys_modules(self) -> None:
        self.assertEqual(
            call_time_bundle_imports(),
            [],
            "Hermes evicts the plugin's modules from sys.modules while the handlers "
            "and hooks it registered stay callable (#1979); a relative import made "
            "inside a function then has no parent package. Bind the sibling at module "
            "scope (`from . import sibling as _sibling`, placed after the module's own "
            "definitions when the two import each other) and read its names at call time.",
        )

    def test_the_gate_sees_a_call_time_import(self) -> None:
        source = "def handler():\n    from ..runtime_reader import read_omh_todo\n"
        nodes = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)]
        self.assertEqual([n.level for n in nodes], [2])


class UnavailableBoardDetailTests(unittest.TestCase):
    """One reason used to cover two faults; `detail` names the import that failed."""

    def test_a_missing_engine_names_the_engine_module(self) -> None:
        tool = load_standalone_bundle(("agent_board_bridge", "tools.agent_board_tool"))["tools.agent_board_tool"]
        result = json.loads(tool.omh_agent_board_handler({"action": "status", "request_id": "r"}))
        self.assertEqual(result["reason"], "omh_agent_board_core_unavailable")
        self.assertTrue(result["detail"].startswith("board_engine_unimportable:omh"), result)

    def test_a_stub_bridge_is_named_as_incomplete(self) -> None:
        with hermes_memory_lane_bundle():
            tool = importlib.import_module(f"{LANE_PACKAGE}.tools.agent_board_tool")
            result = json.loads(tool.omh_agent_board_handler({"action": "status", "request_id": "r"}))
        self.assertEqual(result["detail"], "board_bridge_incomplete")

    def test_an_unimportable_bridge_names_the_module(self) -> None:
        with hermes_memory_lane_bundle():
            sys.modules[f"{LANE_PACKAGE}.agent_board_bridge"] = None  # import of it raises ImportError
            tool = importlib.import_module(f"{LANE_PACKAGE}.tools.agent_board_tool")
            result = json.loads(tool.omh_agent_board_handler({"action": "status", "request_id": "r"}))
        self.assertEqual(result["reason"], "omh_agent_board_core_unavailable")
        self.assertEqual(result["detail"], f"board_bridge_unimportable:{LANE_PACKAGE}.agent_board_bridge")


class UnavailableTeamDetailTests(unittest.TestCase):
    def test_an_unimportable_sibling_names_the_module(self) -> None:
        with hermes_memory_lane_bundle():
            sys.modules[f"{LANE_PACKAGE}.cost_receipt"] = None  # import of it raises ImportError
            tool = importlib.import_module(f"{LANE_PACKAGE}.tools.team_tool")
            result = json.loads(tool.omh_team_handler({"action": "team_status", "team_id": "t"}))
        self.assertEqual(result["reason"], "omh_team_core_unavailable")
        self.assertEqual(result["detail"], f"bundle_module_unimportable:{LANE_PACKAGE}.cost_receipt")


if __name__ == "__main__":
    unittest.main()
