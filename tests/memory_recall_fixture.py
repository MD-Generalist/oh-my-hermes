"""Typed synthetic inputs for canonical recall contract tests."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
from typing import TypeAlias, TypedDict, Unpack

from _local_package import load_local_package

load_local_package()
from omh.plugin_bundle.omh import memory_governance as governance
from omh.plugin_bundle.omh import memory_recall_selector as selector

Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
Payload: TypeAlias = dict[str, Json]
Pair: TypeAlias = tuple[Payload, Payload]
decode: Callable[[str], Json] = json.loads
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
PROJECT = {"kind": "project", "ref": "qa-project"}
GLOBAL = {"kind": "user-global", "ref": "default"}
THREAD = {"kind": "thread", "ref": "session-a"}


class SelectionOptions(TypedDict, total=False):
    allowed_scopes: list[dict[str, str]] | None
    required_scope_kinds: tuple[str, ...]
    now: datetime
    executor_target: str
    session_id: str
    inspection: bool
    include_stale: bool
    include_archived: bool
    observed: str
    observer: str
    pins: set[str]
    operation_states: dict[str, str]
    usage: dict[str, dict[str, int]]
    limit: int
    max_chars: int
    query_intent: str


def mapping(value: Json) -> Payload:
    assert isinstance(value, dict)
    return value


def text(value: Json) -> str:
    assert isinstance(value, str)
    return value


def reviewed(
    record_id: str, *, scope: dict[str, str] | None = None,
    summary: str | None = None, **fields: Json,
) -> Pair:
    lens = PROJECT if scope is None else scope
    # Distinct by default: recall collapses records whose normalized summaries
    # match, so synthetic records meant as separate facts must not share one.
    summary = f"Release checklist requires tests ({record_id})" if summary is None else summary
    record: Payload = {
        "schema_version": "project_memory_record/v2", "record_id": record_id,
        "revision": 1, "record_type": "fact", "summary": summary,
        "scope": {"kind": lens["kind"], "ref": lens["ref"]}, "source": "cli",
        "source_class": "omh_local", "approved_at": "2026-09-01T00:00:00Z",
        "retention": {"class": "standard", "admitted_at": "2026-09-01T00:00:00Z"},
        "revalidation": {"deadline": "2027-01-01T00:00:00Z"},
        **fields,
    }
    digest = governance.canonical_payload_digest({**record})
    review_id = f"review-{record_id}"
    record["admission"] = {"state": "approved_manual", "review_id": review_id, "payload_digest": digest}
    identity = decode(json.dumps(governance.stable_artifact_identity({**record})))
    review: Payload = {"review_id": review_id, "artifact_identity": identity, "payload_digest": digest}
    return record, review


def selection(
    pairs: list[Pair], query: str = "release", **kwargs: Unpack[SelectionOptions],
) -> selector.MemoryRecallSelection:
    options: SelectionOptions = {"allowed_scopes": [PROJECT], "now": NOW, "executor_target": "hermes", **kwargs}
    return selector.select_memory_recall(
        [record for record, _ in pairs], query,
        review_resolver={text(review["review_id"]): {**review} for _, review in pairs},
        **options,
    )


def payload(result: selector.MemoryRecallSelection) -> Payload:
    return mapping(decode(json.dumps(result.pack)))


def included_ids(result: selector.MemoryRecallSelection) -> list[str]:
    items = payload(result)["included_records"]
    assert isinstance(items, list)
    return [text(mapping(item)["record_id"]) for item in items]
