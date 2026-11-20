"""Differential tests against an independent in-memory reference model.

The reference model re-implements the register's bookkeeping from the
specification in plain Python dictionaries and its own v1-equivalent rating
arithmetic.  It never imports or calls the production :func:`apply` or
:func:`snapshot` functions to derive an expectation -- production is only used
as the system under test.  Random multi-batch histories of fund/amend/seal
requests (including deliberately invalid ones that must fail atomically, and
replays of old requests after the version has advanced) drive both
implementations; after every request the complete production snapshot, the
reference state, money conservation, manifest membership, version progression
and failure invariance are checked.
"""
import copy
import json
import os
import random
import tempfile
import unittest
from fractions import Fraction

from parcel_rating.register import RegisterError, apply, snapshot

ACCOUNTS = ["acct-0", "acct-1", "acct-2"]
MANIFESTS = ["man-0", "man-1", "man-2", "man-3"]
PARCELS = [f"parcel-{i}" for i in range(12)]
# (per_step, step_grams, fuel_bp, insurance_bp, girth limit); remote adds 30.
TARIFFS = [
    (100, 500, 0, 0, 1000),
    (120, 500, 50, 0, 1000),
    (70, 250, 0, 25, 800),
]
REMOTE_CENTS = 30


def reference_rate(shipment, tariff_row):
    """Independent v1-equivalent per-piece rating; None means oversize."""
    per_step, step_grams, fuel_bp, insurance_bp, girth_limit = tariff_row
    weight = shipment["weight_grams"]
    length, width, height = (shipment["length_mm"], shipment["width_mm"],
                             shipment["height_mm"])
    girth = length + 2 * (width + height)
    if girth > girth_limit:
        return None
    volume_cm3 = Fraction(length * width * height, 1000)
    dimensional = volume_cm3 * 1000 / 10 ** 9
    chargeable = (-(-max(Fraction(weight), dimensional) // step_grams)) * step_grams
    steps = chargeable // step_grams
    base = steps * per_step + (REMOTE_CENTS if shipment["remote"] else 0)
    fuel = (2 * (base * fuel_bp) + 10000) // (2 * 10000)
    insurance = (2 * (shipment["declared_cents"] * insurance_bp) + 10000) \
        // (2 * 10000)
    return base + fuel + insurance


def make_shipment(pid, rng):
    return {
        "id": pid, "service": "ground", "zone": "Z",
        "weight_grams": rng.choice([1, 250, 500, 501, 1000, 2500]),
        "length_mm": rng.choice([100, 200, 400]),
        "width_mm": rng.choice([100, 200]),
        "height_mm": rng.choice([100, 200]),
        "declared_cents": rng.choice([0, 100, 50000]),
        "remote": rng.choice([True, False]),
    }


def production_tariff(tariff_row):
    per_step, step_grams, fuel_bp, insurance_bp, girth = tariff_row
    return {"service": "ground", "zone": "Z", "base_cents": 0,
            "step_grams": step_grams, "per_step_cents": per_step,
            "divisor_cm3_per_kg": 10 ** 9, "max_girth_mm": girth,
            "remote_cents": REMOTE_CENTS, "fuel_bp": fuel_bp,
            "insurance_bp": insurance_bp, "insurance_free_cents": 0}


class _ReferenceReject(Exception):
    pass


class ReferenceModel:
    """Minimal faithful model: accounts, manifests, bookings, version."""

    def __init__(self):
        self.funded = {}
        # bookings[pid] = (account, manifest, status, total)
        self.bookings = {}
        # manifests[mid] = (status, sealed_version)
        self.manifests = {}
        self.version = 0
        self.canonical_by_id = {}
        self.receipt_by_id = {}

    def held_spent(self):
        held = {a: 0 for a in self.funded}
        spent = {a: 0 for a in self.funded}
        for account, _manifest, status, total in self.bookings.values():
            if status == "held":
                held[account] += total
            elif status == "shipped":
                spent[account] += total
        return held, spent

    def manifest_rows(self):
        rows = []
        for mid in sorted(self.manifests):
            members = sorted(
                pid for pid, (_acc, m, status, _t) in self.bookings.items()
                if m == mid and status != "cancelled")
            rows.append({
                "manifest_id": mid,
                "status": self.manifests[mid][0],
                "member_ids": members,
                "total_cents": sum(self.bookings[pid][3] for pid in members),
                "sealed_version": self.manifests[mid][1],
            })
        return rows

    def account_rows(self):
        held, spent = self.held_spent()
        return [{
            "account_id": account,
            "funded_cents": self.funded[account],
            "held_cents": held[account],
            "spent_cents": spent[account],
            "available_cents": self.funded[account] - held[account]
            - spent[account],
        } for account in sorted(self.funded)]

    def apply(self, request):
        """Return ('ok', receipt), ('retry', receipt) or ('error',)."""
        rid = request["request_id"]
        canonical = canonical_request(request)
        if rid in self.canonical_by_id:
            if self.canonical_by_id[rid] != canonical:
                return ("error",)
            return ("retry", self.receipt_by_id[rid])
        if request["expected_version"] != self.version:
            return ("error",)
        saved = (copy.deepcopy(self.funded), copy.deepcopy(self.bookings),
                 copy.deepcopy(self.manifests), self.version)
        try:
            if request["action"] == "fund":
                for entry in request["entries"]:
                    account = entry["account_id"]
                    self.funded.setdefault(account, 0)
                    self.funded[account] += entry["cents"]
            elif request["action"] == "amend":
                self._amend(request)
            else:
                self._seal(request)
        except _ReferenceReject:
            self.funded, self.bookings, self.manifests, self.version = saved
            return ("error",)
        self.version += 1
        receipt = {"request_id": rid, "version": self.version,
                   "snapshot": self._point_snapshot()}
        self.canonical_by_id[rid] = canonical
        self.receipt_by_id[rid] = copy.deepcopy(receipt)
        return ("ok", receipt)

    def _amend(self, request):
        upsert_ids = [row["shipment"]["id"] for row in request["upserts"]]
        if len(upsert_ids) != len(set(upsert_ids)) \
                or len(request["cancels"]) != len(set(request["cancels"])) \
                or set(upsert_ids).intersection(request["cancels"]):
            raise _ReferenceReject
        provisional = {pid: list(row) for pid, row in self.bookings.items()}
        manifests = dict(self.manifests)
        for row in request["upserts"]:
            pid = row["shipment"]["id"]
            account, mid = row["account_id"], row["manifest_id"]
            if account not in self.funded:
                raise _ReferenceReject
            if mid in manifests and manifests[mid][0] != "open":
                raise _ReferenceReject
            if pid in provisional:
                if provisional[pid][2] != "held":
                    raise _ReferenceReject
                if manifests[provisional[pid][1]][0] != "open":
                    raise _ReferenceReject
            total = reference_rate(row["shipment"], row["_tariff"])
            if total is None:
                raise _ReferenceReject
            manifests.setdefault(mid, ("open", None))
            provisional[pid] = [account, mid, "held", total]
        for pid in request["cancels"]:
            if pid not in provisional or provisional[pid][2] != "held":
                raise _ReferenceReject
            provisional[pid][2] = "cancelled"
        held = {a: 0 for a in self.funded}
        spent = {a: 0 for a in self.funded}
        for account, _m, status, total in provisional.values():
            if status == "held":
                held[account] += total
            elif status == "shipped":
                spent[account] += total
        for account, funded in self.funded.items():
            if funded - held[account] - spent[account] < 0:
                raise _ReferenceReject
        self.bookings = {pid: tuple(row) for pid, row in provisional.items()}
        self.manifests = manifests

    def _seal(self, request):
        for mid in request["manifest_ids"]:
            if mid not in self.manifests or self.manifests[mid][0] != "open":
                raise _ReferenceReject
        sealed_version = self.version + 1
        for mid in request["manifest_ids"]:
            self.manifests[mid] = ("sealed", sealed_version)
            for pid, row in list(self.bookings.items()):
                if row[1] == mid and row[2] == "held":
                    account, m, _status, total = row
                    self.bookings[pid] = (account, m, "shipped", total)

    def _point_snapshot(self):
        return {"version": self.version, "accounts": self.account_rows(),
                "manifests": self.manifest_rows()}


def canonical_request(request):
    """Same ordering rules as the production canonical identity."""
    normalized = {"action": request["action"],
                  "request_id": request["request_id"],
                  "expected_version": request["expected_version"]}
    if request["action"] == "fund":
        normalized["entries"] = sorted(
            ({"account_id": e["account_id"], "cents": e["cents"]}
             for e in request["entries"]), key=lambda e: e["account_id"])
    elif request["action"] == "amend":
        normalized["upserts"] = sorted(
            ({"account_id": u["account_id"],
              "manifest_id": u["manifest_id"],
              "shipment": u["shipment"], "tariff": u["tariff"]}
             for u in request["upserts"]),
            key=lambda u: u["shipment"]["id"])
        normalized["cancels"] = sorted(request["cancels"])
    else:
        normalized["manifest_ids"] = sorted(request["manifest_ids"])
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


class ReferenceModelDifferentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.replayable = {}

    def tearDown(self):
        self.temporary.cleanup()

    def assert_matches_reference(self, db, model):
        snap = snapshot(db)
        self.assertEqual(snap["version"], model.version)
        self.assertEqual(snap["accounts"], model.account_rows())
        self.assertEqual(
            [(m["manifest_id"], m["status"], m["member_ids"],
              m["total_cents"], m["sealed_version"])
             for m in snap["manifests"]],
            [(m["manifest_id"], m["status"], m["member_ids"],
              m["total_cents"], m["sealed_version"])
             for m in model.manifest_rows()])
        reference_bookings = {
            pid: (account, mid, status, total)
            for pid, (account, mid, status, total) in model.bookings.items()}
        production_bookings = {
            row["id"]: (row["account_id"], row["manifest_id"],
                        row["status"], row["quote"]["total_cents"])
            for row in snap["bookings"]}
        self.assertEqual(production_bookings, reference_bookings)
        # Global conservation and non-negative availability.
        funded_sum = sum(a["funded_cents"] for a in snap["accounts"])
        allocated = sum(a["held_cents"] + a["spent_cents"]
                        + a["available_cents"] for a in snap["accounts"])
        self.assertEqual(funded_sum, allocated)
        for account in snap["accounts"]:
            self.assertGreaterEqual(account["available_cents"], 0)
        # Cancelled parcels remain auditable but are never manifest members.
        for row in snap["bookings"]:
            if row["status"] == "cancelled":
                for manifest in snap["manifests"]:
                    self.assertNotIn(row["id"], manifest["member_ids"])

    def _random_request(self, rng, model, seq, replay_probability):
        rid = f"req-{seq}"
        if self.replayable and rng.random() < replay_probability:
            old_rid = rng.choice(sorted(self.replayable))
            return copy.deepcopy(self.replayable[old_rid])
        kind = rng.choices(
            ["fund", "amend", "seal", "stale-version", "terminal-amend"],
            weights=[3, 7, 2, 2, 1])[0]
        if kind == "fund":
            accounts = rng.sample(ACCOUNTS, rng.randint(1, len(ACCOUNTS)))
            return {"request_id": rid, "expected_version": model.version,
                    "action": "fund",
                    "entries": [{"account_id": a,
                                 "cents": rng.randint(1, 400)}
                                for a in accounts]}
        if kind == "stale-version":
            return {"request_id": rid,
                    "expected_version": max(0, model.version - 1),
                    "action": "fund",
                    "entries": [{"account_id": ACCOUNTS[0], "cents": 10}]}
        if kind == "seal":
            open_manifests = [mid for mid, state in model.manifests.items()
                              if state[0] == "open"]
            if not open_manifests:
                return self._random_amend(rng, model, rid, terminal=False)
            chosen = rng.sample(open_manifests,
                                rng.randint(1, len(open_manifests)))
            return {"request_id": rid, "expected_version": model.version,
                    "action": "seal", "manifest_ids": chosen}
        return self._random_amend(
            rng, model, rid, terminal=(kind == "terminal-amend"))

    def _random_amend(self, rng, model, rid, terminal):
        upserts = []
        cancels = []
        held_parcels = [pid for pid, row in model.bookings.items()
                        if row[2] == "held"]
        terminal_parcels = [pid for pid, row in model.bookings.items()
                            if row[2] in ("cancelled", "shipped")]
        unused_parcels = [pid for pid in PARCELS if pid not in model.bookings]
        rng.shuffle(held_parcels)
        rng.shuffle(unused_parcels)
        if terminal and terminal_parcels:
            targets = [terminal_parcels[0]]
        else:
            targets = held_parcels[:2] + unused_parcels[:2]
            rng.shuffle(targets)
            targets = targets[:rng.randint(0, 3)]
        for pid in targets:
            tariff_index = rng.randrange(len(TARIFFS))
            tariff_row = TARIFFS[tariff_index]
            upserts.append({
                "account_id": rng.choice(ACCOUNTS),
                "manifest_id": rng.choice(MANIFESTS),
                "shipment": make_shipment(pid, rng),
                "tariff": production_tariff(tariff_row),
                "_tariff": tariff_row,
            })
        if held_parcels and rng.random() < 0.4:
            cancels.append(rng.choice(held_parcels))
        if not upserts and not cancels:
            pid = (unused_parcels or PARCELS)[0]
            tariff_row = TARIFFS[0]
            upserts.append({
                "account_id": rng.choice(ACCOUNTS),
                "manifest_id": rng.choice(MANIFESTS),
                "shipment": make_shipment(pid, rng),
                "tariff": production_tariff(tariff_row),
                "_tariff": tariff_row,
            })
        return {"request_id": rid, "expected_version": model.version,
                "action": "amend", "upserts": upserts,
                "cancels": cancels}

    @staticmethod
    def _to_production(request):
        request = copy.deepcopy(request)
        for upsert in request.get("upserts", []):
            upsert.pop("_tariff", None)
        return request

    def _run_history(self, seed, steps, replay_probability):
        rng = random.Random(seed)
        model = ReferenceModel()
        db = os.path.join(self.temporary.name, f"history-{seed}.db")
        # Seed an always-valid first request so the database exists before any
        # deliberately failing request exercises rollback semantics.
        seed_request = {
            "request_id": "seed-0", "expected_version": 0, "action": "fund",
            "entries": [{"account_id": a, "cents": 100000}
                        for a in ACCOUNTS]}
        seed_outcome = model.apply(copy.deepcopy(seed_request))
        self.assertEqual(seed_outcome[0], "ok")
        apply(db, self._to_production(seed_request))
        self.replayable["seed-0"] = copy.deepcopy(seed_request)
        for seq in range(1, steps + 1):
            request = self._random_request(rng, model, seq,
                                           replay_probability)
            reference_outcome = model.apply(copy.deepcopy(request))
            try:
                receipt = apply(db, self._to_production(request))
                production_outcome = "ok"
            except RegisterError:
                production_outcome = "error"
            if reference_outcome[0] == "error":
                self.assertEqual(production_outcome, "error",
                                 (seed, seq, request))
            else:
                self.assertEqual(production_outcome, "ok",
                                 (seed, seq, request))
                self._assert_receipt_matches(receipt, reference_outcome[1])
                if reference_outcome[0] == "ok":
                    self.replayable[request["request_id"]] = \
                        copy.deepcopy(request)
            self.assert_matches_reference(db, model)
        return db, model

    def _assert_receipt_matches(self, receipt, expected_receipt):
        self.assertEqual(receipt["request_id"],
                         expected_receipt["request_id"])
        self.assertEqual(receipt["version"], expected_receipt["version"])
        point = receipt["snapshot"]
        expected_point = expected_receipt["snapshot"]
        self.assertEqual(point["version"], expected_point["version"])
        self.assertEqual(point["accounts"], expected_point["accounts"])
        self.assertEqual(
            [(m["manifest_id"], m["status"], m["member_ids"],
              m["total_cents"], m["sealed_version"])
             for m in point["manifests"]],
            [(m["manifest_id"], m["status"], m["member_ids"],
              m["total_cents"], m["sealed_version"])
             for m in expected_point["manifests"]])

    def test_random_histories_with_failures_and_replays(self):
        for seed in (20261119, 42, 7, 99, 31337):
            self.replayable = {}
            db, model = self._run_history(seed, steps=70,
                                          replay_probability=0.2)
            # After the history has advanced, replay every old request again:
            # each returns its original point-in-time receipt and changes
            # neither the current state nor the version.
            current = snapshot(db)
            for rid, request in list(self.replayable.items()):
                stored = model.receipt_by_id[rid]
                receipt = apply(db, self._to_production(copy.deepcopy(request)))
                self._assert_receipt_matches(receipt, stored)
            self.assertEqual(snapshot(db), current)
            self.assert_matches_reference(db, model)

    def test_failed_request_never_consumes_its_id_or_version(self):
        rng = random.Random(555)
        model = ReferenceModel()
        db = os.path.join(self.temporary.name, "failure.db")
        seed_request = {
            "request_id": "seed-0", "expected_version": 0, "action": "fund",
            "entries": [{"account_id": a, "cents": 100000}
                        for a in ACCOUNTS]}
        model.apply(copy.deepcopy(seed_request))
        apply(db, self._to_production(seed_request))
        self.replayable["seed-0"] = copy.deepcopy(seed_request)
        reused_failures = 0
        for seq in range(1, 81):
            request = self._random_request(rng, model, seq,
                                           replay_probability=0.0)
            outcome = model.apply(copy.deepcopy(request))
            try:
                apply(db, self._to_production(request))
                produced = "ok"
            except RegisterError:
                produced = "error"
            if outcome[0] == "error":
                self.assertEqual(produced, "error")
                # Correct the request content but reuse the same id: the
                # failed id was never consumed, so it now succeeds normally.
                fixed = {
                    "request_id": request["request_id"],
                    "expected_version": model.version,
                    "action": "fund",
                    "entries": [{"account_id": ACCOUNTS[0], "cents": 1}]}
                fixed_outcome = model.apply(copy.deepcopy(fixed))
                self.assertEqual(fixed_outcome[0], "ok")
                receipt = apply(db, self._to_production(fixed))
                self._assert_receipt_matches(receipt, fixed_outcome[1])
                self.replayable[fixed["request_id"]] = copy.deepcopy(fixed)
                reused_failures += 1
            else:
                self.assertEqual(produced, "ok")
                self.replayable[request["request_id"]] = copy.deepcopy(request)
            self.assert_matches_reference(db, model)
        self.assertGreater(reused_failures, 5)


if __name__ == "__main__":
    unittest.main()
