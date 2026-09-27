# Web QA observations and same-condition canary comparisons

This page documents the host-owned web QA observation workflow from issue
1383: `web_qa_observation_plan/v1`, `host_web_qa_adapter_receipt/v1`,
`web_qa_observation_run/v1`, and `web_qa_comparison/v1`. OMH core plans,
validates, persists, and compares evidence that a selected host collected. It
never opens a browser, sends a request, deploys, or rolls anything back.

## What a person asks Hermes

You don't need any of the commands below. Describe the outcome in chat and let
Hermes pick `visual-qa`, `browser-operator`, or `deploy-and-monitor`:

- "Run the checkout QA matrix against staging at revision `abc123` on desktop
  Chromium and mobile WebKit, then tell me PASS, REVISE, or BLOCK."
- "Compare the production canary for deployment `deploy-2041` with the
  baseline we captured before the rollout. Same viewports, same locale."
- "The visual score came back 84. What has to change before the next round
  counts?"

Hermes answers in plain language and names the evidence that's missing. When a
host adapter, wrapper, or executor is available, Hermes asks it to collect and
then hands the sanitized result to OMH through the reference below.

## Agent and operator reference

Everything from here down is control-plane material for Hermes Agent, host
adapters, wrappers, coding agents, and maintainers. It isn't a normal user
surface, and none of these commands run by default, on a schedule, or in the
background. There is no launch, watch, or polling loop in core OMH.

### Commands as shipped

Observed from `omh web-qa observation --help` and its subcommands (the
`motion` group is covered under
[Motion interaction captures](#motion-interaction-captures)):

```sh
omh web-qa observation plan    --project-root PATH --plan-json PATH
omh web-qa observation import  --project-root PATH --plan-json PATH --receipt-json PATH [--capture SHA256=PATH ...]
omh web-qa observation show    --project-root PATH --run-id RUN_ID
omh web-qa observation compare --project-root PATH --baseline-run-id RUN_ID --candidate-run-id RUN_ID [--deployment-observation-json PATH]
```

| Subcommand | What it does | What it never does |
| --- | --- | --- |
| `plan` | Normalizes a bounded plan request, derives digests and `run_id`, and reports `completion_state` as `completed` or `not_found`. Creates no files. | Launch, fetch, or reserve anything. |
| `import` | Validates one canonical plan plus one sanitized host receipt, verifies local capture bytes, and persists them atomically. | Retry, recollect, or accept a receipt it can't fully admit. |
| `show` | Re-admits one completed run from disk and projects plan, receipt, observation, captures, and timings. | Trust the stored verdict without recomputing it. |
| `compare` | Re-admits two stored envelopes and reports a comparison verdict. | Deploy, roll back, or treat a flag as a deployment. |

`--project-root` must be an existing local Git root; the store lives under
`.omh/web-visual-qa/observations/<run_id>/` beneath it. `--plan-json` and
`--receipt-json` accept a regular file or `-` for stdin, capped at 256 KiB, with
duplicate keys and non-finite numbers rejected. `--run-id` must match
`web-qa-` followed by 24 hex characters. `--capture` takes `SHA256=PATH` and
repeats once per observed screenshot digest; each path must be a local regular
file (symlinks refused) of 25 MiB or less containing PNG, JPEG, or WebP bytes.

Exit codes follow the rest of `omh`: a refused plan, receipt, or capture raises
an `OmhError` with the reason and no partial state on disk.

### Plan request and normalized plan

The plan request is a closed object with exactly `mode`, `subject`,
`condition`, `authorization`, `limits`, and `round`. Anything extra fails.

`mode` is `matrix` or `canary`.

`subject` is the changing lineage:

- `repository`: absolute credential-free URL, normalized to origin plus path.
- `revision`: pinned 40 or 64 hex characters.
- `observed_deploy_ref`: opaque reference or `""`.
- `deployment`: `null` for `matrix`; for `canary` a closed object with
  `deployment_id`, `environment`, `rollout_phase` (`pre_deploy`, `canary`,
  `rolling`, `complete`), and `window` with UTC `starts_at` and `ends_at`.
  The normalized plan adds `duration_seconds`.

`condition` is the immutable profile a comparison must match exactly:

- `routes`: 1 to 16 entries of `route_id`, `state_id`, `expected_terminal`
  (`document_ready`, `same_origin`, `authenticated_view`), and `url`.
- `viewports`: 1 to 8 entries of `viewport_id`, `width`, `height`, `dpr`.
- `browsers`: 1 to 4 entries of `browser_id`, `engine` (`chromium`,
  `firefox`, `webkit`), `version`.
- `locale`, `timezone`, `auth_fixture_ref` (opaque; never a credential).
- `profiles`: `cache`, `load`, `device`, `cpu`, `network` from closed enums.
- `feature_flags`: up to 64 `flag_id` plus `enabled`/`disabled`.
- `budgets`: `visual.minimum_score` (90 to 100), zero-tolerance
  `functional`, `accessibility`, `console`, and `network` failure counts,
  `network.slow_request_ms`, and `performance` (see below).
- `expected_noise_allowlist`: up to 64 `noise_id` entries for `console` or
  `network`.
- `environment`: `development`, `test`, `staging`, `production`.
- `interaction`: `read_only`, `login`, `mutation`.

The engine and version enums above are what the plan accepts. They aren't a
claim that any given host adapter drives every engine or profile. Support for a
specific browser/profile pair is whatever that adapter's receipt proves cell by
cell; the shipped native collector's actual boundary is listed under
[Native agent-browser collector](#native-agent-browser-collector-host-side)
below.

Sanitization happens at plan time. A route URL is transient input: the stored
route keeps the canonical `origin`, a `path_digest`, a `navigation_digest`
(path plus query plus fragment, hashed), and a `route_digest`. Raw query
values, fragments, and URL credentials never reach disk. `repository` URLs
with credentials, query, or fragment are refused.

`authorization` carries `intent` (must equal `condition.interaction`) and
`staging_test_authorization_ref`. Production plans must be `read_only`.
`login` or `mutation` requires `staging` or `test` and a non-empty
authorization reference. The reference describes a prerequisite; it doesn't
grant anything and the plan output says so in `does_not_authorize`.

`limits` may only tighten the hard caps:

| Cap | Hard maximum |
| --- | --- |
| `max_routes` | 16 |
| `max_viewports` | 8 |
| `max_browsers` | 4 |
| `max_cells` | 64 |
| `max_concurrency` | 4 |
| `max_step_seconds` | 60 |
| `max_run_seconds` | 900 |
| `max_rounds` | 3 |
| `max_attempts_per_read` | 3 |
| `max_artifact_bytes` | 50,000,000 |
| `max_cost_units` | 100 |

`round` is `round_id` plus `ordinal` (1 to `max_rounds`).

The normalized plan adds `required_channels`, the full `matrix` (one cell per
route x viewport x browser with a deterministic `cell_id`), three digests, and
the run identity:

- `subject_digest` covers pinned source/deployment lineage only.
- `condition_digest` covers the immutable condition only.
- `plan_digest` binds mode, both digests, required channels, limits,
  authorization, and round. `run_id` is `web-qa-` plus its first 24 hex
  characters.

Canary plans additionally require a non-empty `observed_deploy_ref`,
`production` for both `condition.environment` and `deployment.environment`,
and a window no longer than `max_run_seconds`.

### Host receipt

`import` takes a `host_web_qa_adapter_receipt/v1` with `receipt_version: 1`.
The receipt is what a selected host adapter observed. It is not a set of flags
OMH will believe; every claim in it must be bound to something OMH can check.

Top-level closed keys: `schema_version`, `receipt_version`, `run_id`,
`subject_digest`, `condition_digest`, `adapter`, `execution`, `cells`,
`release_status`, `release_blockers`, `redaction`.

- `run_id`, `subject_digest`, and `condition_digest` must equal the plan's.
- `adapter` has `adapter_id` and `session_id_digest`.
- `execution` has `command_results`, `artifact_bytes`,
  `session_close_observed`, `attempts`, `cost_units`, `peak_concurrency`,
  `started_at`, `ended_at`. Each command result records `cell_id`,
  `operation`, `success`, `returncode`, `duration_ms`, and stdout/stderr
  digests and byte counts, with `duration_ms` at or under
  `max_step_seconds * 1000`.
- `release_status` is `COLLECTED` or `BLOCK`; anything other than
  `COLLECTED` with an empty `release_blockers` becomes the blocker
  `host_receipt_not_collected`.
- `redaction.status` must be `redacted_before_persistence` and
  `redaction.forbidden` lists what was stripped from `headers`, `cookies`,
  `credentials`, `query_values`, `request_bodies`, `response_bodies`,
  `console_text`, `accessibility_html`.

The store also runs its own privacy scan before writing. Keys containing
`header`, `cookie`, `credential`, `password`, `body`, `email`, `phone`, or
`address`, and strings that look like URLs with query or userinfo, bearer
tokens, `Set-Cookie` lines, email addresses, or phone numbers are refused.

#### Cells and independent channels

Every planned cell must appear once with `cell_id`, `route_id`, `state_id`,
`terminal_status`, `terminal_blocker_id`, `actual`, and `channels`. A missing
cell becomes `host_cell_missing` for the terminal state and every channel.
When `terminal_status` is `observed`, `actual` must reproduce the planned
origin, navigation digest, viewport, browser, locale, timezone, profiles,
auth fixture reference, and expected terminal state exactly.

`channels` must contain exactly the seven required channels, each with
`status` (`observed`, `not_observed`, `blocked`), `blocker_id`, and
`evidence`. A channel that isn't observed needs a named blocker and empty
evidence. An observed channel must bind `operation_digests` to successful,
zero-return-code command results for the same cell and the right operation.
No channel is inferred from another:

| Channel | Bound host operations | Evidence shape |
| --- | --- | --- |
| `screenshot` | `screenshot` | `capture_sha256`, `byte_size`, `captured_at`, `review` |
| `console` | `console` and `errors` | `exceptions[]` with `exception_id`, `classification`, `allowlist_id` |
| `network` | `network_requests` | `requests[]` with canonical `origin`, `path_digest`, `method`, `status`, `duration_ms`, `classification`, `allowlist_id` |
| `critical_flow` | `navigate` | ordered `steps[]` plus optional `trace_reference` |
| `accessibility` | `accessibility_audit` | `findings[]` with `rule_id`, `impact`, `node_count` |
| `keyboard` | `keyboard_tab`, `focus_before`, `focus_after` | `action`, `focus_before`, `focus_after`, `focus_changed` |
| `performance` | `lab_vitals` or `field_vitals` | `evidence_class`, `lab`, `field` |

Screenshot reviews carry `reviewer_id`, `rubric_ref`, `evidence_ref`, the
same `capture_sha256`, both digests, and a `score` of at most 100. A capture
outside the execution window, a missing review, or a lineage mismatch is a
blocker.

Critical-flow steps use `navigate`, `read`, `click`, `extract`, or `submit`
with a `locator` (`role`, `label`, `test_id`, `attribute`, `route` plus a
value digest). `navigate` must bind the planned route navigation digest.
`click` and `submit` are refused when the plan is `read_only` or the
environment is `production`; `submit` further requires `mutation` intent.

#### Retries and terminal failures

Each step records ordered `attempts`. Retries are legal only when all of these
hold: the plan is `read_only`, the action is `navigate`, `read`, or
`extract`, the previous attempt failed with a transient class (`navigation`,
`timeout`, `transport`, `browser_crash`), and the retry carries a fresh
`query_identity`. `auth`, `http_4xx`, `assertion`, `regression`, and
`mutation` are terminal; a retry after one is refused outright. Attempt count
is capped by `max_attempts_per_read` (default three: one initial try plus two
retries). Exhausting that cap on transient failures is the blocker
`critical_flow_transient_attempts_exhausted`; ending on any other failure is
the revision reason `critical_flow_unsuccessful`.

#### Typed offline traces

`critical_flow.trace_reference` may point at an approved
`browser_workflow_trace_reference/v1` from issue 1385 (see
[Browser workflow traces](BROWSER-WORKFLOW-TRACES.md)). OMH resolves it
against the project-local trace store by trace ID, digest, project identity,
and origin allowlist. Anything else, including a well-shaped dictionary that
isn't in the store, is the blocker `untrusted_reusable_trace_reference`.
This is a reference to reviewed offline evidence. Promoting a trace into a
project-local skill (issue 1386, see
[Browser skill promotion](BROWSER-SKILL-PROMOTION.md)) is a separate
operator flow and is never activated by importing an observation.

#### Performance: lab is diagnostic, field is gated

`performance.lab` has `lcp_ms`, `inp_ms`, `cls`, each nullable. Lab samples
never pass or fail a budget. With `evidence_class: "lab"`, `field` must be
`null`; if the plan set `field_gate: "required"`, the run picks up the blocker
`field_p75_not_observed`.

With `evidence_class: "field"`, `field` must carry `source_class` (`rum` or
`crux`), `source_ref`, `source_digest`, the plan's `condition_digest`, a
positive `sample_count`, a `window`, and numeric `p75` values. The published
bars are fixed at the plan level: `lcp_ms_lt` at most 2500, `inp_ms_lt` at
most 200, `cls_lt` at most 0.1, and a plan can only tighten them. A p75 at or
above a bar is `field_performance_budget_exceeded` (REVISE). A field sample
whose `condition_digest` differs from the plan is a blocker.

#### Verdict

The admitted `web_qa_observation_run/v1` reports `PASS`, `REVISE`, or
`BLOCK`, with sorted `blockers` and `revision_reasons`. Any blocker is
`BLOCK`; any revision reason without a blocker is `REVISE`. Missing evidence
is always a blocker, never a soft pass. Notable blockers:

- `host_cell_missing`, `session_cleanup_not_observed`,
  `host_receipt_not_collected`
- `concurrency_cap_exhausted`, `attempt_cap_exhausted`,
  `artifact_cap_exhausted`, `cost_cap_exhausted`, `run_deadline_exhausted`
- `stale_or_out_of_window_capture`, `screenshot_review_missing`,
  `screenshot_review_lineage_mismatch`,
  `visual_score_below_minimum_requires_fresh_capture`
- `critical_flow_steps_missing`, `critical_flow_attempts_missing`,
  `untrusted_reusable_trace_reference`
- `field_p75_not_observed`, `field_evidence_condition_mismatch`

Revision reasons include `unallowlisted_console_exception`,
`first_party_request_failure`, `slow_first_party_request`,
`serious_or_critical_accessibility_finding`, `keyboard_focus_not_observed`,
`critical_flow_unsuccessful`, and `field_performance_budget_exceeded`.

The run's own `does_not_authorize` names `browser_launch`,
`network_request`, `deployment`, `rollback`, and `storage_write`.

### Importing captures

`import` requires one `--capture SHA256=PATH` per observed screenshot digest,
no more and no fewer. Each file's bytes must hash to that digest and match the
receipt's `byte_size`, contain real PNG/JPEG/WebP bytes, and fit inside
`max_artifact_bytes` in total and inside the receipt's own `artifact_bytes`
accounting. Files are copied into
`.omh/web-visual-qa/observations/<run_id>/captures/<sha256>.<ext>` with
`0600` permissions under a `0700` directory, staged first and renamed into
place; any failure removes the staging directory and leaves nothing behind.
OMH owns the copies; the host keeps its originals.

Alongside the captures, `metadata.json` stores the plan, receipt, derived
observation, capture list, and `timings`: `validation_ms` (OMH's own admission
time), `adapter_execution_ms` (the sum of host command durations), `attempts`,
`artifact_bytes` (bytes actually imported), and `reported_artifact_bytes`.
That split keeps host time and OMH overhead separately measurable.

### Completed runs are immutable

A `run_id` is a function of the plan digest. Importing the same plan and a
byte-identical receipt again returns the stored record and writes nothing: no
duplicate captures, sessions, polls, or model turns. Importing the same
`run_id` with a different plan or receipt is refused as a conflict. Every
`show`, `compare`, and `plan` re-admits the stored plan and receipt from disk
and recomputes the observation; a hand-edited `metadata.json` or a changed
capture file fails validation instead of being trusted.

Concurrent identical imports converge on the same terminal record through the
per-run import lock and atomic directory publication. Path admission checks
lexical descent from the observed Git root and refuses symlinks and Windows
reparse points (including junctions) in managed path components; it does not
compare independently resolved spellings of concurrently created directories.
Already completed imports are fully re-admitted before any directory creation,
permission change, or lock-file write, and need no original capture files.

### Motion interaction captures

When transitions, hover, focus, loading, or scroll states are in scope,
`visual-qa` asks for `motion_interaction_capture/v1`. The host records the
motion (MP4 or WebM) and may derive a changed-frame contact sheet from it; OMH
admits only the host's metadata about those artifacts:

```sh
omh web-qa observation motion import --project-root PATH --plan-json PATH --receipt-json PATH
omh web-qa observation motion show   --project-root PATH --run-id RUN_ID --capture-id CAPTURE_ID
omh web-qa observation motion gate   --project-root PATH --run-id RUN_ID --motion-cell CELL_ID [--motion-cell CELL_ID ...]
```

Audience: host adapters, wrappers, and operators, like the rest of this page.

`import` takes the same plan as the observation import and one
`host_motion_capture_receipt/v1` with `receipt_version: 1` and exactly these
keys:

- `run_id` and `cell_id`: one planned matrix cell.
- `lineage`: the producer's own `repository`, `revision`, `route_id`,
  `state_id`, `viewport_id`, and `browser_id`. Every field must equal the
  plan's subject and that cell.
- `producer`: `producer_id` and `version`.
- `requested`: the settings asked for, `sampling_policy` (`every_frame`,
  `fixed_interval`, `changed_frames`) and `max_duration_ms` (at most the plan's
  `max_run_seconds`).
- `observed`: the settings the producer attests it used, `sampling_policy`,
  `started_at`, and `ended_at` (UTC).
- `recording`: `sha256`, `byte_size` (up to 512 MiB), `media_type`
  (`video/mp4` or `video/webm`), `width`, `height`, `duration_ms`, and
  `frame_count` (at most 120 per second of duration). The duration must fit
  both the requested bound and the observed window.
- `contact_sheet`: `null`, or `sha256`, `byte_size`, `media_type`
  (PNG/JPEG/WebP), `source_recording_sha256`, and up to 64 `tiles`. Each tile
  has `tile_index`, its own `source_recording_sha256`, `start_ms`, and
  `end_ms`.

Each refusal raises an error whose message starts with one closed reason code:

| Reason | Refused when |
| --- | --- |
| `lineage_mismatch` | The run, cell, repository, revision, route, state, viewport, or browser differs from the plan. |
| `checkout_revision_mismatch` | The capture's revision isn't the project checkout's `HEAD`, so a capture of an older or newer revision is stale here. |
| `contact_sheet_lineage_mismatch` | The sheet or any tile names a different recording digest, reuses the recording's digest as its own, or claims an interval outside the recording. |
| `digest_invalid` | A digest is missing or isn't lowercase SHA-256. |
| `unsupported_media_type` | The recording isn't MP4/WebM, or the sheet isn't PNG/JPEG/WebP. |
| `metadata_limit_exceeded` | Size, duration, frame count, tile count, or receipt size exceeds its bound. |
| `path_only_evidence` | Any key names a path, URI, URL, file, or location. Artifacts are admitted by digest only. |
| `secret_bearing_metadata` | The store's privacy scan finds credentials, cookies, bearer tokens, URLs with userinfo or query, email addresses, or phone numbers. |
| `malformed_receipt` | A key is missing or extra (inline media such as `data` or `frame` fields included), a type is wrong, or the schema is wrong. |
| `plan_invalid` | The plan doesn't normalize. |
| `stored_record_invalid` | A stored record no longer re-admits, or the store path is unsafe. |

The admitted record is written to
`.omh/web-visual-qa/motion-captures/<run_id>/<capture_id>.json` as
`web_qa_motion_capture_store/v1`. The file holds the plan, the receipt, and the
derived record, and never any media bytes or frames. `capture_id` is `motion-` plus
24 hex characters of the record's canonical digest. Re-importing the same
receipt returns the stored record and writes nothing. A different recording
digest or a different cell gets a different identity, so earlier evidence is
never overwritten. `show` and `gate` re-admit each record from its stored
plan and receipt.

`gate` re-admits the stored observation run and every stored motion record
for it. It then marks each `--motion-cell` `observed`, citing its
`capture_ids`, only when a record matches that cell's run, plan digest,
repository, revision, round, route, state, viewport, and browser exactly. Any
other cell gets `motion_interaction_capture_missing`, and the result is
`BLOCK`. A covered cell keeps the observation's own verdict, so the recording
never turns a `BLOCK` or `REVISE` into `PASS`. Screenshot, console, network,
flow, accessibility, keyboard, and performance evidence remain required
exactly as before.

A motion record proves only that the host declared the named recording, and
any contact sheet derived from it, for the named condition. It doesn't prove
visual correctness, complete interaction coverage, delivery, or PASS, and each
record says so in `does_not_prove`. OMH never records, decodes, or inspects
the media. `import` runs one read-only `git rev-parse --verify HEAD` in the
named project; it launches no browser and makes no network call.

### Comparing runs

`compare` re-admits both envelopes and ignores their persisted verdicts.

If the two `condition_digest` values differ, `status` is `not_comparable`,
`verdict` is `BLOCK` with `condition_digest_mismatch`, and
`condition_differences` lists every differing field path with the baseline and
candidate values. Staging-to-production is the common case here; it can't
claim a same-condition regression.

For comparable runs the result is `PASS`, `REVISE`, or `BLOCK` with per-cell
capture digests and performance regressions. Field p75 regressions beyond the
plan's `relative_tolerances` are `field_lcp_regression_beyond_tolerance`,
`field_inp_regression_beyond_tolerance`, or
`field_cls_regression_beyond_tolerance`, always REVISE, never a softened PASS.
A baseline with any non-visual blocker is `baseline_observation_blocked`.

#### Sub-90 visual score means edits plus a fresh recapture

When any baseline cell scored under 90, the candidate must show real work:

- `subject.revision` must differ (`visual_retest_requires_source_revision_change`).
- `round.ordinal` must be exactly baseline plus one
  (`visual_retest_requires_incremented_round`) and stay within `max_rounds`
  (`visual_retest_round_cap_exhausted`).
- Every failed cell must be present again with a different capture digest and
  a later `captured_at` (`visual_retest_cell_missing`,
  `visual_retest_requires_changed_capture`,
  `visual_retest_requires_newer_capture`).

Rescoring the same capture is not a new round.

#### Canary comparisons

A canary candidate needs `--deployment-observation-json`. The file is a closed
`host_deployment_observation/v1` that a trusted host produced from an already
observed deployment. It's an observation, not a success flag: OMH binds it to
the candidate's `observed_deploy_ref` and checks every field.

Required keys: `schema_version`, `deployment_ref`, `repository`, `revision`,
`deployment_id`, `environment`, `rollout_phase`, `status`, `window`
(`starts_at`, `ends_at`, `duration_seconds`), `observed_at`,
`observation_source_ref`, and 1 to 8 `evidence_digests`.

The comparison blocks when:

- no resolver was supplied (`canary_trusted_deployment_resolver_missing`) or
  the record doesn't resolve (`canary_deployment_receipt_unresolved`);
- `status` isn't `succeeded` (`canary_deployment_not_succeeded`);
- `deployment_ref`, `repository`, `revision`, `deployment_id`,
  `environment`, or `rollout_phase` differ from the candidate subject
  (`canary_deployment_binding_mismatch`), or the window differs
  (`canary_deployment_window_mismatch`);
- `observed_at` is later than the canary window start
  (`canary_deployment_observed_after_canary_window_start`);
- the candidate's observation window or any capture falls outside the
  deployment window;
- the baseline isn't `production`, or its window or any capture isn't
  strictly before `observed_at` (`canary_baseline_is_not_production`,
  `canary_baseline_observation_not_before_deployment`,
  `canary_baseline_not_before_deployment`);
- a canary baseline is compared against a non-canary candidate.

So a canary needs both a pre-deployment production baseline and a candidate
captured entirely inside the declared window, under one condition digest.

Every comparison result carries `does_not_authorize: [execution, deployment,
rollback]` and `rollback_authorized: false`. A blocked canary's
`recommendation` points at the existing `deploy-and-monitor` decision gate.
OMH recommends; a person or a separately observed host action rolls back.

### Where execution lives

Browser sessions, network traffic, deployment observation, and any model turn
belong to the explicitly selected host, wrapper, or executor adapter. Core OMH
stays dependency-free and offline: `omh web-qa observation` opens no browser,
sends no request, and spawns no process. With the feature unused there are
zero browser launches, network calls, model calls, and no extra always-loaded
prompt bytes; the guidance lives inside the demand-loaded skill bodies.

### Native agent-browser collector (host side)

`tools/web_qa_hermes_agent_browser_collector.py` is the shipped host-side
producer. It is not part of the `omh` package and nothing in `omh` imports or
launches it; an operator, wrapper, or Hermes tool runs it explicitly. It drives
the installed `agent-browser` CLI one session per cell, projects each command
into the receipt shape above, and reuses OMH's pure plan and observation
validators only to close its own input/output boundary.

Invocation:

```sh
python tools/web_qa_hermes_agent_browser_collector.py --output-dir DIR [--timeout-seconds N] < request.json
```

`--output-dir` becomes a private (`0700`) directory that receives one
`<cell_id>.png` per observed screenshot and a `.web-qa-collector-cache-v1/`
receipt cache. `--timeout-seconds` defaults to 90 and must stay within the
plan's `max_run_seconds`. Stdin is a `host_web_qa_collector_request/v1`
object of at most 256 KiB with exactly `schema_version`, `plan` (the normalized
plan from `observation plan`), `route_urls` (one transient URL per planned
`cell_id`, checked against the stored origin and navigation digest), and
`fixture_assignments` (the plan's `auth_fixture_ref` mapped to
`{"mode": "anonymous"}`). The receipt is written to stdout; a refused request
prints a `BLOCK` stub with a `blocker_id` and exits 2.

What it actually supports on a POSIX host with `agent-browser` installed:

- Chromium only, anonymous only, read-only only. The plan must carry
  `interaction: read_only` with an empty authorization reference, and the
  collector checks after navigation that the page has no cookies, local
  storage, or session storage. `expected_terminal` may be `document_ready` or
  `same_origin`.
- Cold desktop profiles only: `cache: cold`, `load: normal`,
  `device: desktop`, `cpu: normal`, `network: online`, and no feature flags.
- The viewport, locale, timezone, and Chromium version the plan names must be
  what the browser reports back. The collector sets the viewport; it never
  changes the browser's locale, timezone, or engine to satisfy a plan.

Anything else yields a named blocker for that cell, never a substitution:
`unsupported_browser_engine_<engine>`, `unsupported_authenticated_view`,
`unsupported_<profile>_profile_<value>`, `unsupported_feature_flag_assignment`,
or, once the cell ran, `actual_locale_mismatch`, `actual_timezone_mismatch`,
`actual_version_mismatch`, `fixture_assignment_not_observed`, and similar. A
blocked cell reports every channel as `blocked` with that blocker, so the
imported run is `BLOCK`. A whole matrix that is blocked before any launch is
still admissible through a `capability_preflight` command projection; no
session is implied.

Other collector properties an operator should know:

- Non-POSIX hosts stop at `unsupported_host_platform_posix_file_lock_required`.
- Every command is capped at 60 seconds, the plan's `max_step_seconds`, and
  the remaining run deadline; output over 128 KiB kills the command and marks
  it failed. A launched session is always closed, and a failed close is
  `session_cleanup_not_observed`.
- Before a screenshot the collector reserves the worst-case uncompressed size
  for the viewport against `max_artifact_bytes`; a cell that can't fit is
  `screenshot_storage_reserve_unavailable`. Captures are `chmod 0600` before
  they're hashed.
- The screenshot channel ships with `review: null`. The collector has no
  visual reviewer, so a native receipt imports as `BLOCK` with
  `screenshot_review_missing` until an independent reviewer adds an
  exact-capture review bound to the same `capture_sha256`.
- Re-running the same request against the same `--output-dir` returns the
  cached receipt after re-verifying capture digests, with no new browser
  commands, captures, or writes. A different request for the same `run_id`
  is `completed_run_identity_conflicts_with_request`.
- Console text, request bodies, headers, cookies, and query values never enter
  the receipt; console exceptions are digests, network entries are canonical
  origin plus path digest.

### What has been observed locally

Two scripts exercise the path end to end. Both report numbers only for the
run they just made; they carry no production, universal-support, or
cross-host claim, and neither is run in CI.

`tools/benchmarks/web_qa_hermes_agent_browser_localhost_proof.py` serves a
two-page fixture on `127.0.0.1`, reads the installed browser's real version,
locale, and timezone into the plan, runs the collector against a positive page
and a negative page, and asserts the shape of what came back: all seven
channels observed on the positive page, keyboard focus changed, session close
observed, `BLOCK` carrying `screenshot_review_missing` and no revision
reasons; on the negative page at least two console exceptions, a first-party
404, and the `unallowlisted_console_exception` plus
`first_party_request_failure` revision reasons. It then repeats the positive
request and asserts byte-identical output with zero new writes. Its report
separates `adapter_wall_ms` (the collector process), `pure_core_ms` (OMH's
`build_web_qa_observation` alone), and per-command durations. Pass
`--output-dir` to keep the PNG, normalized plan, and redacted receipt for an
independent review.

`tools/benchmarks/web_qa_observation_import.py --plan PLAN.json --receipt
REVIEWED_RECEIPT.json --capture CAPTURE.png` takes a receipt that already
carries a genuine visual review, creates a throwaway Git project, and drives
the real CLI: `import` must return `PASS`, a repeated `import` must return the
same payload with zero changed or added files under `.omh`, `show` must equal
the import result, and a same-run `compare` must be `PASS` with
`rollback_authorized: false`. Its `web_qa_cli_benchmark/v1` report keeps
`adapter_observation_ms` (from the receipt's execution window), per-command
`omh_cli_wall_ms`, and the store's own `import_timings` apart, and states its
claim boundary as one local fixture observation.

The native QA pass reported for this feature ran those two scripts against a
local fixture: positive and negative localhost collection, independent
exact-capture visual reviews written against the collector's PNG digests, and
real CLI `import`, `show`, and `compare` returning `PASS` with a write-free
repeat import. That is local fixture evidence on one machine. It says nothing
about other engines, profiles, authenticated views, other hosts, or
production targets, and no number from it belongs in prose without the arm,
corpus, and artifact it came from.

### Related skills

- `visual-qa` owns the matrix plan, evidence manifest, and the
  PASS/REVISE/BLOCK narration.
- `browser-operator` owns the interaction boundary for what the host may
  click, type, or submit, and names the native collector's capability
  boundary and the promotion drift rule.
- `deploy-and-monitor` owns the deploy decision gate that consumes a canary
  comparison.
- `workflow-learning` owns the offline trace lifecycle (`omh web-qa trace`)
  and the promotion receipts (`omh web-qa promotion`) that turn an approved
  trace into a project-local skill.

Generated skill bodies live under `skills/omh-visual-qa/`,
`skills/omh-browser/`, `skills/omh-deploy-and-monitor/`, and
`skills/omh-workflow-learning/`; edit `src/skills/catalog_definitions.py` or
`src/skills/catalog_feature_surfaces.py` and regenerate with `omh docs
workflows --output docs/WORKFLOWS.md` plus the tap-skill template write-back
described in `CLAUDE.md`.
