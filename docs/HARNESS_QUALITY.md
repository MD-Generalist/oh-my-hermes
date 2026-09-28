# Harness Quality Contract

`harness_quality/v1` is the small machine-readable contract that tells a chat
wrapper what quality gate a workflow lane is using.

It exists so Discord, Slack, or hosted Hermes wrappers can render better UX
without teaching end users command names or parsing generated skill prose.

## What Users Get

For a chat user, this changes the experience from vague status copy to explicit
state:

- "I need one answer before planning" for clarification lanes.
- "A draft plan is ready to accept or revise" for planning lanes.
- "Feedback is clustered, but no roadmap or coding handoff exists yet" for
  customer triage lanes.
- "A meeting agenda is prepared, but the meeting outcome is not observed yet"
  for meeting lanes.
- "An ops review names risks and blockers, but it is not CI or release
  evidence" for operating review lanes.
- "Operating rhythm history is recorded, but unprovided meeting outcomes remain
  unobserved" for cadence lanes.
- "A report package outline is ready, but approval and binary PPTX export are
  not observed yet" for report lanes.
- "A material package plan is ready, but binary export, render QA, formula
  recalculation, approval, delivery, and upload are not observed yet" for
  material lanes.
- "A reliability review is drafted, but SLO, incident, and error-budget claims
  require metric or source evidence" for reliability lanes.
- "A coding handoff is prepared, but execution is not observed yet" for coding
  lanes.
- "Review or CI is still missing" before merge-ready status is shown.
- "This workflow attempt is now a learning trace" when Hermes records the route,
  next action, missing evidence, eval result, and future regression case without
  storing the raw prompt or silently patching a skill.

The wrapper can choose buttons from `wrapper_actions`, show progress from
`evidence_ladder`, and avoid false claims with `overclaim_guards`.
Local operators can inspect the same contract with `omh harness list`,
`omh harness inspect <name>`, and `omh harness validate`.

## Contract Shape

Every generated workflow catalog entry and relevant runtime payload can expose a
contract shaped like this:

```json
{
  "schema_version": "harness_quality/v1",
  "harness": "coding-handling",
  "quality_tier": "handoff-gated",
  "quality_bar": [
    "Clarify scope before edits when target behavior, files, or verification are missing.",
    "Attach acceptance criteria, verification expectations, and review expectations to the prepared handoff.",
    "Report coding progress from lifecycle evidence, not from the existence of a prepared prompt."
  ],
  "evidence_ladder": [
    "coding_delegation_prepared",
    "executor_dispatch_observed",
    "executor_result_observed",
    "verification_recorded",
    "review_ci_merge_recorded_when_required"
  ],
  "wrapper_actions": ["accept_plan", "show_prompt_handoff", "copy_prompt_handoff", "choose_executor", "send_to_executor", "send_to_codex", "show_status", "record_result"],
  "overclaim_guards": [
    "A prepared coding_delegation.json is not implementation evidence.",
    "Executor completion is not review, CI, merge-readiness, or merge evidence."
  ]
}
```

## Where It Appears

- `omh docs workflows --json` exposes the full local workflow and harness
  catalog, including `workflow_catalog/v1.harnesses[].harness_quality`.
- `omh coding delegate` includes `harness_quality` beside the prepared
  delegation. Dispatch actions are removed unless the payload also includes a
  prepared executor handoff.
- `omh coding delegate --executor codex` includes the dispatch-capable contract
  in both the public payload and `executor_handoff` when the request is specific
  enough to delegate. The primary action is `send_to_executor`; `send_to_codex`
  remains a compatibility alias only for Codex-selected flows.
- `omh coding delegate --executor claude-code`, `--executor omx-runtime`, or
  `--executor generic` returns a prompt-only handoff. It can expose
  `show_prompt_handoff`, `copy_prompt_handoff`, and `choose_executor`, but it
  must not create a lifecycle run or observed execution evidence.
- `omh hermes plan` includes `wrapper_contract.harness_quality` so wrappers can
  render accept/revise/cancel and handoff readiness from the plan contract.
- Runtime records preserve the contract in `coding_delegation.json` when present.
- `omh runtime delegation-status --run <run-id>` includes
  `harness_progress/v1`, which marks ladder steps complete only when the
  corresponding runtime or wrapper evidence is observed.
- `omh learning record`, `omh learning eval`, and `omh learning regression
  replay` expose the `workflow-learning` harness for process-supervision data:
  why a workflow was chosen, what was prepared, what was observed, which
  deterministic checks passed, and what improvement candidate still needs human
  approval.
- `omh learning recap build|list|show` projects one stored runtime run into
  `runtime_learning_recap/v1`: seven separate delivery, verification, review,
  pull-request, CI, merge-readiness and merge cells, each naming `observed`,
  `failed`, or `unavailable` with its supporting observation type and bounded
  opaque evidence references. `omh learning record --from-runtime-run` writes
  the trace and the recap together. The operator `--outcome` is labelled
  supplied assessment and never sets or upgrades an evidence cell, so a run
  recorded as `useful` with no eligible terminal observation stays `unknown`.
  See [Runtime Learning Recap](RUNTIME-LEARNING-RECAP.md).
- `omh learning missed-route` is the wrapper-friendly shortcut for "Hermes did
  not use the expected OMH workflow." It records a metadata-only trace, eval,
  regression placeholder, and review candidate in one step. A provided
  `--fixture-message` is operator-minimized replay text; without it, replay is
  skipped until a fixture is added.
- `omh learning index check` and `omh learning index rebuild` keep the local
  workflow-learning index repairable if metadata records exist but the pointer
  index drifts. Rebuilding the index is not workflow execution, skill mutation,
  or proof that a future workflow improved.
- `omh learning audit` reads the local learning corpus and returns
  `workflow_learning_audit/v1`: trace/eval/regression/export counts, coverage,
  stale-index blockers, regression replay status, and the next repair or review
  action. The same payload includes `learning_audit_card/v1` so Hermes wrappers
  can render a compact review card with record, eval, regression, audit, export,
  replay, index-check, and index-rebuild actions. The audit is readiness
  evidence for the learning corpus only; it does not patch skills, execute
  workflows, or prove future behavior improved.
- `omh learning candidate <trace-id>` returns an
  `improvement_candidate/v1` plus `improvement_candidate_review_card/v1`. The
  review card is the Hermes-facing surface for approve/revise/reject decisions,
  regression-case follow-up, and status narration. It is not a source patch,
  automatic skill mutation, or proof that future behavior changed.
- `omh learning review` returns `workflow_learning_review_queue/v1`: the
  local human-review queue for pending candidates, approved candidates that
  still need a patch proposal, proposals that need regression work, and
  proposals ready to copy as patch handoff material. The queue is review
  navigation only; it does not approve candidates, apply source changes, or
  prove future behavior improved.
- `omh learning review-candidate <candidate-id> --decision approve|revise|reject`
  records the human gate decision for an improvement candidate and refreshes
  its review card. Optional review notes are stored only as hash and length, not
  raw text.
- `omh learning proposal <candidate-id>` records an
  `improvement_patch_proposal/v1` snapshot for the candidate's current review
  and regression state. Draft statuses such as `needs_regression_case` remain
  durable learning records; `ready_for_human_patch` requires approval plus
  passing regression replay. The proposal is a non-applying patch handoff for
  human review; it is not a source patch, automatic skill mutation, or proof
  that future behavior changed.
- `omh learning export` creates a redacted `workflow_learning_export/v1` review
  bundle from selected traces plus related evals, candidates, and regression
  cases, and patch proposals. The bundle omits raw prompts and fixture text; it
  is review material, not model training, automatic skill patching, execution,
  review, CI, merge, or proof that future routing improved. Export bundles are
  derived artifacts and are not part of the canonical learning index repair loop.

## Teaching A Proven Workflow (agent-facing)

A user who has just watched a workflow work can ask Hermes to keep it. The
`omh learning skill-draft` group is the deterministic backend for that request;
users express the intent in natural language and Hermes fills the flags.

- `omh learning skill-draft new "<explicit request>" --name <slug> --source-run
  <run-id> --instruction ... --input name=description --precondition ...
  --stop-condition ... --verification ...` returns `skill_draft/v1`. It records
  a draft only when the message carries an explicit learning signal such as
  "turn this into a skill"; passive activity exits non-zero with
  `no_explicit_learning_signal` and writes nothing. The draft keeps the fixed
  instructions separate from the declared inputs, preconditions, stop
  conditions, and verification steps, and its provenance names the
  user-selected source runs, the transient identifiers redacted out of the
  request, and the pending review decision.
- A draft is inactive by construction. It is stored under
  `.omh/learning/skill-drafts/`, is never written under `skills/`, never becomes
  catalog data, and carries its proposed name under `proposed_skill_name` rather
  than `name`. A name that collides with an installed skill fails validation,
  and an inactive draft carries no copy-ready proposal at all.
- `omh learning skill-draft show <draft-id>` returns the draft plus a live
  `skill_draft_generated_output_check/v1`: the draft validates, the catalog
  contract is clean, the draft projects to a `SkillDefinition` the catalog's own
  per-definition validator accepts, and that definition renders a single-line
  frontmatter description and a catalog-index line inside its byte ceiling.
  `activation_blockers` is what a reviewer has to clear.
- `omh learning skill-draft review <draft-id> --decision approve|revise|reject`
  is the only activation path. Approval recomputes the generated-output checks
  and refuses to activate when any fail, so an approval on its own can never
  activate a draft that would not render a valid skill. Only a passing check set
  plus an explicit approval flips the draft to `active_proposal` and mints the
  copy-ready `skill_draft_proposal/v1`. Review notes are stored as hash and
  length, never raw text.
- Generated bytes are candidate material, not a shipped artifact. The words
  below are contract vocabulary, not a description of what ships today: no
  promotion path exists in this harness, so nothing here reads as an available
  command. They are the rules any promotion path would have to satisfy before
  it could be built.
- A candidate is bytes plus a digest. Whatever renders a skill retains the
  rendered bytes and their sha256 digest under the draft, so a reviewer
  decides on the exact bytes a promotion would write rather than on a promise
  to re-render later. Re-rendering to different bytes yields a different
  digest and reopens the decision, and a decision recorded against a stale
  digest is refused instead of carried forward.
- Promotion is review-gated and atomic. Only an approved draft would be
  promotable, and the write would take the retained candidate bytes whose
  digest the review named. Either the approved artifact exists complete at its
  own path or nothing was written: no half-promoted skill, and the candidate
  stays put so a failed promotion loses no review evidence. Rendering,
  approving, and promoting stay three separate observed events, and none of
  them implies the next.
- Even an activated draft is a proposal. It is not an installed skill, not
  catalog data, and not proof that Hermes Agent `/learn`, a maintainer, or any
  runtime acted on it.

## Wrapper Rules

- Use `wrapper_actions` as platform-neutral action ids; map them to buttons,
  menu items, or thread actions in the adapter.
- Use `evidence_ladder` to show progress, but mark a step complete only when a
  runtime record or wrapper observation proves it.
- Use `quality_bar` as the lane's success checklist.
- Use `overclaim_guards` before changing status text. If a guard conflicts with
  a later artifact, show the blocker instead of the optimistic state.
- Treat `harness_progress/v1.next_step` as a wrapper hint, not as proof that the
  next action has already happened.

## Reply Lint

The reply rules -- OMH's record vocabulary stays in records and tool calls,
awareness lines are never quoted, and a stop or a decision the user owns ends
the turn with the next action offered as a question -- ship as prompt text in
every skill's Runtime Evidence tail, the common rail, and the awareness primers.
Nothing observed whether a reply followed them until a person read "this is an
evidence-bounded surface" or "I will not merge here" and said so.

`omh quality-evidence reply-lint` reads the sentence a person read and reports
where it departs from those rules:

| Finding | What it means |
| --- | --- |
| `record_term_leak` | a record term in the reply: the rail's list (`prepared_not_observed`, `not_observed`, `evidence boundary`, `handoff`, the qualified `surface`/`lane`/`wrapper` forms) plus the Korean renderings read in live replies (`표면`, `레인`) |
| `awareness_line_quoted` | an `[OMH Awareness]`, `Boundary:`, or `Route hint:` line quoted into the reply |
| `refusal_closer` | the closing paragraph declares what will not be done and offers no question |
| `decision_without_question` | the closing paragraph names an approval or a decision the user owns and offers no question |

```sh
omh quality-evidence reply-lint --text-file reply.txt [--user-text-file asked.txt]
omh quality-evidence reply-lint --stdin < reply.txt
omh quality-evidence reply-lint --hermes-session latest --last 5 [--source tui] [--json]
```

Only the closing paragraph decides the last two: a refusal in the body
followed by a closing question is the rule's own shape, what was left undone
stated as the option it leaves open. A term the user's own message names is
explained on request, not leaked, so it is carved out and listed rather than
counted. The Hermes source opens `state.db` read-only, pairs each reply with
the user message it answered, and skips the `[PRIOR CONTEXT ...]` rows a
compaction re-injects. A finding exits 1 so a QA loop can gate on it.

The payload is `reply_lint/v1` and carries its own claim boundary: a clean
result shows that the text carries none of these shapes. It does not show that
the reply was correct, complete, or in the host's own voice, and it is not
execution, review, CI, or merge evidence. `--source` scopes `latest` to the
most recent session that Hermes tagged with that surface (`tui`, `cli`,
`desktop`, ...; `(none)` selects untagged sessions); an explicit id whose tag
differs, or that has no session row to check the tag on, is an error, so a
filter never looks applied when it was not.

## Session Usage

OMH's tools and skills reach a Hermes session through whichever surface opened
it -- the TUI, the CLI, the desktop app, a one-shot run -- and the skill
catalog is installed the same way for all of them. Nothing observed which
surfaces the tools and skills actually reached until a person opened
`state.db` by hand and found whole surfaces with sessions and tool calls but
not one `omh_*` call.

`omh quality-evidence session-usage` reads Hermes' own session store and
reports, per `sessions.source` tag, what Hermes recorded for its sessions:

| Column | What it counts |
| --- | --- |
| `sessions` | session rows with that source tag (`(none)` groups rows without one, and `--source "(none)"` selects them) |
| `tool_calls` | distinct tool results the session persisted (`messages` with `role = 'tool'`) |
| `omh_tool_calls` | those whose `tool_name` starts with `omh_`, plus `omh_tool_names` per name |
| `sessions_with_omh_tool` | sessions with at least one such call |
| `skill_views` | distinct `skill_view` results |
| `omh_skill_views` | those whose result description starts with `[omh] `, or whose name-only result (a reference-file load, a compaction placeholder) names a catalog skill, plus `omh_skill_names` per name |
| `sessions_with_omh_skill_view` | sessions with at least one such load |
| `sessions_with_any_omh` | sessions with either signal |

```sh
omh quality-evidence session-usage [--since 2026-09-01T00:00:00Z] [--source tui] [--json]
```

The database opens `mode=ro` and nothing is written. Every count is over
distinct `tool_call_id` per session, because a compaction re-persists tool
rows under new ids and a raw row count would inflate every surface that ran
long enough to compact; the first row by id decides what a call was, and a
row without a `tool_call_id` counts once by its own id. The OMH-skill signal
is the catalog's own `[omh] ` description prefix carried back in the
`skill_view` result, not the `omh-` display name, because the `ulw-*` skills
are OMH skills too. A reference-file load and the `[skill_view] name=...`
placeholder a compaction leaves carry only a name, which counts when it is a
catalog skill's display name, current or historical; any other result that is
not a JSON object falls back to the marker substring. The window field is
`COALESCE(last_activity_at, started_at)`, and `--since` takes an ISO-8601
timestamp or epoch seconds. Archived and hidden sessions are included: a
session that used OMH and was archived later still used it. An empty window
exits 0 with `session_count: 0` and `observed: true`, because an empty
observation is not failed work; a missing or unreadable database exits 2. A
loop that wants to gate on utilization reads `totals`, not the exit status.

The payload is `session_usage/v1` and carries its own claim boundary: it is a
read-only observation of Hermes' session store grouped by the host's own tag.
It counts tool results and skill loads Hermes persisted to a session; it does
not show that the model consumed them, that a call succeeded, that a skill's
guidance was followed, or that a reply was correct, and it is not execution,
review, CI, or merge evidence.

## Session File Activity

Audience: operators, and agents preparing a review or a handoff. There is no
chat tool for it: a tool would cost every turn schema bytes for a question
asked after a run, not during one.

A tool-call count cannot say which files a run read, wrote, or patched.
`omh quality-evidence file-activity` answers that for one Hermes session at
query time, from the records Hermes already keeps: the `tool_calls` on each
assistant row (the call id, the tool, and its arguments) and the tool row
carrying the same `tool_call_id` (the tool's own result and, for a call that
never ran or whose effect is unknown, `effect_disposition`). OMH keeps no
store for it and records nothing while the session runs.

```sh
omh quality-evidence file-activity --hermes-session <id|latest> [--workspace DIR] [--max-files 100] [--json]
```

The payload is `session_file_activity/v1`:

| Field | What it holds |
| --- | --- |
| `files` | one entry per workspace file, with `first_at` / `last_at` (UTC) and `activity`: one row per operation and outcome, with its call count and first/last time |
| `calls` | distinct `read_file`, `write_file`, and `patch` calls by tool and by outcome, plus `results_without_call` (results whose call row is gone, so no path can be attributed) |
| `workspace` | where the root came from: `argument`, `git_repo_root`, `cwd` (Hermes' own workspace key, in that order), or `unknown` |
| `file_count`, `shown_file_count`, `max_files`, `truncated`, `omitted_file_count` | the bound; the earliest-touched files are kept |
| `omitted_paths` | declared paths not shown, counted by reason |

Operations are `read`, `write`, `update` (a replace-mode patch or a V4A
`Update File`), `add`, `delete`, `move_from`, and `move_to`; a V4A patch
contributes every file it declares, each with the call's outcome.

The outcome is closed: `succeeded`, `failed`, or `unknown`, read from the
recorded result's own fields, the ones Hermes' own classifier reads, and never
from wording. `succeeded` needs a `content` field (`read_file`), a
`bytes_written` field (`write_file`), or `success: true` (`patch`), with no
`error`. `failed` is `effect_disposition: none`, a truthy `error`, or
`success: false`. Everything else is `unknown`: no recorded result,
`effect_disposition: unknown`, or a result that is not a JSON object or lacks
the success field. So a submitted path is never shown as a successful write.

Paths are workspace-relative only. A path outside the workspace root, the root
itself, a relative path when the session recorded no cwd, a `~` path, a
URL-like value, one with control characters, one over 4096 characters, or a
non-string argument is left out and counted under its reason; its value never
reaches the payload or the text. Containment is judged by the path's spelling
after `..` is collapsed: symlinks are not resolved and no named file is read or
stat'ed. A compaction re-persists rows, so a call is one distinct call id per
session, the first row by id deciding both its arguments and its result.

A session with no file calls is an observation and exits 0. A missing
database, an unknown session id, or `--max-files` below 1 exits 2. The claim
boundary says what the payload is not: it is not file-content, diff, test,
review, CI, or merge evidence.

## Cost Receipt

Audience: people ask in chat; agents and operators read the tool payload or
the CLI.

A person asks "how much did this task cost?" in a Hermes chat. The model calls
`omh_run_summary`, whose payload carries a `cost_receipt`
(`omh_cost_receipt/v1`), and relays `cost_receipt.text`. "This task" is the
calling conversation, and the receipt sums what that work recorded:

| Source | What links it to the conversation |
| --- | --- |
| Hermes session | the session plus its compression continuations, the identity set the HUD already owns delegate children with |
| Delegated Hermes children | every session Hermes marks with `model_config._delegate_from` below the conversation, walked recursively the way Hermes' own cascade walks them |
| Fanout units | `origin_session_id` on the unit in `dispatch_summary.json`, stamped by `omh coding fanout dispatch` from the `HERMES_SESSION_ID` Hermes injects into terminal commands; the same stamp on the summary covers that run's recovery attempts |
| Kanban workers | `tasks.session_id` on the board, which Hermes stamps with the originating session when a task is created inside an agent loop (a worker's follow-up task inherits it); the worker's own session is the `worker_session_id` it stamped on its run when it reported, read with its continuations and delegates from the state.db of the profile the run was claimed for. Every board is read, the default one and each named one |

The receipt keeps three numbers apart. Observed cost is only what a record
priced: a Hermes usage row with a cost status other than `unknown` (the
`included` status is a vouched `$0`), or a cost an executor CLI reported for
its unit. Usage with no recorded price is reported as tokens, with the models
named, and is never added to the dollar figure or priced from OMH's own
ballpark table. A delegated child, fanout unit or kanban worker that ran and
recorded no usage is listed as missing, not counted as zero; so is a worker
run that never stamped its session (a crashed worker, one still running), since
no timing window or title is used to guess which session it was. A worker run
with no `spawned` event and no stamp started no worker and is not listed. The
kanban line names the boards it read, and says so when no board exists or one
could not be read. Every receipt also says what it does not cover: fanout runs
dispatched outside a Hermes session or before the stamp existed, standalone
`omh hermes-child dispatch` runs, kanban tasks created from the CLI or
dashboard (which record no originating session), and earlier attempts of a
re-dispatched unit (the dispatch summary keeps the latest).

```sh
omh quality-evidence cost-receipt --session <any session id of the conversation> [--json]
```

The CLI prints the same plain text by default and exits 2 when Hermes has no
row for the session, so "no record" is never read as "cost nothing". Both
surfaces open `state.db` and `kanban.db` with `mode=ro` and read ids, models,
counts and amounts only; no prompt, reply, task body or transcript text is
read. The receipt is not a provider invoice.

## Declared Verdicts

`verification-gate` issues `claim_verdict/v1` as PASS, HOLD or BLOCK,
`code-review` ranks findings per axis, and `ultraqa` reports pass/fail
evidence. Until #1818 those lived in the conversation and nowhere else: a later
session could not ask what the verdict was, whether a HOLD was resolved, or
whether the merge that followed contradicted it, and an absent source read the
same as a clean one. They now survive the session as bounded declarations in
the completion store, `native_completion/v1` at
`$OMH_HOME/runtime/completion/records.json`.

**What is stored, and by whom.** A model declares a result through
`omh_todo action=record` under a scope checkpoint (`action=set`, then
`action=checkpoint` with `accepted=true` and the revision and environment
fingerprints); OMH writes the row. A row carries `kind` (`verification`,
`review`, `qa`), `verdict`, a bounded `summary`, `findings`, `claimed_source`
(`model`, `host_exit`, `independent_review`, `ci`), `claimed_evidence_state`
(`prepared_not_observed` or `observed`) and up to eight evidence references
(receipt keys, artifact or CI-run ids). No transcript, prompt, log or raw
command output is accepted. The wrapper guidance for the three lanes names
this destination in one shared sentence, so a model that reaches a verdict is
told where it goes and that it stays a declaration there; `code-review`'s
entry is the review category's action and boundary plus that sentence,
derived from the category rather than copied.

**What the store refuses to say.** Every row is written and read back as
`standing: model_declaration`, `observed: false`, whatever evidence state the
writer claimed. A stored PASS is evidence that a PASS was claimed, exactly as
a done mark on the plan todo is evidence that done was claimed. A row with
`observed: true` on disk is malformed, not evidence.

**How it reads.** In a session, `omh_todo action=recall`, bound by the host to
the active profile and project. From the command line, over the home you name:

```sh
omh runtime verdict show [--session <id>] [--checkpoint <id>] [--revision <r> --environment <e>]
```

The payload is `native_completion_read/v1`, and carries `standing:
model_declaration` and the claim boundary at its top level on every status,
read or malformed, so a reader that stops at the first line is told what every
row says. `store_state` is `absent` (no
store), `empty` (a store with no checkpoints) or `present`; `sources` per kind
is `absent`, `stale`, `declared_no_findings` or `declared_findings`, so "no
record" and "a record that declared nothing found" never read alike, and a
filter that matches nothing still reports the store present. Freshness is a
judgment against a binding: with `--revision` and `--environment` a row is
`current` only for that exact pair and inside the age cap, otherwise `stale`,
and the checkpoint carries the `completion` projection whose status is always
`not_verified`; without them every row is `unbound` and no completion
judgment is made, because completeness against no binding is not a judgment.
A half binding is refused. `--session` keeps the checkpoints a session
declared or recorded into. A store that cannot be read is `malformed` and
exits 1; a missing store exits 0 with `store_state: absent`, because nothing
declared is not failed work. The read writes nothing and creates nothing.

**Why staleness is a revision, not a clock.** A plan todo hides after 24 hours
because a checklist nobody touched for a day is no longer the plan. A verdict
is attached to a change, so it is current for the revision and environment it
was declared against and stale the moment either moves, whether that takes a
minute or a month; the 30-day cap only bounds a dossier whose revision nobody
moves. The store never deletes: a stale row is still the row that was
declared, and the read says so on the source rather than by dropping it.

One limit worth knowing: a verdict needs a checkpoint, so a session with no
accepted plan reaches the store in three calls (`set`, `checkpoint`,
`record`), not one.

## Golden Examples

See `examples/wrapper-golden/harness-quality.json` for deterministic examples
covering coding handoff, planning, research, and clarification lanes.

The business workflow pack adds non-coding harnesses such as
`business-research`, `strategy-synthesis`, `meeting-facilitation`,
`customer-insight-triage`, `ops-review`, `operating-rhythm`, `report-package`,
`materials-package`, and `reliability-review`. These give wrappers the same
evidence discipline for company work: research briefs are not fetched data,
strategy briefs are not accepted decisions, meeting briefs are not meeting
minutes, feedback triage is not a roadmap, ops review is not release or CI
evidence, report packages are not binary decks or approvals, material packages
are not binary exports, render/formula QA, uploads, approvals, or delivery, and
reliability reviews are not proof that SLOs passed without metric or source
evidence.
