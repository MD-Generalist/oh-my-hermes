"""An unresolved memory record decays to ``open``, not to a verdict (issue #1528).

A record whose outcome is undecided used to have two places to go: delivered
like a settled fact until its review deadline, then ``stale`` and gone. Neither
half was "still open, still unresolved". The contracts pinned here:

1. A person marks a record ``open``; nothing infers it. Only confirm or correct
   writes ``resolved``, retire ends it, and no timeout, reminder, or dreaming
   pass ever changes that marker.
2. An open record past its review deadline is delivered as ``open`` with its
   age, never held back as ``stale`` and never read as decided. A record that
   is NOT open gets today's verdict, byte for byte.
3. Expiry, a changed source, and an unreadable source still outrank ``open``,
   and ``open_max_days`` bounds every non-durable open record as
   ``unresolved_expired`` -- a question that died unanswered.
4. The provider asks: one ``omh reminder:`` line per pack, at most every
   ``open_ask_days`` per record, recorded in a ledger that is written only
   when the pack is actually served. The reminder writes nothing to a record.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from _local_package import load_local_package

load_local_package()
from omh.local_store import atomic_write_text
from omh.maintenance.doctor import run_doctor
from omh.plugin_bundle.omh.memory_governance import evaluate_memory_replay
from omh.plugin_bundle.omh.memory_open_reminders import (
    open_reminders_path,
    read_open_reminders,
    render_open_reminder,
)
from omh.plugin_bundle.omh.memory_prefetch_receipt import read_prefetch_receipt, validate_prefetch_receipt
from omh.plugin_bundle.omh.memory_provider import OmhMemoryProvider, RecallStatus
from omh.plugin_bundle.omh.memory_records import render_memory_records
from omh.plugin_bundle.omh.hermes_memory import read_approved_records
from omh.plugin_bundle.omh.memory_open_reminders import select_open_reminder
from omh.commands.memory import _memory_retire_exit_code, cmd_memory_retire
from omh.workflows import memory as memory_workflow
from omh.workflows.memory import (
    approve_project_memory_candidate,
    apply_memory_retirement,
    build_memory_retirement,
    build_project_memory_recall_pack,
    build_project_memory_review,
    build_project_memory_status,
    capture_project_memory_candidate,
    confirm_due_project_memory_records,
    confirm_project_memory_record,
    keep_memory_record_open,
    memory_recall_pack_for_handoff,
    validate_project_memory_recall_pack,
    validate_project_memory_record,
)
from omh.workflows.memory_lifecycle import (
    apply_memory_correction,
    apply_memory_reapproval,
    build_memory_correction,
    build_memory_reapproval,
)
from omh.workflows.memory_lifecycle_executor import execute_memory_lifecycle
from omh.wrapper.continuity import _warning_count as continuity_warning_count
from project_identity_fixture import memory_paths as resolve_paths

PAST = "2020-01-01T00:00:00Z"
DAY = timedelta(days=1)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _approved(paths, summary: str, **capture) -> dict:
    captured = capture_project_memory_candidate(paths, summary, **capture)
    return approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]


def _record_path(paths, record_id: str) -> Path:
    return paths.memory_dir / "records" / f"{record_id}.json"


def _stored(paths, record_id: str) -> dict:
    return json.loads(_record_path(paths, record_id).read_text(encoding="utf-8"))


def _mutate_record(paths, record_id: str, **fields) -> dict:
    """Rewrite non-digested metadata; `staleness` is outside the payload digest."""
    stored = _stored(paths, record_id)
    stored.update(fields)
    atomic_write_text(_record_path(paths, record_id), json.dumps(stored), private=True)
    return stored


def _open_record(
    paths,
    summary: str,
    *,
    days_open: int = 34,
    past_deadline: bool = True,
    ceiling_days: int = 365,
    now: datetime | None = None,
    **capture,
) -> dict:
    """An approved open record whose question has been open for ``days_open`` days.

    ``now`` is the instant the caller measures from, and a test that captures
    its own ``now`` before building the record must pass it. The helper's own
    clock read happens after ``_approved`` has done real file I/O, so an
    earlier ``now`` sits closer to ``open_since`` than the caller intended and
    ``open_days`` floors one day short. ``_stamp`` truncates the microseconds,
    which buys back under a second of that gap; approval on a loaded runner
    spends more. It is a race against approval, not a date boundary, so it
    reproduces at any hour and only on a slow enough machine.
    """
    record = _approved(paths, summary, unresolved=True, **capture)
    since = (now if now is not None else datetime.now(timezone.utc)) - timedelta(days=days_open)
    staleness = {
        **record["staleness"],
        "resolution": "open",
        "open_since": _stamp(since),
        "open_expires_at": _stamp(since + timedelta(days=ceiling_days)),
    }
    fields = {"staleness": staleness}
    if past_deadline:
        fields["staleness"] = {**staleness, "stale_after": PAST, "review_due_at": PAST}
        fields["revalidation"] = {"deadline": PAST}
    return _mutate_record(paths, record["record_id"], **fields)


def _write_policy(paths, **cadence) -> None:
    paths.setup_profile_path.parent.mkdir(parents=True, exist_ok=True)
    paths.setup_profile_path.write_text(
        json.dumps({"schema_version": "setup_profile/v1", "memory_policy": {"mode": "review-first", **cadence}}),
        encoding="utf-8",
    )


class NonOpenVerdictIsUnchangedTests(unittest.TestCase):
    """A record that is not open gets the verdict it got before this feature.

    The pre-change verdict shape is spelled out literally per fixture; the two
    new keys are the only addition, and they are constant for a non-open
    record. If any of these dicts move, the old contract moved with them.
    """

    def _verdict(self, record: dict, *, now: datetime) -> dict:
        return memory_workflow._record_staleness(record, now=now)

    def test_every_pre_existing_fixture_keeps_its_exact_verdict(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            now = datetime.now(timezone.utc)
            constant = {"resolution": "", "open_days": 0}

            durable = _approved(paths, "the license is Apache-2.0", retention_class="durable")
            self.assertEqual(
                self._verdict(durable, now=now),
                {"state": "fresh", "reason": "", "stale_after": "", "review_due_at": "", "expires_at": "", "source_state": "", **constant},
            )

            fact = _approved(paths, "docs gates are byte-exact")
            deadline = fact["staleness"]["review_due_at"]
            self.assertEqual(
                self._verdict(fact, now=now),
                {"state": "fresh", "reason": "", "stale_after": deadline, "review_due_at": deadline, "expires_at": "", "source_state": "", **constant},
            )
            due_soon = datetime.fromisoformat(deadline.replace("Z", "+00:00")) - 3 * DAY
            self.assertEqual(
                self._verdict(fact, now=due_soon),
                {"state": "fresh", "reason": "review_due_soon", "stale_after": deadline, "review_due_at": deadline, "expires_at": "", "source_state": "", **constant},
            )

            stale = _mutate_record(
                paths, fact["record_id"], revalidation={"deadline": PAST}, staleness={"stale_after": PAST, "stale_after_days": None, "review_due_at": PAST}
            )
            self.assertEqual(
                self._verdict(stale, now=now),
                {"state": "stale", "reason": "review_due", "stale_after": PAST, "review_due_at": PAST, "expires_at": "", "source_state": "", **constant},
            )

            expired = _approved(paths, "volatile branch note", ttl_days=1)
            expired = _mutate_record(paths, expired["record_id"], ttl={"ttl_days": 1, "expires_at": PAST})
            deadline = expired["staleness"]["review_due_at"]
            self.assertEqual(
                self._verdict(expired, now=now),
                {"state": "expired", "reason": "retention_expired", "stale_after": deadline, "review_due_at": deadline, "expires_at": PAST, "source_state": "", **constant},
            )

            episode = _approved(paths, "the deploy went out at noon", record_type="episode")
            expires_at = episode["ttl"]["expires_at"]
            deadline = episode["staleness"]["review_due_at"]
            near = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) - 2 * DAY
            self.assertEqual(
                self._verdict(episode, now=near),
                {"state": "fresh", "reason": "expires_soon", "stale_after": deadline, "review_due_at": deadline, "expires_at": expires_at, "source_state": "", **constant},
            )

            source = root / "cited.md"
            atomic_write_text(source, "the original claim\n")
            cited = _approved(paths, "claim with a cited source", source_ref=str(source))
            deadline = cited["staleness"]["review_due_at"]
            atomic_write_text(source, "the claim, edited\n")
            self.assertEqual(
                self._verdict(cited, now=now),
                {"state": "stale", "reason": "source_changed", "stale_after": deadline, "review_due_at": deadline, "expires_at": "", "source_state": "changed", **constant},
            )
            source.unlink()
            self.assertEqual(
                self._verdict(cited, now=now),
                {"state": "unknown", "reason": "source_unreadable", "stale_after": deadline, "review_due_at": deadline, "expires_at": "", "source_state": "unreadable", **constant},
            )

    def test_only_the_exact_open_spelling_counts(self) -> None:
        # Fail closed: a typo in the marker cannot exempt a record from its deadline.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approved(paths, "a record with a garbled marker")
            for value in ("Open", "OPEN", "yes", True, 1, "unresolved"):
                with self.subTest(value=value):
                    stored = _mutate_record(
                        paths,
                        record["record_id"],
                        revalidation={"deadline": PAST},
                        staleness={"stale_after": PAST, "review_due_at": PAST, "resolution": value, "open_since": PAST},
                    )
                    verdict = memory_workflow._record_staleness(stored, now=datetime.now(timezone.utc))
                    self.assertEqual((verdict["state"], verdict["reason"], verdict["resolution"], verdict["open_days"]), ("stale", "review_due", "", 0))


class OpenVerdictTests(unittest.TestCase):
    """The fourth state, and what still outranks it."""

    def test_an_open_record_past_its_deadline_is_open_with_its_age(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "should the cache use redis or memcached", days_open=34)
            verdict = memory_workflow._record_staleness(record, now=datetime.now(timezone.utc))
            self.assertEqual((verdict["state"], verdict["reason"]), ("open", "unresolved"))
            self.assertEqual(verdict["resolution"], "open")
            self.assertEqual(verdict["open_days"], 34)
            self.assertEqual(verdict["review_due_at"], PAST, "the deadline is still reported; it just is not a verdict")

    def test_an_open_record_inside_its_deadline_is_fresh_and_still_marked(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "which region hosts the failover", days_open=3, past_deadline=False)
            verdict = memory_workflow._record_staleness(record, now=datetime.now(timezone.utc))
            self.assertEqual(verdict["state"], "fresh")
            self.assertEqual((verdict["resolution"], verdict["open_days"]), ("open", 3))

    def test_expiry_source_change_and_unreadable_source_outrank_open(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            now = datetime.now(timezone.utc)

            ttl = _open_record(paths, "open with a ttl", ttl_days=1, now=now)
            ttl = _mutate_record(paths, ttl["record_id"], ttl={"ttl_days": 1, "expires_at": PAST})
            self.assertEqual(memory_workflow._record_staleness(ttl, now=now)["reason"], "retention_expired")

            ceiling = _open_record(paths, "open past the ceiling", days_open=400, ceiling_days=365, now=now)
            verdict = memory_workflow._record_staleness(ceiling, now=now)
            self.assertEqual((verdict["state"], verdict["reason"], verdict["open_days"]), ("expired", "unresolved_expired", 400))
            self.assertEqual(
                memory_workflow._record_staleness(ceiling, now=now - 40 * DAY)["reason"],
                "unresolved",
                "the ceiling is a clock, not a flag: before it the same record is open",
            )

            source = root / "cited.md"
            atomic_write_text(source, "the original claim\n")
            cited = _open_record(paths, "open with a cited source", source_ref=str(source), now=now)
            atomic_write_text(source, "the claim, edited\n")
            self.assertEqual(memory_workflow._record_staleness(cited, now=now)["reason"], "source_changed")
            source.unlink()
            self.assertEqual(memory_workflow._record_staleness(cited, now=now)["reason"], "source_unreadable")

    def test_a_durable_open_record_has_no_ceiling_but_always_a_deadline(self) -> None:
        # Durable means "does not expire", so no ceiling. But "unresolved"
        # means "needs an answer by then", so the record still carries the
        # default review deadline -- without one it could never reach `open`,
        # never be asked about, and never be answered.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approved(paths, "which license should the SDK carry", retention_class="durable", unresolved=True)
            self.assertEqual(record["staleness"]["resolution"], "open")
            self.assertNotIn("open_expires_at", record["staleness"])
            created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            self.assertEqual(record["staleness"]["review_due_at"], _stamp(created + 90 * DAY))
            self.assertEqual(record["revalidation"]["deadline"], record["staleness"]["review_due_at"])
            self.assertEqual(record["staleness"]["stale_after_days"], 90)
            far = datetime.now(timezone.utc) + 3000 * DAY
            verdict = memory_workflow._record_staleness(record, now=far)
            self.assertEqual((verdict["state"], verdict["reason"]), ("open", "unresolved"), "open forever, never expired, never stale")

    def test_every_unresolved_record_carries_a_review_deadline(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _write_policy(paths, stale_after_days_default=30)
            for kwargs in (
                {"record_type": "decision", "retention_class": "durable"},
                {"record_type": "episode"},
                {"record_type": "fact", "retention_class": "volatile"},
            ):
                with self.subTest(**kwargs):
                    captured = capture_project_memory_candidate(paths, "an open question", unresolved=True, **kwargs)
                    candidate = captured["candidate"]
                    created = datetime.fromisoformat(candidate["created_at"].replace("Z", "+00:00"))
                    self.assertEqual(candidate["staleness"]["review_due_at"], _stamp(created + 30 * DAY))
                    self.assertEqual(candidate["staleness"]["cadence_source"], "default")
                    card = build_project_memory_review(paths, candidate_id=candidate["candidate_id"])["cards"][0]
                    self.assertEqual(card["review_due_at"], candidate["staleness"]["review_due_at"], "the deadline is disclosed on the card")
                    record = approve_project_memory_candidate(paths, candidate["candidate_id"])["record"]
                    self.assertEqual(record["revalidation"]["deadline"], candidate["staleness"]["review_due_at"])
            # approve --unresolved on a candidate that minted no deadline of its own.
            plain = capture_project_memory_candidate(paths, "a durable question", record_type="decision", retention_class="durable")
            self.assertEqual(plain["candidate"]["staleness"]["review_due_at"], "")
            record = approve_project_memory_candidate(paths, plain["candidate"]["candidate_id"], unresolved=True)["record"]
            approved = datetime.fromisoformat(record["approved_at"].replace("Z", "+00:00"))
            self.assertEqual(record["revalidation"]["deadline"], _stamp(approved + 30 * DAY))
            self.assertEqual(record["staleness"]["review_due_at"], record["revalidation"]["deadline"])
            # A durable re-class at approval keeps the deadline the card showed.
            captured = capture_project_memory_candidate(paths, "re-classed open question", unresolved=True)
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"], retention_class="durable")["record"]
            self.assertEqual(record["revalidation"]["deadline"], captured["candidate"]["staleness"]["review_due_at"])
            self.assertNotIn("open_expires_at", record["staleness"])


class OpenMarkerMintingTests(unittest.TestCase):
    """--unresolved on capture and approve, disclosed on the card and in the policy."""

    def test_capture_unresolved_starts_the_clock_at_capture_and_carries_through_approval(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            captured = capture_project_memory_candidate(paths, "does the gateway need sticky sessions", unresolved=True)
            candidate = captured["candidate"]
            self.assertIs(candidate["unresolved"], True)
            self.assertEqual(candidate["staleness"]["resolution"], "open")
            self.assertEqual(candidate["staleness"]["open_since"], candidate["created_at"])
            since = datetime.fromisoformat(candidate["created_at"].replace("Z", "+00:00"))
            self.assertEqual(candidate["staleness"]["open_expires_at"], _stamp(since + 365 * DAY))

            card = build_project_memory_review(paths, candidate_id=candidate["candidate_id"])["cards"][0]
            self.assertIs(card["unresolved"], True)
            self.assertEqual(card["open_expires_at"], candidate["staleness"]["open_expires_at"])

            record = approve_project_memory_candidate(paths, candidate["candidate_id"])["record"]
            self.assertEqual(record["staleness"]["resolution"], "open")
            self.assertEqual(record["staleness"]["open_since"], candidate["created_at"], "approval must not move the clock the reviewer saw")
            self.assertEqual(record["staleness"]["open_expires_at"], candidate["staleness"]["open_expires_at"])
            self.assertEqual(validate_project_memory_record(record), [])

    def test_an_ordinary_card_and_record_carry_no_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            captured = capture_project_memory_candidate(paths, "an ordinary settled fact")
            self.assertNotIn("unresolved", captured["candidate"])
            card = build_project_memory_review(paths, candidate_id=captured["candidate"]["candidate_id"])["cards"][0]
            self.assertNotIn("unresolved", card)
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]
            self.assertNotIn("resolution", record["staleness"])

    def test_approve_unresolved_starts_the_clock_at_approval(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            captured = capture_project_memory_candidate(paths, "is the retry budget three or five")
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"], unresolved=True)["record"]
            self.assertEqual(record["staleness"]["resolution"], "open")
            self.assertEqual(record["staleness"]["open_since"], record["approved_at"])
            since = datetime.fromisoformat(record["approved_at"].replace("Z", "+00:00"))
            self.assertEqual(record["staleness"]["open_expires_at"], _stamp(since + 365 * DAY))

    def test_a_durable_reclass_at_approval_drops_the_ceiling(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            captured = capture_project_memory_candidate(paths, "which font ships in the brand kit", unresolved=True)
            record = approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"], retention_class="durable")["record"]
            self.assertEqual(record["staleness"]["resolution"], "open")
            self.assertNotIn("open_expires_at", record["staleness"])

    def test_the_ceiling_and_ask_cadence_are_policy_tunables(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            policy = memory_workflow.read_project_memory_policy(paths)
            self.assertEqual((policy["open_max_days"], policy["open_ask_days"]), (365, 14))
            _write_policy(paths, open_max_days=30, open_ask_days=3)
            policy = memory_workflow.read_project_memory_policy(paths)
            self.assertEqual((policy["open_max_days"], policy["open_ask_days"]), (30, 3))
            record = _approved(paths, "does the nightly need a canary", unresolved=True)
            since = datetime.fromisoformat(record["staleness"]["open_since"].replace("Z", "+00:00"))
            self.assertEqual(record["staleness"]["open_expires_at"], _stamp(since + 30 * DAY))
            _write_policy(paths, open_max_days=0, open_ask_days=999)
            policy = memory_workflow.read_project_memory_policy(paths)
            self.assertEqual((policy["open_max_days"], policy["open_ask_days"]), (365, 14), "invalid values fall back to the named defaults")


class OpenDeliveryTests(unittest.TestCase):
    """Delivered with the marker and its age, never excluded, never read as decided."""

    def test_an_open_record_past_its_deadline_is_delivered_while_a_stale_sibling_is_not(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            open_record = _open_record(paths, "cache question: redis or memcached", days_open=34, tags=["cache"])
            settled = _approved(paths, "cache ttl is one hour", tags=["cache"])
            _mutate_record(paths, settled["record_id"], revalidation={"deadline": PAST}, staleness={"stale_after": PAST, "review_due_at": PAST})

            pack = build_project_memory_recall_pack(paths, "cache")

            self.assertEqual(validate_project_memory_recall_pack(pack), [])
            [item] = pack["included_records"]
            self.assertEqual(item["record_id"], open_record["record_id"])
            self.assertEqual(item["resolution"], "open")
            self.assertEqual(item["resolution_marker"], "open · 34 days unresolved")
            self.assertEqual((item["staleness"]["state"], item["staleness"]["reason"], item["staleness"]["open_days"]), ("open", "unresolved", 34))
            self.assertEqual(item["eligibility_reason"], "eligible")
            self.assertEqual(pack["unresolved_delivered"], 1)
            self.assertEqual([entry["reason"] for entry in pack["excluded_records"]], ["stale_review_required"])
            warnings = {warning["record_id"]: warning for warning in pack["freshness_warnings"]}
            open_warning = warnings[open_record["record_id"]]
            self.assertEqual((open_warning["state"], open_warning["reason_code"], open_warning["delivered"]), ("open", "unresolved_open", True))
            for verb in ("confirm", "keep-open", "retire"):
                self.assertIn(verb, open_warning["next_action"])
            self.assertEqual(warnings[settled["record_id"]]["reason_code"], "stale_review_required")

    def test_the_eligibility_exemption_lives_in_the_shared_evaluator(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "which queue backs the outbox", days_open=10)
            reviews = memory_workflow._project_memory_review_resolver(paths)
            verdict = evaluate_memory_replay(record, review_resolver=reviews)
            self.assertEqual((verdict["eligible"], verdict["reason_code"]), (True, "eligible"))
            status = build_project_memory_status(paths)
            self.assertEqual(status["counts"]["eligible_records"], 1, "memory status counts what recall delivers")
            self.assertEqual(status["counts"]["unresolved"], 1)

    def test_a_ceiling_expired_open_record_is_out_like_an_expired_one(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "abandoned question about the cache", days_open=400, tags=["cache"])
            pack = build_project_memory_recall_pack(paths, "cache")
            self.assertEqual(pack["record_count"], 0)
            self.assertEqual(pack["unresolved_delivered"], 0)
            [excluded] = pack["excluded_records"]
            self.assertEqual(excluded["reason"], "unresolved_expired")
            [warning] = pack["freshness_warnings"]
            self.assertEqual((warning["reason_code"], warning["delivered"]), ("unresolved_expired", False))
            self.assertIn("died unanswered", warning["detail"])
            inspected = build_project_memory_recall_pack(paths, "cache", include_stale=True)
            self.assertEqual(inspected["record_count"], 0, "--include-stale does not resurrect a dead question")
            self.assertEqual(inspected["excluded_records"][0]["record_id"], record["record_id"])

    def test_the_ceiling_is_one_gate_for_recall_status_and_the_bridge(self) -> None:
        # The ceiling lives in the shared evaluator beside `expired_*`, so every
        # surface that asks "is this record eligible?" gets the same answer.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            dead = _open_record(paths, "abandoned question about the cache", days_open=400, tags=["cache"])
            live = _open_record(paths, "live question about the cache", days_open=30, tags=["cache"])
            reviews = memory_workflow._project_memory_review_resolver(paths)
            verdict = evaluate_memory_replay(dead, review_resolver=reviews)
            self.assertEqual((verdict["eligible"], verdict["reason_code"]), (False, "unresolved_expired"))
            self.assertTrue(evaluate_memory_replay(live, review_resolver=reviews)["eligible"])
            status = build_project_memory_status(paths)
            self.assertEqual(status["counts"]["expired_records"], 1, "status counts a dead question as expired")
            self.assertEqual(status["counts"]["eligible_records"], 1)
            self.assertEqual(status["counts"]["ineligible_records"], 1)
            self.assertEqual(
                [row["record_id"] for row in status["hermes_memory"]["promotable"]],
                [live["record_id"]],
                "the bridge promotes the live question and never the dead one",
            )
            approved = {record["record_id"] for record in read_approved_records(paths.omh_home)}
            self.assertEqual(approved, {live["record_id"]}, "the bridge's approved view excludes the dead question")
            pack = build_project_memory_recall_pack(paths, "cache")
            self.assertEqual([item["record_id"] for item in pack["included_records"]], [live["record_id"]])
            self.assertEqual([entry["reason"] for entry in pack["excluded_records"]], ["unresolved_expired"])

    def test_a_malformed_deadline_or_ceiling_on_an_open_record_still_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "open with a garbled deadline", days_open=10)
            reviews = memory_workflow._project_memory_review_resolver(paths)
            garbled_deadline = {**record, "revalidation": {"deadline": "not-a-date"}}
            self.assertEqual(evaluate_memory_replay(garbled_deadline, review_resolver=reviews)["reason_code"], "revalidation_parse_error")
            garbled_ceiling = {**record, "staleness": {**record["staleness"], "open_expires_at": "not-a-date"}}
            self.assertEqual(evaluate_memory_replay(garbled_ceiling, review_resolver=reviews)["reason_code"], "retention_parse_error")
            naive_ceiling = {**record, "staleness": {**record["staleness"], "open_expires_at": "2020-01-01T00:00:00"}}
            self.assertEqual(evaluate_memory_replay(naive_ceiling, review_resolver=reviews)["reason_code"], "unresolved_expired", "a naive stamp reads as UTC")

    def test_the_handoff_carries_the_open_record_and_its_advisory_is_not_a_warning_count(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _open_record(paths, "release notes: changelog or commits", days_open=20, tags=["release"])
            pack = memory_recall_pack_for_handoff(paths, "release notes", executor_target="codex")
            assert pack is not None
            self.assertEqual(pack["record_count"], 1)
            self.assertEqual(pack["included_records"][0]["resolution_marker"], "open · 20 days unresolved")
            self.assertEqual([w["reason_code"] for w in pack["freshness_warnings"]], ["unresolved_open"])
            self.assertEqual(continuity_warning_count(pack), 0, "an advisory notice never flips continuity to warnings_present")

    def test_status_lists_open_records_oldest_first_and_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            young = _open_record(paths, "the young question", days_open=2, past_deadline=False)
            old = _open_record(paths, "the old question", days_open=60)
            _approved(paths, "a settled fact")
            status = build_project_memory_status(paths)
            self.assertEqual(status["counts"]["unresolved"], 2)
            self.assertEqual([row["record_id"] for row in status["open_records"]], [old["record_id"], young["record_id"]])
            first = status["open_records"][0]
            self.assertEqual(sorted(first), ["last_asked_at", "open_days", "open_expires_at", "open_since", "record_id", "review_due_at", "state", "summary"])
            self.assertEqual((first["open_days"], first["state"], first["review_due_at"], first["last_asked_at"]), (60, "open", PAST, ""))
            self.assertEqual(status["open_records"][1]["state"], "fresh")
            keep_memory_record_open(paths, old["record_id"])
            self.assertTrue(build_project_memory_status(paths)["open_records"][0]["last_asked_at"])
            for index in range(25):
                _open_record(paths, f"filler question {index}", days_open=1, past_deadline=False)
            status = build_project_memory_status(paths)
            self.assertEqual(status["counts"]["unresolved"], 27)
            self.assertEqual(len(status["open_records"]), 20)


class OpenAnswerTests(unittest.TestCase):
    """confirm resolves, keep-open resets the clock, correct resolves, retire ends."""

    def test_confirm_is_the_answer_resolved(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "retry budget question", days_open=34, tags=["retry"])
            result = confirm_project_memory_record(paths, record["record_id"])
            self.assertTrue(result["applied"])
            self.assertTrue(result["was_open"])
            self.assertFalse(result["was_stale"], "an open record past its deadline was never stale")
            self.assertIn("resolved", result["next_action"])
            stored = _stored(paths, record["record_id"])
            self.assertEqual(stored["staleness"]["resolution"], "resolved")
            self.assertEqual(stored["staleness"]["open_since"], record["staleness"]["open_since"])
            self.assertEqual(stored["staleness"]["resolved_at"], result["confirmed_at"])
            self.assertNotIn("open_expires_at", stored["staleness"])
            self.assertEqual(validate_project_memory_record(stored), [])
            verdict = memory_workflow._record_staleness(stored, now=datetime.now(timezone.utc))
            self.assertEqual((verdict["state"], verdict["resolution"], verdict["open_days"]), ("fresh", "resolved", 0))
            pack = build_project_memory_recall_pack(paths, "retry")
            self.assertEqual(pack["included_records"][0]["resolution_marker"], "")
            self.assertEqual(pack["unresolved_delivered"], 0)
            again = confirm_project_memory_record(paths, record["record_id"])
            self.assertFalse(again["was_open"])
            self.assertEqual(_stored(paths, record["record_id"])["staleness"]["resolved_at"], result["confirmed_at"], "a later confirm keeps the answer date")

    def test_confirm_answers_an_open_record_that_has_no_deadline(self) -> None:
        # A restored or legacy open record carries no review deadline; "resolved"
        # must still be an answer it can take. Resolving mints no clock nobody
        # asked for, unless the operator states one.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "restored open question", days_open=40)
            stored = _mutate_record(
                paths,
                record["record_id"],
                revalidation={},
                staleness={key: value for key, value in record["staleness"].items() if key not in {"stale_after", "review_due_at", "stale_after_days"}},
            )
            self.assertEqual(memory_workflow._record_staleness(stored, now=datetime.now(timezone.utc))["review_due_at"], "")
            result = confirm_project_memory_record(paths, record["record_id"])
            self.assertEqual((result["applied"], result["reason_code"], result["was_open"]), (True, "confirmed", True))
            self.assertEqual(result["review_due_at"], "")
            self.assertIsNone(result["stale_after_days"])
            self.assertFalse(result["shortened"])
            self.assertIn("no review deadline", result["next_action"])
            resolved = _stored(paths, record["record_id"])
            self.assertEqual(resolved["staleness"]["resolution"], "resolved")
            self.assertNotIn("deadline", resolved["revalidation"])
            self.assertEqual(resolved["revalidation"]["confirmed_at"], result["confirmed_at"])
            self.assertEqual(validate_project_memory_record(resolved), [])
            verdict = memory_workflow._record_staleness(resolved, now=datetime.now(timezone.utc) + 3000 * DAY)
            self.assertEqual((verdict["state"], verdict["resolution"]), ("fresh", "resolved"))

            other = _open_record(paths, "another restored question", days_open=40)
            _mutate_record(
                paths,
                other["record_id"],
                revalidation={},
                staleness={key: value for key, value in other["staleness"].items() if key not in {"stale_after", "review_due_at", "stale_after_days"}},
            )
            dated = confirm_project_memory_record(paths, other["record_id"], stale_after_days=30)
            self.assertTrue(dated["applied"])
            self.assertEqual(dated["stale_after_days"], 30)
            self.assertEqual(_stored(paths, other["record_id"])["revalidation"]["deadline"], dated["review_due_at"])

            settled = _approved(paths, "a durable settled fact", retention_class="durable")
            refused = confirm_project_memory_record(paths, settled["record_id"])
            self.assertEqual((refused["applied"], refused["reason_code"]), (False, "no_review_deadline"), "the refusal stays for records that are not open")

    def test_confirm_all_due_never_answers_an_open_question(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            open_record = _open_record(paths, "an open question", days_open=34)
            settled = _approved(paths, "a review-due fact")
            _mutate_record(paths, settled["record_id"], revalidation={"deadline": PAST}, staleness={"stale_after": PAST, "review_due_at": PAST})
            before = _record_path(paths, open_record["record_id"]).read_bytes()
            result = confirm_due_project_memory_records(paths)
            self.assertEqual([row["record_id"] for row in result["confirmed"]], [settled["record_id"]])
            self.assertEqual(result["open_count"], 1)
            self.assertIn("unresolved record(s) were not touched", result["next_action"])
            self.assertEqual(_record_path(paths, open_record["record_id"]).read_bytes(), before)

    def test_confirm_refuses_a_question_that_died_unanswered(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "abandoned question", days_open=400)
            result = confirm_project_memory_record(paths, record["record_id"])
            self.assertEqual((result["applied"], result["reason_code"]), (False, "unresolved_expired"))
            self.assertIn("died unanswered", result["detail"])

    def test_keep_open_writes_the_ledger_and_nothing_else(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "still thinking about it", days_open=34)
            before = _record_path(paths, record["record_id"]).read_bytes()
            result = keep_memory_record_open(paths, record["record_id"])
            self.assertEqual((result["applied"], result["reason_code"], result["state"], result["open_days"]), (True, "kept_open", "open", 34))
            self.assertEqual(result["asked_count"], 1)
            self.assertEqual(result["review_due_at"], PAST, "the deadline is untouched")
            self.assertEqual(_record_path(paths, record["record_id"]).read_bytes(), before, "keep-open never writes the record")
            ledger = read_open_reminders(paths.omh_home)
            self.assertEqual(ledger[record["record_id"]]["asked_at"], result["asked_at"])
            self.assertEqual(ledger[record["record_id"]]["asked_count"], 1)
            # Windows reports 0o666 for a file chmod'ed 0o600 (CLAUDE.md's
            # platform pitfall); the sibling sidecar tests read it the same way.
            self.assertEqual(open_reminders_path(paths.omh_home).stat().st_mode & 0o777, 0o666 if os.name == "nt" else 0o600)
            second = keep_memory_record_open(paths, record["record_id"])
            self.assertEqual(second["asked_count"], 2)
            expected_next = datetime.fromisoformat(second["asked_at"].replace("Z", "+00:00")) + 14 * DAY
            self.assertEqual(second["next_ask_after"], _stamp(expected_next))

    def test_keep_open_refuses_what_is_not_open(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            settled = _approved(paths, "a settled fact")
            refused = keep_memory_record_open(paths, settled["record_id"])
            self.assertEqual((refused["applied"], refused["reason_code"]), (False, "not_open"))
            missing = keep_memory_record_open(paths, "mem_ffffffffffffffff")
            self.assertEqual((missing["applied"], missing["reason_code"]), (False, "record_not_found"))
            dead = _open_record(paths, "abandoned question", days_open=400)
            expired = keep_memory_record_open(paths, dead["record_id"])
            self.assertEqual((expired["applied"], expired["reason_code"]), (False, "unresolved_expired"))
            resolved = _open_record(paths, "already answered", days_open=5)
            confirm_project_memory_record(paths, resolved["record_id"])
            self.assertEqual(keep_memory_record_open(paths, resolved["record_id"])["reason_code"], "not_open")
            self.assertEqual(read_open_reminders(paths.omh_home), {}, "a refusal writes no ledger line")
            with self.assertRaises(ValueError):
                keep_memory_record_open(paths, "../escape")

    def test_correct_resolves_the_question_and_history_keeps_it_open(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "cache question: redis or memcached", days_open=34)
            now = datetime.now(timezone.utc)
            plan = build_memory_correction(paths, record["record_id"], 1, "The cache is redis, decided after the load test", now=now)
            apply_memory_correction(paths, plan, transaction_executor=execute_memory_lifecycle)
            history = json.loads((paths.memory_dir / "history" / f"{record['record_id']}.r1.json").read_text(encoding="utf-8"))
            self.assertEqual(history["staleness"]["resolution"], "open", "the superseded revision records what was unresolved")
            candidate_id = plan.report["manifest"][2]["target_id"].split(":", 1)[1]
            candidate = json.loads((paths.memory_dir / "candidates" / f"{candidate_id}.json").read_text(encoding="utf-8"))
            replacement = candidate["replacement"]["staleness"]
            self.assertEqual(replacement["resolution"], "resolved")
            self.assertEqual(replacement["open_since"], record["staleness"]["open_since"])
            self.assertTrue(replacement["resolved_at"])
            self.assertNotIn("open_expires_at", replacement)
            reapproval = build_memory_reapproval(paths, candidate_id, reviewer_claim="reviewer", now=now)
            apply_memory_reapproval(paths, reapproval, transaction_executor=execute_memory_lifecycle)
            successor = _stored(paths, record["record_id"])
            self.assertEqual(successor["revision"], 2)
            self.assertEqual(successor["staleness"]["resolution"], "resolved")
            self.assertEqual(successor["staleness"]["open_since"], record["staleness"]["open_since"])
            self.assertNotIn("open_expires_at", successor["staleness"])
            verdict = memory_workflow._record_staleness(successor, now=now)
            self.assertEqual((verdict["state"], verdict["resolution"], verdict["open_days"]), ("fresh", "resolved", 0))

    def test_retire_names_a_dead_question_and_drops_a_live_one_only_by_name(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            dead = _open_record(paths, "abandoned question", days_open=400)
            live = _open_record(paths, "still open question", days_open=34)
            settled = _approved(paths, "a settled live fact")
            now = datetime.now(timezone.utc)

            report = build_memory_retirement(paths, now=now)
            self.assertEqual({row["record_id"]: row["reason"] for row in report["expired"]}, {dead["record_id"]: "unresolved_expired"})
            self.assertEqual(report["expired"][0]["expires_at"], dead["staleness"]["open_expires_at"])
            self.assertEqual(report["target_record_id"], "")

            targeted = build_memory_retirement(paths, now=now, record_id=live["record_id"])
            self.assertEqual(targeted["target_record_id"], live["record_id"])
            self.assertEqual([(row["record_id"], row["reason"]) for row in targeted["expired"]], [(live["record_id"], "unresolved_dropped")])
            refused = build_memory_retirement(paths, now=now, record_id=settled["record_id"])
            self.assertEqual(refused["expired"], [])
            self.assertEqual(refused["skipped"], [{"path_name": f"{settled['record_id']}.json", "reason": "not_expired"}])
            absent = build_memory_retirement(paths, now=now, record_id="mem_ffffffffffffffff")
            self.assertEqual(absent["skipped"], [{"path_name": "mem_ffffffffffffffff.json", "reason": "record_not_found"}])
            with self.assertRaises(ValueError):
                build_memory_retirement(paths, now=now, record_id="../escape")

            applied = apply_memory_retirement(paths, now=now, record_id=live["record_id"])
            self.assertEqual([row["reason"] for row in applied["moved"]], ["unresolved_dropped"])
            self.assertFalse(_record_path(paths, live["record_id"]).exists())
            self.assertTrue(_record_path(paths, dead["record_id"]).exists(), "a targeted retire touches only its target")
            journal = [json.loads(line) for line in (paths.memory_dir / "archive" / "retirements.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([line["reason"] for line in journal], ["unresolved_dropped"])
            swept = apply_memory_retirement(paths, now=now)
            self.assertEqual([(row["record_id"], row["reason"]) for row in swept["moved"]], [(dead["record_id"], "unresolved_expired")])
            self.assertTrue(_record_path(paths, settled["record_id"]).exists())


def _provider(root: Path, **kwargs) -> OmhMemoryProvider:
    provider = OmhMemoryProvider(root / ".omh")
    provider.initialize("s1", hermes_home=str(root / ".hermes"), agent_context="primary", cwd=str(root), **kwargs)
    return provider


class ProviderReminderTests(unittest.TestCase):
    """The provider asks; the person answers; the reminder writes no record."""

    def test_one_reminder_line_is_served_disclosed_and_recorded_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            record = _open_record(paths, "cache question: redis or memcached", days_open=34)
            before = _record_path(paths, record["record_id"]).read_bytes()
            provider = _provider(root)
            self.assertEqual(read_open_reminders(root / ".omh"), {}, "rendering is not asking")

            pack = provider.prefetch("cache")

            expected = (
                f'omh reminder: "cache question: redis or memcached" ({record["record_id"]}) has been unresolved for 34 days — '
                f"resolved, still open, or drop it? Answer with omh memory confirm {record['record_id']} · "
                f"omh memory keep-open {record['record_id']} · omh memory retire {record['record_id']}"
            )
            self.assertEqual(pack.count("omh reminder:"), 1)
            self.assertIn(expected, pack)
            self.assertLess(pack.index("</memory_records>"), pack.index("omh reminder:"), "the line follows the records section")
            self.assertIn('resolution="open · 34 days unresolved"', pack)
            self.assertIn('<unresolved count="1">1 unresolved records delivered</unresolved>', pack)
            self.assertEqual(provider.recall_status(), RecallStatus(provider_label="OMH", count=1), "the reminder never moves the recall count")
            self.assertEqual(provider.latest_open_reminder()["record_id"], record["record_id"])
            self.assertEqual(provider.latest_open_reminder()["open_days"], 34)
            receipt = provider.latest_prefetch_receipt()
            self.assertEqual(receipt["reminder"], {"record_id": record["record_id"], "open_days": 34})
            self.assertEqual(validate_prefetch_receipt(receipt), [])
            self.assertEqual(read_prefetch_receipt(root / ".omh")["reminder"], receipt["reminder"])
            ledger = read_open_reminders(root / ".omh")
            self.assertEqual(ledger[record["record_id"]]["asked_count"], 1)
            provider.prefetch("cache")
            self.assertEqual(read_open_reminders(root / ".omh")[record["record_id"]]["asked_count"], 1, "one pack is one ask, however many API calls it serves")
            self.assertEqual(_record_path(paths, record["record_id"]).read_bytes(), before, "no reminder path mutates a record")
            self.assertEqual(memory_workflow._record_staleness(_stored(paths, record["record_id"]), now=datetime.now(timezone.utc))["state"], "open")

    def test_a_principal_switch_re_renders_under_the_new_lens_before_serving(self) -> None:
        # The pack was rendered under one lens; a prefetch under another must
        # not serve that pack -- and must not serve nothing either, which is
        # what blanking it did. It renders again under the arriving lens, and
        # the ask is recorded once, for the lens that actually saw it.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            record = _open_record(paths, "question rendered under one lens", days_open=34)
            provider = _provider(root)
            other_principal = {
                "schema_version": "memory_principal_context/v1",
                "principal": "principal:v1:" + "a" * 64,
                "profile_ref": "profile_fixture",
                "surface_ref": "fixture",
                "session_ref": "s1",
                "turn_ref": "turn_1",
                "actor_kind": "human",
                "identity_evidence_refs": ["evidence:fixture"],
                "binding_state": "validated_local",
            }
            pack = provider.prefetch("question", principal_context=other_principal)
            self.assertIn(f"({record['record_id']})", pack, "re-rendered under the new lens, the question is asked")
            self.assertEqual(provider.recall_status(), RecallStatus(provider_label="OMH", count=1))
            self.assertEqual(provider.latest_open_reminder()["record_id"], record["record_id"])
            self.assertEqual(provider.latest_prefetch_receipt()["reminder"]["record_id"], record["record_id"])
            self.assertEqual(read_open_reminders(root / ".omh")[record["record_id"]]["asked_count"], 1)
            provider.prefetch("question", principal_context=other_principal)
            self.assertEqual(read_open_reminders(root / ".omh")[record["record_id"]]["asked_count"], 1, "the same rendered pack is one ask")
            self.assertEqual(read_open_reminders(root / ".omh")[record["record_id"]]["asked_count"], 1)

    def test_a_durable_open_record_is_asked_about_once_its_deadline_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            record = _approved(paths, "which license should the SDK carry", record_type="decision", retention_class="durable", unresolved=True)
            deadline = datetime.fromisoformat(record["staleness"]["review_due_at"].replace("Z", "+00:00"))
            provider = _provider(root)
            self.assertNotIn("omh reminder:", provider.prefetch("license"), "inside the deadline: marker, no ask")
            provider.on_turn_start(2, "later")
            provider.queue_prefetch("license", now=deadline + DAY)
            pack = provider.prefetch("license")
            self.assertIn(f"({record['record_id']}) has been unresolved for 91 days", pack)
            self.assertEqual(read_open_reminders(root / ".omh")[record["record_id"]]["asked_count"], 1)
            answered = confirm_project_memory_record(paths, record["record_id"])
            self.assertEqual((answered["applied"], answered["was_open"]), (True, True))
            self.assertEqual(_stored(paths, record["record_id"])["staleness"]["resolution"], "resolved")

    def test_only_records_the_pack_would_deliver_are_asked_about(self) -> None:
        # Two of the three answers refuse a superseded or archived record, so a
        # reminder about one is a question with no working answer.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            superseded = _open_record(paths, "a superseded open question", days_open=90)
            _mutate_record(paths, superseded["record_id"], superseded_by="mem_0000000000000001")
            archived = _open_record(paths, "an archived open question", days_open=80)
            _mutate_record(
                paths,
                archived["record_id"],
                attention={"schema_version": "omh_memory_attention/v1", "tier": "archive", "reason": "parked", "previous_tier": "active", "changed_at": PAST},
            )
            provider = _provider(root)
            pack = provider.prefetch("question")
            self.assertNotIn("omh reminder:", pack)
            self.assertIsNone(provider.latest_open_reminder())
            self.assertIsNone(provider.latest_prefetch_receipt()["reminder"])
            self.assertEqual(read_open_reminders(root / ".omh"), {})
            # An eligible open record is asked about even when this turn's query
            # does not mention it: the reminder is eligibility-bound, not
            # query-bound.
            unrelated = _open_record(paths, "the cache question nobody typed", days_open=34)
            # A query that overlaps no record at all re-admits the active tier,
            # so one record must match for the open one to stay out of the pack.
            _approved(paths, "zzz overlaps the deploy notes")
            provider.on_turn_start(2, "next")
            provider.queue_prefetch("zzz nothing overlaps")
            pack = provider.prefetch("zzz nothing overlaps")
            self.assertNotIn('<record id="%s"' % unrelated["record_id"], pack)
            self.assertIn(f"({unrelated['record_id']})", pack)
            self.assertEqual(set(read_open_reminders(root / ".omh")), {unrelated["record_id"]})

    def test_a_queued_rerender_that_is_never_served_does_not_ask(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            _open_record(paths, "queued but never served", days_open=34)
            provider = _provider(root)
            provider.queue_prefetch("anything")
            provider.queue_prefetch("anything else")
            self.assertEqual(read_open_reminders(root / ".omh"), {})
            self.assertIsNone(provider.latest_open_reminder())
            self.assertIsNone(provider.latest_prefetch_receipt())

    def test_the_ask_cadence_holds_and_the_rest_queue_by_age(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            older = _open_record(paths, "the older question", days_open=60)
            younger = _open_record(paths, "the younger question", days_open=20)
            provider = _provider(root)
            first = provider.prefetch("question")
            self.assertIn(f"({older['record_id']})", first)
            self.assertNotIn(younger["record_id"], first.split("omh reminder:")[1])
            provider.on_turn_start(2, "next")
            provider.queue_prefetch("question")
            second = provider.prefetch("question")
            self.assertEqual(second.count("omh reminder:"), 1)
            self.assertIn(f"({younger['record_id']})", second, "the rest queue by age")
            provider.on_turn_start(3, "next")
            provider.queue_prefetch("question")
            third = provider.prefetch("question")
            self.assertNotIn("omh reminder:", third, "both were asked inside open_ask_days")
            ledger = read_open_reminders(root / ".omh")
            stale_ask = datetime.now(timezone.utc) - 15 * DAY
            atomic_write_text(
                open_reminders_path(root / ".omh"),
                json.dumps({"schema_version": "omh_memory_open_reminders/v1", "records": {
                    older["record_id"]: {"asked_at": _stamp(stale_ask), "asked_count": 1},
                    younger["record_id"]: ledger[younger["record_id"]],
                }}),
            )
            provider.on_turn_start(4, "next")
            provider.queue_prefetch("question")
            fourth = provider.prefetch("question")
            self.assertIn(f"({older['record_id']})", fourth, "after open_ask_days the question is asked again")
            self.assertEqual(read_open_reminders(root / ".omh")[older["record_id"]]["asked_count"], 2)

    def test_still_open_resets_the_clock_without_touching_the_record(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            record = _open_record(paths, "still open, thanks", days_open=34)
            before = _record_path(paths, record["record_id"]).read_bytes()
            answer = keep_memory_record_open(paths, record["record_id"])
            self.assertTrue(answer["applied"])
            provider = _provider(root)
            pack = provider.prefetch("open")
            self.assertNotIn("omh reminder:", pack)
            self.assertIn('resolution="open · 34 days unresolved"', pack, "the record is still delivered as open")
            self.assertIsNone(provider.latest_prefetch_receipt()["reminder"])
            self.assertEqual(_record_path(paths, record["record_id"]).read_bytes(), before)

    def test_an_open_record_inside_its_deadline_or_on_a_shared_surface_is_not_asked_about(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            _open_record(paths, "young question", days_open=3, past_deadline=False)
            pack = _provider(root).prefetch("question")
            self.assertNotIn("omh reminder:", pack, "the review deadline is the first ask")
            self.assertIn('resolution="open · 3 days unresolved"', pack)
            _open_record(paths, "old question", days_open=40)
            shared = _provider(root, platform="discord").prefetch("question")
            self.assertNotIn("omh reminder:", shared, "a group chat is not asked to settle the operator's question")
            self.assertEqual(read_open_reminders(root / ".omh"), {})

    def test_the_reminder_summary_is_the_bounded_redacted_projection(self) -> None:
        line = render_open_reminder({"record_id": "mem_a", "summary": 'say "hi"\nthen go', "open_days": 3})
        self.assertEqual(line.count("\n"), 0)
        self.assertIn("\"say 'hi' then go\" (mem_a) has been unresolved for 3 days", line)

    def test_the_records_section_stays_parseable_and_bounded_with_open_records(self) -> None:
        records = [
            {"record_id": "mem_open", "record_type": "decision", "summary": "a < b & c", "approved_at": "2026-09-01T00:00:00Z", "resolution": "open", "resolution_marker": "open · 34 days unresolved"},
            {"record_id": "mem_settled", "record_type": "fact", "summary": "settled", "approved_at": "2026-09-01T00:00:00Z", "resolution": ""},
        ]
        text, count = render_memory_records(records)
        self.assertEqual(count, 2)
        root = ElementTree.fromstring(text)
        open_element, settled_element = root.findall("record")
        self.assertEqual(open_element.attrib, {"id": "mem_open", "type": "decision", "approved": "2026-09-01", "resolution": "open · 34 days unresolved"})
        self.assertEqual(settled_element.attrib, {"id": "mem_settled", "type": "fact", "approved": "2026-09-01"})
        self.assertEqual(root.find("unresolved").attrib, {"count": "1"})
        self.assertEqual(root.find("unresolved").text, "1 unresolved records delivered")
        for budget in range(len(text) + 2):
            bounded, _count = render_memory_records(records, budget_chars=budget)
            self.assertLessEqual(len(bounded), budget)
            if bounded:
                ElementTree.fromstring(bounded)


class ReviewRegressionTests(unittest.TestCase):
    """Findings from the #1534 review, each pinned where it bit."""

    def test_a_lone_open_record_never_blanks_the_section_at_any_budget(self) -> None:
        # The summary line is emitted for a lone open record, so its budget must
        # be reserved for one too -- measured before: budgets 353-420 blanked
        # the whole section for a 200-char open summary while a settled record
        # never did.
        open_record = {
            "record_id": "mem_open", "record_type": "fact", "summary": "q" * 200,
            "approved_at": "2026-09-01T00:00:00Z", "resolution": "open", "resolution_marker": "open · 34 days unresolved",
        }
        settled = {**open_record, "record_id": "mem_settled", "resolution": "", "resolution_marker": ""}
        for label, record in (("open", open_record), ("settled", settled)):
            full, count = render_memory_records([record])
            self.assertEqual(count, 1)
            for budget in range(len(full) + 3):
                with self.subTest(record=label, budget=budget):
                    text, rendered = render_memory_records([record], budget_chars=budget)
                    self.assertLessEqual(len(text), budget)
                    if budget >= len(full):
                        self.assertEqual((text, rendered), (full, 1), "a budget the section fits in must never blank it")
                    if text:
                        ElementTree.fromstring(text)

    def test_a_principal_bound_open_record_is_asked_about_under_the_pack_bound(self) -> None:
        scope = {"kind": "project", "ref": "demo"}
        since = datetime.now(timezone.utc) - 20 * DAY
        v3 = {
            "schema_version": "project_memory_record/v3",
            "record_id": "mem_v3open",
            "scope": scope,
            "summary": "a principal-bound open question",
            "staleness": {"resolution": "open", "open_since": _stamp(since), "review_due_at": PAST, "stale_after": PAST},
        }
        now = datetime.now(timezone.utc)
        asked = select_open_reminder([v3], {}, now=now, allowed_scopes=[scope], eligible_record_ids={"mem_v3open"})
        self.assertIsNotNone(asked)
        assert asked is not None
        self.assertEqual((asked["record_id"], asked["open_days"]), ("mem_v3open", 20))
        self.assertIsNone(
            select_open_reminder([v3], {}, now=now, allowed_scopes=[scope], eligible_record_ids=set()),
            "the pack's own principal and audience rules reach the reminder through the eligible set",
        )
        legacy = {**v3, "schema_version": "project_memory_record/v1"}
        self.assertIsNone(select_open_reminder([legacy], {}, now=now, allowed_scopes=[scope], eligible_record_ids={"mem_v3open"}))

    def test_a_targeted_retire_that_did_nothing_exits_non_zero(self) -> None:
        self.assertEqual(_memory_retire_exit_code({"target_record_id": "mem_x", "expired": [], "skipped": [{"path_name": "mem_x.json", "reason": "record_not_found"}]}), 1)
        self.assertEqual(_memory_retire_exit_code({"target_record_id": "mem_x", "expired": [], "skipped": [{"path_name": "mem_x.json", "reason": "not_expired"}]}), 1)
        self.assertEqual(_memory_retire_exit_code({"target_record_id": "mem_x", "expired": [{"record_id": "mem_x", "reason": "unresolved_dropped"}], "skipped": []}), 0)
        self.assertEqual(_memory_retire_exit_code({"target_record_id": "", "expired": [], "skipped": [{"path_name": "mem_c.json", "reason": "corrupt_json"}]}), 0, "the sweep keeps its own vocabulary")
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            live = _open_record(paths, "a live open question", days_open=10)
            settled = _approved(paths, "a settled live fact")

            def run(record_id: str, *, apply: bool) -> tuple[int, dict]:
                args = argparse.Namespace(
                    omh_home=str(paths.omh_home), hermes_home=str(paths.hermes_home), scope=None,
                    record_id=record_id, apply=apply, window_days=7,
                )
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = cmd_memory_retire(args)
                return code, json.loads(buffer.getvalue())

            code, payload = run("mem_ffffffffffffffff", apply=True)
            self.assertEqual((code, payload["moved"]), (1, []))
            code, payload = run(settled["record_id"], apply=False)
            self.assertEqual((code, payload["skipped"][0]["reason"]), (1, "not_expired"))
            code, payload = run(live["record_id"], apply=True)
            self.assertEqual((code, [row["reason"] for row in payload["moved"]]), (0, ["unresolved_dropped"]))
            code, payload = run("", apply=False)
            self.assertEqual((code, payload["target_record_id"]), (0, ""))


class DoctorOpenRecordsTests(unittest.TestCase):
    def _check(self, paths):
        return next(item for item in run_doctor(paths) if item.name == "memory_open_records")

    def test_no_aging_open_record_reads_as_nothing_to_know(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _open_record(paths, "a young open question", days_open=100)
            check = self._check(paths)
            self.assertTrue(check.ok)
            self.assertEqual(check.severity, "ok")
            self.assertIn("182 days", check.message)

    def test_an_open_record_past_half_the_ceiling_is_a_warning_with_the_three_verbs(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _open_record(paths, "an aging open question", days_open=200)
            check = self._check(paths)
            self.assertTrue(check.ok, "an aging question is a thing to know, never a fault")
            self.assertEqual(check.severity, "warning")
            self.assertIn(f"{record['record_id']} (200d)", check.message)
            self.assertIn("omh memory confirm / keep-open / retire", check.message)
            _write_policy(paths, open_max_days=30)
            self.assertIn("15 days", self._check(paths).message)


if __name__ == "__main__":
    unittest.main()
