# Checked Team Lane

Load this reference when an accepted `ultrawork` plan splits into in-session `delegate_task` lanes and each lane must count as done only when its own check passes. `omh_team` holds the team: it hands back `delegate_task` entries, learns from the host when each helper starts and returns, runs each lane's check command itself in this session's workspace, and sends a failing lane back for a fix. A helper's summary is never how a lane is accepted.

## Before `team_start`

- Every lane has one `verification_command`, and that exact text is written in the plan the person accepted: put it in the lane's `omh_todo` item before the plan is accepted. A command that is not word for word in the accepted plan is refused as `command_not_in_accepted_plan`.
- The plan is this session's own `omh_todo` plan with `plan_stage=accepted`. Pass its reference as `plan_ref`; a call without the right one is refused as `plan_ref_mismatch` and the refusal carries the accepted plan's reference.
- A check is one plain command: no shell, no `sh`/`bash`/`env` or `python -c` as the program, no pipes or `;`, no forge CLI, and no `git` verb that moves a remote.
- `team_start` asks the person to approve the exact command list. Where nobody can answer, it is refused; report that instead of retrying.

## The Loop

1. `team_start` with `team_id`, `plan_ref`, and one unit per lane (`unit_id`, `title`, `depends_on`, `verification_command`). `max_repair_attempts` defaults to 2 and is capped at 3.
2. Dispatch the returned `delegate_task` entries unchanged in one call. Each `goal` starts with the attempt key OMH reserved; you may add to `context`, never edit `goal`.
3. When the helpers return, call `team_reconcile`. While any team helper is still out it checks nothing and says so (`delegations_in_flight`).
4. Relay the result's `say` line. Dispatch any new entries it returns: fix-ups for lanes whose check failed, and lanes whose parents have all passed.
5. Repeat from 3 until `team_state` is `done` or `blocked`. Those are the only two stops.

`team_status` reports each lane's state, tries used against the maximum, its last check (`command`, `exit_code`, `observed_at`), and this conversation's cost including helpers.

## What Counts

- A lane is accepted only on an exit code OMH observed. An accepted lane carries `evidence: {kind: team_check, ref: <team_id>/<unit_id>/attempt-<n>/check}`; cite that when closing the matching `omh_todo` item.
- A check that could not run cleanly (timeout, command not found, workspace missing, files changed while it ran) spends no try; three in a row block the lane.
- A failing check with tries left returns a fix-up entry carrying only `{command, exit_code}`; the helper reruns the command itself to see the output.
- Out of tries, the lane is `blocked` with `repair_budget_exhausted` and its last check. Report that lane and stop; do not start a new team to route around it.

## Limits to State

- Helpers share one workspace. A check is attributed to its lane, but another lane's edits are in the same tree; checks wait until every team helper is back to narrow this, not remove it.
- A check runs code the helpers wrote, as the person's own user account with the real home folder.
- Parallel helpers in one call share one model route; a lane that needs a different model is dispatched in its own call after `omh_delegate_route`.
- Board lanes (`references/kanban-lane.md`) are not checked by `omh_team` yet; they still follow the prepare and readback recipe there.
