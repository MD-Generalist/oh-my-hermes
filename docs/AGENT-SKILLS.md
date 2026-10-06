# Agent Skills projection

OMH can project its reviewed workflow catalog into the open Agent Skills
`SKILL.md` format for Claude Code, Codex, Cursor, opencode, OpenClaw, and pi.
This is a guidance projection, not a port of the Hermes runtime, plugin, or
wrapper. The existing managed Hermes skills and their installation are unchanged.

## Clone-and-run host installers (no Python or omh required)

Clone once, then run the chosen adapter **from the target project's root
directory** (it need not be a git repository). The script locates the canonical
`agent-skills/` source beside itself, not in your current directory:

```sh
git clone https://github.com/rlaope/oh-my-hermes.git "$HOME/oh-my-hermes"
cd /path/to/your/project
sh "$HOME/oh-my-hermes/.cursor/install.sh"
# User scope instead of this project:
sh "$HOME/oh-my-hermes/.cursor/install.sh" --user
```

Replace `.cursor` with `.claude`, `.codex`, `.opencode`, `.openclaw`, or `.pi`.
Each directory contains generated `install.sh`, `install.ps1`, and
`manifest.json`; there are no handwritten host skill copies. POSIX installation
uses ordinary shell utilities plus `sha256sum` or `shasum` (automatic fallback),
with no Python, omh, package manager, network access, or git subprocess.

Windows-native PowerShell (5.1 or 7):

```powershell
git clone https://github.com/rlaope/oh-my-hermes.git "$HOME/oh-my-hermes"
Set-Location C:/path/to/your/project
& "$HOME/oh-my-hermes/.cursor/install.ps1"
& "$HOME/oh-my-hermes/.cursor/install.ps1" -User
```

Claude installs only `.claude/skills/`; the other five adapters install only
`.agents/skills/`. User scope places the same relative path under the user's
home. These paths come from the recorded [host matrix](#host-support-matrix),
not invented vendor-specific directories. In particular, `.openclaw/` and
`.pi/` contain installers, **not** new host skill scan paths. OpenClaw's custom
`OPENCLAW_STATE_DIR` caveat below still applies; no alternative path is guessed.

Both installers verify the complete source file inventory and SHA256s before
writing, copy only listed files, verify the installed bytes, and print the
installed skill names. Re-running is idempotent. An explicit reinstall replaces
listed files, so review/back up local edits first. Other files and skills
(including existing `.claude/skills/triage-sweep`, `review-sweep`, and
`model-onboarding`) are preserved. Symlink/reparse-point destinations are
refused. Interrupted copies report failure; rerun after resolving the cause.
Clone scripts do not track or delete retired files from older packs.

If omh is already installed, the same catalog manifest selects the destination:

```sh
# Host-selected repo scope defaults to the CURRENT project directory, like the scripts.
omh install --target agents --host cursor
omh install --target agents --host claude --scope user
omh install --target agents --host cursor --status --json
```

The CLI and both clone scripts write the same skill bytes and
`.omh-agent-skills-manifest.json` receipt. With a matching package version/checkout,
fresh installed trees are byte-identical, including the receipt, in either scope.
Single-target receipts use `target_dirs: ["."]` relative to the receipt directory,
not install-time absolute paths; CLI status still reports the actual absolute
destination. The CLI retains its collision checks and manifest-owned retired
file cleanup; the clone scripts deliberately copy only the committed pack.
Neither path installs the Hermes-native `skills/` projection, registers the
Hermes plugin, adds host rules/plugins/MCP tools, or proves host execution.
**Installing** needs no Python; **using** workflows marked `requires-omh-cli`
still needs that optional CLI. Phase 1 is copy-only, not a runtime port.

## Install through omh without a host selector (agents and operators)

Install the `oh-my-hermes` Python package so `omh` is on PATH, then choose one
scope explicitly:

```sh
# From anywhere inside the target git repository; writes at the git root.
omh install --target agents --scope repo

# User-wide: two independent copies, not symlinks.
omh install --target agents --scope user

# Read-only inspection (add --json for machine-readable output).
omh install --target agents --scope repo --status
omh install --target agents --scope user --status --json
```

Repo scope generates `.agents/skills/<name>/SKILL.md` and a `.claude/skills/`
copy at the git root, and fails closed outside a repository. Claude Code
2.1.270 discovered the repo `.claude/skills/` copy but not `.agents/skills/`
in observed parent QA (2026-09-13; details below).
User scope likewise generates both `~/.agents/skills/` and `~/.claude/skills/`,
because Claude Code does not document `~/.agents/skills/` as a user scan path. References
are copied alongside each skill. Generation works from an installed package;
it does not read this repository's committed projection tree.

`--dry-run` reports current status without writing. Source import, Hermes
profiles, and release-selection options do not apply to this target. No Hermes
configuration is registered, and no vendor configuration is written. Existing
symlink destinations and conflicting files OMH never installed are refused;
unrelated skills are preserved. Repo-wide Hermes source imports exclude only
manifest-owned files in the Claude mirror, not neighboring custom Claude skills.
An explicit import rooted at the mirror still imports the selected source.

### Manifest and refresh

`.omh-agent-skills-manifest.json` uses `schema_version:
omh_agent_skills_projection/v1`, a content-addressed `catalog_revision`,
`target_dirs`, and a `files` mapping from projection-relative path to SHA256.
Without a host selector, both scopes retain absolute `target_dirs` and write the
**same manifest** at their two roots; each listed file digest applies to both
copies. Host-selected single-target installs instead use the portable `["."]`
receipt described above. Older absolute single-target receipts report stale
(clean drift) until an explicit reinstall refreshes them. Manifest paths never
choose the write destinations.

Status separates `projection` (`fresh`, `stale`, `missing`) from `drift`
(`clean`, `locally_modified`, `unknown`), and includes `locally_modified` and
`next_action`. Human output spells the drift state `locally-modified`. A missing
or edited mirror is not a fresh install in either scope. `mirror:` prefixes identify mirror
file drift. An interrupted install stays missing/stale until every copy agrees.

Inspect and back up local edits before repeating the install command. An
explicit reinstall replaces managed local edits, refreshes both chosen-scope copies,
and removes retired manifest-owned files; it does not import those edits into
the catalog. There is no automatic update, host reload, or background sync.

## Portability classes

The single reviewed table in `src/skills/catalog_portable.py` classifies catalog
**definition fields**, not generated-file keywords. Unknown/new entries are
`hermes-only` until reviewed.

- **portable**: the workflow needs only the host's ordinary reasoning, files,
  tools, or explicitly authorized tasks. Artifact schema names describe output
  contracts; they do not conjure RPC tools or enforce a prompt's policy.
- **requires-omh-cli**: the workflow also uses local deterministic OMH commands
  or records. Its frontmatter declares `Requires the omh CLI on PATH (pip
  install oh-my-hermes).` For example, `omh-frontend` uses offline design data,
  and `omh-codebase-uml` generates its model through the CLI. `omh-docs` answers
  read-only product questions from official sources and bounded local CLI or
  metadata; it routes requested mutations elsewhere rather than requiring a
  Hermes runtime.
- **hermes-only**: the workflow requires Hermes runtime, registration, native
  plugin tools, or wrapper semantics. It is absent from this projection.

The ULW projection includes `ulw-work`, `ulw-plan`, `ulw-loop`, `ulw-qa`,
`ulw-research`, `ulw-perf`, `ulw-context`, and `ulw-interview`. Portable overrides
replace native delegation with the host's actual subagent/task mechanism and
native plan-recording commands with an explicitly chosen durable file/ledger.
Sequential lanes or a named unavailable capability replace missing delegation,
never fabricated participants. Domain contracts and portable references are
shared with the catalog; Hermes-coupled references are not shipped. Progressive
specialist contracts are inlined, retaining their portable procedure references.

### When a replaced catalog section moves

A definition's `portable_overrides` entry **replaces** that catalog section in
the portable body; the catalog's own lines for that section are never
consulted. That is deliberate — a Hermes-shaped line cannot be filtered down,
it has to be rewritten by someone who knows what the host can do — and it means
a line added to the section is absent from the portable body with every drift
gate still green. `docs agent-skills --check` compares the shipped bytes
against a projection that already applied the override, so it agrees with the
override by construction.

The override sits in the same `SkillDefinition` literal as the section it
replaces (`src/skills/catalog_definitions.py`, or the
`_feature_surface_skill(...)` call in `src/skills/catalog_feature_surfaces.py`),
so the edit that moves a section is made beside the replacement that shadows
it. Beside it, `portable_override_shadows` records the sha256 of the JSON form
of each replaced section as it read when the override was last reviewed. When
the section moves, `tests/test_agent_skills_projection.py` fails naming
`<skill>::<section>: shadowed section changed` and prints the new digest. Re-read
the override against the new section, edit it if the line belongs in the
portable body, then update the digest in the same literal. Updating the digest
without editing the override is a valid outcome and records the choice not to
mirror the line.

`tests/test_agent_skills_projection.py` fails, naming `<skill>::<section>`,
when an override key is not a `SkillDefinition` field (a renamed section) or
when a replacement line is absent from the rendered portable body. The second
check is a floor, not a proof: an override whose lines the body already
carries without it (a no-op override, or lines shared with the catalog
section) passes it, so the field-name check is what catches a mis-targeted
section.

### Loop boundary

`ulw-loop` is `requires-omh-cli`. `src/commands/loop.py` calls local loop-ledger
functions in `src/workflows/goal_loop.py`: start/status do not require a running
Hermes process or an existing Hermes home. They create/inspect metadata only.

The default `hermes_goal` driver label is **prepared metadata**, not evidence
that another host has native `/goal` controls. This projection does not port
native goal activation, native gates, or contiguous Hermes-session turn
observations. An external resumable-goal driver requires an explicitly chosen
coding owner and a session-bound `host_observed` capability snapshot; it is
never assumed. Otherwise the host executes authorized work and keeps its own
continuation/evidence ledger. Missing native evidence remains unavailable;
queued ticks never establish execution or goal completion. Linked OMH goal
completion retains its existing evidence gate.

### Hermes-only boundary list

These installable workflows are intentionally absent:

| Workflows | Boundary |
| --- | --- |
| `omh-routing`, `omh-meta-router`, `omh-gateway-intent-card` | Hermes chat/wrapper intake, routing, and delivery continuity |
| `ulw-maestro`, `omh-executor-runtime-readiness` | Hermes harness versus explicit external-owner handoff and readiness semantics |
| `omh-doctor`, `omh-skill`, `omh-capability-toggle` | Managed Hermes installation, inventory, registration, and product diagnostic surfaces |
| `omh-model-setup`, `omh-model-optimization`, `omh-parallel-tools`, `omh-websearch-setup` | Native model slots, mixture routing, or Hermes capability/configuration changes |
| `omh-morning-brief`, `omh-buzz` | Hermes MCP setup or Buzz gateway/transport |
| `omh-research-department`, `omh-automation-blueprint` | Hermes profiles, recurring-intent lifecycle, cron, and delivery composition |
| `omh-agent-board`, `omh-browser`, `omh-achievements` | Native plugin RPC, browser admission/effects, or Hermes badge artifacts |
| `omh-memory-sync` | Hermes USER.md/MEMORY.md review and native write ownership |
| `omh-prompt-import-readiness` | External prompts being exposed as Hermes slash commands |
| `omh-agent-evaluation` | The catalog's paired-run output depends on Hermes-child dispatch receipts |
| `omh-workflow-learning` | Native browser-promotion receipts and managed-skill visibility semantics |

Retired/reference-only ULW surfaces (`ulw-ralph`, `ulw-goal`, `ulw-team`, and
`ulw-process`) and `omh-quality-evidence-loop` remain non-installable here too. A skill mentioned
as a possible next workflow but absent from the installed pack is unavailable,
not authorization to imitate its host-specific behavior.

## Host support matrix

Except for the explicitly observed Claude Code discovery result below, this
is a documentation/source compatibility assessment, not observed skill
selection or execution in six running hosts.

**Observed correction, 2026-09-13 (parent Phase B QA):** Claude Code 2.1.270,
launched in a temporary repository with
`claude -p "list ulw-/omh- skills" --model haiku`, returned `NONE` in two
isolated runs with only `.agents/skills/` installed. After adding the
`.claude/skills/` copy, the same prompt discovered `ulw-context`,
`ulw-interview`, `ulw-loop`, and `omh-plan`. This supersedes the earlier
standard-facts assumption that Claude Code scans repo `.agents/skills/`.
It establishes discovery for the reported version and runs, not execution of
every workflow or a guarantee about other versions.

| Host | Repo scan path | User installation | Discovery and caveats |
| --- | --- | --- | --- |
| Claude Code | `.claude/skills/` mirror (observed 2.1.270) | `~/.claude/skills/` mirror | Repo `.agents/skills/` not discovered in two isolated runs; `.claude/skills/` discovery observed |
| Codex | `.agents/skills/` | `~/.agents/skills/` | Model-selected progressive disclosure; standard-path contract |
| Cursor | `.agents/skills/` | `~/.agents/skills/` | Standard-path support; user-scope scanning not independently verified |
| opencode | `.agents/skills/` | `~/.agents/skills/` | Permission-gated `skill` tool, implicit nomination; description must be 1-1024 characters |
| OpenClaw | `.agents/skills/` | `~/.agents/skills/` with default state root | Implicit and `/skill <name>`/`$name`; workspace `skills/` can outrank this pack |
| pi | `.agents/skills/` | `~/.agents/skills/` | Trusted projects only; implicit and `/skill:name`; first duplicate name wins |

OpenClaw **skips `~/.agents/skills/` when `OPENCLAW_STATE_DIR` points elsewhere**.
No extra OpenClaw target is installed in v1. Its injected discovery summary is
shorter than the full description; frontmatter and loaded body remain separate
surfaces. Resolve reference paths from the loaded skill's base directory
(`{baseDir}` where supplied), not a hardcoded home. Host precedence and existing
same-name skills can affect nomination; installation does not settle it.

The renderer enforces 1-1024-character descriptions. If the catalog description
plus its optional trigger/alias tail exceeds that budget, this target drops the
tail; an over-budget base description fails generation. Hermes bytes are pinned
and unchanged.

## Non-portable capabilities

- **`omh_*` RPC tools** such as todo, delegation-route, and run-summary tools
  are not skills. An MCP adapter is feasible for Claude Code, Codex, Cursor,
  opencode, and OpenClaw; pi needs a TypeScript extension (`registerTool`).
  This install implements neither adapter and registers no tools.
- **File-backed memory provider**: Claude Code, Codex, and OpenClaw already
  have native memory, so another provider is largely redundant there. Cursor,
  opencode, and pi have a provider gap. Model-invoked memory tools and
  session-start context injection have different semantics; neither follows
  from installing a memory-related workflow document.
- **TUI/HUD widgets** are genuinely host-specific. Claude Code status-line
  scripts and pi custom UI could support different adapters; Codex's fixed
  status items, opencode's theme/keybind TUI, and Cursor's extension UI are
  not interchangeable widget APIs. No widget is ported here.
- **Chat-wrapper routing** is Hermes-specific. OpenClaw has analogous gateway
  channel routing; other hosts' subagents/tasks do not implement OMH wrapper
  continuity. A portable workflow does not port that control plane.

## Prepared versus observed

`prepared_not_observed` remains the boundary. A plan, skill copy, manifest,
validation pass, or fresh status proves no host selection, tool invocation,
review, CI, or merge. Report execution only from actual host/tool observations.
Host accounting is reported only when observed; otherwise say unavailable.

## Maintainer byte gate

```sh
omh docs agent-skills
omh docs agent-skills --check
# The external validator accepts ONE skill directory, not a collection root.
npx skills-ref validate agent-skills/ulw-work
```

The committed `agent-skills/` tree is generated from the packaged catalog. The
same command regenerates all 18 host adapter files beside it; `--check` checks
every adapter's exact bytes while leaving existing host skill/configuration
files alone. `--output /tmp/stage/agent-skills` stages the tree and its sibling
adapter directories without modifying the checkout.

The gate detects missing, stale, and extra canonical files, including
references, and missing/stale adapter files. Catalog host rows live in
`src/skills/host_adapters.py`; `host_adapter_render.py` produces both scripts
from the same manifest, with `transform: copy-only`. `source_digest` is SHA256
of the UTF-8, LF-terminated inventory sorted by relative POSIX path, one
`<file-sha256>  <path>` per line. Both scripts embed and verify those file
hashes; the gate pins the inventory to the canonical producer. `.gitattributes`
forces LF for generated Markdown, shell, PowerShell, and JSON so Windows
`core.autocrlf` cannot invalidate checksums or byte comparisons.

Edit catalog/portable overrides or the appropriate producer, regenerate, and
commit source plus projection together. Never hand-edit adapter outputs,
`skills/*`, or `docs/WORKFLOWS.md` to implement this target.

See [Documentation](README.md), [Direction](DIRECTION.md), and
[Workflow Reference](WORKFLOWS.md) for the original Hermes boundaries.
