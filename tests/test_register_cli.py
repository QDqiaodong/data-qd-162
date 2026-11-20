"""Real-process tests for the parcel-register console script (schema v5).

These tests spawn ``python -m parcel_rating.register_cli`` subprocesses so the
persistence guarantees are verified across process boundaries, including:

* envelope/SHA provenance and stdout/file equivalence;
* read-only snapshots that never create or modify a database;
* cross-process retry returning the original point-in-time receipt after the
  database version has advanced;
* a report-publication failure *after* the SQLite commit, followed by replay
  recovery with no double booking;
* same-expected-version concurrent processes with exactly one winner;
* path collisions, symlinks, malformed sources and preserved old reports;
* the legacy ``parcel-rating`` entry points remain untouched.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = "parcel_rating.register_cli"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

TARIFF = {
    "service": "ground", "zone": "Z", "base_cents": 0, "step_grams": 500,
    "per_step_cents": 100, "divisor_cm3_per_kg": 10 ** 9,
    "max_girth_mm": 1000, "remote_cents": 0, "fuel_bp": 0,
    "insurance_bp": 0, "insurance_free_cents": 0,
}


def shipment(pid):
    return {"id": pid, "service": "ground", "zone": "Z",
            "weight_grams": 500, "length_mm": 100, "width_mm": 100,
            "height_mm": 100, "declared_cents": 0, "remote": False}


def fund(rid, version, entries):
    return {"request_id": rid, "expected_version": version, "action": "fund",
            "entries": entries}


def amend(rid, version, upserts=(), cancels=()):
    return {"request_id": rid, "expected_version": version,
            "action": "amend", "upserts": list(upserts),
            "cancels": list(cancels)}


def upsert(account, manifest, pid, rate=100):
    tariff = dict(TARIFF, per_step_cents=rate)
    return {"account_id": account, "manifest_id": manifest,
            "shipment": shipment(pid), "tariff": tariff}


def seal(rid, version, manifest_ids):
    return {"request_id": rid, "expected_version": version,
            "action": "seal", "manifest_ids": list(manifest_ids)}


class RegisterCliTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.dir = Path(self.temporary.name)
        self.db = self.dir / "register.db"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def invoke(self, *args, expect_code=None):
        completed = subprocess.run(
            [sys.executable, "-s", "-m", MODULE, *map(str, args)],
            capture_output=True, text=True, timeout=120, env=ENV)
        if expect_code is not None:
            self.assertEqual(completed.returncode, expect_code,
                             completed.stderr)
        return completed

    def apply_request(self, name, payload):
        source = self.write(name + ".json", payload)
        output = self.dir / (name + ".report.json")
        completed = self.invoke("--database", self.db, "--source", source,
                                "--output", output, expect_code=0)
        report = json.loads(output.read_text())
        self.assertEqual(json.loads(completed.stdout), report)
        self.assertEqual(report["schema_version"], 5)
        self.assertEqual(report["source_sha256"],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        return report, output

    def snapshot_via_cli(self, name="snap"):
        output = self.dir / (name + ".json")
        completed = self.invoke("--database", self.db, "--snapshot",
                                "--output", output, expect_code=0)
        report = json.loads(output.read_text())
        self.assertEqual(json.loads(completed.stdout), report)
        self.assertEqual(report["schema_version"], 5)
        self.assertIsNone(report["source_sha256"])
        return report["result"]


class EnvelopeAndPersistenceTests(RegisterCliTestBase):
    def test_apply_envelope_and_snapshot_envelope(self):
        report, _ = self.apply_request(
            "fund", fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        self.assertEqual(set(report), {"schema_version", "source_sha256",
                                       "result"})
        receipt = report["result"]
        self.assertEqual(set(receipt), {"request_id", "version", "snapshot"})
        self.assertEqual((receipt["request_id"], receipt["version"]),
                         ("f1", 1))
        state = self.snapshot_via_cli()
        self.assertEqual(set(state), {"version", "accounts", "bookings",
                                      "manifests"})
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["accounts"][0]["available_cents"], 100)

    def test_snapshot_forbids_source_and_creates_nothing(self):
        source = self.write("fund.json",
                            fund("f1", 0, [{"account_id": "A", "cents": 1}]))
        missing_db = self.dir / "missing.db"
        result = self.invoke("--database", missing_db, "--snapshot",
                             "--output", self.dir / "out.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse((self.dir / "out.json").exists())
        self.assertFalse(missing_db.exists())
        # --snapshot and --source are mutually exclusive.
        result = self.invoke("--database", self.db, "--snapshot",
                             "--source", source,
                             "--output", self.dir / "out.json")
        self.assertEqual(result.returncode, 2)

    def test_full_linked_acceptance_through_real_processes(self):
        self.apply_request("fund", fund("f1", 0, [
            {"account_id": "A", "cents": 100},
            {"account_id": "B", "cents": 100}]))
        self.apply_request("amend1", amend("a1", 1, [
            upsert("A", "M", "P"), upsert("B", "N", "Q")]))
        self.apply_request("swap", amend("a2", 2, [
            upsert("B", "N", "P"), upsert("A", "M", "Q")]))
        self.apply_request("seal", seal("s1", 3, ["M"]))
        state = self.snapshot_via_cli("after-seal")
        accounts = {row["account_id"]: row for row in state["accounts"]}
        self.assertEqual((accounts["A"]["held_cents"],
                          accounts["A"]["spent_cents"]), (0, 100))
        self.assertEqual((accounts["B"]["held_cents"],
                          accounts["B"]["available_cents"]), (100, 0))
        # The mixed cancel fails in a separate process and changes nothing.
        before = json.dumps(state, sort_keys=True)
        bad = self.write("bad-cancel.json",
                         amend("x1", 4, cancels=["P", "Q"]))
        failed = self.invoke("--database", self.db, "--source", bad,
                             "--output", self.dir / "bad.report.json")
        self.assertEqual(failed.returncode, 2)
        self.assertFalse((self.dir / "bad.report.json").exists())
        after = self.snapshot_via_cli("after-failure")
        self.assertEqual(json.dumps(after, sort_keys=True), before)
        bookings = {row["id"]: row for row in after["bookings"]}
        self.assertEqual((bookings["P"]["status"], bookings["Q"]["status"]),
                         ("held", "shipped"))
        # Cancelling P alone in another process releases B.
        self.apply_request("cancel-p",
                           amend("a3", 4, cancels=["P"]))
        final = self.snapshot_via_cli("final")
        accounts = {row["account_id"]: row for row in final["accounts"]}
        self.assertEqual((accounts["B"]["held_cents"],
                          accounts["B"]["available_cents"]), (0, 100))
        self.assertEqual(accounts["A"]["spent_cents"], 100)


class CrossProcessRetryTests(RegisterCliTestBase):
    def test_retry_after_version_advance_returns_original_snapshot(self):
        first_request = fund("f1", 0, [
            {"account_id": "A", "cents": 100}, {"account_id": "B", "cents": 50}])
        first_source = self.write("f1.json", first_request)
        first_output = self.dir / "f1.report.json"
        self.invoke("--database", self.db, "--source", first_source,
                    "--output", first_output, expect_code=0)
        original = json.loads(first_output.read_text())
        # Advance the version in separate processes with later requests.
        self.apply_request("f2", fund("f2", 1, [
            {"account_id": "A", "cents": 7}]))
        self.apply_request("seal-empty-setup", amend("a1", 2, [
            upsert("A", "M", "P")]))
        self.apply_request("seal", seal("s1", 3, ["M"]))
        current = self.snapshot_via_cli("current")
        self.assertEqual(current["version"], 4)
        # Replay the original bytes in a brand-new process.
        replay_output = self.dir / "f1-replay.report.json"
        self.invoke("--database", self.db, "--source", first_source,
                    "--output", replay_output, expect_code=0)
        replayed = json.loads(replay_output.read_text())
        # The receipt is the original point-in-time receipt...
        self.assertEqual(replayed["result"], original["result"])
        self.assertEqual(replayed["result"]["snapshot"]["version"], 1)
        # ...while the digest reflects this invocation's source bytes
        # (identical bytes here, so the digest is equal too).
        self.assertEqual(replayed["source_sha256"],
                         hashlib.sha256(first_source.read_bytes()).hexdigest())
        # No double posting and the live database keeps advancing.
        again = self.snapshot_via_cli("after-replay")
        self.assertEqual(again, current)
        self.assertEqual(
            {a["account_id"]: a["funded_cents"] for a in again["accounts"]},
            {"A": 107, "B": 50})

    def test_same_id_different_content_rejected_in_new_process(self):
        self.apply_request("f1", fund("f1", 0, [
            {"account_id": "A", "cents": 100}]))
        before = self.snapshot_via_cli()
        conflict = self.write("f1-conflict.json",
                              fund("f1", 0, [{"account_id": "A", "cents": 99}]))
        result = self.invoke("--database", self.db, "--source", conflict,
                             "--output", self.dir / "conflict.report.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("request_id", result.stderr)
        self.assertEqual(self.snapshot_via_cli(), before)


class PublicationFailureTests(RegisterCliTestBase):
    def test_commit_then_export_failure_recovers_without_double_booking(self):
        self.apply_request("f1", fund("f1", 0, [
            {"account_id": "A", "cents": 100}]))
        request = fund("f2", 1, [{"account_id": "A", "cents": 25}])
        source = self.write("f2.json", request)
        # Force publication to fail *after* the SQLite commit by making the
        # output's parent a regular file: common.save's mkdir then raises
        # FileExistsError, while the database transaction has already committed.
        blocker = self.dir / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        blocked_output = blocker / "report.json"
        completed = self.invoke("--database", self.db, "--source", source,
                                "--output", blocked_output)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("report publication failed after commit",
                      completed.stderr)
        self.assertIn("committed request_id=f2", completed.stderr)
        self.assertIn("version=2", completed.stderr)
        # The database committed exactly once.
        state = self.snapshot_via_cli("after-failed-export")
        self.assertEqual(state["version"], 2)
        account = state["accounts"][0]
        self.assertEqual((account["funded_cents"],
                          account["available_cents"]), (125, 125))
        # Replaying the identical request restores the original receipt and
        # does not book the 25 cents a second time.
        recovered_output = self.dir / "f2.recovered.json"
        self.invoke("--database", self.db, "--source", source,
                    "--output", recovered_output, expect_code=0)
        recovered = json.loads(recovered_output.read_text())
        self.assertEqual(recovered["result"]["version"], 2)
        self.assertEqual(
            recovered["result"]["snapshot"]["accounts"][0]["funded_cents"],
            125)
        final = self.snapshot_via_cli("final")
        self.assertEqual(final["version"], 2)
        self.assertEqual(final["accounts"][0]["funded_cents"], 125)


class ConcurrencyCliTests(RegisterCliTestBase):
    def test_concurrent_same_version_processes_have_one_winner(self):
        self.apply_request("seed", fund("seed", 0, [
            {"account_id": "A", "cents": 10 ** 9}]))
        processes = []
        outputs = []
        for i in range(6):
            source = self.write(f"c{i}.json", fund(
                f"c{i}", 1, [{"account_id": "A", "cents": 1}]))
            output = self.dir / f"c{i}.report.json"
            outputs.append(output)
            processes.append(subprocess.Popen(
                [sys.executable, "-s", "-m", MODULE, "--database",
                 str(self.db), "--source", str(source), "--output",
                 str(output)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV))
        results = [process.communicate() for process in processes]
        codes = [process.returncode for process in processes]
        self.assertEqual(sorted(codes).count(0), 1)
        self.assertEqual(sorted(codes).count(2), 5)
        # Every loser reports a version conflict rather than a lost update.
        for stdout, stderr in results:
            if stderr:
                self.assertIn("version conflict", stderr.decode())
        state = self.snapshot_via_cli()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["accounts"][0]["funded_cents"], 10 ** 9 + 1)
        self.assertEqual(len(list(self.dir.glob("c*.report.json"))), 1)


class PathAndMalformedInputTests(RegisterCliTestBase):
    def test_collisions_symlinks_and_report_preservation(self):
        source = self.write("f1.json",
                            fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        # Source/database/output collisions.
        self.assertEqual(self.invoke(
            "--database", self.db, "--source", source,
            "--output", source).returncode, 2)
        self.assertEqual(self.invoke(
            "--database", source, "--source", source,
            "--output", self.dir / "x.json").returncode, 2)
        # Symlinked database and output paths are refused up front.
        link_db = self.dir / "link.db"
        link_db.symlink_to(self.db)
        self.assertEqual(self.invoke(
            "--database", link_db, "--source", source,
            "--output", self.dir / "x.json").returncode, 2)
        link_out = self.dir / "link.out.json"
        link_out.symlink_to(self.dir / "target")
        self.assertEqual(self.invoke(
            "--database", self.db, "--source", source,
            "--output", link_out).returncode, 2)
        # Malformed sources keep any previous report intact.
        self.apply_request("good", fund("f1", 0, [
            {"account_id": "A", "cents": 100}]))
        good_report = (self.dir / "good.report.json").read_bytes()
        for raw in ("{", '{"x":1,"x":2}', '{"x":NaN}', "[]",
                    json.dumps({"request_id": "z", "expected_version": True,
                                "action": "fund",
                                "entries": [{"account_id": "A",
                                             "cents": 1}]})):
            bad = self.dir / "bad.json"
            bad.write_text(raw, encoding="utf-8")
            failed = self.invoke(
                "--database", self.db, "--source", bad,
                "--output", self.dir / "good.report.json")
            self.assertEqual(failed.returncode, 2)
            self.assertEqual((self.dir / "good.report.json").read_bytes(),
                             good_report)

    def test_duplicate_json_keys_rejected_by_shared_reader(self):
        source = self.dir / "dup.json"
        source.write_text(
            '{"request_id":"z","expected_version":0,"action":"fund",'
            '"entries":[{"account_id":"A","cents":1}],"action":"fund"}',
            encoding="utf-8")
        result = self.invoke("--database", self.db, "--source", source,
                             "--output", self.dir / "dup.report.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate JSON key", result.stderr)


class LegacyEntryPointTests(RegisterCliTestBase):
    def test_parcel_rating_four_modes_remain_unchanged(self):
        # The new console script is independent; the old CLI run signature and
        # the four schema versions keep their exact contracts.
        legacy_env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        source = ROOT / "fixtures" / "input.json"
        output = self.dir / "v1.json"
        completed = subprocess.run(
            [sys.executable, "-s", "-m", "parcel_rating.cli",
             "--source", str(source), "--output", str(output)],
            capture_output=True, text=True, env=legacy_env, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["result"]["total_cents"], 1742)
        for flag, fixture, schema in (
                ("--plan", "plan_input.json", 2),
                ("--temporal", "temporal_input.json", 3),
                ("--reconcile", "reconcile_input.json", 4)):
            output = self.dir / f"schema{schema}.json"
            completed = subprocess.run(
                [sys.executable, "-s", "-m", "parcel_rating.cli", flag,
                 "--source", str(ROOT / "fixtures" / fixture),
                 "--output", str(output)],
                capture_output=True, text=True, env=legacy_env, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output.read_text())["schema_version"],
                             schema)


if __name__ == "__main__":
    unittest.main()
