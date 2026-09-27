"""Contract tests for lineage-bound `motion_interaction_capture/v1` import (#1801)."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

import omh.workflows.web_qa_motion_capture_store as motion_store
from omh.commands.web_qa_observations import add_web_qa_observation_commands
from omh.workflows.web_qa_motion_capture_store import (
    MOTION_CAPTURE_REFUSAL_REASONS,
    MotionCaptureImportError,
    build_motion_interaction_capture,
    import_motion_capture,
    list_motion_captures,
    motion_capture_gate,
    project_motion_evidence,
    read_motion_capture,
)
from omh.workflows.web_qa_observation_plan import build_web_qa_observation_plan
from omh.workflows.web_qa_observation_store import import_web_qa_observation
from test_web_qa_observation import object_list, object_value
from test_web_qa_observation_plan import observation_request
from test_web_qa_observation_store import PNG, capture_receipt


RECORDING = hashlib.sha256(b"recording-a").hexdigest()
OTHER_RECORDING = hashlib.sha256(b"recording-b").hexdigest()
SHEET = hashlib.sha256(b"sheet-a").hexdigest()


def plan_for(revision: str, *, single_cell: bool = False, round_ordinal: int = 1, extra_state: bool = False) -> dict[str, object]:
    request = observation_request()
    object_value(request["subject"])["revision"] = revision
    condition = object_value(request["condition"])
    if single_cell:
        for key in ("routes", "viewports", "browsers"):
            condition[key] = object_list(condition[key])[:1]
    if extra_state:
        routes = object_list(condition["routes"])
        routes.append({**routes[0], "state_id": "signed-in"})
    request["round"] = {**object_value(request["round"]), "ordinal": round_ordinal}
    return build_web_qa_observation_plan(request)


def cell_where(plan: dict[str, object], **fields: object) -> dict[str, object]:
    return next(cell for cell in object_list(plan["matrix"]) if all(cell[key] == value for key, value in fields.items()))


def motion_receipt(plan: dict[str, object], cell: dict[str, object] | None = None, *, recording: str = RECORDING) -> dict[str, object]:
    chosen = cell or object_list(plan["matrix"])[0]
    subject = object_value(plan["subject"])
    return {
        "schema_version": "host_motion_capture_receipt/v1",
        "receipt_version": 1,
        "run_id": plan["run_id"],
        "cell_id": chosen["cell_id"],
        "lineage": {
            "repository": subject["repository"],
            "revision": subject["revision"],
            "route_id": chosen["route_id"],
            "state_id": chosen["state_id"],
            "viewport_id": chosen["viewport_id"],
            "browser_id": chosen["browser_id"],
        },
        "producer": {"producer_id": "hermes-agent-browser/recorder", "version": "1.4.0"},
        "requested": {"sampling_policy": "changed_frames", "max_duration_ms": 10_000},
        "observed": {"sampling_policy": "changed_frames", "started_at": "2026-09-07T10:00:00Z", "ended_at": "2026-09-07T10:00:05Z"},
        "recording": {"sha256": recording, "byte_size": 400_000, "media_type": "video/webm", "width": 1440, "height": 900, "duration_ms": 4_000, "frame_count": 240},
        "contact_sheet": {
            "sha256": SHEET,
            "byte_size": 90_000,
            "media_type": "image/png",
            "source_recording_sha256": recording,
            "tiles": [
                {"tile_index": 0, "source_recording_sha256": recording, "start_ms": 0, "end_ms": 400},
                {"tile_index": 1, "source_recording_sha256": recording, "start_ms": 400, "end_ms": 4_000},
            ],
        },
    }


class MotionCaptureStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q"], cwd=self.root, check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], cwd=self.root, check=True)
        self.revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()
        self.plan = plan_for(self.revision)

    def managed_files(self) -> list[Path]:
        return sorted(path for path in (self.root / ".omh").rglob("*") if path.is_file() and not path.name.endswith(".lock"))

    def assert_refused(self, reason: str, plan: object, receipt: object) -> None:
        self.assertIn(reason, MOTION_CAPTURE_REFUSAL_REASONS)
        with self.assertRaises(MotionCaptureImportError) as caught:
            import_motion_capture(self.root, plan, receipt)
        self.assertEqual(caught.exception.reason, reason)
        self.assertTrue(str(caught.exception).startswith(f"{reason}: "))
        self.assertEqual(self.managed_files(), [], "a refused import must leave no record behind")

    def test_valid_import_is_a_deterministic_lineage_bound_record(self) -> None:
        receipt = motion_receipt(self.plan)
        record = import_motion_capture(self.root, self.plan, receipt)
        cell = object_list(self.plan["matrix"])[0]
        self.assertEqual(record["schema_version"], "motion_interaction_capture/v1")
        self.assertRegex(str(record["capture_id"]), r"^motion-[a-f0-9]{24}$")
        self.assertEqual(record, build_motion_interaction_capture(self.plan, deepcopy(receipt)))
        self.assertEqual(
            record["lineage"],
            {
                "repository": object_value(self.plan["subject"])["repository"],
                "revision": self.revision,
                "route_id": cell["route_id"],
                "state_id": cell["state_id"],
                "viewport_id": cell["viewport_id"],
                "browser_id": cell["browser_id"],
                "locale": "en-US",
                "round_id": object_value(self.plan["round"])["round_id"],
                "round_ordinal": 1,
            },
        )
        self.assertEqual(object_value(record["contact_sheet"])["source_recording_sha256"], RECORDING)
        self.assertEqual(object_value(record["recording"])["sha256"], RECORDING)
        self.assertIn("visual_qa_pass", list(record["does_not_prove"]))
        self.assertEqual(read_motion_capture(self.root, str(self.plan["run_id"]), str(record["capture_id"])), record)
        self.assertEqual(list_motion_captures(self.root, str(self.plan["run_id"])), [record])

    def test_reimport_is_idempotent_and_a_new_digest_is_a_distinct_identity(self) -> None:
        first = import_motion_capture(self.root, self.plan, motion_receipt(self.plan))
        files = self.managed_files()
        before = [path.read_bytes() for path in files]
        with patch.object(motion_store, "_write_private", side_effect=AssertionError("re-import rewrote the record")):
            self.assertEqual(import_motion_capture(self.root, self.plan, motion_receipt(self.plan)), first)
        self.assertEqual([path.read_bytes() for path in self.managed_files()], before)

        second = import_motion_capture(self.root, self.plan, motion_receipt(self.plan, recording=OTHER_RECORDING))
        self.assertNotEqual(second["capture_id"], first["capture_id"])
        other_cell = cell_where(self.plan, viewport_id="mobile", route_id="checkout", browser_id="chrome-126")
        third = import_motion_capture(self.root, self.plan, motion_receipt(self.plan, other_cell))
        self.assertEqual(len({first["capture_id"], second["capture_id"], third["capture_id"]}), 3)
        self.assertEqual(read_motion_capture(self.root, str(self.plan["run_id"]), str(first["capture_id"])), first)
        self.assertEqual(len(list_motion_captures(self.root, str(self.plan["run_id"]))), 3)

    def test_lineage_mismatch_is_refused_by_name(self) -> None:
        captured = cell_where(self.plan, route_id="checkout", viewport_id="desktop", browser_id="chrome-126")
        for key, value in (
            ("viewport_id", "mobile"),
            ("route_id", "catalog"),
            ("state_id", "signed-in"),
            ("browser_id", "firefox-127"),
            ("revision", "0" * 40),
            ("repository", "https://github.com/acme/other"),
        ):
            with self.subTest(field=key):
                receipt = motion_receipt(self.plan, captured)
                self.assertNotEqual(object_value(receipt["lineage"])[key], value)
                object_value(receipt["lineage"])[key] = value
                self.assert_refused("lineage_mismatch", self.plan, receipt)
        receipt = motion_receipt(self.plan)
        receipt["cell_id"] = "cell-" + "0" * 24
        self.assert_refused("lineage_mismatch", self.plan, receipt)

    def test_capture_for_another_revision_than_the_checkout_is_refused(self) -> None:
        stale_plan = plan_for("a" * 40)
        stale_receipt = motion_receipt(stale_plan)
        build_motion_interaction_capture(stale_plan, stale_receipt)
        self.assert_refused("checkout_revision_mismatch", stale_plan, stale_receipt)

    def test_contact_sheet_tiles_must_bind_to_the_recording_digest(self) -> None:
        cases = {
            "tile names another recording": lambda sheet: object_list(sheet["tiles"])[1].update(source_recording_sha256=OTHER_RECORDING),
            "sheet names another recording": lambda sheet: sheet.update(source_recording_sha256=OTHER_RECORDING),
            "tile claims an uncaptured interval": lambda sheet: object_list(sheet["tiles"])[1].update(end_ms=4_001),
            "sheet replaces the recording identity": lambda sheet: sheet.update(sha256=RECORDING),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                receipt = motion_receipt(self.plan)
                mutate(object_value(receipt["contact_sheet"]))
                self.assert_refused("contact_sheet_lineage_mismatch", self.plan, receipt)

    def test_other_fail_closed_refusals_name_their_reason(self) -> None:
        cases: list[tuple[str, object]] = [
            ("digest_invalid", lambda r: object_value(r["recording"]).update(sha256="not-a-digest")),
            ("digest_invalid", lambda r: object_value(r["recording"]).update(sha256=RECORDING.upper())),
            ("unsupported_media_type", lambda r: object_value(r["recording"]).update(media_type="video/quicktime")),
            ("unsupported_media_type", lambda r: object_value(r["contact_sheet"]).update(media_type="image/gif")),
            ("metadata_limit_exceeded", lambda r: object_value(r["recording"]).update(duration_ms=10_001)),
            ("metadata_limit_exceeded", lambda r: object_value(r["recording"]).update(frame_count=481)),
            ("metadata_limit_exceeded", lambda r: object_value(r["recording"]).update(byte_size=512 * 1024 * 1024 + 1)),
            ("metadata_limit_exceeded", lambda r: object_value(r["requested"]).update(max_duration_ms=900_001)),
            ("path_only_evidence", lambda r: object_value(r["recording"]).update(path="/tmp/capture.webm")),
            ("path_only_evidence", lambda r: r.update(recording_uri="file:///tmp/capture.webm")),
            ("secret_bearing_metadata", lambda r: object_value(r["producer"]).update(producer_id="https://user@recorder.example.test/x")),
            ("secret_bearing_metadata", lambda r: object_value(r["producer"]).update(version="Bearer abc")),
            ("malformed_receipt", lambda r: object_value(r["recording"]).pop("sha256")),
            ("malformed_receipt", lambda r: r.update(schema_version="host_motion_capture_receipt/v2")),
        ]
        for reason, mutate in cases:
            with self.subTest(reason=reason):
                receipt = motion_receipt(self.plan)
                mutate(receipt)
                self.assert_refused(reason, self.plan, receipt)
        self.assert_refused("plan_invalid", {"mode": "matrix"}, motion_receipt(self.plan))

    def test_store_holds_metadata_only_and_refuses_media_bytes(self) -> None:
        receipt = motion_receipt(self.plan)
        receipt["recording"] = {**object_value(receipt["recording"]), "data": "AAAAHGZ0eXBpc29t"}
        self.assert_refused("malformed_receipt", self.plan, receipt)
        receipt = motion_receipt(self.plan)
        object_list(object_value(receipt["contact_sheet"])["tiles"])[0]["frame"] = "iVBORw0KGgo="
        self.assert_refused("malformed_receipt", self.plan, receipt)

        record = import_motion_capture(self.root, self.plan, motion_receipt(self.plan))
        files = self.managed_files()
        self.assertEqual([path.name for path in files], [f"{record['capture_id']}.json"])
        self.assertEqual(files[0].parent.name, self.plan["run_id"])
        self.assertEqual(files[0].parent.parent, self.root / ".omh" / "web-visual-qa" / "motion-captures")
        stored = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(set(stored), {"schema_version", "project_identity", "plan", "receipt", "record"})
        self.assertEqual(stored["schema_version"], "web_qa_motion_capture_store/v1")
        self.assertLess(files[0].stat().st_size, 32_768)

    def test_tampered_stored_record_is_refused_on_read(self) -> None:
        record = import_motion_capture(self.root, self.plan, motion_receipt(self.plan))
        path = self.managed_files()[0]
        stored = json.loads(path.read_text(encoding="utf-8"))
        object_value(stored["record"]["lineage"])["viewport_id"] = "mobile"
        path.write_text(json.dumps(stored), encoding="utf-8", newline="\n")
        with self.assertRaises(MotionCaptureImportError) as caught:
            read_motion_capture(self.root, str(self.plan["run_id"]), str(record["capture_id"]))
        self.assertEqual(caught.exception.reason, "stored_record_invalid")


class MotionEvidenceGateTests(unittest.TestCase):
    """The gate cites a record for exactly the captured condition and nothing broader."""

    revision = "4c5c1e8f9a2b3c4d5e6f7081920a1b2c3d4e5f60"

    def observation(self, plan: dict[str, object], verdict: str = "PASS") -> dict[str, object]:
        return {"run_id": plan["run_id"], "plan_digest": plan["plan_digest"], "verdict": verdict, "blockers": [] if verdict == "PASS" else ["x:host_cell_missing"]}

    def status(self, plan: dict[str, object], records: list[dict[str, object]], cell: dict[str, object]) -> str:
        gated = project_motion_evidence(plan, self.observation(plan), records, [str(cell["cell_id"])])
        return str(object_list(gated["motion_cells"])[0]["status"])

    def test_record_covers_only_its_route_state_viewport_and_browser(self) -> None:
        plan = plan_for(self.revision, extra_state=True)
        captured = cell_where(plan, route_id="checkout", state_id="anonymous", viewport_id="desktop", browser_id="chrome-126")
        record = build_motion_interaction_capture(plan, motion_receipt(plan, captured))
        self.assertEqual(self.status(plan, [record], captured), "observed")
        neighbours = {
            "route": cell_where(plan, route_id="catalog", state_id="anonymous", viewport_id="desktop", browser_id="chrome-126"),
            "state": cell_where(plan, route_id="checkout", state_id="signed-in", viewport_id="desktop", browser_id="chrome-126"),
            "viewport": cell_where(plan, route_id="checkout", state_id="anonymous", viewport_id="mobile", browser_id="chrome-126"),
            "browser": cell_where(plan, route_id="checkout", state_id="anonymous", viewport_id="desktop", browser_id="firefox-127"),
        }
        for label, cell in neighbours.items():
            with self.subTest(other=label):
                self.assertEqual(self.status(plan, [record], cell), "blocked")

    def test_record_cannot_satisfy_another_revision_or_round(self) -> None:
        plan = plan_for(self.revision)
        record = build_motion_interaction_capture(plan, motion_receipt(plan))
        for label, other in (("revision", plan_for("b" * 40)), ("round", plan_for(self.revision, round_ordinal=2))):
            with self.subTest(other=label):
                cell = object_list(other["matrix"])[0]
                self.assertEqual(self.status(other, [record], cell), "blocked")
                forged = deepcopy(record)
                forged["run_id"] = other["run_id"]
                forged["plan_digest"] = other["plan_digest"]
                self.assertEqual(self.status(other, [forged], cell), "blocked", "lineage fields must still match")

    def test_motion_evidence_never_raises_the_observation_verdict(self) -> None:
        plan = plan_for(self.revision)
        cell_id = str(object_list(plan["matrix"])[0]["cell_id"])
        record = build_motion_interaction_capture(plan, motion_receipt(plan))
        covered = project_motion_evidence(plan, self.observation(plan), [record], [cell_id])
        self.assertEqual((covered["verdict"], covered["blockers"]), ("PASS", []))
        self.assertEqual(object_list(covered["motion_cells"])[0]["capture_ids"], [record["capture_id"]])
        missing = project_motion_evidence(plan, self.observation(plan), [], [cell_id])
        self.assertEqual(missing["verdict"], "BLOCK")
        self.assertEqual(missing["blockers"], [f"{cell_id}:motion_interaction_capture:motion_interaction_capture_missing"])
        for base in ("BLOCK", "REVISE"):
            with self.subTest(base=base):
                self.assertEqual(project_motion_evidence(plan, self.observation(plan, base), [record], [cell_id])["verdict"], base)
        self.assertEqual(project_motion_evidence(plan, self.observation(plan), [], [])["verdict"], "PASS", "motion out of scope adds no blocker")


class MotionGateStoredRunTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q"], cwd=self.root, check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], cwd=self.root, check=True)
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()
        self.plan = plan_for(revision, single_cell=True)
        receipt, digest = capture_receipt(self.plan)
        image = self.root / "capture.png"
        image.write_bytes(PNG)
        imported = import_web_qa_observation(self.root, self.plan, receipt, {digest: image})
        self.assertEqual(object_value(imported["observation"])["verdict"], "PASS")
        self.cell_id = str(object_list(self.plan["matrix"])[0]["cell_id"])

    def test_stored_run_blocks_until_its_motion_cell_is_imported(self) -> None:
        run_id = str(self.plan["run_id"])
        self.assertEqual(motion_capture_gate(self.root, run_id, [self.cell_id])["verdict"], "BLOCK")
        record = import_motion_capture(self.root, self.plan, motion_receipt(self.plan))
        gated = motion_capture_gate(self.root, run_id, [self.cell_id])
        self.assertEqual(gated["verdict"], "PASS")
        self.assertEqual(object_list(gated["motion_cells"])[0]["capture_ids"], [record["capture_id"]])

    def test_cli_motion_import_show_and_gate(self) -> None:
        parser = argparse.ArgumentParser()
        web_qa = parser.add_subparsers(dest="top", required=True).add_parser("web-qa")
        add_web_qa_observation_commands(web_qa.add_subparsers(dest="web_qa", required=True))
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8", newline="\n")
        receipt_path = self.root / "motion.json"
        receipt_path.write_text(json.dumps(motion_receipt(self.plan)), encoding="utf-8", newline="\n")

        def run(*argv: str) -> dict[str, object]:
            args = parser.parse_args(["web-qa", "observation", "motion", *argv])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            return json.loads(output.getvalue())

        root = str(self.root)
        record = run("import", "--project-root", root, "--plan-json", str(plan_path), "--receipt-json", str(receipt_path))
        shown = run("show", "--project-root", root, "--run-id", str(self.plan["run_id"]), "--capture-id", str(record["capture_id"]))
        self.assertEqual(shown, record)
        gated = run("gate", "--project-root", root, "--run-id", str(self.plan["run_id"]), "--motion-cell", self.cell_id)
        self.assertEqual(gated["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
