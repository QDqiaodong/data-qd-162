"""Schema v4 reconciliation tests.

The independent oracle in this module never calls the production solver
(``reconcile`` or its helpers). It re-derives the payable baseline with its own
v1 arithmetic, enumerates every whole-credit allocation and every debit->rated
shipment bijection on small inputs, keeps the joint complete explanations and
projects their support sets itself, so agreement is a genuine cross-check
rather than a shared implementation.
"""
import copy
import json
import time
import unittest
from itertools import product
from random import Random

from parcel_rating.model import analyze, reconcile

STEP = 500


def make_tariff(girth=1000, per_step=100, base=0):
    return {"service": "ground", "zone": "Z1", "base_cents": base,
            "step_grams": STEP, "per_step_cents": per_step,
            "divisor_cm3_per_kg": 10 ** 9, "max_girth_mm": girth,
            "remote_cents": 0, "fuel_bp": 0, "insurance_bp": 0,
            "insurance_free_cents": 0}


def make_shipment(sid, total_cents, oversize=False, per_step=100, girth=1000,
                  base=0):
    """Build a real v1 shipment whose rated total is exactly ``total_cents``.

    A huge divisor keeps dimensional weight negligible, so chargeable weight
    is the actual weight rounded to 500 g and the total is
    base + steps * per_step_cents (fuel, insurance, remote all zero). A zero
    total uses ``base=0`` and a zero per-step rate; the parcel itself still
    carries a legal positive weight.
    """
    if oversize:
        # girth 1000 + 2*(300+300) = 2200 > limit 1000.
        length, width, height = 1000, 300, 300
        weight = 500
    else:
        if total_cents == 0:
            assert base == 0 and per_step == 0
            steps = 1
        elif per_step == 0:
            assert total_cents == base
            steps = 1
        else:
            assert (total_cents - base) % per_step == 0
            steps = (total_cents - base) // per_step
        weight = steps * STEP  # callers keep steps*500 within the 10**9 cap
        length = width = height = 100  # girth 500, within the limit
    return {"id": sid, "service": "ground", "zone": "Z1",
            "weight_grams": weight, "length_mm": length, "width_mm": width,
            "height_mm": height, "declared_cents": 0, "remote": False}


def build_case(shipments, debits, credits, per_step=100, girth=1000, base=0):
    return {
        "rating": {"tariffs": [make_tariff(girth=girth, per_step=per_step,
                                           base=base)],
                   "shipments": shipments},
        "debits": debits,
        "credits": credits,
    }


def debit(did, gross, candidates):
    return {"debit_id": did, "gross_cents": gross,
            "shipment_candidates": list(candidates)}


def credit(cid, cents, candidates):
    return {"credit_id": cid, "cents": cents,
            "debit_candidates": list(candidates)}


# ---------------------------------------------------------------------------
# Independent oracle: own rating arithmetic, full joint enumeration.
# ---------------------------------------------------------------------------

def oracle_rated_totals(rating):
    """Recompute rated totals and the oversize id list without analyze()."""
    tariff_by_key = {(t["service"], t["zone"]): t for t in rating["tariffs"]}
    payable, excluded = {}, []
    for s in rating["shipments"]:
        tariff = tariff_by_key[(s["service"], s["zone"])]
        girth = s["length_mm"] + 2 * (s["width_mm"] + s["height_mm"])
        if girth > tariff["max_girth_mm"]:
            excluded.append(s["id"])
            continue
        steps = -(-s["weight_grams"] // tariff["step_grams"])
        payable[s["id"]] = (tariff["base_cents"]
                            + steps * tariff["per_step_cents"])
    return payable, sorted(excluded)


def oracle_reconcile(data):
    """Brute-force every complete explanation; project reconciled + support."""
    payable, excluded = oracle_rated_totals(data["rating"])
    payable_ids = sorted(payable)
    debits = data["debits"]
    credits = data["credits"]
    gross = {d["debit_id"]: d["gross_cents"] for d in debits}
    debit_cand = {d["debit_id"]: d["shipment_candidates"] for d in debits}
    credit_cand = {c["credit_id"]: c["debit_candidates"] for c in credits}

    explanations = []
    # 1. Every whole-credit allocation (each credit picks one candidate).
    credit_choices = [credit_cand[c["credit_id"]] for c in credits]
    for allocation in product(*credit_choices):
        net = dict(gross)
        for c, target in zip(credits, allocation):
            net[target] -= c["cents"]
        if any(value < 0 for value in net.values()):
            continue
        # 2. Every debit->shipment bijection over declared candidates whose
        #    exact net equals the payable total (zero net needs a zero payable).
        options = []
        for d in debits:
            did = d["debit_id"]
            options.append([sid for sid in debit_cand[did]
                            if sid in payable and payable[sid] == net[did]])
        for matching in product(*options):
            if len(set(matching)) != len(matching):
                continue
            if set(matching) != set(payable_ids):
                continue
            explanations.append((tuple(allocation), tuple(matching)))

    expected_total = sum(payable.values())
    net_total = sum(gross.values()) - sum(c["cents"] for c in credits)
    result = {
        "expected_shipments": [{"id": sid, "expected_cents": payable[sid]}
                               for sid in payable_ids],
        "excluded_oversize_ids": excluded,
        "expected_total_cents": expected_total,
        "net_debit_cents": net_total,
        "reconciled": bool(explanations),
        "debit_support": [],
        "credit_support": [],
    }
    if explanations:
        for i, d in enumerate(debits):
            ids = sorted({matching[i] for _, matching in explanations})
            result["debit_support"].append({
                "debit_id": d["debit_id"],
                "status": "forced" if len(ids) == 1 else "ambiguous",
                "shipment_ids": ids,
            })
        for j, c in enumerate(credits):
            ids = sorted({allocation[j] for allocation, _ in explanations})
            result["credit_support"].append({
                "credit_id": c["credit_id"],
                "status": "forced" if len(ids) == 1 else "ambiguous",
                "debit_ids": ids,
            })
    else:
        for d in debits:
            result["debit_support"].append(
                {"debit_id": d["debit_id"], "status": "blocked",
                 "shipment_ids": []})
        for c in credits:
            result["credit_support"].append(
                {"credit_id": c["credit_id"], "status": "blocked",
                 "debit_ids": []})
    result["debit_support"].sort(key=lambda row: row["debit_id"])
    result["credit_support"].sort(key=lambda row: row["credit_id"])
    return result


# ---------------------------------------------------------------------------
# Hand-built linked acceptance scenarios from the contract.
# ---------------------------------------------------------------------------

class LinkedAcceptanceTests(unittest.TestCase):
    def case_100_200(self, c2_cents):
        shipments = [make_shipment("A", 100), make_shipment("B", 200),
                     make_shipment("X", None, oversize=True)]
        # Sanity: the payable baseline really comes from v1 analyze.
        baseline = analyze({"tariffs": [make_tariff()], "shipments": shipments})
        by_id = {s["id"]: s for s in baseline["shipments"]}
        self.assertEqual(by_id["A"]["total_cents"], 100)
        self.assertEqual(by_id["B"]["total_cents"], 200)
        self.assertEqual(by_id["X"]["status"], "oversize")
        debits = [debit("D1", 250, ["A", "B"]), debit("D2", 150, ["A", "B"])]
        credits = [credit("C1", 50, ["D1", "D2"]),
                   credit("C2", c2_cents, ["D1"])]
        return build_case(shipments, debits, credits)

    def test_closure_forces_D1_B_D2_A_and_C1_D2(self):
        report = reconcile(self.case_100_200(50))
        self.assertTrue(report["reconciled"])
        self.assertEqual(report["expected_total_cents"], 300)
        self.assertEqual(report["net_debit_cents"], 300)
        self.assertEqual(report["excluded_oversize_ids"], ["X"])
        d = {row["debit_id"]: row for row in report["debit_support"]}
        c = {row["credit_id"]: row for row in report["credit_support"]}
        self.assertEqual(d["D1"], {"debit_id": "D1", "status": "forced",
                                   "shipment_ids": ["B"]})
        self.assertEqual(d["D2"], {"debit_id": "D2", "status": "forced",
                                   "shipment_ids": ["A"]})
        self.assertEqual(c["C1"], {"credit_id": "C1", "status": "forced",
                                   "debit_ids": ["D2"]})
        self.assertEqual(c["C2"], {"credit_id": "C2", "status": "forced",
                                   "debit_ids": ["D1"]})

    def test_c2_sixty_blocks_everything_despite_local_edges(self):
        data = self.case_100_200(60)
        report = reconcile(data)
        self.assertFalse(report["reconciled"])
        # Total nets 290; every row still has declared local candidates but no
        # complete explanation exists, so all support sets are empty.
        self.assertEqual(report["net_debit_cents"], 290)
        self.assertEqual(report["expected_total_cents"], 300)
        self.assertTrue(all(row["status"] == "blocked" and row["shipment_ids"] == []
                            for row in report["debit_support"]))
        self.assertTrue(all(row["status"] == "blocked" and row["debit_ids"] == []
                            for row in report["credit_support"]))

    def test_equal_totals_but_unique_cover_failure(self):
        # D1 net 100 and D2 net 200, both declaring only A: B is never covered
        # even though expected_total == net_debit == 300.
        shipments = [make_shipment("A", 100), make_shipment("B", 200)]
        debits = [debit("D1", 100, ["A"]), debit("D2", 200, ["A"])]
        data = build_case(shipments, debits, [])
        report = reconcile(data)
        self.assertFalse(report["reconciled"])
        self.assertEqual(report["expected_total_cents"], 300)
        self.assertEqual(report["net_debit_cents"], 300)
        self.assertTrue(all(row["status"] == "blocked"
                            for row in report["debit_support"]))


class AmbiguityAcceptanceTests(unittest.TestCase):
    def test_equal_amounts_keep_both_identity_supports(self):
        # A/B both pay 100; D1/D2 gross 150 each; C1/C2 50 each can hit either
        # debit. Two distinct allocations both close, and identity must not be
        # merged just because the money is equal.
        shipments = [make_shipment("A", 100), make_shipment("B", 100)]
        debits = [debit("D1", 150, ["A", "B"]), debit("D2", 150, ["A", "B"])]
        credits = [credit("C1", 50, ["D1", "D2"]),
                   credit("C2", 50, ["D1", "D2"])]
        data = build_case(shipments, debits, credits)
        report = reconcile(data)
        self.assertTrue(report["reconciled"])
        for row in report["debit_support"]:
            self.assertEqual(row["status"], "ambiguous")
            self.assertEqual(row["shipment_ids"], ["A", "B"])
        for row in report["credit_support"]:
            self.assertEqual(row["status"], "ambiguous")
            self.assertEqual(row["debit_ids"], ["D1", "D2"])
        self.assertEqual(report, oracle_reconcile(data))


class ZeroPayableTests(unittest.TestCase):
    def test_zero_rated_payable_and_credit_placement_interaction(self):
        # Real v1 tariffs: ground/Z1 has per-step 0 and base 0 so the 500 g
        # parcel A is rated for exactly 0; premium/Z1 has a 100-cent base and
        # per-step 0 so B is rated for exactly 100. X is over-girth and is an
        # excluded non-payable identity.
        shipments = [
            make_shipment("A", 0, per_step=0),
            make_shipment("B", 100, base=100, per_step=0),
            make_shipment("X", None, oversize=True),
        ]
        tariffs = [
            make_tariff(per_step=0, base=0),
            make_tariff(girth=1000, per_step=0, base=100),
        ]
        tariffs[1]["service"] = "premium"
        shipments[1]["service"] = "premium"
        baseline = analyze({"tariffs": tariffs, "shipments": shipments})
        by_id = {s["id"]: s for s in baseline["shipments"]}
        self.assertEqual(by_id["A"]["status"], "rated")
        self.assertEqual(by_id["A"]["total_cents"], 0)
        self.assertEqual(by_id["B"]["total_cents"], 100)
        self.assertEqual(by_id["X"]["status"], "oversize")
        debits = [debit("D1", 50, ["A"]), debit("D2", 100, ["B"])]
        credits = [credit("C", 50, ["D1", "D2"])]
        data = {"rating": {"tariffs": tariffs, "shipments": shipments},
                "debits": debits, "credits": credits}
        report = reconcile(data)
        self.assertTrue(report["reconciled"])
        self.assertEqual(report["expected_shipments"],
                         [{"id": "A", "expected_cents": 0},
                          {"id": "B", "expected_cents": 100}])
        self.assertEqual(report["excluded_oversize_ids"], ["X"])
        d = {row["debit_id"]: row for row in report["debit_support"]}
        c = {row["credit_id"]: row for row in report["credit_support"]}
        self.assertEqual(d["D1"], {"debit_id": "D1", "status": "forced",
                                   "shipment_ids": ["A"]})
        self.assertEqual(d["D2"], {"debit_id": "D2", "status": "forced",
                                   "shipment_ids": ["B"]})
        self.assertEqual(c["C"], {"credit_id": "C", "status": "forced",
                                  "debit_ids": ["D1"]})
        self.assertEqual(report, oracle_reconcile(data))


class ZeroBillAndPartialTests(unittest.TestCase):
    def test_empty_everything_is_closed_zero_bill(self):
        data = build_case([], [], [])
        report = reconcile(data)
        self.assertTrue(report["reconciled"])
        self.assertEqual(report["expected_shipments"], [])
        self.assertEqual(report["excluded_oversize_ids"], [])
        self.assertEqual(report["expected_total_cents"], 0)
        self.assertEqual(report["net_debit_cents"], 0)
        self.assertEqual(report["debit_support"], [])
        self.assertEqual(report["credit_support"], [])

    def test_credits_without_debits_is_not_closed(self):
        # An empty-candidate credit is a legal declaration but has no possible
        # assignment, so the bill cannot close.
        shipments = [make_shipment("A", 100)]
        data = build_case(shipments, [], [credit("C1", 10, [])])
        report = reconcile(data)
        self.assertFalse(report["reconciled"])
        self.assertEqual(report["net_debit_cents"], -10)
        self.assertEqual(report["credit_support"],
                         [{"credit_id": "C1", "status": "blocked",
                           "debit_ids": []}])

    def test_debit_without_credit_must_match_payable_exactly(self):
        shipments = [make_shipment("A", 100)]
        data = build_case(shipments, [debit("D1", 100, ["A"])], [])
        self.assertTrue(reconcile(data)["reconciled"])
        bad = build_case(shipments, [debit("D1", 120, ["A"])], [])
        self.assertFalse(reconcile(bad)["reconciled"])


# ---------------------------------------------------------------------------
# Deterministic generated samples vs the independent brute-force oracle.
# ---------------------------------------------------------------------------

def generate_case(seed):
    rng = Random(seed)
    n_ship = rng.randrange(0, 5)
    per_step = rng.choice((50, 100, 250))
    oversize = rng.randrange(0, 2)
    shipments = []
    totals = []
    for i in range(n_ship):
        steps = rng.randrange(1, 6)
        totals.append(steps * per_step)
        shipments.append(make_shipment(f"S{i}", steps * per_step,
                                       per_step=per_step))
    for i in range(oversize):
        shipments.append(make_shipment(f"O{n_ship + i}", None, oversize=True))
    payable_ids = [f"S{i}" for i in range(n_ship)]

    n_debit = rng.randrange(0, n_ship + 2)
    debits = []
    for i in range(n_debit):
        pool = list(payable_ids)
        rng.shuffle(pool)
        candidates = pool[:rng.randrange(0, min(len(pool), 4) + 1)]
        # Gross either aligned to a plausible payable or deliberately loose.
        gross = rng.choice((0, 50, 100, 250, 500, 10 ** 3, 10 ** 9))
        debits.append(debit(f"D{i}", gross, candidates))

    n_credit = rng.randrange(0, 5)
    credits = []
    for i in range(n_credit):
        pool = [d["debit_id"] for d in debits]
        rng.shuffle(pool)
        candidates = pool[:rng.randrange(0, min(len(pool), 2) + 1)]
        cents = rng.choice((1, 50, 100, 250, 10 ** 6, 10 ** 15))
        credits.append(credit(f"C{i}", cents, candidates))
    return build_case(shipments, debits, credits, per_step=per_step)


class OracleComparisonTests(unittest.TestCase):
    def test_generated_samples_match_brute_force(self):
        for seed in range(400):
            with self.subTest(seed=seed):
                data = generate_case(seed)
                self.assertEqual(reconcile(copy.deepcopy(data)),
                                 oracle_reconcile(data))

    def test_named_edge_cases_match_oracle(self):
        # Credit overshoot: a credit larger than every candidate gross.
        overshoot = build_case(
            [make_shipment("A", 100)],
            [debit("D1", 100, ["A"])],
            [credit("C1", 150, ["D1"])])
        # No credits at all.
        no_credit = build_case(
            [make_shipment("A", 100), make_shipment("B", 200)],
            [debit("D1", 100, ["A"]), debit("D2", 200, ["A", "B"])],
            [])
        # Empty candidate arrays: legal declarations, no possible edge.
        empty_cand = build_case(
            [make_shipment("A", 100)],
            [debit("D1", 100, [])],
            [credit("C1", 10, [])])
        # Huge amounts at the single-line ceiling; the payable itself is a
        # real zero-total rated shipment (zero per-step rate, positive weight).
        huge = build_case(
            [make_shipment("A", 0, per_step=0)],
            [debit("D1", 10 ** 15, ["A"])],
            [credit("C1", 10 ** 15, ["D1"])],
            per_step=0)
        # Ambiguity on one association only.
        one_sided = build_case(
            [make_shipment("A", 100), make_shipment("B", 100)],
            [debit("D1", 150, ["A", "B"]), debit("D2", 150, ["B"])],
            [credit("C1", 50, ["D1"]), credit("C2", 50, ["D2"])])
        # Equal totals but no matching (same totals, wrong declared identity).
        same_total_no_cover = build_case(
            [make_shipment("A", 100), make_shipment("B", 100)],
            [debit("D1", 100, ["A"]), debit("D2", 100, ["A"])],
            [])
        for data in (overshoot, no_credit, empty_cand, huge, one_sided,
                     same_total_no_cover):
            with self.subTest():
                self.assertEqual(reconcile(copy.deepcopy(data)),
                                 oracle_reconcile(data))


class PermutationAndImmutabilityTests(unittest.TestCase):
    def test_input_permutation_and_no_mutation(self):
        data = generate_case(23)
        snapshot = json.dumps(data, sort_keys=True)
        expected = reconcile(data)
        self.assertEqual(json.dumps(data, sort_keys=True), snapshot)
        shuffled = copy.deepcopy(data)
        rng = Random(5)
        rng.shuffle(shuffled["debits"])
        rng.shuffle(shuffled["credits"])
        rng.shuffle(shuffled["rating"]["shipments"])
        for d in shuffled["debits"]:
            rng.shuffle(d["shipment_candidates"])
        for c in shuffled["credits"]:
            rng.shuffle(c["debit_candidates"])
        self.assertEqual(reconcile(shuffled), expected)
        self.assertEqual(json.dumps(shuffled, sort_keys=True),
                         json.dumps(copy.deepcopy(shuffled), sort_keys=True))


class FullScaleTests(unittest.TestCase):
    def full_data(self, feasible):
        rng = Random(99)
        # Twelve distinct rated payables 100..1200 and no oversize rows.
        shipments = [make_shipment(f"S{i:02d}", (i + 1) * 100)
                     for i in range(12)]
        if feasible:
            # Fourteen 50-cent credits (the maximum) are placed two each on
            # debits D0..D6, so only those seven debits carry credit; their
            # gross is raised by exactly 100 above their target while D7..D11
            # keep gross == target. Every credit also names an adjacent debit
            # as a second candidate, so the solver must prove a feasible
            # allocation (and analyze the alternatives) rather than assume it.
            debits, credits = [], []
            for i in range(12):
                target = (i + 1) * 100
                gross = target + 100 if i < 7 else target
                candidates = [f"S{j:02d}"
                              for j in range(max(0, i - 1), min(12, i + 2))]
                debits.append(debit(f"D{i:02d}", gross, candidates))
            cid = 0
            for owner in range(7):
                for _ in range(2):
                    other = (owner + 1) % 12
                    credits.append(credit(f"C{cid:02d}", 50,
                                          [f"D{owner:02d}", f"D{other:02d}"]))
                    cid += 1
            assert cid == 14
        else:
            debits = [debit(f"D{i:02d}", (i + 1) * 100, ["S00"])
                      for i in range(12)]
            credits = []
        return build_case(shipments, debits, credits)

    def test_full_scale_feasible_runs_without_permutation_enumeration(self):
        data = self.full_data(True)
        # The intended allocation: both credits of owner i (i<7) land on D_i,
        # giving net target for every debit and a diagonal unique cover.
        self.assertEqual(sum(c["cents"] for c in data["credits"]), 700)
        started = time.perf_counter()
        report = reconcile(data)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 10.0)
        self.assertTrue(report["reconciled"])
        self.assertEqual(len(report["debit_support"]), 12)
        self.assertEqual(len(report["credit_support"]), 14)
        self.assertEqual(report["net_debit_cents"], report["expected_total_cents"])
        self.assertEqual(report["expected_total_cents"], 7800)
        d = {row["debit_id"]: row for row in report["debit_support"]}
        for i in range(12):
            self.assertIn(f"S{i:02d}", d[f"D{i:02d}"]["shipment_ids"])
        # Every declared debit and credit has exactly one output row.
        self.assertEqual({row["debit_id"] for row in report["debit_support"]},
                         {d_["debit_id"] for d_ in data["debits"]})
        self.assertEqual({row["credit_id"] for row in report["credit_support"]},
                         {c_["credit_id"] for c_ in data["credits"]})

    def test_full_scale_infeasible_cover_failure(self):
        data = self.full_data(False)
        report = reconcile(data)
        self.assertFalse(report["reconciled"])
        self.assertTrue(all(row["status"] == "blocked"
                            for row in report["debit_support"]))

    def test_huge_aggregates_unbounded_by_line_ceiling(self):
        # Aggregates may exceed 10^15 even though each line is capped.
        shipments = [make_shipment(f"S{i:02d}", 0, per_step=0) for i in range(12)]
        debits = [debit(f"D{i:02d}", 10 ** 15, [f"S{i:02d}"]) for i in range(12)]
        credits = [credit(f"C{i:02d}", 10 ** 15, [f"D{i:02d}"]) for i in range(12)]
        report = reconcile(build_case(shipments, debits, credits, per_step=0))
        self.assertTrue(report["reconciled"])
        self.assertEqual(report["net_debit_cents"], 0)
        self.assertEqual(report["expected_total_cents"], 0)


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, payload, needle=""):
        with self.assertRaises(ValueError) as ctx:
            reconcile(payload)
        self.assertIn(needle, str(ctx.exception))

    def base(self):
        return build_case([make_shipment("A", 100)],
                          [debit("D1", 100, ["A"])],
                          [credit("C1", 50, ["D1"])])

    def test_top_level_and_rating_shape(self):
        good = self.base()
        self.assert_invalid({}, "fields")
        self.assert_invalid({**good, "extra": 1}, "fields")
        bad = copy.deepcopy(good)
        del bad["rating"]["tariffs"]
        self.assert_invalid(bad, "fields")
        bad = copy.deepcopy(good)
        bad["rating"]["extra"] = 1
        self.assert_invalid(bad, "fields")

    def test_v1_rating_contract_is_inherited(self):
        bad = self.base()
        bad["rating"]["shipments"][0]["zone"] = "ZZ"
        self.assert_invalid(bad, "tariff")
        bad = self.base()
        bad["rating"]["shipments"][0]["remote"] = 1
        self.assert_invalid(bad, "boolean")
        bad = self.base()
        dup = copy.deepcopy(bad["rating"]["shipments"][0])
        bad["rating"]["shipments"].append(dup)
        self.assert_invalid(bad, "duplicate")
        # Thirteen shipments exceed the narrowed reconcile cap (v1 plan-style).
        bad = self.base()
        bad["rating"]["shipments"] = [make_shipment(f"S{i:02d}", 100)
                                      for i in range(13)]
        self.assert_invalid(bad, "bounded")

    def test_debit_shape_and_bounds(self):
        good = self.base()
        cases = [
            ([{"debit_id": "D1", "gross_cents": True,
               "shipment_candidates": ["A"]}], "integer"),
            ([{"debit_id": "D1", "gross_cents": 1.0,
               "shipment_candidates": ["A"]}], "integer"),
            ([{"debit_id": "D1", "gross_cents": "100",
               "shipment_candidates": ["A"]}], "integer"),
            ([{"debit_id": "D1", "gross_cents": -1,
               "shipment_candidates": ["A"]}], "integer"),
            ([{"debit_id": "D1", "gross_cents": 10 ** 15 + 1,
               "shipment_candidates": ["A"]}], "integer"),
            ([{"debit_id": "D1", "gross_cents": 0, "shipment_candidates": ["A"]},
              {"debit_id": "D1", "gross_cents": 0, "shipment_candidates": ["A"]}],
             "duplicate debit"),
            ([{"debit_id": "D1", "gross_cents": 0,
               "shipment_candidates": ["A", "A"]}], "duplicate candidate"),
            ([{"debit_id": "D1", "gross_cents": 0,
               "shipment_candidates": ["NOPE"]}], "unknown or excluded"),
            ([{"debit_id": "D1", "gross_cents": 0, "shipment_candidates": "X"}],
             "bounded array"),
            ([{"debit_id": "D1", "gross_cents": 0,
               "shipment_candidates": ["A"] * 13}], "bounded array"),
            ([{"debit_id": " spaced ", "gross_cents": 0,
               "shipment_candidates": []}], "identifier"),
            ([{"debit_id": "D1", "gross_cents": 0}], "fields"),
        ]
        for debits, needle in cases:
            bad = copy.deepcopy(good)
            bad["debits"] = debits
            with self.subTest(needle=needle):
                self.assert_invalid(bad, needle)

    def test_oversize_candidate_is_rejected_not_zero_payable(self):
        data = build_case([make_shipment("A", 100),
                           make_shipment("X", None, oversize=True)],
                          [debit("D1", 100, ["X"])], [])
        self.assert_invalid(data, "unknown or excluded")

    def test_credit_shape_and_bounds(self):
        good = self.base()
        cases = [
            ([credit("C1", 0, ["D1"])], "integer"),
            ([credit("C1", True, ["D1"])], "integer"),
            ([credit("C1", 1.5, ["D1"])], "integer"),
            ([credit("C1", "50", ["D1"])], "integer"),
            ([credit("C1", 10 ** 15 + 1, ["D1"])], "integer"),
            ([credit("C1", 50, ["D1"]), credit("C1", 50, ["D1"])],
             "duplicate credit"),
            ([credit("C1", 50, ["D1", "D1"])], "duplicate candidate"),
            ([credit("C1", 50, ["GHOST"])], "unknown or excluded"),
            ([credit("C1", 50, ["D1", "D1", "D1"])], "bounded array"),
            ([{"credit_id": "C1", "cents": 50}], "fields"),
        ]
        for credits, needle in cases:
            bad = copy.deepcopy(good)
            bad["credits"] = credits
            with self.subTest(needle=needle):
                self.assert_invalid(bad, needle)

    def test_array_limits(self):
        good = self.base()
        bad = copy.deepcopy(good)
        bad["debits"] = [debit(f"D{i}", 0, []) for i in range(13)]
        self.assert_invalid(bad, "bounded")
        bad = copy.deepcopy(good)
        bad["credits"] = [credit(f"C{i}", 1, []) for i in range(15)]
        self.assert_invalid(bad, "bounded")

    def test_independent_id_namespaces(self):
        # A credit and debit sharing one literal id is legal: separate spaces.
        data = build_case([make_shipment("A", 0, per_step=0)],
                          [debit("SAME", 10, ["A"])],
                          [credit("SAME", 10, ["SAME"])],
                          per_step=0)
        report = reconcile(data)
        self.assertTrue(report["reconciled"])
        self.assertEqual(report["debit_support"][0]["debit_id"], "SAME")
        self.assertEqual(report["credit_support"][0]["credit_id"], "SAME")


if __name__ == "__main__":
    unittest.main()
