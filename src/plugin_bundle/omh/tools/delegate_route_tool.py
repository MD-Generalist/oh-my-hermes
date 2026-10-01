from __future__ import annotations

from .. import runtime_paths

import json
import time
from typing import Any

from ..delegation_route_restore import (
    newest_written_provenance,
    omh_wrote_current_keys,
    provenance_route_keys,
    restore_delegation_baseline,
    write_route_with_baseline,
)
# Every write of the three keys now goes through `delegation_route_restore`,
# which pairs it with the baseline bookkeeping under one lock. Reaching for
# the raw writer here again would reopen the window this PR closed.
from ..delegation_routing import read_delegation_route, read_session_provider
from ..hermes_delegation import (
    HERMES_MIXTURE_CATEGORY_CHAINS,
    append_delegation_route_provenance,
    chain_alias_for,
    load_mixture_chain_overrides,
    load_model_provider_routes,
    effective_provider_entitlements,
    provider_serves_alias,
    providers_serving_alias,
    mixture_chain_overrides_path,
    model_provider_routes_path,
    resolve_provider_model,
    effective_mixture_category_chains,
)
from ..host_observation import OBSERVATION_SCHEMA, attach_public_observation, host_session_id, observe_plugin_tool_call
from ..orchestration_say import route_say, with_say

_EVIDENCE_BOUNDARY = (
    "Prepared route only: the delegation.* keys apply to the NEXT delegate_task "
    "dispatch (Hermes re-reads config.yaml per dispatch). Writing a route is not "
    "execution, dispatch, or completion evidence."
)


# Anthropic models that cannot disable thinking: the Fable-tier generation
# (Fable 5.x and its Mythos sibling), Opus 5.5, and Sonnet 5.5. A
# thinking-off request is a 400 on Mythos 5.1, on Opus 5.5 (Anthropic's
# whats-new-opus-5-5, read 2026-09-23), and on Sonnet 5.5 (its migration
# guide, read 2026-10-01: the API's one thinking-off mode, `between_tools`,
# is a different request no Hermes build sends), and is dropped silently on
# Fable 5.1, so an effort that means
# "no thinking" never reaches delegation.reasoning_effort for these models.
# Opus 5 and Sonnet 5 still accept it, so the Opus and Sonnet entries name
# the 5.5 generation only; the dotted spellings are a gateway's (OpenRouter's
# `anthropic/claude-opus-5.5`, `anthropic/claude-sonnet-5.5`). The bare
# `sonnet` word is deliberately absent: Claude Code resolves it to Sonnet 4.5
# on Bedrock, Google Cloud, and Foundry, where thinking-off is accepted.
# Matched on the alias and on the wire id's model segment, after a Bedrock
# `anthropic.` vendor prefix and any regional prefix before it
# (`us.anthropic.claude-opus-5-5`). This guard refuses on the model name
# alone, so it also covers regional ids the core contract table does not
# declare; core routing raises the no-thinking rung on the spellings it does.
_NO_THINKING_EFFORTS = frozenset({"none", "off", "false", "disabled"})
_ALWAYS_THINKING_CLAUDE_PREFIXES = (
    "claude-fable-", "claude-mythos-", "claude-opus-5-5", "claude-opus-5.5", "claude-sonnet-5-5",
    "claude-sonnet-5.5",
)


def _always_thinking_claude(model: str) -> bool:
    normalized = str(model or "").strip().casefold()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[1]
    if "anthropic." in normalized:
        normalized = normalized.rsplit("anthropic.", 1)[1]
    return normalized in {"fable", "mythos"} or normalized.startswith(_ALWAYS_THINKING_CLAUDE_PREFIXES)


def _chain_entry(
    alias: str,
    effort: str,
    routes: dict[str, tuple[str, str]],
) -> dict[str, str]:
    """Render one chain position.

    The public shape always carries alias, provider, executable model, and
    effort; unresolved aliases report an empty provider explicitly.
    """
    entry = {
        "alias": alias,
        "provider": "",
        "model": alias,
        "reasoning_effort": effort,
    }
    wire_model, provider = resolve_provider_model(alias, routes=routes)
    if provider:
        entry["provider"] = provider
        entry["model"] = wire_model
    return entry


def _clear_to_baseline(
    hermes_home: Any,
    omh_home: Any,
    *,
    trigger: str,
    unrecorded_clear: str,
    expected_previous: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Put the recorded baseline back, or remove the keys when none was recorded.

    Both halves run inside the restore's own lock. Doing the unrecorded
    removal out here instead left a window: a route landing in it recorded a
    person's pinned model as a baseline that the next restore then discarded
    as `foreign_edit`, losing the pinned model with nothing reported.

    The two callers ask for different strengths of that removal, which is
    why `unrecorded_clear` is passed rather than assumed. `clear` is a
    person asking, so it removes. Chain exhaustion is automatic, so it may
    only remove a value OMH can prove it wrote -- otherwise a single failed
    lane deleted the model the person had pinned.

    Deliberately NOT scoped to the calling session or task, unlike the two
    hooks. Both callers here are asking for the route to come off now, and
    the recorded writer exists to stop an AUTOMATIC restore acting on
    someone else's route, not to refuse an explicit instruction. The value
    check still holds, so a key a person set by hand is safe either way.
    """
    return restore_delegation_baseline(
        hermes_home,
        omh_home=omh_home,
        trigger=trigger,
        unrecorded_clear=unrecorded_clear,
        expected_previous=expected_previous,
    )


def _fallback_position(
    route: dict[str, str], omh_home: Any
) -> tuple[dict[str, str], str]:
    """The route a fallback is advancing FROM, and where that came from.

    The live keys are used ONLY when OMH can prove it wrote them. They are
    not simply "empty once the route has been taken back": a turn-end
    restore puts the PERSON's pinned model back, and reading that as the
    chain position is how one failed lane came to report a whole exhausted
    chain, never try the next candidate, and then delete the pin. A pinned
    model that is in no chain produced a hard error, and one in several
    chains an `ambiguous origins` refusal -- all on the recovery path for a
    child that has just died on HTTP 400, the worst moment to fail.

    Provenance records every route OMH wrote, so the position survives the
    restore even though the value does not.
    """
    if route.get("model") and omh_wrote_current_keys(route, omh_home):
        return dict(route), "live_route"
    recovered = provenance_route_keys(newest_written_provenance(omh_home))
    return (recovered, "provenance") if recovered.get("model") else ({}, "none")


OMH_DELEGATE_ROUTE_SCHEMA = {
    "name": "omh_delegate_route",
    "description": (
        "Route the NEXT Hermes-native delegate_task dispatch onto a mixture model "
        "category (ultrabrain, deep, deep-work, architect, capable, unspecified-high, "
        "unspecified-low, quick, simple-work, writing, visual-engineering, artistry) by "
        "writing the delegation.model / "
        "delegation.reasoning_effort keys Hermes reads per dispatch. Sequence per lane: "
        "set the route, call delegate_task for that lane, then set the next lane's route. "
        "SET THE ROUTE IN THE SAME TURN THAT DISPATCHES. How long a route lives depends on "
        "the surface: in the Hermes TUI and CLI it is put back when the turn ends, so a "
        "route set in one turn and dispatched in the next runs on the previous model rather "
        "than yours; on a chat platform it lasts the session. Routing in the dispatching "
        "turn is correct on both, so do not rely on a route you set earlier. You do NOT "
        "have to clear at the end; call clear only to put the previous values back sooner. "
        "A restore is skipped, and says so, when the keys no longer hold what OMH wrote -- "
        "a value someone else set is never overwritten. Children already running keep their model. "
        "Hermes has NO provider-side fallback: a child whose model the billing account "
        "cannot serve dies on an HTTP 400 yet its delegation still reports completed with "
        "the error text as the result — a completed child with no recorded model usage "
        "means exactly this. When that happens call action=fallback with the category "
        "returned by set to advance the route and re-dispatch; shared routes fail "
        "closed without that origin. An exhausted chain restores what the delegation keys "
        "held before OMH first wrote them, which is the user's own pinned model when they "
        "had one and parent inheritance when they did not; if it cannot prove OMH wrote "
        "the current value it changes nothing and reports unrecorded_value_not_ours. "
        "Relay any `say` field to the user once, in their language and your own words."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "clear", "status", "fallback"],
                "description": (
                    "set writes a route for the current turn; clear restores the keys to "
                    "what they held before OMH first wrote them, which is parent inheritance "
                    "unless the user had pinned their own; status reads the current route; fallback "
                    "advances the current route to the next candidate in its category "
                    "chain (restoring that baseline once the chain is exhausted). "
                    "Reuse the category returned by set; ambiguous origins fail closed."
                ),
            },
            "category": {
                "type": "string",
                "enum": sorted(HERMES_MIXTURE_CATEGORY_CHAINS),
                "description": (
                    "Mixture category to route to; resolves to the chain head "
                    "(e.g. ultrabrain -> gpt-6-astra xhigh). Required for set unless "
                    "an explicit model is given."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Explicit model id override; wins over the category's chain head. "
                    "Use for a fallback candidate when the head is unavailable, or to pin "
                    "a model the user named for the run (e.g. 'use fable'): keep the "
                    "fitting category for the lane label and pass the user's model (plus "
                    "reasoning_effort) here on every lane of that run."
                ),
            },
            "reasoning_effort": {
                "type": "string",
                "description": (
                    "Explicit reasoning effort override (e.g. low, medium, high, xhigh). "
                    "Defaults to the chain entry's declared effort; omitted keys inherit "
                    "the parent session's level."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Hermes provider override. Supply it together with model so the "
                    "provider/wire identity remains atomic."
                ),
            },
            "hermes_home": {
                "type": "string",
                "description": "Standalone operator override only. Native Hermes calls reject this field; omit it to use the active profile.",
            },
            "omh_home": {
                "type": "string",
                "description": "Standalone operator override only. Native Hermes calls reject this field; omit it to use the active profile.",
            },
            "observation": OBSERVATION_SCHEMA,
        },
    },
}


def omh_delegate_route_handler(args: dict[str, Any], **kwargs) -> str:
    if error := runtime_paths.tool_home_error(args):
        return json.dumps(error, sort_keys=True)
    observation = observe_plugin_tool_call("omh_delegate_route", args, kwargs)
    # Only the host keyword names the session; tool args are model-supplied and
    # must not be able to claim another session's route record. Two stores
    # read it: the restore baseline below, and the provenance record, whose
    # owner decides which conversation's HUD may label a child with this
    # route.
    session_id = host_session_id(kwargs)
    # The host gives a tool handler `task_id` but never `turn_id`
    # (`model_tools._execute_tool`), so the task is the finest scope a route
    # can record. What that means differs by flow and both are ordinary: in
    # TUI and CLI the host mints a fresh task per turn, so the scope is the
    # turn; on every gateway platform `task_id` IS the session id
    # (`gateway/run_turn_runner.py`), so the scope is the session and a
    # route survives later turns of it.
    task_id = str(kwargs.get("task_id", "") or "").strip()
    action = str(args.get("action", "") or "set").strip().lower()
    hermes_home = runtime_paths.plugin_home(args.get("hermes_home"), hermes=True)
    omh_home = runtime_paths.plugin_home(args.get("omh_home"))
    # Every chain read below honors the user's routing/model-chains.json
    # overrides and the provider reorder (routing/providers.json plus the
    # providers Hermes is linked to); the category vocabulary itself stays
    # the shipped closed set. One function owns that composition so
    # `omh model-chains show`, the HUD label projection, and this dispatch
    # path never disagree on a head.
    _overrides, override_status = load_mixture_chain_overrides(omh_home)
    chains = effective_mixture_category_chains(omh_home, hermes_home)
    # Chains name models the way a person says them. A host that reaches
    # models through a provider needs a provider id and that provider's own
    # (often namespaced) model string; routing/model-providers.json supplies
    # that mapping. With no document every alias dispatches unchanged.
    provider_routes, provider_route_status = load_model_provider_routes(omh_home)

    if action == "status":
        route = read_delegation_route(hermes_home)
        if route.get("model"):
            route.setdefault("provider", "")
            route["alias"] = chain_alias_for(
                str(route["model"]),
                str(route["provider"]),
                provider_routes,
            )
        payload: dict[str, Any] = {
            "status": "status",
            "route": route,
            "categories": {
                category: [
                    _chain_entry(alias, effort, provider_routes)
                    for alias, effort in chain
                ]
                for category, chain in chains.items()
            },
            "chain_overrides": override_status,
            "chain_overrides_path": str(mixture_chain_overrides_path(omh_home)),
            "provider_routes": provider_route_status,
            "provider_routes_path": str(model_provider_routes_path(omh_home)),
            "evidence_boundary": _EVIDENCE_BOUNDARY,
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)

    if action == "clear":
        result = _clear_to_baseline(
            hermes_home, omh_home, trigger="clear", unrecorded_clear="always"
        )
        if result.get("status") in ("cleared", "restored"):
            # A cleared route must supersede the head/fallback record that
            # preceded it, or a later child on a coincidentally matching
            # model would still inherit that record's label.
            result["route_provenance"] = append_delegation_route_provenance(
                {
                    "origin": "cleared",
                    "session_id": session_id,
                    "written_at": time.time(),
                },
                omh_home,
            )
        result["evidence_boundary"] = _EVIDENCE_BOUNDARY
        return json.dumps(attach_public_observation(result, observation), sort_keys=True)

    if action == "fallback":
        if override_status.startswith("invalid:"):
            payload = {"status": "error", "error": override_status}
            return json.dumps(
                attach_public_observation(payload, observation),
                sort_keys=True,
            )
        if provider_route_status.startswith("invalid:"):
            payload = {"status": "error", "error": provider_route_status}
            return json.dumps(
                attach_public_observation(payload, observation),
                sort_keys=True,
            )
        route = read_delegation_route(hermes_home)
        # A fallback normally happens a turn AFTER the route was written --
        # the child dies on HTTP 400 and the model is told next turn -- and
        # by then the turn-end restore has taken the route back out. The
        # chain position therefore comes from provenance when the live keys
        # no longer carry it; `expected_previous` below still guards against
        # what is actually in the file.
        position, position_source = _fallback_position(route, omh_home)
        current_model = str(position.get("model", ""))
        current_provider = str(position.get("provider", ""))
        # The live route holds whatever was dispatched, which is the provider's
        # wire model when a route applied. Chain positions are keyed by alias,
        # so translate back before any chain lookup below -- otherwise a routed
        # model reads as absent from the very chain it came from.
        if not current_provider and any(
            current_model in {alias, wire_model}
            for alias, (_, wire_model) in provider_routes.items()
        ):
            payload = {
                "status": "error",
                "error": (
                    f"current route {current_model!r} requires configured "
                    "provider identity"
                ),
            }
            return json.dumps(
                attach_public_observation(payload, observation),
                sort_keys=True,
            )
        current_alias = chain_alias_for(
            current_model,
            current_provider,
            provider_routes,
        )
        if not current_model:
            payload = {
                "status": "error",
                "error": "no active route to fall back from; use set with a category first",
                # This is the return that fires when the position could not
                # be recovered AT ALL, so it is the one an operator most
                # needs the source on. `_fallback_position` reports "none"
                # here rather than omitting the field.
                "position_source": position_source,
                "from": current_model,
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        category = str(args.get("category", "") or "").strip()
        if category and category not in chains:
            payload = {
                "status": "error",
                "error": (
                    f"unknown category {category!r}; choose one of "
                    + ", ".join(sorted(chains))
                ),
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        if not current_provider and "/" in current_model:
            payload = {
                "status": "error",
                "error": f"current route {current_model!r} requires an explicit provider",
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        matches = [
            (name, index)
            for name, chain in chains.items()
            for index, (alias, _) in enumerate(chain)
            if alias == current_alias and (not category or name == category)
        ]
        if not matches:
            payload = {
                "status": "error",
                "error": (
                    f"current route model {current_model!r} does not match "
                    + (
                        f"category {category!r}"
                        if category
                        else "any mixture chain; pass category explicitly"
                    )
                ),
                # An operator reading this has to be able to see WHICH
                # position the tool thought it had, and whether it came from
                # the live keys or from provenance.
                "position_source": position_source,
                "from": current_model,
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        if len(matches) > 1:
            origins = ", ".join(sorted({name for name, _ in matches}))
            payload = {
                "status": "error",
                "error": (
                    f"current route model {current_model!r} has ambiguous origins "
                    f"across {origins}; pass category explicitly"
                ),
                "position_source": position_source,
                "from": current_model,
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        category, index = matches[0]
        chain = chains[category]
        if index + 1 >= len(chain):
            # Chain exhausted: put the person's own delegation keys back so the
            # next dispatch runs on a model known to work -- their pinned one
            # when they had one, parent inheritance when they did not -- instead
            # of one more rejection.
            result = _clear_to_baseline(
                hermes_home,
                omh_home,
                trigger="chain_exhausted",
                unrecorded_clear="if_omh_wrote",
                expected_previous=route,
            )
            if result.get("status") in ("cleared", "restored"):
                result["status"] = "exhausted_to_inherit"
                result["route_provenance"] = append_delegation_route_provenance(
                    {
                        "origin": "exhausted_to_inherit",
                        "category": category,
                        "from_alias": current_alias,
                        "session_id": session_id,
                        "written_at": time.time(),
                    },
                    omh_home,
                )
            result["category"] = category
            result["from"] = current_model
            result["position_source"] = position_source
            result["evidence_boundary"] = _EVIDENCE_BOUNDARY
            return json.dumps(attach_public_observation(with_say(result, route_say(result)), observation), sort_keys=True)
        next_model, next_effort = chain[index + 1]
        wire_model, next_provider = resolve_provider_model(
            next_model, routes=provider_routes
        )
        if "/" in wire_model and not next_provider:
            payload = {
                "status": "error",
                "error": (
                    f"next route {next_model!r} has no provider-aware wire model; "
                    "fallback refused without mutation"
                ),
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
        result = write_route_with_baseline(
            hermes_home,
            omh_home=omh_home,
            session_id=session_id,
            task_id=task_id,
            model=wire_model,
            reasoning_effort=next_effort,
            provider=next_provider,
            expected_previous=route,
        )
        if result.get("status") == "routed":
            result["status"] = "fell_back"
            result["applied"]["alias"] = next_model
            result["category"] = category
            result["from"] = current_model
            result["position_source"] = position_source
            result["route_provenance"] = append_delegation_route_provenance(
                {
                    "origin": "fallback",
                    "category": category,
                    "alias": next_model,
                    "wire_model": wire_model,
                    "provider": next_provider,
                    "reasoning_effort": next_effort,
                    "from_alias": current_alias,
                    "session_id": session_id,
                    "written_at": time.time(),
                },
                omh_home,
            )
            result["fallback_candidates"] = [
                _chain_entry(alias, chain_effort, provider_routes)
                for alias, chain_effort in chain[index + 2 :]
            ]
        result["evidence_boundary"] = _EVIDENCE_BOUNDARY
        return json.dumps(attach_public_observation(with_say(result, route_say(result)), observation), sort_keys=True)

    if action != "set":
        payload = {
            "status": "error",
            "error": f"unknown action {action!r}; use set, clear, status, or fallback",
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)

    if override_status.startswith("invalid:"):
        payload = {"status": "error", "error": override_status}
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if provider_route_status.startswith("invalid:"):
        payload = {"status": "error", "error": provider_route_status}
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    category = str(args.get("category", "") or "").strip()
    model = str(args.get("model", "") or "").strip()
    effort = str(args.get("reasoning_effort", "") or "").strip()
    provider = str(args.get("provider", "") or "").strip()
    if category and category not in chains:
        payload = {
            "status": "error",
            "error": (
                f"unknown category {category!r}; choose one of "
                + ", ".join(sorted(chains))
            ),
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if provider and not model:
        payload = {
            "status": "error",
            "error": "provider and model overrides must appear together",
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if not model and not category:
        payload = {"status": "error", "error": "set needs a category or an explicit model"}
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    alias = model
    if category and not model:
        alias, head_effort = chains[category][0]
        if not effort:
            effort = head_effort
    always_thinking = _always_thinking_claude(alias)
    if always_thinking and effort.casefold() in _NO_THINKING_EFFORTS:
        payload = {
            "status": "error",
            "error": (
                f"{alias} always thinks; a reasoning_effort of {effort!r} asks the provider to "
                "disable thinking, which this generation rejects. Route `low` instead."
            ),
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if provider:
        wire_model = model
    else:
        wire_model, provider = resolve_provider_model(alias, routes=provider_routes)
    if not provider:
        # With no provider the child inherits the session's own, and nothing
        # checked that it can serve the model being pinned. Observed live: a
        # Fable alias inherited an `openai-codex` session and every dispatch
        # returned HTTP 400, twice, before the operator worked out the model
        # needed a different provider and hunted down its wire id by hand.
        # Refuse only what is known wrong -- an unrecorded provider, an alias
        # the catalog never described, or a multi-vendor account all answer
        # "unknown" and dispatch unchanged -- and refuse it beside the answer.
        entitlements, _status, _providers = effective_provider_entitlements(omh_home, hermes_home)
        session_provider = read_session_provider(hermes_home)
        if provider_serves_alias(alias, session_provider, entitlements) is False:
            candidates = providers_serving_alias(alias, entitlements)
            remedy = (
                f"pin one of your providers that can: {', '.join(candidates)}"
                if candidates
                else "record which provider serves it in routing/model-providers.json"
            )
            payload = {
                "status": "error",
                "error": (
                    f"{alias} would inherit this session's provider {session_provider!r}, "
                    f"which does not serve it. Pass an explicit provider with its wire model, "
                    f"or {remedy}."
                ),
                "alias": alias,
                "inherited_provider": session_provider,
                "providers_serving_alias": list(candidates),
            }
            return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    if "/" in wire_model and not provider:
        payload = {
            "status": "error",
            "error": "a wire-shaped model requires an explicit provider",
        }
        return json.dumps(attach_public_observation(payload, observation), sort_keys=True)
    result = write_route_with_baseline(
        hermes_home,
        omh_home=omh_home,
        session_id=session_id,
        task_id=task_id,
        model=wire_model,
        reasoning_effort=effort,
        provider=provider,
    )
    if result.get("status") == "routed":
        result["applied"]["alias"] = alias
        result["category"] = category
        result["route_provenance"] = append_delegation_route_provenance(
            {
                "origin": "head" if category and not model else "explicit",
                "category": category,
                "alias": alias,
                "wire_model": wire_model,
                "provider": provider,
                "reasoning_effort": effort,
                "session_id": session_id,
                "written_at": time.time(),
            },
            omh_home,
        )
        chain = chains.get(category, ())
        result["fallback_candidates"] = [
            _chain_entry(alias, chain_effort, provider_routes)
            for alias, chain_effort in chain[1:]
        ]
    result["evidence_boundary"] = _EVIDENCE_BOUNDARY
    return json.dumps(attach_public_observation(with_say(result, route_say(result)), observation), sort_keys=True)
