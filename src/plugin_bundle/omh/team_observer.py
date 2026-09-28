"""Host lifecycle facts for `omh_team`: a helper started, a helper came back.

`omh_team` must not check a part while any of its helpers is still editing
the shared workspace. Whether a helper is out is a fact only the host has, so
it comes from the host's own `subagent_start` / `subagent_stop` callbacks,
never from the model saying so:

* `subagent_start` carries the child's goal. A team entry's goal begins with
  the attempt key OMH reserved (`[omh-team:<team>/<unit>/attempt-<n>]`), and
  only an attempt the team record already holds, still undispatched, binds to
  the child's session id.
* `subagent_stop` carries that child session id; the attempt it bound is
  marked returned, which is what lets `team_reconcile` check it.

A goal without the marker is not a team helper and costs one prefix test.
Nothing here raises: both are observers the host fires on the spawn path, and
a failure leaves the attempt undispatched, which `team_reconcile` hands back
and eventually blocks as `dispatch_not_observed` -- visible, never silent.
"""
from __future__ import annotations

import time

from . import runtime_paths

_MARKER_PREFIX = "[omh-team:"


def observe_team_dispatch(kwargs: dict[str, object]) -> bool:
    goal = kwargs.get("child_goal")
    if not isinstance(goal, str) or not goal.startswith(_MARKER_PREFIX):
        return False
    try:
        from omh.workflows import team

        home, parent = _parent(kwargs)
        if not parent:
            return False
        return team.observe_dispatch(home, parent, child_session_id=kwargs.get("child_session_id"),
                                     goal=goal, now=time.time())
    except (ImportError, OSError, ValueError, RuntimeError):
        return False


def observe_team_return(kwargs: dict[str, object]) -> bool:
    child = kwargs.get("child_session_id")
    if not isinstance(child, str) or not child.strip():
        return False
    try:
        from omh.workflows import team

        # Most homes never start a team: one stat before any session lookup.
        if not (runtime_paths.plugin_home(kwargs.get("omh_home")) / "runtime" / "teams").is_dir():
            return False
        home, parent = _parent(kwargs)
        if not parent or not team.teams_dir(home, parent).is_dir():
            return False
        return team.observe_return(home, parent, child_session_id=child, now=time.time())
    except (ImportError, OSError, ValueError, RuntimeError):
        return False


def _parent(kwargs: dict[str, object]) -> tuple[object, str]:
    from .runtime_reader import reading_session_id

    home = runtime_paths.plugin_home(kwargs.get("omh_home"))
    hermes = runtime_paths.plugin_home(kwargs.get("hermes_home"), hermes=True)
    parent = kwargs.get("parent_session_id")
    return home, reading_session_id(hermes, parent) if isinstance(parent, str) else ""
