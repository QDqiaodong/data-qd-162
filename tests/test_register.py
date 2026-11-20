"""Schema v5 register API tests: SQLite facts, idempotency and invariants."""
import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from fractions import Fraction

from parcel_rating import register
from parcel_rating.register import RegisterError, apply, snapshot

TARIFF_100 = {
    "service": "ground", "zone": "Z", "base_cents": 0, "step_grams": 500,
    "per_step_cents": 100, "divisor_cm3_per_kg": 10 ** 9,
    "max_girth_mm": 1000, "remote_cents": 0, "fuel_bp": 0,
    "insurance_bp": 0, "insurance_free_cents": 0,
}


def tariff(per_step=100, **overrides):
    row = dict(TARIFF_100)
    row["per_step_cents"] = per_step
    row.update(overrides)
    return row


def shipment(pid, **overrides):
    row = {"id": pid, "service": "ground", "zone": "Z", "weight_grams": 500,
           "length_mm": 100, "width_mm": 100, "height_mm": 100,
           "declared_cents": 0, "remote": False}
    row.update(overrides)
    return row


def upsert(account, manifest, pid, rate=100, **ship_overrides):
    return {"account_id": account, "manifest_id": manifest,
            "shipment": shipment(pid, **ship_overrides),
            "tariff": tariff(per_step=rate)}


def fund(rid, version, entries):
    return {"request_id": rid, "expected_version": version, "action": "fund",
            "entries": entries}


def amend(rid, version, upserts=(), cancels=()):
    return {"request_id": rid, "expected_version": version, "action": "amend",
            "upserts": list(upserts), "cancels": list(cancels)}


def seal(rid, version, manifest_ids):
    return {"request_id": rid, "expected_version": version,
            "action": "seal", "manifest_ids": list(manifest_ids)}


class RegisterTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temporary.name, "register.db")

    def tearDown(self):
        self.temporary.cleanup()

    def accounts_by_id(self, snap):
        return {row["account_id"]: row for row in snap["accounts"]}

    def bookings_by_id(self, snap):
        return {row["id"]: row for row in snap["bookings"]}

    def manifests_by_id(self, snap):
        return {row["manifest_id"]: row for row in snap["manifests"]}

    def assert_invariants(self, snap):
        # Every account row carries exact integer conservation.
        held = {a["account_id"]: 0 for a in snap["accounts"]}
        spent = {a["account_id"]: 0 for a in snap["accounts"]}
        for booking in snap["bookings"]:
            self.assertEqual(set(booking),
                             {"id", "account_id", "manifest_id", "status",
                              "shipment", "tariff", "quote"})
            self.assertIn(booking["status"], ("held", "cancelled", "shipped"))
            if booking["status"] == "held":
                held[booking["account_id"]] += booking["quote"]["total_cents"]
            elif booking["status"] == "shipped":
                spent[booking["account_id"]] += booking["quote"]["total_cents"]
        for account in snap["accounts"]:
            self.assertEqual(account["held_cents"], held[account["account_id"]])
            self.assertEqual(account["spent_cents"], spent[account["account_id"]])
            self.assertEqual(
                account["available_cents"],
                account["funded_cents"] - account["held_cents"]
                - account["spent_cents"])
            self.assertGreaterEqual(account["available_cents"], 0)
        # Every held parcel sits in exactly one open manifest; shipped parcels
        # stay in their sealed manifest; cancelled parcels are never members.
        manifests = self.manifests_by_id(snap)
        manifest_members = {mid: [] for mid in manifests}
        for booking in snap["bookings"]:
            mid = booking["manifest_id"]
            self.assertIn(mid, manifests)
            if booking["status"] == "held":
                self.assertEqual(manifests[mid]["status"], "open")
                manifest_members[mid].append(booking["id"])
            elif booking["status"] == "shipped":
                self.assertEqual(manifests[mid]["status"], "sealed")
                manifest_members[mid].append(booking["id"])
        for mid, row in manifests.items():
            self.assertEqual(row["member_ids"], sorted(manifest_members[mid]))
            self.assertEqual(
                row["total_cents"],
                sum(self.bookings_by_id(snap)[bid]["quote"]["total_cents"]
                    for bid in row["member_ids"]))
            if row["status"] == "open":
                self.assertIsNone(row["sealed_version"])
            else:
                self.assertIsInstance(row["sealed_version"], int)
        # Global money conservation across the whole ledger.
        self.assertEqual(
            sum(a["funded_cents"] for a in snap["accounts"]),
            sum(a["held_cents"] + a["spent_cents"] + a["available_cents"]
                for a in snap["accounts"]))


class LinkedAcceptanceTests(RegisterTestBase):
    def test_swap_seal_failed_mixed_cancel_and_cleanup(self):
        # A/B each fund 100; P holds 100 on A/M, Q holds 100 on B/N.
        receipt = apply(self.db, fund("f1", 0, [
            {"account_id": "A", "cents": 100},
            {"account_id": "B", "cents": 100}]))
        self.assertEqual(receipt["version"], 1)
        self.assertEqual(receipt["snapshot"]["version"], 1)
        apply(self.db, amend("a1", 1, [
            upsert("A", "M", "P"), upsert("B", "N", "Q")]))
        snap = snapshot(self.db)
        self.assertEqual(snap["version"], 2)
        self.assertEqual(self.accounts_by_id(snap)["A"]["held_cents"], 100)
        self.assertEqual(self.manifests_by_id(snap)["M"]["total_cents"], 100)

        # One amend swaps the parcels across both accounts and manifests while
        # quotes stay 100: it must succeed as a batch, never fail on a
        # transiently negative intermediate balance.
        swapped = amend("a2", 2, [
            upsert("B", "N", "P"), upsert("A", "M", "Q")])
        apply(self.db, swapped)
        snap = snapshot(self.db)
        bookings = self.bookings_by_id(snap)
        self.assertEqual((bookings["P"]["account_id"],
                          bookings["P"]["manifest_id"]), ("B", "N"))
        self.assertEqual((bookings["Q"]["account_id"],
                          bookings["Q"]["manifest_id"]), ("A", "M"))
        for account in snap["accounts"]:
            self.assertEqual(account["available_cents"], 0)
            self.assertEqual(account["held_cents"], 100)

        # Sealing M moves Q's frozen quote from A.held to A.spent.
        apply(self.db, seal("s1", 3, ["M"]))
        snap = snapshot(self.db)
        accounts = self.accounts_by_id(snap)
        self.assertEqual(
            (accounts["A"]["held_cents"], accounts["A"]["spent_cents"]),
            (0, 100))
        self.assertEqual(
            (accounts["B"]["held_cents"], accounts["B"]["spent_cents"],
             accounts["B"]["available_cents"]),
            (100, 0, 0))
        sealed = self.manifests_by_id(snap)["M"]
        self.assertEqual((sealed["status"], sealed["member_ids"],
                          sealed["total_cents"], sealed["sealed_version"]),
                         ("sealed", ["Q"], 100, 4))
        bookings = self.bookings_by_id(snap)
        self.assertEqual(bookings["Q"]["status"], "shipped")
        self.assertEqual(bookings["P"]["status"], "held")

        # A batch cancelling held P together with shipped Q must fail as a
        # whole: P stays held, Q stays shipped, version and receipts unchanged.
        before = snapshot(self.db)
        with self.assertRaises(RegisterError):
            apply(self.db, amend("x1", 4, cancels=["P", "Q"]))
        after = snapshot(self.db)
        self.assertEqual(before, after)
        self.assertEqual(after["version"], 4)
        bookings = self.bookings_by_id(after)
        self.assertEqual(bookings["P"]["status"], "held")
        self.assertEqual(bookings["Q"]["status"], "shipped")
        self.assertEqual(self.accounts_by_id(after)["B"]["available_cents"], 0)
        # The failed request occupies no request id.
        rows = list(sqlite3.connect(self.db).execute(
            "SELECT request_id FROM requests"))
        self.assertEqual({r[0] for r in rows}, {"f1", "a1", "a2", "s1"})

        # Cancelling P alone releases B's hold; A's spent amount is untouched.
        apply(self.db, amend("a3", 4, cancels=["P"]))
        snap = snapshot(self.db)
        accounts = self.accounts_by_id(snap)
        self.assertEqual(
            (accounts["B"]["held_cents"], accounts["B"]["available_cents"]),
            (0, 100))
        self.assertEqual(accounts["A"]["spent_cents"], 100)
        # Cancelling every member keeps the empty open manifest in the book.
        self.assertEqual(self.manifests_by_id(snap)["N"]["member_ids"], [])
        self.assertEqual(self.manifests_by_id(snap)["N"]["status"], "open")
        # The cancelled parcel remains auditable with its last accepted fact.
        cancelled = self.bookings_by_id(snap)["P"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["quote"]["total_cents"], 100)
        self.assert_invariants(snap)


class QuoteLifecycleTests(RegisterTestBase):
    def test_failed_replacement_keeps_old_quote_until_topup(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        apply(self.db, amend("a1", 1, [upsert("A", "M", "P", rate=100)]))
        # Reprice the same parcel from 100 to 120 while only 100 is funded:
        # the whole batch fails and the old 100 quote/hold survives.
        before = snapshot(self.db)
        with self.assertRaises(RegisterError):
            apply(self.db, amend("a2", 2, [upsert("A", "M", "P", rate=120)]))
        self.assertEqual(snapshot(self.db), before)
        self.assertEqual(
            self.bookings_by_id(snapshot(self.db))["P"]["quote"]["total_cents"],
            100)
        # The failed request id can be reused after correcting the request.
        apply(self.db, fund("f2", 2, [{"account_id": "A", "cents": 20}]))
        apply(self.db, amend("a2", 3, [upsert("A", "M", "P", rate=120)]))
        snap = snapshot(self.db)
        booking = self.bookings_by_id(snap)["P"]
        self.assertEqual(booking["quote"]["total_cents"], 120)
        account = self.accounts_by_id(snap)["A"]
        self.assertEqual((account["held_cents"], account["available_cents"]),
                         (120, 0))

    def test_sealed_amounts_are_permanent_even_with_new_tariff(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 120}]))
        apply(self.db, amend("a1", 1, [upsert("A", "M", "P", rate=120)]))
        apply(self.db, seal("s1", 2, ["M"]))
        # Neither replacement, movement nor cancellation touches a sealed member.
        for bad in (
                amend("a2", 3, [upsert("A", "M", "P", rate=100)]),
                amend("a3", 3, [upsert("A", "N", "P", rate=120)]),
                amend("a4", 3, cancels=["P"])):
            with self.assertRaises(RegisterError):
                apply(self.db, bad)
        snap = snapshot(self.db)
        self.assertEqual(snap["version"], 3)
        booking = self.bookings_by_id(snap)["P"]
        self.assertEqual((booking["status"], booking["manifest_id"],
                          booking["quote"]["total_cents"]),
                         ("shipped", "M", 120))
        self.assertEqual(self.accounts_by_id(snap)["A"]["spent_cents"], 120)

    def test_zero_rated_parcel_is_a_real_member_but_oversize_is_rejected(self):
        free_tariff = tariff(per_step=0)
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 1}]))
        apply(self.db, amend("a1", 1, [
            {"account_id": "A", "manifest_id": "M",
             "shipment": shipment("Z"), "tariff": free_tariff}]))
        snap = snapshot(self.db)
        zero = self.bookings_by_id(snap)["Z"]
        self.assertEqual(zero["quote"]["status"], "rated")
        self.assertEqual(zero["quote"]["total_cents"], 0)
        self.assertEqual(self.manifests_by_id(snap)["M"]["member_ids"], ["Z"])
        self.assertEqual(self.accounts_by_id(snap)["A"]["available_cents"], 1)
        # An oversize parcel cannot be registered at all and cannot masquerade
        # as a zero-cents member; the batch leaves no side effect.
        oversize = shipment("BIG", length_mm=900)  # girth 1300 > 1000
        before = snapshot(self.db)
        with self.assertRaisesRegex(RegisterError, "oversize"):
            apply(self.db, amend("o1", 2, [
                upsert("A", "M", "OK2"),
                {"account_id": "A", "manifest_id": "M",
                 "shipment": oversize, "tariff": TARIFF_100}]))
        self.assertEqual(snapshot(self.db), before)

    def test_half_cent_rounding_and_step_boundaries_match_v1(self):
        # base 100 with fuel 50bp is exactly 0.5 cent -> half-up to 1.
        fuel_tariff = tariff(per_step=100, fuel_bp=50)
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 10 ** 6}]))
        apply(self.db, amend("a1", 1, [
            {"account_id": "A", "manifest_id": "M",
             "shipment": shipment("F"), "tariff": fuel_tariff}]))
        quote = self.bookings_by_id(snapshot(self.db))["F"]["quote"]
        self.assertEqual((quote["base_cents"], quote["fuel_cents"],
                          quote["total_cents"]), (100, 1, 101))
        # Insurance half-up on the exact tie as well.
        insured = tariff(per_step=0, insurance_bp=50)
        apply(self.db, amend("a2", 2, [
            {"account_id": "A", "manifest_id": "M",
             "shipment": shipment("I", declared_cents=100),
             "tariff": insured}]))
        quote = self.bookings_by_id(snapshot(self.db))["I"]["quote"]
        self.assertEqual(quote["insurance_cents"], 1)
        # Step boundary: exactly 500g bills one step, 501g bills two.
        apply(self.db, amend("a3", 3, [upsert("A", "M", "S1", weight_grams=500)]))
        apply(self.db, amend("a4", 4, [upsert("A", "M", "S2", weight_grams=501)]))
        bookings = self.bookings_by_id(snapshot(self.db))
        self.assertEqual(bookings["S1"]["quote"]["weight_steps"], 1)
        self.assertEqual(bookings["S2"]["quote"]["weight_steps"], 2)
        self.assertEqual(bookings["S2"]["quote"]["chargeable_weight_grams"], 1000)


class RequestContractTests(RegisterTestBase):
    def test_canonical_retry_returns_original_receipt_after_advance(self):
        first_request = fund("f1", 0, [
            {"account_id": "A", "cents": 100}, {"account_id": "B", "cents": 50}])
        original = apply(self.db, copy.deepcopy(first_request))
        # Advance the database with unrelated later requests.
        apply(self.db, fund("f2", 1, [{"account_id": "A", "cents": 25}]))
        apply(self.db, fund("f3", 2, [{"account_id": "A", "cents": 25}]))
        current = snapshot(self.db)
        self.assertEqual(current["version"], 3)
        # Replaying the identical request returns the first receipt and its
        # point-in-time snapshot, never a second posting.
        replayed = apply(self.db, copy.deepcopy(first_request))
        self.assertEqual(replayed, original)
        self.assertEqual(replayed["snapshot"]["version"], 1)
        self.assertEqual(snapshot(self.db), current)
        self.assertEqual(
            self.accounts_by_id(snapshot(self.db))["A"]["funded_cents"], 150)

    def test_array_and_object_key_reordering_is_the_same_request(self):
        entries = [{"account_id": "A", "cents": 100},
                   {"account_id": "B", "cents": 200}]
        request = fund("f1", 0, entries)
        original = apply(self.db, copy.deepcopy(request))
        reordered = {
            "entries": list(reversed(request["entries"])),
            "action": "fund", "expected_version": 0, "request_id": "f1"}
        # Re-serialize an entry with shuffled internal key order.
        reordered["entries"][0] = {
            "cents": reordered["entries"][0]["cents"],
            "account_id": reordered["entries"][0]["account_id"]}
        self.assertEqual(apply(self.db, reordered), original)
        # Amend upserts/cancels and seal lists are likewise order-insensitive.
        apply(self.db, fund("f2", 1, [{"account_id": "B", "cents": 300}]))
        first = amend("a1", 2, [upsert("A", "M", "P"), upsert("B", "N", "Q")])
        apply(self.db, first)
        shuffled = amend("a1", 2,
                         [first["upserts"][1], first["upserts"][0]])
        self.assertEqual(apply(self.db, shuffled)["request_id"], "a1")

    def test_same_id_different_content_is_rejected_without_rewrite(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        before = snapshot(self.db)
        # Different entries and a different expected_version both participate
        # in canonical request identity.
        for variant in (
                fund("f1", 0, [{"account_id": "A", "cents": 101}]),
                fund("f1", 1, [{"account_id": "A", "cents": 100}])):
            with self.assertRaisesRegex(RegisterError, "request_id"):
                apply(self.db, variant)
            self.assertEqual(snapshot(self.db), before)
        # A syntactically invalid reuse is a format error and also changes
        # nothing.
        with self.assertRaises(ValueError):
            apply(self.db, {
                "request_id": "f1", "expected_version": 0,
                "action": "seal", "manifest_ids": ["M"]})
        self.assertEqual(snapshot(self.db), before)

    def test_new_id_with_stale_expected_version_conflicts(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        before = snapshot(self.db)
        with self.assertRaisesRegex(RegisterError, "version conflict"):
            apply(self.db, fund("f2", 0, [{"account_id": "A", "cents": 1}]))
        self.assertEqual(snapshot(self.db), before)

    def test_input_data_is_never_mutated(self):
        request = fund("f1", 0, [{"account_id": "A", "cents": 100}])
        blob = json.dumps(request, sort_keys=True)
        apply(self.db, request)
        self.assertEqual(json.dumps(request, sort_keys=True), blob)
        request2 = amend("a1", 1, [upsert("A", "M", "P")])
        blob2 = json.dumps(request2, sort_keys=True)
        apply(self.db, request2)
        self.assertEqual(json.dumps(request2, sort_keys=True), blob2)

    def test_identity_reuse_of_cancelled_and_shipped_parcels(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        apply(self.db, amend("a1", 1, [upsert("A", "M", "P")]))
        apply(self.db, amend("a2", 2, cancels=["P"]))
        with self.assertRaisesRegex(RegisterError, "cannot be re-registered"):
            apply(self.db, amend("a3", 3, [upsert("A", "M", "P")]))
        apply(self.db, amend("a4", 3, [upsert("A", "N", "Q")]))
        apply(self.db, seal("s1", 4, ["N"]))
        # Even targeting a different open manifest, the shipped id is dead.
        with self.assertRaisesRegex(RegisterError, "cannot be re-registered"):
            apply(self.db, amend("a5", 5, [upsert("A", "M", "Q")]))

    def test_empty_manifest_can_be_sealed_and_unknown_manifests_open_on_use(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        with self.assertRaisesRegex(RegisterError, "unknown manifest"):
            apply(self.db, seal("s0", 1, ["EMPTY"]))
        apply(self.db, amend("a1", 1, [upsert("A", "EMPTY", "P")]))
        apply(self.db, amend("a2", 2, cancels=["P"]))
        receipt = apply(self.db, seal("s1", 3, ["EMPTY"]))
        row = self.manifests_by_id(receipt["snapshot"])["EMPTY"]
        self.assertEqual((row["status"], row["member_ids"], row["total_cents"]),
                         ("sealed", [], 0))
        self.assertEqual(row["sealed_version"], 4)
        # Sealing twice is rejected; the first seal stays intact.
        with self.assertRaisesRegex(RegisterError, "not open"):
            apply(self.db, seal("s2", 4, ["EMPTY"]))

    def test_strict_validation_booleans_duplicates_unknown_fields(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        bad_requests = [
            {},
            {"request_id": "x", "expected_version": 1, "action": "fund",
             "entries": [], "extra": 1},
            fund("x", True, [{"account_id": "A", "cents": 1}]),
            fund("x", 1, [{"account_id": "A", "cents": True}]),
            fund("x", 1, [{"account_id": "A", "cents": 0}]),
            fund("x", 1, [{"account_id": "A", "cents": 10 ** 15 + 1}]),
            fund("x", 1, [{"account_id": "A", "cents": 1},
                          {"account_id": "A", "cents": 2}]),
            amend("x", 1),  # neither upserts nor cancels
            amend("x", 1, [upsert("A", "M", "P"), upsert("A", "N", "P")]),
            amend("x", 1, cancels=["P", "P"]),
            amend("x", 1, [upsert("A", "M", "P")], cancels=["P"]),
            amend("x", 1, [upsert("GHOST", "M", "P")]),
            amend("x", 1, cancels=["GHOST"]),
            seal("x", 1, []),
            seal("x", 1, ["M", "M"]),
            {"request_id": "x", "expected_version": -1, "action": "fund",
             "entries": [{"account_id": "A", "cents": 1}]},
            {"request_id": "x", "expected_version": 1.5, "action": "fund",
             "entries": [{"account_id": "A", "cents": 1}]},
            {"request_id": "x", "expected_version": 1, "action": "unknown",
             "entries": []},
        ]
        for bad in bad_requests:
            with self.subTest(bad=bad):
                before = snapshot(self.db)
                with self.assertRaises((RegisterError, ValueError)):
                    apply(self.db, bad)
                self.assertEqual(snapshot(self.db), before)

    def test_amend_rejects_sealed_manifests_and_missing_service_zone(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 1000}]))
        apply(self.db, amend("a1", 1, [upsert("A", "M", "P")]))
        apply(self.db, seal("s1", 2, ["M"]))
        with self.assertRaisesRegex(RegisterError, "not open"):
            apply(self.db, amend("a2", 3, [
                {"account_id": "A", "manifest_id": "M",
                 "shipment": shipment("N1"), "tariff": TARIFF_100}]))
        mismatched = dict(TARIFF_100, zone="OTHER")
        with self.assertRaisesRegex(ValueError, "tariff"):
            apply(self.db, amend("a3", 3, [
                {"account_id": "A", "manifest_id": "N",
                 "shipment": shipment("N2"), "tariff": mismatched}]))

    def test_oversize_in_multi_batch_leaves_zero_side_effects(self):
        apply(self.db, fund("f1", 0, [
            {"account_id": "A", "cents": 1000},
            {"account_id": "B", "cents": 1000}]))
        apply(self.db, amend("a1", 1, [upsert("A", "M", "HOLD")]))
        before = snapshot(self.db)
        bad = amend("a2", 2, [
            upsert("B", "N", "NEW"),
            {"account_id": "B", "manifest_id": "N",
             "shipment": shipment("BIG", length_mm=900),
             "tariff": TARIFF_100}], cancels=["HOLD"])
        with self.assertRaisesRegex(RegisterError, "oversize"):
            apply(self.db, bad)
        self.assertEqual(snapshot(self.db), before)


class PrecisionAndStorageTests(RegisterTestBase):
    def test_held_and_spent_aggregates_beyond_64_bit_remain_exact(self):
        # Individual v1 line items stay within their tariff bounds (rate
        # <=10^9 per step), but account and manifest aggregates are unbounded
        # exact integer cents. Ten parcels each quoting 1.01*10^18 cents (rate
        # 10^9/step, 10^9 grams, 1-gram step, plus 1% fuel = 10^16) sum to
        # 1.01*10^19, beyond signed 64-bit range; storage stays decimal text.
        giant_rate = tariff(per_step=10 ** 9, step_grams=1, fuel_bp=100)
        per_parcel = 10 ** 18 + 10 ** 16
        piece_count = 10
        giant_total = per_parcel * piece_count
        self.assertGreater(giant_total, 2 ** 63)
        # A single fund entry is capped at 10^15 and each account appears once
        # per request, so the ten funders each accumulate their 1.01*10^18
        # balance over repeated funds; every cumulative total stays exact past
        # signed 64-bit range.
        entries_per_funder, tail = divmod(per_parcel, 10 ** 15)
        assert tail == 0
        for i in range(entries_per_funder):
            apply(self.db, fund(
                f"fund-{i}", i,
                [{"account_id": f"F{k}", "cents": 10 ** 15}
                 for k in range(piece_count)]))
        self.assertEqual(
            sum(a["funded_cents"] for a in snapshot(self.db)["accounts"]),
            giant_total)
        apply(self.db, amend("a1", entries_per_funder, [
            {"account_id": f"F{i}", "manifest_id": "M",
             "shipment": shipment(f"G{i}", weight_grams=10 ** 9,
                                  length_mm=1, width_mm=1, height_mm=1),
             "tariff": giant_rate}
            for i in range(piece_count)]))
        snap = snapshot(self.db)
        for booking in snap["bookings"]:
            self.assertEqual(booking["quote"]["total_cents"], per_parcel)
        self.assertEqual(self.manifests_by_id(snap)["M"]["total_cents"],
                         giant_total)
        held_sum = sum(a["held_cents"] for a in snap["accounts"])
        self.assertEqual(held_sum, giant_total)
        apply(self.db, seal("s1", entries_per_funder + 1, ["M"]))
        spent_sum = sum(a["spent_cents"]
                        for a in snapshot(self.db)["accounts"])
        self.assertEqual(spent_sum, giant_total)
        # Raw SQLite storage is text, never a floating point or overflowing int.
        raw_values = [row[0] for row in sqlite3.connect(self.db).execute(
            "SELECT total_cents FROM bookings")]
        self.assertTrue(all(isinstance(value, str) for value in raw_values))
        self.assertEqual(sum(int(value) for value in raw_values), giant_total)

    def test_cumulative_funding_beyond_64_bit_is_exact(self):
        # Repeated 10^15-cent funds accumulate past 2^63 without any 64-bit
        # overflow or float conversion. Each request funds ten distinct
        # accounts (one entry per account is the per-request limit); 1000
        # requests drive the ledger-wide total past the signed 64-bit wall,
        # while every per-account cumulative balance is itself a large exact
        # integer. Single-booking aggregates crossing 2^63 are covered by the
        # held/spent aggregate test above.
        db_path = os.path.join(self.temporary.name, "cumulative.db")
        account_count = 10
        request_count = 1000
        for i in range(request_count):
            apply(db_path, fund(
                f"h{i}", i,
                [{"account_id": f"X{k}", "cents": 10 ** 15}
                 for k in range(account_count)]))
        snap = snapshot(db_path)
        per_account = request_count * 10 ** 15
        ledger_total = per_account * account_count
        self.assertGreater(ledger_total, 2 ** 63)
        self.assertEqual(
            sum(a["funded_cents"] for a in snap["accounts"]), ledger_total)
        for account in snap["accounts"]:
            self.assertEqual(account["funded_cents"], per_account)
        # Raw SQLite storage is canonical decimal text, never float or int.
        raw_values = [row[0] for row in sqlite3.connect(db_path).execute(
            "SELECT funded_cents FROM accounts")]
        self.assertTrue(all(isinstance(value, str) for value in raw_values))
        self.assertEqual(sum(int(value) for value in raw_values),
                         ledger_total)

    def test_persistence_survives_a_new_connection_and_process(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        apply(self.db, amend("a1", 1, [upsert("A", "M", "P")]))
        # A brand new connection in this process sees the committed facts.
        reopened = snapshot(self.db)
        self.assertEqual(reopened["version"], 2)
        self.assertEqual(self.bookings_by_id(reopened)["P"]["status"], "held")
        # And a separate Python process reads exactly the same snapshot.
        import subprocess
        import sys
        code = (
            "import json, sys;"
            "from parcel_rating.register import snapshot;"
            "print(json.dumps(snapshot(sys.argv[1]), sort_keys=True))")
        completed = subprocess.run(
            [sys.executable, "-c", code, self.db],
            capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(completed.stdout), reopened)


class SnapshotAndSchemaTests(RegisterTestBase):
    def test_snapshot_never_creates_or_modifies_a_database(self):
        missing = os.path.join(self.temporary.name, "missing.db")
        with self.assertRaisesRegex(RegisterError, "does not exist"):
            snapshot(missing)
        self.assertFalse(os.path.exists(missing))
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        before = os.path.getmtime(self.db)
        first = snapshot(self.db)
        self.assertEqual(snapshot(self.db), first)
        # Read-only mode does not even acquire a writer lock or change bytes.
        self.assertEqual(os.path.getmtime(self.db), before)

    def test_foreign_and_unsupported_databases_are_rejected(self):
        foreign = os.path.join(self.temporary.name, "foreign.db")
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE unrelated (a)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RegisterError, "not a parcel-register"):
            snapshot(foreign)
        with self.assertRaisesRegex(RegisterError, "not a parcel-register"):
            apply(foreign, fund("x", 0, [{"account_id": "A", "cents": 1}]))

        unsupported = os.path.join(self.temporary.name, "unsupported.db")
        conn = sqlite3.connect(unsupported)
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES ('format', 'parcel-register');"
            "INSERT INTO meta VALUES ('schema_version', '99');"
            "CREATE TABLE accounts (x); CREATE TABLE manifests (x);"
            "CREATE TABLE bookings (x); CREATE TABLE requests (x);")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RegisterError, "unsupported"):
            snapshot(unsupported)

        garbage = os.path.join(self.temporary.name, "garbage.db")
        with open(garbage, "wb") as stream:
            stream.write(b"definitely not sqlite" * 16)
        with self.assertRaises(RegisterError):
            snapshot(garbage)
        with self.assertRaises(RegisterError):
            apply(garbage, fund("x", 0, [{"account_id": "A", "cents": 1}]))

    def test_failed_first_request_leaves_no_business_facts(self):
        # A business failure on a brand-new path may leave an empty database
        # file, but the rolled-back transaction means no schema, fact or
        # successful request record is ever committed.
        path = os.path.join(self.temporary.name, "fresh.db")
        with self.assertRaises(RegisterError):
            apply(path, amend("x", 0, [upsert("A", "M", "P")]))
        if os.path.exists(path):
            names = {row[0] for row in sqlite3.connect(path).execute(
                "SELECT name FROM sqlite_master")}
            self.assertEqual(names, set())
        # The corrected request then initializes and succeeds normally.
        apply(path, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        self.assertEqual(snapshot(path)["version"], 1)

    def test_format_failure_before_connect_creates_nothing(self):
        path = os.path.join(self.temporary.name, "never.db")
        with self.assertRaises((RegisterError, ValueError)):
            apply(path, {"request_id": "x", "expected_version": 0,
                         "action": "fund", "entries": []})
        self.assertFalse(os.path.exists(path))

    def test_snapshot_shape_is_exact(self):
        apply(self.db, fund("f1", 0, [{"account_id": "A", "cents": 100}]))
        apply(self.db, amend("a1", 1, [upsert("A", "M", "P")]))
        snap = snapshot(self.db)
        self.assertEqual(set(snap), {"version", "accounts", "bookings",
                                     "manifests"})
        self.assertEqual(set(snap["accounts"][0]),
                         {"account_id", "funded_cents", "held_cents",
                          "spent_cents", "available_cents"})
        self.assertEqual(set(snap["manifests"][0]),
                         {"manifest_id", "status", "member_ids",
                          "total_cents", "sealed_version"})
        self.assertEqual(set(snap["bookings"][0]),
                         {"id", "account_id", "manifest_id", "status",
                          "shipment", "tariff", "quote"})


class ConcurrencyTests(RegisterTestBase):
    def test_same_expected_version_only_one_writer_succeeds(self):
        apply(self.db, fund("seed", 0, [
            {"account_id": "A", "cents": 10 ** 9}]))
        self.assertEqual(snapshot(self.db)["version"], 1)
        errors = []
        receipts = []

        def worker(rid):
            try:
                receipts.append(apply(self.db, fund(
                    rid, 1, [{"account_id": "A", "cents": 1}])))
            except RegisterError as error:
                errors.append(str(error))

        threads = [threading.Thread(target=worker, args=(f"c{i}",))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(errors), 7)
        self.assertTrue(all("version conflict" in error for error in errors))
        self.assertEqual(snapshot(self.db)["version"], 2)
        self.assertEqual(
            self.accounts_by_id(snapshot(self.db))["A"]["funded_cents"],
            10 ** 9 + 1)

    def test_concurrent_identical_requests_both_return_the_one_receipt(self):
        request = fund("same", 0, [{"account_id": "A", "cents": 100}])
        results = []
        errors = []

        def worker():
            try:
                results.append(apply(self.db, copy.deepcopy(request)))
            except RegisterError as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(snapshot(self.db)["version"], 1)
        self.assertEqual(
            self.accounts_by_id(snapshot(self.db))["A"]["funded_cents"], 100)

    def test_concurrent_first_requests_serialize_schema_creation(self):
        # Regression guard: schema DDL on a brand-new database must run inside
        # the BEGIN IMMEDIATE transaction (executescript would implicitly
        # commit and release the write lock, producing "table meta already
        # exists" / duplicate-key races). Identical first requests across many
        # fresh databases all collapse onto one receipt, and distinct-id first
        # requests have exactly one winner.
        request = fund("seed", 0, [{"account_id": "A", "cents": 50}])
        for index in range(8):
            fresh = os.path.join(self.temporary.name, f"race-{index}.db")
            results, errors = [], []

            def worker():
                try:
                    results.append(apply(fresh, copy.deepcopy(request)))
                except RegisterError as error:
                    errors.append(error)

            threads = [threading.Thread(target=worker)
                       for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(errors)
            self.assertEqual(len(results), 6)
            self.assertTrue(all(result == results[0] for result in results))
            self.assertEqual(snapshot(fresh)["version"], 1)

        # Distinct ids on one fresh database: only one can establish version 1.
        fresh = os.path.join(self.temporary.name, "race-distinct.db")
        receipts, errors = [], []

        def distinct_worker(rid):
            try:
                receipts.append(apply(fresh, fund(
                    rid, 0, [{"account_id": "A", "cents": 1}])))
            except RegisterError as error:
                errors.append(str(error))

        threads = [threading.Thread(target=distinct_worker, args=(f"d{i}",))
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(errors), 5)
        self.assertTrue(all("version conflict" in error for error in errors))
        self.assertEqual(snapshot(fresh)["version"], 1)


if __name__ == "__main__":
    unittest.main()
