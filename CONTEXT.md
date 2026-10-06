# oh-my-hermes

Shared language for oh-my-hermes, written for coding agents. Users mostly need
`omh setup`, `omh update`, and `omh doctor`; agents work across every surface
below and keep blurring which product or record owns which term — these
definitions pin that down. Product direction lives in `docs/DIRECTION.md`; the
operating contract lives in `AGENTS.md`. This file is a glossary only.

## Language

### Products

**OMH (oh-my-hermes)**:
This repo — a deterministic wrapper orchestration layer installed next to
Hermes Agent: skill catalog, router, prepared-handoff generator, and
metadata-only status surfaces. Makes no LLM, API, or network calls, with one
scoped exception: the opt-in `omh_jev_ask` tool sends typed questions to Jev
with the user's own key when the user names Jev in that turn.
_Avoid_: Hermes plugin (that is one distribution surface, not the product),
coding executor, Hermes patch

**Hermes Agent**:
Nous Research's agent product that OMH integrates with — a separate codebase
installed on the user's machine (typically under `~/.hermes/hermes-agent`).
OMH never modifies its code and does not vendor it.
_Avoid_: Hermes runtime (that names an OMH executor path), our agent

### State roots

**Hermes home**:
`$HERMES_HOME`, default `~/.hermes` — Hermes Agent's own state root. OMH only
adds managed, explicitly installed artifacts under it (`plugins/omh`,
`tui-widgets/`, skill registration in `config.yaml`).
_Avoid_: using it for OMH runtime state

**OMH home**:
`$OMH_HOME`, default `~/.omh` — OMH's state root; managed skills and the
runtime metadata the HUD reads live here. Exactly one home is active per
invocation: the user-scope default, `OMH_HOME`, `--omh-home`, or
`--scope project`, which resolves to the repository's own `./.omh`.
_Avoid_: probing several guessed homes, Hermes home

### Skills and routing

**Managed skill**:
A generated workflow document (`skills/*/SKILL.md`) OMH installs under the OMH
home and registers into Hermes' `skills.external_dirs` so Hermes can invoke it
in chat. Generated from the skill catalog; never hand-edited.
_Avoid_: slash command, prompt template, OMC skill (that is a different
product's concept)

**Hermes skill category**:
The dashboard group Hermes shows a skill under in its startup banner and skills
list. Hermes derives it from the SKILL.md's DIRECTORY, not from frontmatter, and
only when the path relative to a registered skills dir has three or more parts —
so managed skills install at `<skills_dir>/<category>/<label>/SKILL.md`.
`hermes_skill_category()` in `src/skills/catalog.py` owns the mapping: the
skill's Hermes role, with the ULW engines carved out as `ultrawork`. The repo's
own `skills/` tap tree stays flat, because Hermes' tap lister reads only one
directory level below a tap path.
_Avoid_: `SkillDefinition.category` (the catalog's fine-grained phase field,
which Hermes never reads), capability family

**Agent Skills projection**:
A separately generated, portability-reviewed view of the catalog under
`agent-skills/`. Hosts discover installed copies in `.agents/skills/`;
both repo and user scopes also copy them into `.claude/skills/` under the same
scope root for Claude Code. This is not a
managed Hermes skill, a plugin, an RPC tool, or a runtime port. A fresh copy is
not evidence that a host loaded or executed it. See [Agent Skills](docs/AGENT-SKILLS.md).
_Avoid_: managed skill, Hermes plugin, execution adapter

**Skill catalog**:
The source of truth for every skill and its metadata (`src/skills/catalog.py`
plus render code). Skills, `docs/WORKFLOWS.md`, `docs/ROLES.md`, and the demo
cards are byte-exact projections of it.
_Avoid_: editing any generated projection directly

**Router**:
OMH's deterministic chat-intake classifier that maps a natural-language
request to a workflow, skill, or intervention using normalized phrase and
token matching. Not a model and not an LLM dispatcher.
_Avoid_: LLM router, model routing (that names executor model selection)

**Route hint**:
A non-binding recommendation of the nearest OMH workflow for a message, as
returned by `omh_recommend` or `omh recommend`. It records nothing and
authorizes nothing.
_Avoid_: dispatch, delegation, decision

**Routing query**:
The prepared stages of one message (`RoutingQuery` in
`src/routing/query.py`: executable, scrubbed, routing text, normalized,
tokens), built once and taken as input by every router reader that needs the
full prep chain.
_Avoid_: re-deriving the prep chain at a call site; passing a half-prepared
string into the scorer

### Runtime evidence

**Run**:
One recorded unit of coding work under `$OMH_HOME/runtime/runs/<id>` — the
place prepared handoffs, observations, and effect receipts about that work
accumulate.
_Avoid_: session (wrapper sessions are a different record), task

**Run record**:
An on-disk run, dispatch, or receipt file (`run.json`, `events.jsonl`,
`dispatch_summary.json`, `external_effect_receipts.jsonl`, …) that the
control-plane writers produce and the Hermes-side readers in the plugin bundle
consume. Its file names, schema versions, and closed vocabularies are owned
once, by `src/plugin_bundle/omh/run_records.py`; the writers import them.
_Avoid_: a second definition of a file name or schema version, a parity test as
the owner

**Prepared handoff**:
OMH's output contract for coding work — a payload a coding owner may execute
later. Preparing one is not dispatch, execution, review, CI, or merge
evidence; its status is `prepared_not_observed`.
_Avoid_: run, execution, delegation result

**Observed evidence**:
A recorded observation that something actually happened (dispatch, execution,
verification, review, CI, merge), as opposed to something being prepared or
claimed. The only basis for completion claims in reports and status surfaces.
_Avoid_: treating a prepared artifact, a declaration, or an executor's
self-report as evidence

**Claim boundary**:
The sentence attached to a record stating what that record is *not* evidence
of. Every metadata artifact OMH writes carries one.
_Avoid_: disclaimer (it is a validated contract field, not prose)

**Executor progress binding**:
The metadata record that links a run or wrapper session to a live executor so
its progress events can be projected as HUD activity rows. Bindings age from
active to stale to expired; they never prove results.
_Avoid_: process handle, job

**Plan todo**:
The declared checklist HUD surfaces render above the Hermes prompt input.
Items are plan declarations; a done mark never upgrades into observed
evidence. One record per declaring session: a plan stamped with `session_ref`
lives at `$OMH_HOME/runtime/todos/<session key>.json` and renders only for
the session that owns it — the TUI widget names its own session from the
host's active-session file, the plugin tool and hooks from the session that
invoked them — so a plan declared from Slack, Discord, or a second TUI is
neither shown in nor overwritten by another session. An unstamped record
(`omh runtime todo set` without `--session`) keeps the home-wide
`$OMH_HOME/runtime/todo.json`, scoped by write time against the reading
session's start; a reader with no live TUI row to date it against (a gateway
session), or a host that cannot say which session is reading, keeps the
age-only behavior. A widget reference that names no live TUI row is first
offered to the host's active-session lease registry, which pairs a created
session's transport id with its durable key; an unpaired reference stays its
own identity and reads no other session's record.

One field on this record refuses work rather than describing it, which is why
it is named here: a planning run stamps `plan_stage: awaiting_acceptance` when
it declares the checklist, and while that holds, a `write_file` or `patch` in
the owning session is escalated to the host's human-approval gate so the
person is asked before the run implements. `accepted` is their recorded
go-ahead and ends it; the field's ABSENCE is unknown rather than "not
accepted", so every plan that is not a planning run — a delivery checklist, a
CLI write, a record predating the field — is ungated. Still a declaration and
still never evidence: the stamp says what the run claims about the person's
answer, never that a plan was reviewed or that anything was implemented.
_Avoid_: task list as evidence, TodoWrite (that is another product's tool
name), reading an absent `plan_stage` as an unaccepted plan

**Completion dossier**:
The record a verification verdict, a review finding set, or a QA result
survives its session in: `native_completion/v1` at
`$OMH_HOME/runtime/completion/records.json`, written by OMH from an
`omh_todo action=record` declaration under a scope checkpoint, read back with
`omh_todo action=recall` inside a session or `omh runtime verdict show` from
the command line. Every row keeps `standing: model_declaration` and
`observed: false` whatever `claimed_evidence_state` (`prepared_not_observed`
or `observed`) its writer claimed, so a stored PASS is evidence that a PASS
was claimed, exactly as a done mark on the plan todo is. The read adds
`freshness` — `current` or `stale` against a stated revision and environment,
`unbound` when none is stated — and a source state per kind (`absent`,
`stale`, `declared_no_findings`, `declared_findings`), so a missing record
and a record that declared nothing found never read alike. A verdict is bound
to the revision it was declared for and goes stale when that revision moves,
not on a clock; the 30-day cap only bounds a dossier nobody moves.
_Avoid_: reading a stored PASS as verification, reading an absent dossier as a
clean run, review transcript (the store holds summaries and evidence ids,
never transcripts or raw output)

### Coding delegation

**Default coding lane (Hermes harness)**:
Absent an explicit coding-owner choice, coding work runs inside the Hermes
harness and no external coding CLI is selected. This is the default and the
normal path — most coding work in chat is this lane, and none of the
Maestro/handoff machinery below participates in it. The nine-term contract in
`src/coding/orchestration_vocabulary.py` pins this wording.
_Avoid_: coding handoff (that is the Maestro lane), assuming an external
executor by default, reading handoff modules as the main coding path

**Coding owner**:
The executor selected to perform coding work for a run: Codex, Claude Code, a
Hermes runtime/handoff path, or a generic executor profile. OMH language,
schemas, and reports stay neutral across all of them.
_Avoid_: defaulting to Codex in wording, the agent

**Maestro**:
The operator lane by which an external coding CLI becomes the coding owner
after an explicit user choice — `src/coding/maestro/` prepares a handoff for
the chosen CLI and never runs it. Its facade rejects the `hermes` profile
(`HermesNativeSelectionError`), so the default lane and Maestro never blur in
code; keep them separate in prose too. Prepared handoffs, executor capability
snapshots, executor prompting contracts, throughput overlays, and the handoff
sections of `wrapper-routing.md` all belong to this lane, not to the default
lane. The lane's skill-facing surface is the `ulw-maestro` engine (canonical
`maestro`), whose explicit-owner precondition is the same gate stated here.
_Avoid_: treating Maestro surfaces as the default coding path, legacy (it is
current, just not default), "coding delegation" as a synonym for all coding
work

**Fanout dispatch**:
OMH's one sanctioned execution surface — the explicit, operator-invoked
`omh coding fanout dispatch` (multi-unit) or its `omh coding run` single-run
entry (one unit, same engine, one call) that spawns local agent CLIs as
subprocesses. Nothing else in OMH executes anything.
_Avoid_: implicit execution, background dispatch

**Programmatic tool calling (`execute_code`)**:
Hermes Agent's own tool: the model writes a Python script that calls a
sandboxed subset of Hermes tools over RPC, collapsing a multi-step tool chain
into one inference turn; only the script's stdout returns to context. Part of
the default coding lane and owned entirely by Hermes — OMH neither implements
nor wraps it and may only describe it in awareness guidance.
_Avoid_: fanout dispatch (OMH's separate opt-in surface), an OMH execution
surface, code-mode batching (a Maestro-lane handoff instruction, currently
inert)

**Wrapper session**:
The metadata record of a chat-surface interaction (Discord, Slack, hosted
chat) driving OMH through the chat contract. A wrapper session is
conversation state, not coding evidence.
_Avoid_: run, transcript

### OMH surfaces inside Hermes

**OMH plugin**:
The Python bundle OMH distributes into `$HERMES_HOME/plugins/omh` — Hermes
tools (`omh_*`), lifecycle hooks, the memory provider, and the runtime reader.
A managed copy of `src/plugin_bundle/omh`, never a symlink, and enabled via
Hermes' `plugins.enabled` list.
_Avoid_: equating it with OMH itself

**Plugin tool**:
One of the `omh_*` tools the plugin registers with Hermes (for example
`omh_recommend`, `omh_hud`, `omh_todo`). Tools are metadata-only; the ones
that write touch only OMH-owned artifacts in the configured OMH home.
_Avoid_: MCP tool (the MCP bridge is a separate, allowlisted surface), shell
command

**Awareness**:
The bounded guidance the plugin's hooks inject into Hermes context so Hermes
knows which OMH tools and workflows exist and when to use them. Awareness is
instruction, never state and never evidence.
_Avoid_: system prompt, memory

**Memory provider**:
OMH's deterministic file-backed memory implementation that Hermes loads only
when `memory.provider: omh` is selected in Hermes config. Hermes runs at most
one external provider, so the key is a slot, not a list.
_Avoid_: claiming the slot when another product holds it

**HUD payload**:
The metadata-only JSON projection built by `read_omh_hud()` from OMH home and
Hermes home — plugin readiness, activity rows, the plan todo, display lines.
Status narration, never execution, review, CI, merge, or token-usage evidence.
_Avoid_: runtime state (the payload is a read-only projection of it)

### Hermes terminal surfaces

**Classic TUI**:
Hermes Agent's Python prompt_toolkit terminal UI — what `hermes` runs when
`display.interface` selects the classic REPL. It does not load user widget
files; its extension point is wrapper-CLI method hooks.
_Avoid_: treating it as the surface OMH widgets render in

**Modern TUI**:
Hermes Agent's TypeScript terminal UI — what `hermes --tui` runs, and what
bare `hermes` runs when `display.interface: tui` is set. Loads user widget
apps from `$HERMES_HOME/tui-widgets/*.mjs`. The only Hermes surface that
renders OMH's widgets.
_Avoid_: ui-tui (internal directory name), dashboard TUI

**Widget zone**:
A named slot in the Modern TUI layout where an ambient widget app renders.
`dock-top` sits above the prompt input (below the top status rule);
`dock-bottom` sits below the prompt input (above the bottom status rule).
_Avoid_: assuming dock-bottom means above the input

**OMH status widget**:
`omh-status.mjs`, the managed Modern-TUI widget file OMH installs into
`$HERMES_HOME/tui-widgets/`. It registers two ambient apps that frame the
composer like the classic REPL: a `dock-top` app renders the plan-todo
checklist above the input — its `[Plan]` header carries the transient
`parallel shot ×N` badge — closed by the frame rule above the composer,
and a `dock-bottom` app opens with the frame rule
below the input and renders the status HUD (header always visible when
installed; activity rows only during live work).
_Avoid_: statusline (that is a different, host-owned surface), HUD (the widget
renders the HUD payload; it is not the payload)

**Hermes Desktop**:
Hermes Agent's Electron app (`hermes desktop`). It loads no `tui-widgets/`
file; its extension point is a desktop plugin, one uncompiled ESM file that
default-exports a `HermesPlugin`. OMH's desktop half is `desktop/plugin.js`
plus `dashboard/manifest.json` and `dashboard/plugin_api.py` inside the
installed bundle (`$HERMES_HOME/plugins/omh/`). By its loader source, the app
copies `desktop/` to `$HERMES_HOME/desktop-plugins/omh/` beside a
`.hermes-package.json` marker, and the `hermes serve` process it spawns mounts
the backend's `router` at `/api/plugins/omh/` while `omh` is in the host's
`plugins.enabled` (the registration `omh setup` and `omh update` write, and
`omh doctor` reports as `plugin_enabled`); `plugin.js` polls
`GET /api/plugins/omh/hud` and draws the structured payload in a right-hand
`omh` pane -- the plan checklist with its phases, glyphs and progress, the
agent rows with their route identity (`category:name(model:effort)`) and
metrics, the fanout DAG while one is active -- plus a compact agent and plan
count in the status bar, in the app's own visual language (theme tokens, the
bundled panes' typography); the same HUD payload the status widget renders,
with the same formatters, and the main session's model from the app's own
state, which the payload carries only per subagent row. The route reads the
launch profile's home: the `profile` query the app appends when it routes a
non-primary profile through a shared backend is not honoured, as the host's
own bundled plugin backends do not honour it (a local non-primary profile has
its own backend and is unaffected). The half ships OFF: the marker forces it
disabled until the user switches it on under Capabilities -> Plugins, and that
decision lives in the app's renderer storage, which OMH neither reads nor
writes. The browser dashboard (`hermes dashboard`) reads the same
`dashboard/manifest.json` and registers a tab for every manifest; OMH marks
it `tab.hidden`, so that dashboard gets no `omh` tab (it still requests the
manifest's default entry script and logs its absence at console level).
_Avoid_: reading `desktop-plugins/` as install truth (it is the app's copy;
the bundle under `plugins/omh/` is what `omh update` refreshes), claiming the
half is enabled (not observable from OMH), claiming the app's own copy step
was observed (the pane and the status-bar item were observed from a
hand-placed standalone copy in an isolated home; the copy step is read from
the app's source), OMH status widget (that is the Modern-TUI surface)

### Host surfaces OMH reads

**Live TUI session row**:
A row in `$HERMES_HOME/state.db` `sessions`, scoped to `source='tui'`, not
ended, archived, or hidden, not a delegation child, ordered by the host's own
`last_activity_at`. Keyed on the durable session key
(`20260917_132533_8da9b8`), which is what the plan-todo records are keyed on
too. Read by `live_tui_session_rows` for the approval-bypass projection and
the plan-todo scope.
_Avoid_: treating an empty result as a negative answer (it means the question
is unanswerable here)

**Active-session lease registry**:
`$HERMES_HOME/runtime/active_sessions.json`, the host's own record of open
chat surfaces. One entry per lease, carrying the durable session key as
`session_id` and the gateway transport id as `metadata.live_session_id`,
alongside `surface`. It is the only place the two names for one TUI session
meet — the transport id appears in no `state.db` column — so it is how a
widget reference taken from the per-TUI active-session file resolves to the
session that owns a plan. Written by `_claim_active_session_slot`
(`tui_gateway/session_lifecycle.py`) on a session's first real turn, not at
creation, so a TUI nobody has prompted in is named by no entry.

This is a read-only coupling to a Hermes-private file shape, and part of the
plugin's compatibility surface alongside the hooks and tools:
`requires_hermes` in `src/plugin_bundle/omh/plugin.yaml` currently reads
`">=0.21.1"` (a floor only, so a new Hermes minor does not delist the
plugin), and testing OMH against a new Hermes release includes checking that
the file, both field names, and the `surface` value still hold. If the shape
moves, the read must go quiet and the reference stay its own identity — never
fall back to guessing which session is reading.
_Avoid_: writing to it, treating it as a published API, reading a non-`tui`
lease as a TUI identity

**Hermes child usage ledger**:
The `sessions` rows of the disposable `HERMES_HOME/state.db` that
`omh coding hermes-child dispatch` hands its child, read once after the child
exits and before that home is removed (`src/coding/_hermes_child_usage.py`).
Hermes writes its `--usage-file` report for `-z/--oneshot` alone, and that mode
cannot read a prompt from stdin, so on the `chat --query-file -` transport the
ledger is the only record of the turn's spend: every API call queues
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`reasoning_tokens`, `api_call_count`, `estimated_cost_usd`, `cost_status`,
`cost_source`, `model`, and `billing_provider` into the row
(`agent/turn_usage.py` → `SessionDB.queue_token_counts`), drained at turn
finalize and again when the quiet CLI exits. The home belongs to that child
alone, so every row in the file is its spend and the rows are summed; the
result is the `-z` report's vocabulary, which the observation builder already
reads. The child runs with `--toolsets file --safe-mode`, so no delegate
subagent opens a row of its own and a context-compression rotation is the only
source of a second row. The sum equals the `-z` counters only because of that
restriction: a delegate child writes its own `sessions` row, and the `-z`
main-loop keys exclude subagent tokens while its cost includes them, so a
later `--toolsets` widening that admits delegation has to revisit this read.
The columns have been in the table since Hermes 2026-04, before the
`requires_hermes` floor.

This is a read-only coupling to a Hermes-private file shape, the same class as
the lease registry above: widening `requires_hermes` includes checking that
the table and these column names still hold. If the shape moves, the read goes
quiet and `usage` stays empty — never zero, never estimated.
_Avoid_: writing to it, reading it before the child has exited, reading any
`state.db` other than the disposable one the dispatcher created, treating an
empty result as a measured zero, treating a `timed_out` or `cancelled`
result's usage as complete (the child was killed, deltas still in Hermes'
daemon token-writer queue are lost, and the read is a lower bound with no
marker of its own)

**Originating session stamp**:
`origin_session_id` on a fanout unit (and on the dispatch summary, for that
run's recovery attempts), copied by `omh coding fanout dispatch` from the
`HERMES_SESSION_ID` Hermes injects into every terminal command's environment
(`tools/environments/local._inject_session_context_env`) — the session-db id
of the spawning conversation, the same value `processes.json` records as
`parent_session_id`. It is the only link from a fanout unit to the
conversation that asked for it, and the cost receipt reads nothing else to
attribute a unit.
_Avoid_: attributing a unit by timing or by todo text, stamping a value that
is not id-shaped, reading an absent stamp as "belongs to no one" rather than
"not attributable"

### Fault domains

**OMH install fault**:
A managed artifact under a host root is missing, stale, or was never refreshed
on this machine; the repo itself is fine. `omh doctor` proves it — it reports
plugin, widget, and managed-skill state — and `omh setup` or `omh update`
fixes it. Measure the domain before assuming one: this and the Hermes
user-config fault are cheap reads and come first, an OMH product fault needs a
clean-tree reproduction, and a Hermes-side fault comes last and only with both
of its proofs in hand.
_Avoid_: opening a PR for it, reaching for `hermes update`

**Hermes user-config fault**:
A user-owned key is set inconsistently with what the reporter expects —
`model.default`, `model.provider`, `model.base_url`, or a display choice the
operator declined to migrate. Reading the key proves it. OMH normally reports
the inconsistency and stops there. The narrow display exception is the branded
TUI choice: fresh canonical configs default to `display.interface: tui` and
`display.skin: omh`; interactive setup/update may replace canonical display
values only after a default-Yes confirmation (or `--yes`), which also sets
`display.sections` to collapsed (`thinking`, `tools`, `subagents`) — that key
is consent-only and unset-only per section, never replacing a value the
person set. No,
`--no-omh-tui`, and every noncanonical YAML shape preserve the existing
display choice byte-for-byte. JSON suppresses prompting but `--yes` remains
explicit consent; without `--yes`, JSON preserves explicit canonical values.
Dry-run may preview the accepted change but never persists it. Check this
fault alongside the install fault, before reproducing anything.
_Avoid_: rewriting a declined or noncanonical display choice, filing it as a
product fault

**OMH product fault**:
The behaviour reproduces from a clean install and on the repo dev tree,
independent of the reporter's machine — prove it by running
`uv run python -m omh.cli …` in a clean checkout. The fix is a repo change
with tests, which makes this the only fault domain that produces a PR.
_Avoid_: claiming it before a clean-tree reproduction

**Hermes-side fault**:
Hermes Agent's own code or built assets are genuinely behind, which is what
makes `hermes update` the answer. Two proofs are required together, never
either one alone: `git -C "$HERMES_HOME/hermes-agent" rev-parse HEAD` behind
that repo's `origin/main`, and the built Modern-TUI bundle older than its
TypeScript sources. A visual symptom that reads as "an old TUI" is almost
always an OMH product fault or a Hermes user-config fault instead — in one
real session Hermes sat at its `origin/main` HEAD with a freshly built
Modern-TUI bundle while the visual complaint was entirely valid, and the
missing chrome turned out to be an OMH product gap. OMH never patches Hermes
either way.
_Avoid_: prescribing `hermes update` from a visual symptom alone, treating one
of the two proofs as sufficient

### Repo guard vocabulary

**Byte gate**:
A CI check that compares a generated artifact byte-for-byte against its
regeneration from source (`omh docs … --check`). A one-character drift fails;
the fix is always to edit the source and regenerate, never the artifact.
_Avoid_: lint (byte gates prove provenance, not style)

**Routing corpora**:
The two named guard corpora for router changes: `ROUTING_PRECISION_CASES`
(negative controls; failure metric `overroute_count`) and
`ROUTING_INTERVENTION_CASES` (positive interventions; failure metric
`missed_intervention_count`). Every trigger change ships cases in both.
_Avoid_: underroute (that name matches nothing in the code)

**Budget ledger**:
The one module (`src/maintenance/drift.py`) that measures every per-turn and
footprint number, holds its limit and kind (`ratchet` | `ceiling`), and renders
the verdict (`drift_report()`, also `verdict`). `skill_content_smoke` and
`omh release drift` read their budget verdicts from it.
_Avoid_: a second comparer, a count or headroom formula restated in a test

**Managed artifact**:
A file OMH installs and refreshes under a host-owned root and may safely
overwrite on setup/update — the plugin bundle, the widget file, the identity
skin (`skins/omh.yaml`), managed skills, and the config keys OMH inserted.
The branded-TUI consent is the narrow exception for existing canonical config:
accepting the interactive default or passing `--yes` may set
`display.interface: tui`, `display.skin: omh`, and a collapsed
`display.sections` so bare `omh` and `hermes` open the same surface and a long
run does not bury the conversation. Installs already carrying the first two
are not prompted, and take the third through `--yes`. Declining,
passing `--no-omh-tui`, or using a noncanonical YAML shape leaves the display
configuration untouched. Everything else under a host root is user-owned and
preserved.
_Avoid_: overwriting anything OMH did not write without explicit consent,
rewriting a declined or noncanonical display choice
