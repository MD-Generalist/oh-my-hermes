"""Verification discipline for prepared fanout unit prompts.

Six deterministic text blocks ride every dispatched unit prompt:

1. **Goal echo-back** — before any tool use the subagent restates the goal,
   its own deliverable, and the completion criteria, and stops to report (not
   guess) if its reading conflicts with the declared boundary.
2. **Pre-declared completion criteria** — "done" is defined BEFORE work
   starts, as a numbered list derived from the unit contract, so completion
   is a check against stated criteria rather than a feeling.
3. **Verification stop conditions** — verification is mandatory (exactly one
   full pass is the floor, never skipped) and bounded (after the criteria
   pass, re-verifying is forbidden; on failure, at most two fix-and-verify
   cycles before reporting the failing criterion instead of looping).
4. **Failure-kind discipline** — a permission, sandbox, or policy denial is
   a boundary, not a bug (never retried through another route), and
   "blocked" requires a named concrete condition that survives the bounded
   fix cycles — difficulty, uncertainty, or remaining work is not blocked.
5. **Structured return** — the unit's final report ends with one fenced
   JSON object in the `fanout_unit_result/v1` expected-evidence shape. The
   sidecar file is the primary machine-read return; when a contracted
   sidecar is missing the collector parses this block from captured stdout
   and validates it the same way, so collection is parse-then-validate on
   either path and never prose-scraping.
6. **Capped structural search** — code exploration is bounded the same way
   verification is: a few targeted structural-search-or-grep passes before a
   full-file read, escalating only when a bounded pass finds nothing or stays
   ambiguous, and stopping the moment the target is found. Shared verbatim
   with the `executor_prompting_contract/v1` payload's
   `structural_search_discipline` field (`.coding_contracts.
   STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE`) so the two cannot drift.

The blocks split across two placement zones for prompt-cache hygiene: the
goal echo-back, verification-stop, failure-kind, structured-return, and
capped-search blocks are unit-invariant,
so `shared_unit_preamble_lines()` places them (with the overall goal) at the
byte-identical head every sibling prompt of one fanout shares, and the unit
zone of `unit_prompt_assembly.assemble_unit_prompt` carries only the
unit-varying remainder — numbered criteria, role protocol, calibration, and
the domain bundle. Every major
serving stack caches prompt prefixes by exact bytes, so sibling prompts that
share their head let the first dispatch write the cache the rest read; the
rule itself ships as `PROMPT_CACHE_COMPOSITION_PROTOCOL`.

High-effort routes additionally get a per-family calibration block that
counters the known over-verification inertia of strong reasoning models.
Calibration is keyed by model family and selected only when the routed
reasoning effort is in the high tier; families the table has not met get the
generic block — no family carries richer guidance than another without a
stated reason, and no vendor is privileged.

Everything here is pure data and pure functions: the blocks land in prepared
prompts (subprocess argv), and `unit_prompt_assembly` is the one module that
joins them into the text a unit receives.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from .coding_contracts import STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE
from .model_contracts import contract_model_id

# Policy ceiling for a fully-assembled unit prompt (bytes of UTF-8). The
# worst-case combination across roles, owners, and calibration blocks is
# asserted under this in tests; runtime never truncates.
UNIT_PROMPT_MAX_BYTES: Final[int] = 8000

# Reasoning efforts that mark a route as high-effort for calibration purposes.
HIGH_EFFORT_TIER: Final[frozenset[str]] = frozenset({"high", "xhigh", "max"})

GOAL_ECHO_PROTOCOL: Final[str] = (
    "Before using tools, restate the overall goal, your deliverable, and the numbered criteria. "
    "If they conflict with your brief, stop and report the conflict instead of guessing."
)

VERIFICATION_STOP_PROTOCOL: Final[str] = (
    "After finishing work, run one full verification pass; verification is never skipped. "
    "A failed check blocks only a stated criterion; report other observations. Once criteria pass, "
    "STOP verification, including after unrelated edits. After two failed fix-and-verify cycles, "
    "commit what passes and report the failed criterion and output."
)

FAILURE_KIND_PROTOCOL: Final[str] = (
    "A permission, sandbox, or policy denial is a boundary, not a bug: do not try another tool or route; "
    "record it and continue within the boundary. Report blocked only for a named condition persisting "
    "after bounded fixes; difficulty, uncertainty, or remaining work is not blocked. For an unreachable "
    "objective (missing target, policy refusal, infeasible criteria), report process_declined with "
    "decline_reason, not process_failed."
)

# Tool batching (2026-09-11, from the Codex Desktop prompt review in
# MODEL_OPTI.md — a community source adopted for the discipline it states):
# independent reads and searches go out together; anything that depends on
# a result, mutates, needs approval, or waits goes one at a time. Decorative
# shell separators are noise inside a bounded output capture. It rides the
# unit section, not the shared head: the head is frozen at its measured
# small-model budget (`src/quality/small_model_prompt_budget.py`), and a
# batching rule is the first thing a weak lane may drop, so it must never
# displace a stop rule there. Every unit still receives it.
TOOL_BATCHING_PROTOCOL: Final[str] = (
    "Tool discipline: issue independent reads and searches together in one turn and inspect every "
    "result; keep dependent steps, edits, approvals, waits, and follow-ups that adapt to a result "
    "sequential. Do not decorate shell output with separator commands (echo '====', printf '---'); the "
    "capture is bounded and the noise displaces evidence."
)

# The sidecar file is the primary machine-read return
# (`fanout_dispatch._intake_unit_result`). The block restates the sidecar
# object when a sidecar path was given, so the two returns cannot disagree,
# and when the contracted sidecar file is missing the collector falls back
# to parsing this block out of captured stdout and validating it against the
# same `fanout_unit_result/v1` schema (`fanout_unit_results.
# validate_unit_result`) plus the dispatch identity check; nothing scrapes
# the surrounding prose for results. Executor-neutral: every owner emits the
# same shape.
PARENT_CLARIFICATION_PROTOCOL: Final[str] = '''Never contact the user directly: preserve work and return input_required for a blocking parent decision. An answer changes no scope or approval authority; await explicit parent redispatch.
For that status, add the exact input_required object below. Limits: decision_id=lowercase slug 1..64; question/blocking_reason=1..300 chars; options=1..8 unique strings of 1..80 chars, or answer_shape={kind:text,max_chars:integer 1..300}; affected_unit_ids=[your unit]; redacted_context=0..8 strings of 1..160 chars. Text is printable single-line metadata, secret-free and non-executable, without transcripts. Copy given dispatch identities and use current HEAD:

```json
{"schema_version":"fanout_unit_result/v1","unit_id":"unit-b","run_id":"fanout-0123456789ab-unit-b","fanout_id":"fanout-0123456789ab","base_sha":"aaaaaaa","head_sha":"aaaaaaa","process_status":"input_required","input_required":{"decision_id":"decision-d1","question":"Choose output format","blocking_reason":"Encoding choice blocks work","answer_shape":{"kind":"options","options":["json","text"]},"affected_unit_ids":["unit-b"],"redacted_context":[]},"changed_paths":[],"checks":[],"findings":[]}
```
'''

# One schema example serves both structured return and clarification, avoiding
# duplicate field inventories in the shared head; every byte remains budgeted.
UNIT_RESULT_RETURN_PROTOCOL: Final[str] = (
    "End with one fenced ```json fanout_unit_result/v1 object, matching the sidecar. The collector "
    "parses and validates the sidecar or, if absent, that block; prose is never scraped.\n"
    + PARENT_CLARIFICATION_PROTOCOL
)

PROMPT_CACHE_COMPOSITION_PROTOCOL: Final[str] = (
    "Prompt-cache discipline: every major serving stack caches prompt prefixes by exact bytes "
    "(Anthropic prefix caching, OpenAI automatic prefix caching, Gemini implicit caching, DeepSeek "
    "context caching), and one changed byte invalidates everything after it. Keep the shared preamble "
    "byte-identical across sibling unit prompts — stable ordering, no timestamps, counts, or other "
    "volatile status — append unit-specific content after the shared preamble, and stagger a fan-out "
    "so the first dispatch writes the cache its siblings read."
)

REVIEW_ROLE_PROTOCOL: Final[str] = (
    "Review discipline: a finding blocks only when it violates a stated success criterion of the reviewed "
    "work; list every other finding as non-blocking. Cap re-review at two rounds — after that, report the "
    "remaining criterion-cited blockers rather than starting another round."
)

# Per-family counters to the over-verification inertia of high-effort routes.
# Keyed by `model_family()` output; "generic" is the mandatory fallback so an
# unknown family never gets weaker discipline than a known one.
HIGH_EFFORT_CALIBRATIONS: Final[dict[str, str]] = {
    "gpt": (
        "High-effort calibration: your reasoning depth is for the hard parts of THIS unit, not for "
        "re-deriving settled facts. Once the decisive fact is in view, act on it; once a criterion has "
        "passed, it is settled evidence — reopen it only when new output contradicts it, never to "
        "reassure yourself."
    ),
    "claude": (
        "High-effort calibration: follow the numbered criteria as the complete checklist — do not grow "
        "the checklist mid-run, and once you have enough to act, act instead of gathering more context. "
        "Deliberate deeply only where correctness is genuinely at risk; mechanical steps run directly and "
        "the single verification pass proves them. Edit surgically rather than rewriting a file; fix "
        "only what the criteria name and report adjacent findings instead of changing them; keep scratch "
        "checks out of the repository, and commit tests only where a criterion asks for them or the repo "
        "already keeps tests for this kind of change, sized like their neighbors. Add no helpers, "
        "fallbacks, validation, flags, or shims beyond what the criteria name; when you can just change "
        "the code, change it. Every progress claim points at a tool result from this run — a failed "
        "check is reported with its output, a skipped step as skipped."
    ),
    "gemini": (
        "High-effort calibration: a claim without the tool output that proves it is not evidence — "
        "run the actual check and report from its output, never from memory. Done-sounding language "
        "before the single mandatory verification pass is a failure, not optimism, and creative "
        "expansion outside the declared boundary is a defect here, not an improvement."
    ),
    "grok": (
        "High-effort calibration: speed is your default; the numbered criteria are the brake. A fast "
        "first answer never skips the single mandatory verification pass, and a quicker path never "
        "trades away the declared boundary. When search surfaces many candidates, pick by the stated "
        "criteria once and act instead of re-querying for reassurance."
    ),
    "kimi": (
        "High-effort calibration: reserve the decompose-compare-verify loop for the genuinely hard "
        "parts; mechanical steps are low-entropy — execute them directly without enumerating "
        "alternatives. Decide each approach once and reopen it only when new output contradicts it. "
        "If you catch yourself listing options for a step no criterion distinguishes, stop analyzing "
        "and act."
    ),
    "glm": (
        "High-effort calibration: use interleaved reasoning only where it improves a tool decision: "
        "interpret each result, choose the next bounded action, and preserve prior reasoning context "
        "when the runtime exposes it — returned complete and unmodified, in its original order, as "
        "this family's preserved-thinking contract expects. The 5.3 generation cannot disable "
        "thinking, so reasoning depth is the routed effort level, never a request for no thinking. "
        "Mechanical steps need no extended plan. Keep the change goal-shaped, and let the single "
        "verification pass prove it."
    ),
    "qwen": (
        "High-effort calibration: current Qwen3-Coder is a non-thinking coding-agent model, so do not "
        "ask it to emit reasoning or thinking tags. Give the exact goal, repository state, allowed "
        "boundaries, tool schemas, and completion criteria, then follow one explicit plan. Recover "
        "from failures using observed tool output and stop after one passing verification run."
    ),
    "deepseek": (
        "High-effort calibration: treat the model version and declared thinking mode as contract "
        "fields; never apply legacy R1 prompting to every DeepSeek model. Preserve runtime-provided "
        "reasoning context across tool results only on a reasoning-capable route; otherwise use the "
        "same explicit goal, boundaries, and completion criteria without thinking tags. Edit by exact "
        "literal strings — a unique match with exact whitespace — as this family's edit training "
        "expects. Make the smallest correct change, verify once, and stop."
    ),
    "mistral": (
        "High-effort calibration: instructions are followed literally here, so the stated criteria "
        "are the whole contract — check every one even when the change looks obviously right. "
        "Concision is for the output, never for the evidence: the single mandatory verification "
        "pass runs regardless of how small the diff is."
    ),
    "llama": (
        "High-effort calibration: the serving deployment is part of the contract — tool-calling "
        "support, context limits, and output limits come from the host, not the model name. Prove a "
        "capability with a real call before depending on it, fall back to explicit step-by-step "
        "tool use when structured calling is unreliable, and stop after one passing verification run."
    ),
    "codestral": (
        "High-effort calibration: this is a code-completion specialist — work in file-scoped, "
        "concrete edits rather than open-ended investigation, keep each step's expected output "
        "small and explicit, and prove the change with the repository's own check commands instead "
        "of prose explanation."
    ),
    "solar": (
        "High-effort calibration: an efficient instruction-follower, not a long-horizon reasoner — "
        "follow the one explicit plan you were given in bounded steps instead of deriving a new "
        "one, report a missing constraint rather than inferring it, and verify once against the "
        "stated criteria before stopping."
    ),
    "generic": (
        "High-effort calibration: reserve extended reasoning for genuine ambiguity with materially "
        "different outcomes. Decide once, act, verify once against the criteria, and stop — speed is "
        "never a reason to skip the verification pass, and thoroughness is never a reason to repeat it."
    ),
}


# Calibration for the MAIN agent — the one COMPOSING the split, the unit
# prompts, and the briefings — keyed by ITS OWN model family. The user picks
# what Hermes runs on (a claude-family fable/opus, a gpt-family sol/terra, a
# gemini, a kimi, a qwen, ...), and each family fails composition differently: the
# guidance counters the composer's own defaults, never the subagents'.
# Same key set as HIGH_EFFORT_CALIBRATIONS (parity-tested) so no family gets
# subagent discipline without composer discipline, and "generic" stays the
# mandatory fallback for families the table has not met.
MAIN_AGENT_COMPOSITION_CALIBRATIONS: Final[dict[str, str]] = {
    "gpt": (
        "Composition calibration: compose outcome-first, but never compress the contract away — "
        "every unit prompt keeps its declared boundary, dependencies, numbered criteria, and the "
        "one-pass verification floor spelled out. A tighter prompt that drops a stated invariant is "
        "a worse prompt."
    ),
    "claude": (
        "Composition calibration: split only what the goal requires — no speculative units, and no unit "
        "whose only job is re-checking the split itself; a fresh-context review of a unit's deliverable "
        "against its criteria is a legitimate unit. Delegate a unit when it is independent of the work "
        "you keep and its completion can be judged from the evidence it returns; keep in line anything "
        "that finishes in a handful of tool calls. The "
        "criteria you write are a closed checklist: state them once, completely, and freeze. Your closing report is "
        "the reader's first look at the run — lead with the outcome in plain sentences, drop the "
        "working shorthand, and give the one or two things you need from them."
    ),
    "gemini": (
        "Composition calibration: compose from tool-verified facts, not recall — run the inventory "
        "and readiness commands before naming owners or models, and never describe a unit as "
        "prepared until the actual prepare command produced its artifact. A split narrated without "
        "the commands behind it is not a split."
    ),
    "kimi": (
        "Composition calibration: partitioning work is mostly low-entropy — decide the split once, "
        "freeze it, and reserve deep reasoning for boundary overlaps and dependency cycles. Do not "
        "enumerate alternative splits nobody asked for; if two partitions both satisfy the "
        "boundaries, take the first and move."
    ),
    "glm": (
        "Composition calibration: use interleaved reasoning only to interpret evidence between "
        "contract-building tools; mechanical field assembly needs no extra planning. This family "
        "rewards lean, mechanically explicit unit prompts — exact schemas and invocation rules over "
        "narrative instruction — and its tool-call formatting decays in very long contexts, so keep "
        "each unit's scope bounded rather than letting one unit sprawl. Every unit carries its owner, "
        "boundary, and known route fields. Once boundaries are clean and dependencies acyclic, freeze "
        "the smallest split that covers the goal."
    ),
    "grok": (
        "Composition calibration: pick the partition once by the stated boundaries and dispatch; "
        "re-querying for a better split is re-verifying a settled decision."
    ),
    "qwen": (
        "Composition calibration: current Qwen3-Coder is non-thinking; freeze one ordered split with "
        "exact owners, boundaries, tool contracts, dependencies, roles, and verification commands "
        "instead of requesting reasoning tags. Validate once and move to dispatch."
    ),
    "deepseek": (
        "Composition calibration: keep the DeepSeek model version and thinking mode explicit in the "
        "prepared route. Preserve runtime reasoning context only when the selected model and executor "
        "support it; otherwise compose exact owners, scopes, dependencies, and verification commands "
        "without synthetic thinking instructions. Validate once and stop."
    ),
    "mistral": (
        "Composition calibration: write unit prompts literally and completely — a Mistral-family "
        "executor follows what is written, not what was implied, so every boundary, dependency, "
        "criterion, and verification command must be stated; never rely on the unit inferring an "
        "unstated invariant."
    ),
    "llama": (
        "Composition calibration: compose for the deployment, not the brand — confirm the served "
        "variant's tool contract and context budget before assigning units, and keep each unit "
        "prompt self-contained so a host with a smaller context window still receives the full "
        "contract."
    ),
    "codestral": (
        "Composition calibration: route codestral units as narrow, file-scoped implementation "
        "slices with exact verification commands; investigation, review, and synthesis belong on a "
        "generalist lane, and a unit that mixes them belongs split."
    ),
    "solar": (
        "Composition calibration: put the depth in the composition, not the unit — give each solar "
        "unit one explicit plan with short bounded steps, exact criteria, and its verification "
        "command, because the unit will execute the plan it is given rather than derive a better one."
    ),
    "generic": (
        "Composition calibration: compose the contract fields exactly, validate the split once with "
        "the validation command, and stop — composing is preparing evidence, and a prepared "
        "contract is the only proof a split exists."
    ),
}


# Exact-model overrides, resolved BEFORE the family tables. A generation whose
# documented traits differ from its family's (GPT-6 Astra against the GPT-5.6
# guidance) gets its own counter here without touching the family block, so
# the older generation's prompts stay byte-stable. Keyed by the exact model
# id after the provider prefix is stripped (`contract_model_id`); the two
# tables share one key set (parity-tested) for the same reason the family
# tables do. Resolution order everywhere: exact model -> family -> generic.
MODEL_HIGH_EFFORT_CALIBRATIONS: Final[dict[str, str]] = {
    # GPT-6 Astra, per OpenAI's latest-model guide (2026-09): asks more
    # readily, follows instructions more strictly and pauses on conflicting
    # skill text, delegates less than a harness expects, and tests more
    # broadly than a change needs. Each sentence counters one of those; the
    # universal echo-back, criteria, one-pass verification, and repair caps
    # are not restated. No monitoring language, no chain-of-thought requests.
    "gpt-6-astra": (
        "High-effort calibration: the user's instructions outrank any skill or guideline text, and "
        "the numbered criteria are the complete task — nothing outside them is owed. Ask one focused "
        "question only when a missing input would materially change the result; otherwise state the "
        "assumption and proceed. Size tests to the change: a reversible, low-impact edit that mirrors "
        "its implementation needs no new test, and a green check is re-run only when its inputs changed."
    ),
    # GPT-6.1 Sol: Astra's wording, byte for byte. The vendor's Codex client
    # gives 6.1 Sol GPT-6 Astra's base prompt (GPT-6 Sol keeps an older one),
    # and the latest-model guide states every countered trait for the GPT-6
    # family from behaviour observed with Astra. Measured on Astra
    # (2026-09-05), editorial and unmeasured for 6.1 Sol until a `family` vs
    # `optimized` pair runs. The guide's keep-working prompts are not
    # imported. GPT-6 Sol keeps the family block.
    "gpt-6.1-sol": (
        "High-effort calibration: the user's instructions outrank any skill or guideline text, and "
        "the numbered criteria are the complete task — nothing outside them is owed. Ask one focused "
        "question only when a missing input would materially change the result; otherwise state the "
        "assumption and proceed. Size tests to the change: a reversible, low-impact edit that mirrors "
        "its implementation needs no new test, and a green check is re-run only when its inputs changed."
    ),
    # DeepSeek V4.1 Flash, per the vendor's API guides and model card
    # (2026-09-10): thinking on by default, reasoning_content returned on
    # every tool-calling turn, post-trained on synthesized long-horizon agent
    # tasks, and the family-wide exact-string edit training; the vendor's
    # own harness adapter notes that the live API rejects an assistant turn
    # whose answer sits only in the reasoning channel. The family block's
    # conditional clauses (reasoning-capable or not, R1 or not) resolve
    # here, so this block states the resolved contract. Measured 2026-09-13
    # (MODEL_OPTI.md): the first wording asked the visible reply to carry
    # "the verification output" and the blocker report to carry "the
    # observed output", and cost 25% more tokens than the family block for
    # the same pass set, most of it re-running checks on tasks the model was
    # not going to pass. Both phrases are gone; the family block's closing
    # rule ("make the smallest correct change, verify once, and stop") is
    # kept verbatim, because that is the sentence the override had dropped.
    "deepseek-v4.1-flash": (
        "High-effort calibration: this is DeepSeek V4.1 Flash with thinking on by default, and the "
        "runtime returns your earlier reasoning on every tool turn, so the visible reply carries the "
        "change and the stop — not a restatement of reasoning the context already holds — and a turn "
        "that ends without a tool call carries its answer in the visible text, never only in "
        "reasoning. Edit by exact literal strings — a unique match with exact whitespace — as this "
        "family's edit training expects. When the evidence already in hand cannot satisfy a "
        "criterion, report the blocker from that evidence rather than widening the search, and leave "
        "a passed criterion closed. Make the smallest correct change, verify once, and stop."
    ),
}
MODEL_COMPOSITION_CALIBRATIONS: Final[dict[str, str]] = {
    "gpt-6-astra": (
        "Composition calibration: write the user's intent into each unit prompt above any skill "
        "text, so a delegate that meets conflicting guidance follows the unit contract rather than "
        "pausing. Delegate every unit that is independent of the work you keep — this model "
        "delegates less than a fanout expects, and an undelegated independent unit is latency you "
        "chose. Set each unit's effort from its task state: the documented floor for routine "
        "follow-ups, deeper only while a criterion holds unresolved hard reasoning or contradictory "
        "evidence, and a change of effort lands on the next prepared unit rather than on a claimed "
        "mid-conversation switch."
    ),
    # GPT-6.1 Sol: Astra's composer wording, byte for byte, on the evidence
    # above; its documented floor is `low`, as Astra's is.
    "gpt-6.1-sol": (
        "Composition calibration: write the user's intent into each unit prompt above any skill "
        "text, so a delegate that meets conflicting guidance follows the unit contract rather than "
        "pausing. Delegate every unit that is independent of the work you keep — this model "
        "delegates less than a fanout expects, and an undelegated independent unit is latency you "
        "chose. Set each unit's effort from its task state: the documented floor for routine "
        "follow-ups, deeper only while a criterion holds unresolved hard reasoning or contradictory "
        "evidence, and a change of effort lands on the next prepared unit rather than on a claimed "
        "mid-conversation switch."
    ),
    "deepseek-v4.1-flash": (
        "Composition calibration: the composer runs on DeepSeek V4.1 Flash with thinking on by "
        "default and its reasoning returned on every tool turn, so the visible composition is the "
        "ordered split — exact owners, scopes, dependencies, and verification commands — not a replay "
        "of planning already in context, and it carries no synthetic thinking instructions. A unit "
        "routed to this model takes low, high, or max — its documented ladder; the vendor's own table "
        "turns medium and xhigh into high, so an undocumented rung is a rung you did not choose. "
        "Validate once and stop."
    ),
}


def composition_calibration_for_model(model_id: str) -> str:
    """Return the main-agent composition calibration for the composer's own model.

    Exact-model overrides win, then family from `model_family()`
    (provider-prefixed ids welcome); unknown or blank families get the
    generic block — a composer never goes without discipline just because
    the table has not met its model.
    """
    from .model_routing import model_family

    override = MODEL_COMPOSITION_CALIBRATIONS.get(contract_model_id(str(model_id or "")))
    if override:
        return override
    family = model_family(str(model_id or ""))
    return MAIN_AGENT_COMPOSITION_CALIBRATIONS.get(
        family, MAIN_AGENT_COMPOSITION_CALIBRATIONS["generic"]
    )


# Work-domain skill bundles: when a unit DECLARES a work domain, the
# delegate prompt carries the matching OMH skill's distilled discipline and
# a pointer to the full generated guidance. Deterministic data — the domain
# is explicit unit data, never inferred from text — and executor-neutral:
# the delegate follows the discipline inline; it does not need omh
# installed.
DOMAIN_SKILL_GUIDANCE: Final[dict[str, tuple[str, str]]] = {
    "devops": (
        "omh-build-failure-triage",
        "Classify the failure (build/typecheck/lint/test/CI) before fixing; ship the minimal safe fix "
        "and re-run exactly the failed gate as proof.",
    ),
    "app_development": (
        "omh-frontend",
        "Ship user-visible increments with evidence: after each feature slice, run the app-level check "
        "that proves the screen/flow works, not just unit tests.",
    ),
    "research": (
        "omh-research-brief",
        "Every claim carries its source; mark anything not actually fetched as not observed instead of "
        "guessing, and separate evidence from inference in the summary.",
    ),
    "x_platform_data": (
        "omh-live-info-operator",
        "Treat platform data as time-stamped observations: record when and where each datum was read, "
        "and never extrapolate silently past the observation window.",
    ),
}


def domain_skill_guidance_line(unit: Mapping[str, Any]) -> str:
    """Return the OMH skill-bundle line for a unit's declared work domain, or ''."""
    domain = str(unit.get("domain", "") or "").strip().casefold().replace("-", "_")
    if not domain:
        handoff = unit.get("handoff", {}) if isinstance(unit.get("handoff"), Mapping) else {}
        route = handoff.get("model_route") if isinstance(handoff.get("model_route"), Mapping) else None
        domain = str(route.get("domain", "") or "") if route else ""
    if not domain:
        return ""
    entry = DOMAIN_SKILL_GUIDANCE.get(domain)
    if entry is None:
        return ""
    label, discipline = entry
    return (
        f"OMH skill bundle ({domain}): follow the `{label}` discipline — {discipline} "
        f"(full guidance ships as skills/{label}/SKILL.md in the oh-my-hermes install)."
    )


# The last criterion of every unit: fanout transport (the dispatcher collects
# and merges the unit branch), named so the assembled prompt can carry it as
# its own block and a lane without a collector can omit it by name.
UNIT_BRANCH_COMMIT_CRITERION: Final[str] = (
    "The work is committed on the unit branch; nothing else is merged or pushed."
)


def completion_criteria_for_unit(unit: Mapping[str, Any]) -> list[str]:
    """Return the pre-declared, numbered 'done means' criteria for one unit.

    Derived deterministically from the frozen unit contract: boundary
    confinement and committed work are always criteria; the contract's
    integration checks become the unit-specific ones.
    """
    boundary = unit.get("boundary", {}) if isinstance(unit.get("boundary"), Mapping) else {}
    file_scope = ", ".join(str(path) for path in boundary.get("file_scope", []))
    criteria = [f"Every edit stays inside: {file_scope}." if file_scope else "Every edit stays inside the declared file scope."]
    for check in unit.get("integration_checks", []) or []:
        text = str(check).strip()
        if text:
            criteria.append(text[0].upper() + text[1:] if text[0].islower() else text)
    criteria.append(UNIT_BRANCH_COMMIT_CRITERION)
    return criteria


def calibration_for_route(model_route: Mapping[str, Any] | None, *, family_only: bool = False) -> str:
    """Return the high-effort calibration block for a routed unit, or ''.

    Selected only when the route's effective reasoning effort is in the high
    tier; an exact-model override on the recorded `selected_model` wins, then
    family comes from the already-recorded `model_family` (falling back to
    generic for unknown/blank families).

    `family_only=True` skips the exact-model override and returns the block
    the model would inherit from its family. That is the measurement arm
    `docs/MODEL-ONBOARDING.md` §8 asks for when an override ships: the
    override is kept only if it measures at least as well as the inherited
    block on the same corpus. Production callers never pass it.
    """
    entry = calibration_entry_for_route(model_route, family_only=family_only)
    return entry[1] if entry is not None else ""


def calibration_entry_for_route(
    model_route: Mapping[str, Any] | None, *, family_only: bool = False
) -> tuple[str, str] | None:
    """Return `(key, block)` for `calibration_for_route`, or None when none fires.

    The key names the table entry that answered: the contract model id for an
    exact-model override, otherwise the family (`generic` for an unknown one).
    Two keys can carry byte-equal text, so the key is what identifies which
    calibration a unit received.
    """
    if not isinstance(model_route, Mapping):
        return None
    effort = str(model_route.get("selected_reasoning_effort", "") or "").casefold()
    if effort not in HIGH_EFFORT_TIER:
        return None
    if not family_only:
        model_id = contract_model_id(str(model_route.get("selected_model", "") or ""))
        override = MODEL_HIGH_EFFORT_CALIBRATIONS.get(model_id)
        if override:
            return model_id, override
    family = str(model_route.get("model_family", "") or "").casefold()
    if family not in HIGH_EFFORT_CALIBRATIONS:
        family = "generic"
    return family, HIGH_EFFORT_CALIBRATIONS[family]


def shared_unit_preamble_lines(goal_text: str) -> list[str]:
    """Byte-identical head shared by every sibling unit prompt of one fanout.

    Cached prompt prefixes are exact byte matches on every provider, so the
    lines here depend only on the goal text — never on the unit — and callers
    must place them before any unit-specific byte.
    """
    return [
        f"Overall goal: {goal_text.strip()}",
        GOAL_ECHO_PROTOCOL,
        VERIFICATION_STOP_PROTOCOL,
        FAILURE_KIND_PROTOCOL,
        UNIT_RESULT_RETURN_PROTOCOL,
        STRUCTURAL_SEARCH_DISCIPLINE_GUIDANCE,
    ]

