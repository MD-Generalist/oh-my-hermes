"""Reviewed project-memory records as a prefetch section.

The reviewed store (`memory/records/`, `project_memory_record/v2`) and the
provider used to be two products that never met. `omh memory recall` ranked the
records into a pack for a coding handoff, and the Hermes provider served only
memory blocks, so a record the reviewer admitted never reached a Hermes turn --
measured on a machine three weeks into daily use: zero records rendered, ever.

This module is the bridge, and it no longer ranks anything itself. The first
version carried a reduced ranker -- query overlap, then recency -- which meant
a record about another project or another executor reached every Hermes turn,
and pins, usage, attention, lifecycle and character budgets that the handoff
honoured were silently ignored here. Selection now goes through
`memory_recall_selector.select_memory_recall`, the same contract the handoff
uses, fed with the same store snapshots (records, immutable reviews, operation
states, usage counters, pins) read from the provider's homes. This module
reads the snapshot, states the explicit scope allowlist, renders the selected
records into one bounded section, and reports exactly which records that
section carries. No model call, no network, no import of the `omh` control
plane (the Hermes process cannot import it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .hermes_memory import RECORDS_DIRECTORY_LABEL, REVIEWS_DIRECTORY_LABEL, _read_reviews, read_reviewed_records
from .memory_recall_selector import MemoryRecallSelection, select_memory_recall
from .memory_recall_support import PROJECT_MEMORY_RECALL_PACK_SCHEMA_VERSION

DEFAULT_RECORD_RENDER_BUDGET_CHARS = 2400
DEFAULT_RECORD_LIMIT = 6
RECORD_SUMMARY_LIMIT_CHARS = 500
# The live provider serves the Hermes session itself, so the perspective lens
# is Hermes: records observed about another executor stay out of the turn.
PREFETCH_EXECUTOR_TARGET = "hermes"
# Sidecar schemas the control plane writes (`omh.workflows.memory`); the plugin
# reads them with the same identifiers so a foreign or older sidecar is ignored
# rather than misread. `test_memory_prefetch_canonical` pins both to the
# control-plane constants.
MEMORY_RECALL_USAGE_SCHEMA_VERSION = "omh_memory_recall_usage/v1"
MEMORY_PINS_SCHEMA_VERSION = "omh_memory_pins/v1"
# The same reference shape the control plane accepts for operation ids, so an
# unsafe id is classified `invalid` here exactly as the handoff classifies it.
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass(frozen=True)
class RecordStoreSnapshot:
    """What the selector needs from the stores, read once, never written."""

    records: tuple[dict[str, Any], ...]
    reviews: dict[str, dict[str, object]]
    operation_states: dict[str, str]
    usage: dict[str, dict[str, object]]
    pins: frozenset[str]
    home_digests: tuple[str, ...]
    # Basenames of record and review files the reader skipped as unreadable
    # or unparseable, plus a directory label for a directory it could not
    # list. Names only, never content. Empty means every file was read.
    unreadable: tuple[str, ...] = ()
    # True when a records or reviews directory could not be listed at all: the
    # store may be full, and this snapshot cannot say.
    store_read_error: bool = False


@dataclass(frozen=True)
class RenderedRecordSection:
    """The section text and the records it actually carries, in order."""

    text: str
    rendered: tuple[dict[str, str], ...]
    omissions: dict[str, int]
    budget_chars: int = DEFAULT_RECORD_RENDER_BUDGET_CHARS


@dataclass(frozen=True)
class PreparedPrefetch:
    """One selection and its rendering under one clock; input to the receipt."""

    selection: MemoryRecallSelection
    section: RenderedRecordSection
    clock: datetime


def read_project_memory_records(
    homes: list[Path] | tuple[Path, ...],
    *,
    unreadable: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Reviewed v2 records from every home, first home wins on a shared record id.

    No replay prefilter: supersession, expiry and review staleness are the
    selector's verdicts at the caller's clock, so the live receipt can name
    them exactly as the handoff does. Legacy display-only records and records
    without a resolvable review never enter the snapshot. ``unreadable``
    collects what the reader skipped (see ``read_reviewed_records``).
    """
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for home in homes:
        for record in read_reviewed_records(home, unreadable=unreadable):
            record_id = str(record.get("record_id", "") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            records.append(record)
    return records


def read_record_store_snapshot(homes: list[Path] | tuple[Path, ...]) -> RecordStoreSnapshot:
    """Records, reviews, operation states, usage and pins from every home.

    First home wins on a shared record id, review id or usage entry, matching
    `read_project_memory_records`; pins are a union because a pin in either
    store is a delivery-priority hint, never an eligibility input.
    """
    # Reviews are read twice below; only this pass collects, so a corrupt
    # review is named once.
    unreadable: list[str] = []
    records = read_project_memory_records(homes, unreadable=unreadable)
    reviews: dict[str, dict[str, object]] = {}
    usage: dict[str, dict[str, object]] = {}
    pins: set[str] = set()
    for home in homes:
        memory_dir = Path(home).expanduser() / "memory"
        for review_id, review in _read_reviews(memory_dir / "reviews").items():
            reviews.setdefault(review_id, review)
        for record_id, entry in _read_usage(memory_dir / "usage.json").items():
            usage.setdefault(record_id, entry)
        pins.update(_read_pins(memory_dir / "pins.json"))
    return RecordStoreSnapshot(
        records=tuple(records),
        reviews=reviews,
        operation_states=_operation_states(homes, records),
        usage=usage,
        pins=frozenset(pins),
        home_digests=tuple(_sha256(str(Path(home).expanduser().resolve())) for home in homes),
        unreadable=tuple(unreadable),
        store_read_error=any(name in (RECORDS_DIRECTORY_LABEL, REVIEWS_DIRECTORY_LABEL) for name in unreadable),
    )


def prefetch_scope_allowlist(
    *,
    project_identity: str,
    session_id: str = "",
    principal_ref: str = "",
    include_legacy_personal: bool = True,
) -> list[dict[str, str]]:
    """Explicit user-global, current project and current thread labels.

    Nothing is inferred from a stored record. A blank project identity yields
    an empty allowlist, which the selector resolves to `scope_unresolved` and
    an empty pack: fail closed rather than widen to a wildcard.
    """
    identity = str(project_identity or "").strip()
    if not identity:
        return []
    scopes = ([{"kind": "user-global", "ref": "default"}] if include_legacy_personal else [])
    scopes.append({"kind": "project", "ref": identity})
    if principal_ref:
        scopes.append({"kind": "user", "ref": principal_ref})
    thread = str(session_id or "").strip()
    if thread:
        scopes.append({"kind": "thread", "ref": thread})
    return scopes


def select_prefetch_records(
    snapshot: RecordStoreSnapshot,
    query: str = "",
    *,
    allowed_scopes: list[dict[str, str]] | tuple[dict[str, str], ...],
    session_id: str = "",
    executor_target: str = PREFETCH_EXECUTOR_TARGET,
    limit: int = DEFAULT_RECORD_LIMIT,
    max_chars: int | None = None,
    now: datetime | None = None,
    policy: dict[str, object] | None = None,
    query_intent: str | None = None,
    principal_context: dict[str, object] | None = None,
    shared_surface: bool = False,
) -> MemoryRecallSelection:
    """The canonical selection for a live prefetch: delivery mode, never inspection."""
    return select_memory_recall(
        list(snapshot.records),
        str(query or ""),
        allowed_scopes=list(allowed_scopes),
        required_scope_kinds=("project",),
        inspection=False,
        review_resolver=snapshot.reviews,
        operation_states=snapshot.operation_states,
        policy=policy,
        usage=snapshot.usage,
        pins=set(snapshot.pins),
        executor_target=executor_target,
        session_id=session_id,
        limit=limit,
        max_chars=max_chars,
        now=now,
        query_intent=query_intent,
        principal_context=principal_context,
        shared_surface=shared_surface,
    )


def prepare_prefetch_records(
    snapshot: RecordStoreSnapshot,
    query: str = "",
    *,
    allowed_scopes: list[dict[str, str]] | tuple[dict[str, str], ...],
    session_id: str = "",
    executor_target: str = PREFETCH_EXECUTOR_TARGET,
    limit: int = DEFAULT_RECORD_LIMIT,
    max_chars: int | None = None,
    budget_chars: int = DEFAULT_RECORD_RENDER_BUDGET_CHARS,
    now: datetime | None = None,
    policy: dict[str, object] | None = None,
    query_intent: str | None = None,
    principal_context: dict[str, object] | None = None,
    shared_surface: bool = False,
) -> PreparedPrefetch:
    """Select, then render, under one clock. The receipt is built from this."""
    clock = now if now is not None else _utc_now()
    selection = select_prefetch_records(
        snapshot,
        query,
        allowed_scopes=allowed_scopes,
        session_id=session_id,
        executor_target=executor_target,
        limit=limit,
        max_chars=max_chars,
        now=clock,
        policy=policy,
        query_intent=query_intent,
        principal_context=principal_context,
        shared_surface=shared_surface,
    )
    return PreparedPrefetch(selection, render_selected_memory_records(selection, snapshot.records, budget_chars=budget_chars), clock)


def render_selected_memory_records(
    selection: MemoryRecallSelection,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    budget_chars: int = DEFAULT_RECORD_RENDER_BUDGET_CHARS,
) -> RenderedRecordSection:
    """Render the canonical selection in its order, bounded by the section budget.

    Selection already applied the record and character budgets; the only cut
    this renderer makes is the serialized section bound, and it reports that
    cut as `render_budget_exhausted` beside the selector's own `over_budget`
    count so the pack can still say "this is not everything". The rendered
    tuple names exactly the records inside the returned text, each with the
    digest of its stored summary -- the same digest an incident anchors on.
    """
    pack = selection.pack
    if pack.get("schema_version") != PROJECT_MEMORY_RECALL_PACK_SCHEMA_VERSION:
        raise ValueError("incompatible recall pack schema for prefetch rendering")
    included = pack.get("included_records")
    if not isinstance(included, list):
        raise ValueError("incompatible recall pack: included_records is not a list")
    by_id = {str(record.get("record_id", "")): record for record in records}
    items: list[dict[str, Any]] = []
    for item in included:
        if not isinstance(item, dict):
            raise ValueError("incompatible recall pack: included record is not an object")
        record_id = str(item.get("record_id", ""))
        if record_id not in by_id:
            raise ValueError("incompatible recall pack: selected record is absent from the snapshot")
        items.append(item)
    text, rendered_items = _render_bounded(
        items,
        budget_chars=budget_chars,
        limit=None,
        omissions={"render_budget_exhausted": 0, "over_budget": int(selection.exclusion_reason_counts.get("over_budget", 0))},
        preserve_prefix=True,
    )
    rendered = tuple(
        {
            "record_id": str(item.get("record_id", "")),
            "content_digest": _sha256(str(by_id[str(item.get("record_id", ""))].get("summary", "") or "")),
        }
        for item in rendered_items
    )
    omissions = {
        "render_budget_exhausted": len(items) - len(rendered_items),
        "over_budget": int(selection.exclusion_reason_counts.get("over_budget", 0)),
    }
    return RenderedRecordSection(text=text, rendered=rendered, omissions=omissions, budget_chars=max(budget_chars, 0))


def render_memory_records(
    records: list[dict[str, Any]],
    *,
    budget_chars: int = DEFAULT_RECORD_RENDER_BUDGET_CHARS,
    limit: int = DEFAULT_RECORD_LIMIT,
) -> tuple[str, int]:
    """Bound the entire section, including escaped text and aggregate omissions.

    Return no section when even its omission report cannot fit. The count
    includes only complete record elements, never omission metadata. This is
    the bounded renderer on its own, for callers that already hold an ordered
    list; the provider goes through `render_selected_memory_records`.
    """
    text, rendered = _render_bounded(
        records,
        budget_chars=budget_chars,
        limit=limit,
        omissions={"render_budget_exhausted": 0, "record_limit_reached": 0},
    )
    return text, len(rendered)


def _render_bounded(
    records: list[dict[str, Any]],
    *,
    budget_chars: int,
    limit: int | None,
    omissions: dict[str, int],
    preserve_prefix: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    if not records:
        return "", []
    lines, rendered = ["<memory_records>"], []
    omissions = dict(omissions)
    closing = "</memory_records>"
    used = len(lines[0]) + 1 + len(closing)
    # Reserve every possible report once, at the largest count it can carry,
    # not an unbounded line per record.
    reserve = sum(
        len(f'\n  <omitted count="{max(len(records), count)}" reason="{reason}" />')
        for reason, count in omissions.items()
    )
    reserve_needed = len(records) > 1 or any(omissions.values())
    # The one-line unresolved summary has its own reserve, at the largest
    # count it can carry, taken whenever ANY record is open regardless of
    # count. It is separate from the omission reserve on purpose: a lone
    # record with nothing omitted skips that reserve so it fits at its exact
    # serialized boundary, but the summary line IS emitted for a lone open
    # record -- so skipping its reserve admitted the record and then blanked
    # the whole section at the closing tag, while folding it into the
    # omission reserve would cut the lone record short of its boundary.
    unresolved_reserve = len("\n" + _unresolved_line(len(records))) if any(_is_open(record) for record in records) else 0
    for record in records:
        if limit is not None and len(rendered) >= max(limit, 0):
            omissions["record_limit_reached"] += 1
            continue
        element = _render_record(record)
        if (preserve_prefix and omissions["render_budget_exhausted"]) or (
            used + 1 + len(element) + (reserve if reserve_needed else 0) + unresolved_reserve > max(budget_chars, 0)
        ):
            omissions["render_budget_exhausted"] += 1
            continue
        used += 1 + len(element)
        rendered.append(record)
        lines.append(element)
    unresolved = sum(1 for record in rendered if _is_open(record))
    if unresolved:
        lines.append(_unresolved_line(unresolved))
    lines.extend(f'  <omitted count="{count}" reason="{reason}" />' for reason, count in omissions.items() if count)
    lines.append(closing)
    text = "\n".join(lines)
    return (text, rendered) if len(text) <= max(budget_chars, 0) else ("", [])


def _is_open(record: dict[str, Any]) -> bool:
    return str(record.get("resolution", "") or "") == "open"


def _unresolved_line(count: int) -> str:
    """One summary line per section, never a banner per record."""
    return f'  <unresolved count="{count}">{count} unresolved records delivered</unresolved>'


def _render_record(record: dict[str, Any]) -> str:
    summary = str(record.get("summary", "") or "")[:RECORD_SUMMARY_LIMIT_CHARS]
    approved = str(record.get("approved_at", "") or "")[:10]
    # An open record carries its delivery marker on the element itself, so a
    # model reading the section sees `open · N days unresolved` beside the
    # summary and cannot read the record as decided. Settled records render
    # exactly as before.
    marker = str(record.get("resolution_marker", "") or "") if _is_open(record) else ""
    resolution = f' resolution="{_attribute(marker)}"' if marker else ""
    return (
        f'  <record id="{_attribute(record.get("record_id", ""))}" type="{_attribute(record.get("record_type", ""))}"'
        f' approved="{_attribute(approved)}"{resolution}>{_text(summary)}</record>'
    )


def _read_usage(path: Path) -> dict[str, dict[str, object]]:
    data = _read_json_object(path)
    if data is None or data.get("schema_version") != MEMORY_RECALL_USAGE_SCHEMA_VERSION:
        return {}
    entries = data.get("records")
    if not isinstance(entries, dict):
        return {}
    usage: dict[str, dict[str, object]] = {}
    for record_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        times = entry.get("times_recalled")
        usage[str(record_id)] = {
            "times_recalled": times if isinstance(times, int) and not isinstance(times, bool) and times > 0 else 0,
            "last_recalled_at": str(entry.get("last_recalled_at", "")),
        }
    return usage


def _read_pins(path: Path) -> set[str]:
    data = _read_json_object(path)
    if data is None or data.get("schema_version") != MEMORY_PINS_SCHEMA_VERSION:
        return set()
    record_ids = data.get("record_ids")
    if not isinstance(record_ids, list):
        return set()
    return {str(record_id) for record_id in record_ids if str(record_id)}


def _operation_states(homes: list[Path] | tuple[Path, ...], records: list[dict[str, Any]]) -> dict[str, str]:
    """State per cited operation, first home that holds the file wins."""
    states: dict[str, str] = {}
    for record in records:
        operation_id = record.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id or operation_id in states:
            continue
        if not _SAFE_REF.fullmatch(operation_id):
            states[operation_id] = "invalid"
            continue
        states[operation_id] = "unavailable"
        for home in homes:
            operation = _read_json_object(Path(home).expanduser() / "memory" / "operations" / f"{operation_id}.json")
            if operation is not None:
                states[operation_id] = str(operation.get("state", ""))
                break
    return states


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _attribute(value: object) -> str:
    return _text(str(value or "")).replace('"', "&quot;")


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "DEFAULT_RECORD_LIMIT",
    "DEFAULT_RECORD_RENDER_BUDGET_CHARS",
    "MEMORY_PINS_SCHEMA_VERSION",
    "MEMORY_RECALL_USAGE_SCHEMA_VERSION",
    "PREFETCH_EXECUTOR_TARGET",
    "PreparedPrefetch",
    "RECORD_SUMMARY_LIMIT_CHARS",
    "RecordStoreSnapshot",
    "RenderedRecordSection",
    "prefetch_scope_allowlist",
    "prepare_prefetch_records",
    "read_project_memory_records",
    "read_record_store_snapshot",
    "render_memory_records",
    "render_selected_memory_records",
    "select_prefetch_records",
]
