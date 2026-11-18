import json
import unittest
from fractions import Fraction
from itertools import product
from pathlib import Path

from parcel_rating.model import plan, half_up

ROOT = Path(__file__).resolve().parents[1]


def tariff(service, zone, base=0, per_step=0, fuel=0, insurance=0, free=0,
           remote_fee=0, step=500, girth=3000, divisor=5000):
    return {"service": service, "zone": zone, "base_cents": base, "step_grams": step,
            "per_step_cents": per_step, "divisor_cm3_per_kg": divisor,
            "max_girth_mm": girth, "remote_cents": remote_fee, "fuel_bp": fuel,
            "insurance_bp": insurance, "insurance_free_cents": free}


def rule(service, zone, pieces=12, grams=10 ** 12, minimum=0):
    return {"service": service, "zone": zone, "max_pieces": pieces,
            "max_chargeable_grams": grams, "minimum_base_cents": minimum}


def parcel(pid, services=("A",), zone="Z1", weight=500, length=100, width=100,
           height=100, declared=0, remote=False):
    return {"id": pid, "services": list(services), "zone": zone,
            "weight_grams": weight, "length_mm": length, "width_mm": width,
            "height_mm": height, "declared_cents": declared, "remote": remote}


def girth_mm(p):
    return p["length_mm"] + 2 * (p["width_mm"] + p["height_mm"])


def oracle(data):
    """Independent brute-force oracle with the full three-level tie objective.

    Enumerates every service choice per parcel, filters capacity-infeasible
    covers, recomputes every group bill (minimum base, one group-level fuel
    rounding, summed insurance), and applies (total, nonempty groups,
    Unicode service vector) ordering itself.
    """
    tariffs = {(t["service"], t["zone"]): t for t in data["tariffs"]}
    rules = {(r["service"], r["zone"]): r for r in data["dispatch_rules"]}
    shipments = sorted(data["shipments"], key=lambda s: s["id"])
    choices = []
    for s in shipments:
        options = []
        for service in s["services"]:
            t = tariffs[(service, s["zone"])]
            if girth_mm(s) <= t["max_girth_mm"]:
                dim = Fraction(s["length_mm"] * s["width_mm"] * s["height_mm"],
                               t["divisor_cm3_per_kg"])
                charge = max(Fraction(s["weight_grams"]), dim)
                steps = -(-charge.numerator // (charge.denominator * t["step_grams"]))
                chargeable = steps * t["step_grams"]
                base = t["base_cents"] + steps * t["per_step_cents"] + (
                    t["remote_cents"] if s["remote"] else 0)
                insured = max(0, s["declared_cents"] - t["insurance_free_cents"])
                ins = half_up(Fraction(insured * t["insurance_bp"], 10000))
                options.append((service, chargeable, base, ins))
        choices.append(options)

    best = None
    for combo in product(*choices):
        groups = {}
        for s, option in zip(shipments, combo):
            key = (option[0], s["zone"])
            groups.setdefault(key, []).append((s["id"], option))
        feasible = True
        total = 0
        group_view = []
        for key, members in groups.items():
            r = rules[key]
            if len(members) > r["max_pieces"]:
                feasible = False
                break
            weight = sum(m[1][1] for m in members)
            if weight > r["max_chargeable_grams"]:
                feasible = False
                break
            subtotal = sum(m[1][2] for m in members)
            billed = max(subtotal, r["minimum_base_cents"])
            fuel = half_up(Fraction(billed * tariffs[key]["fuel_bp"], 10000))
            insurance = sum(m[1][3] for m in members)
            total += billed + fuel + insurance
            group_view.append((key, tuple(sorted(m[0] for m in members))))
        if not feasible:
            continue
        vector = tuple(option[0] for option in combo)
        score = (total, len(groups), vector)
        if best is None or score < best[0]:
            best = (score, sorted(group_view))
    return best


class AcceptanceScenarioTests(unittest.TestCase):
    def base_data(self):
        return {
            "tariffs": [tariff("A", "Z1", per_step=100), tariff("B", "Z1", per_step=200)],
            "dispatch_rules": [rule("A", "Z1", pieces=2, grams=1000, minimum=1000),
                               rule("B", "Z1", pieces=2, grams=1000)],
            "shipments": [parcel("P1", ("A", "B")), parcel("P2", ("A", "B"))],
        }

    def test_local_cheap_is_not_global_optimum(self):
        report = plan(self.base_data())
        self.assertTrue(report["feasible"])
        self.assertEqual(report["total_cents"], 400)
        self.assertEqual([a["service"] for a in report["assignments"]], ["B", "B"])
        group = report["groups"][0]
        self.assertEqual((group["service"], group["piece_count"],
                          group["subtotal_base_cents"], group["billed_base_cents"],
                          group["minimum_topup_cents"], group["total_cents"]),
                         ("B", 2, 400, 400, 0, 400))

    def test_removing_minimum_switches_optimum(self):
        data = self.base_data()
        data["dispatch_rules"][0]["minimum_base_cents"] = 0
        report = plan(data)
        self.assertEqual(report["total_cents"], 200)
        self.assertEqual([a["service"] for a in report["assignments"]], ["A", "A"])

    def test_capacity_and_minimum_force_recomparison(self):
        data = self.base_data()
        data["dispatch_rules"][0]["minimum_base_cents"] = 0
        data["dispatch_rules"][0]["max_pieces"] = 1
        data["dispatch_rules"][1]["minimum_base_cents"] = 1000
        report = plan(data)
        # Mixed cover: A=100 plus B billed at its 1000 minimum => 1100;
        # both on B: one group billed 1000 => 1000, which must win.
        self.assertEqual(report["total_cents"], 1000)
        self.assertEqual(len(report["groups"]), 1)
        self.assertEqual(report["groups"][0]["service"], "B")
        self.assertEqual(report["groups"][0]["minimum_topup_cents"], 600)


class FuelBoundaryTests(unittest.TestCase):
    def test_group_fuel_rounded_once_at_half(self):
        data = {
            "tariffs": [tariff("A", "Z1", base=100, fuel=50)],
            "dispatch_rules": [rule("A", "Z1", pieces=2, grams=1000)],
            "shipments": [parcel("P1", ("A",)), parcel("P2", ("A",))],
        }
        report = plan(data)
        group = report["groups"][0]
        # Per piece half_up(100*50/10000)=half_up(0.5)=1; summing piece fuel
        # would give 2.  The group bill rounds once: half_up(200*50/10000)=1.
        self.assertEqual(group["subtotal_base_cents"], 200)
        self.assertEqual(group["fuel_cents"], 1)
        self.assertEqual(group["total_cents"], 201)
        self.assertEqual(report["total_cents"], 201)

    def test_fuel_above_half_rounds_up(self):
        data = {
            # Billed base 202 with 75bp => 1.515 => rounds up to 2 (a single
            # group-level rounding, never per-piece fuel summed).
            "tariffs": [tariff("A", "Z1", base=101, fuel=75)],
            "dispatch_rules": [rule("A", "Z1", pieces=2, grams=1000)],
            "shipments": [parcel("P1", ("A",)), parcel("P2", ("A",))],
        }
        group = plan(data)["groups"][0]
        self.assertEqual(group["fuel_cents"], 2)

    def test_insurance_sums_per_piece(self):
        data = {
            "tariffs": [tariff("A", "Z1", base=100, insurance=100, free=0)],
            "dispatch_rules": [rule("A", "Z1", pieces=2, grams=1000)],
            "shipments": [parcel("P1", ("A",), declared=150), parcel("P2", ("A",), declared=250)],
        }
        group = plan(data)["groups"][0]
        # half_up(1.5)=2 and half_up(2.5)=3
        self.assertEqual(group["insurance_cents"], 5)
        self.assertEqual(group["total_cents"], 200 + group["fuel_cents"] + 5)


class CandidateEvidenceTests(unittest.TestCase):
    def test_oversize_candidate_kept_and_other_chosen(self):
        data = {
            "tariffs": [tariff("A", "Z1", girth=499), tariff("B", "Z1", per_step=10)],
            "dispatch_rules": [rule("A", "Z1"), rule("B", "Z1")],
            "shipments": [parcel("P1", ("A", "B"))],
        }
        report = plan(data)
        evidence = {c["service"]: c for c in report["candidates"]}
        self.assertEqual(evidence["A"]["status"], "oversize")
        self.assertEqual(evidence["A"]["girth_mm"], 500)
        self.assertNotIn("chargeable_weight_grams", evidence["A"])
        self.assertNotIn("base_cents", evidence["A"])
        self.assertEqual(evidence["B"]["status"], "eligible")
        self.assertEqual(evidence["B"]["chargeable_weight_grams"], 500)
        self.assertEqual(evidence["B"]["base_cents"], 10)
        self.assertEqual(evidence["B"]["insurance_cents"], 0)
        self.assertEqual(report["assignments"], [{"id": "P1", "service": "B", "zone": "Z1"}])

    def test_all_oversize_is_infeasible_but_reports_evidence(self):
        data = {
            "tariffs": [tariff("A", "Z1", girth=100)],
            "dispatch_rules": [rule("A", "Z1", pieces=2)],
            "shipments": [parcel("P1", ("A",)), parcel("P2", ("A",))],
        }
        report = plan(data)
        self.assertFalse(report["feasible"])
        self.assertIsNone(report["total_cents"])
        self.assertEqual(report["assignments"], [])
        self.assertEqual(report["groups"], [])
        self.assertEqual(len(report["candidates"]), 2)
        self.assertTrue(all(c["status"] == "oversize" for c in report["candidates"]))

    def test_every_declared_candidate_has_exactly_one_evidence(self):
        data = {
            "tariffs": [tariff("A", "Z1"), tariff("B", "Z1")],
            "dispatch_rules": [rule("A", "Z1"), rule("B", "Z1")],
            "shipments": [parcel("P1", ("A", "B")), parcel("P2", ("B",))],
        }
        report = plan(data)
        self.assertEqual([(c["id"], c["service"]) for c in report["candidates"]],
                         [("P1", "A"), ("P1", "B"), ("P2", "B")])


class CapacityTests(unittest.TestCase):
    def data(self):
        return {
            "tariffs": [tariff("A", "Z1", per_step=10)],
            "dispatch_rules": [rule("A", "Z1", pieces=2, grams=1000)],
            "shipments": [parcel("P1", ("A",)), parcel("P2", ("A",))],
        }

    def test_chargeable_weight_exactly_at_capacity(self):
        self.assertTrue(plan(self.data())["feasible"])

    def test_chargeable_weight_over_capacity_infeasible(self):
        data = self.data()
        data["dispatch_rules"][0]["max_chargeable_grams"] = 999
        self.assertFalse(plan(data)["feasible"])

    def test_piece_capacity_boundary(self):
        data = self.data()
        data["dispatch_rules"][0]["max_pieces"] = 1
        self.assertFalse(plan(data)["feasible"])

    def test_capacity_uses_chargeable_not_actual_weight(self):
        # Actual 600g rounds to a 1000g chargeable step; 600g allowance fails.
        data = {
            "tariffs": [tariff("A", "Z1", per_step=10, step=1000)],
            "dispatch_rules": [rule("A", "Z1", grams=600)],
            "shipments": [parcel("P1", ("A",), weight=600)],
        }
        self.assertFalse(plan(data)["feasible"])
        data["dispatch_rules"][0]["max_chargeable_grams"] = 1000
        report = plan(data)
        self.assertTrue(report["feasible"])
        self.assertEqual(report["groups"][0]["chargeable_weight_grams"], 1000)


class StructureAndRecomputeTests(unittest.TestCase):
    def fixture_report(self):
        data = {
            "tariffs": [tariff("A", "Z1", base=100, per_step=25, fuel=800,
                               insurance=100, free=50),
                        tariff("B", "Z2", base=50, per_step=40, fuel=300,
                               remote_fee=75)],
            "dispatch_rules": [rule("A", "Z1", pieces=3, grams=10 ** 9, minimum=300),
                               rule("B", "Z2", pieces=3, grams=10 ** 9)],
            "shipments": [
                parcel("P1", ("A",), zone="Z1", declared=1000, remote=True),
                parcel("P2", ("A",), zone="Z1", weight=700),
                parcel("P3", ("A",), zone="Z1", weight=250, declared=20),
                parcel("P4", ("B",), zone="Z2", weight=600, remote=True),
            ],
        }
        return data, plan(data)

    def test_group_bill_recomputes_from_report(self):
        data, report = self.fixture_report()
        tariffs = {(t["service"], t["zone"]): t for t in data["tariffs"]}
        rules = {(r["service"], r["zone"]): r for r in data["dispatch_rules"]}
        evidence = {(c["id"], c["service"]): c for c in report["candidates"]}
        self.assertTrue(report["feasible"])
        group_sum = 0
        for group in report["groups"]:
            members = group["member_ids"]
            self.assertEqual(members, sorted(members))
            self.assertEqual(group["piece_count"], len(members))
            s = sum(evidence[(m, group["service"])]["base_cents"] for m in members)
            weight = sum(evidence[(m, group["service"])]["chargeable_weight_grams"]
                         for m in members)
            ins = sum(evidence[(m, group["service"])]["insurance_cents"] for m in members)
            self.assertEqual(group["subtotal_base_cents"], s)
            self.assertEqual(group["chargeable_weight_grams"], weight)
            self.assertEqual(group["insurance_cents"], ins)
            billed = max(s, rules[(group["service"], group["zone"])]["minimum_base_cents"])
            self.assertEqual(group["billed_base_cents"], billed)
            self.assertEqual(group["minimum_topup_cents"], billed - s)
            fuel = half_up(Fraction(billed * tariffs[(group["service"], group["zone"])]["fuel_bp"], 10000))
            self.assertEqual(group["fuel_cents"], fuel)
            self.assertEqual(group["total_cents"], billed + fuel + ins)
            group_sum += group["total_cents"]
        self.assertEqual(group_sum, report["total_cents"])
        ids = [a["id"] for a in report["assignments"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(sorted(ids), sorted(s["id"] for s in data["shipments"]))

    def test_sorted_orders_and_empty_input(self):
        data, report = self.fixture_report()
        self.assertEqual([c["id"] + c["service"] for c in report["candidates"]],
                         sorted(c["id"] + c["service"] for c in report["candidates"]))
        self.assertEqual([(g["service"], g["zone"]) for g in report["groups"]],
                         sorted((g["service"], g["zone"]) for g in report["groups"]))
        empty = plan({"tariffs": data["tariffs"], "dispatch_rules": data["dispatch_rules"],
                      "shipments": []})
        self.assertEqual(empty, {"feasible": True, "candidates": [], "assignments": [],
                                 "groups": [], "total_cents": 0})

    def test_input_permutation_does_not_change_result(self):
        data, expected = self.fixture_report()
        shuffled = json.loads(json.dumps(data))
        shuffled["shipments"] = list(reversed(shuffled["shipments"]))
        shuffled["tariffs"] = list(reversed(shuffled["tariffs"]))
        shuffled["dispatch_rules"] = list(reversed(shuffled["dispatch_rules"]))
        for s in shuffled["shipments"]:
            s["services"] = list(reversed(s["services"]))
        self.assertEqual(plan(shuffled), expected)


class TieBreakTests(unittest.TestCase):
    def test_unicode_service_vector_on_equal_price(self):
        data = {
            "tariffs": [tariff("A", "Z1", per_step=10), tariff("B", "Z1", per_step=10)],
            "dispatch_rules": [rule("A", "Z1", pieces=2), rule("B", "Z1", pieces=2)],
            "shipments": [parcel("P2", ("A", "B")), parcel("P1", ("A", "B"))],
        }
        report = plan(data)
        self.assertEqual([a["service"] for a in
                          sorted(report["assignments"], key=lambda a: a["id"])],
                         ["A", "A"])

    def test_fewer_nonempty_groups_wins_at_equal_total(self):
        data = {
            "tariffs": [tariff("A", "Z1", per_step=20), tariff("B", "Z1", per_step=20)],
            "dispatch_rules": [rule("A", "Z1", pieces=1), rule("B", "Z1", pieces=2)],
            "shipments": [parcel("P1", ("A", "B")), parcel("P2", ("A", "B"))],
        }
        report = plan(data)
        # Mixed cover costs the same but opens two groups; the one-group B
        # cover must be preferred.
        self.assertEqual(len(report["groups"]), 1)
        self.assertEqual(report["groups"][0]["service"], "B")

    def test_unicode_orders_by_code_point(self):
        data = {
            "tariffs": [tariff("A", "Z1"), tariff("中", "Z1")],
            "dispatch_rules": [rule("A", "Z1"), rule("中", "Z1")],
            "shipments": [parcel("P1", ("中", "A"))],
        }
        report = plan(data)
        # 'A' (U+0041) precedes '中' (U+4E2D).
        self.assertEqual(report["assignments"][0]["service"], "A")


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, payload, needle):
        with self.assertRaisesRegex(ValueError, needle):
            plan(payload)

    def test_top_level_shape(self):
        good_tariff = tariff("A", "Z1")
        good_rule = rule("A", "Z1")
        self.assert_invalid({}, "fields")
        self.assert_invalid({"tariffs": [], "shipments": [], "dispatch_rules": [],
                             "extra": 1}, "fields")
        self.assert_invalid({"tariffs": [{"bad": 1}], "shipments": [],
                             "dispatch_rules": []}, "fields")
        self.assert_invalid({"tariffs": [good_tariff], "shipments": [],
                             "dispatch_rules": [{"service": "A", "zone": "Z1",
                                                 "max_pieces": 13,
                                                 "max_chargeable_grams": 1,
                                                 "minimum_base_cents": 0}]}, "bounds")
        self.assert_invalid({"tariffs": [good_tariff], "shipments": [],
                             "dispatch_rules": [{"service": "A", "zone": "Z1",
                                                 "max_pieces": 1,
                                                 "max_chargeable_grams": 0,
                                                 "minimum_base_cents": 0}]}, "bounds")
        self.assert_invalid({"tariffs": [good_tariff], "shipments": [],
                             "dispatch_rules": [{"service": "A", "zone": "Z1",
                                                 "max_pieces": 1,
                                                 "max_chargeable_grams": 1,
                                                 "minimum_base_cents": -1}]}, "bounds")
        self.assert_invalid({"tariffs": [good_tariff], "shipments": [],
                             "dispatch_rules": [good_rule, good_rule]}, "duplicate")
        self.assert_invalid({"tariffs": [good_tariff], "shipments": [],
                             "dispatch_rules": [rule("A", "ZZ")]}, "matching")

    def test_shipment_shape(self):
        base = {"tariffs": [tariff("A", "Z1"), tariff("B", "Z1")],
                "dispatch_rules": [rule("A", "Z1"), rule("B", "Z1")]}
        cases = [
            ([parcel("P1", ("A", "B")), parcel("P1", ("A",))], "duplicate"),
            ([parcel("P1", ())], "nonempty"),
            ([parcel("P1", ("A", "A"))], "duplicate"),
            ([parcel("P1", ("A", "C"))], "dispatch rule"),
            ([{**parcel("P1"), "services": "A"}], "nonempty"),
            ([{**parcel("P1"), "remote": 0}], "boolean"),
            ([{**parcel("P1"), "weight_grams": 0}], "bounds"),
            ([{**parcel("P1"), "id": " spaced "}], "identifier"),
        ]
        for shipments, needle in cases:
            with self.subTest(needle=needle):
                self.assert_invalid({**base, "shipments": shipments}, needle)

    def test_limit_counts(self):
        five_rules = [rule(f"S{i}", "Z1") for i in range(5)]
        five_tariffs = [tariff(f"S{i}", "Z1") for i in range(5)]
        self.assert_invalid({"tariffs": five_tariffs,
                             "dispatch_rules": five_rules, "shipments": []}, "bounded")
        thirteen = [parcel(f"P{i}", ("S0",)) for i in range(13)]
        self.assert_invalid({"tariffs": [tariff("S0", "Z1")],
                             "dispatch_rules": [rule("S0", "Z1")],
                             "shipments": thirteen}, "bounded")


class OracleComparisonTests(unittest.TestCase):
    def build(self, seed):
        services = ["A", "B", "C", "D"][: 1 + seed % 4]
        zone = "Z1"
        tariffs, rules = [], []
        for service in services:
            tariffs.append(tariff(
                service, zone,
                base=(seed * 7) % 300, per_step=(seed * 13 + 5) % 250,
                fuel=(seed * 137) % 1200, insurance=(seed * 31) % 300,
                free=(seed * 11) % 200, remote_fee=(seed * 17) % 400,
                girth=400 + (seed % 3) * 200))
            rules.append(rule(service, zone,
                              pieces=1 + (seed * 3) % 4,
                              grams=800 + (seed * 211) % 3000,
                              minimum=(seed * 53) % 900))
        n = 2 + seed % 6
        shipments = []
        for i in range(n):
            candidate_services = [s for s in services if (i + seed + ord(s)) % 3 != 0]
            if not candidate_services:
                candidate_services = [services[0]]
            shipments.append(parcel(
                f"P{i:02d}", candidate_services,
                zone=zone,
                weight=100 + ((i + 1) * (seed + 2) * 137) % 1400,
                length=50 + ((i + seed) * 53) % 250,
                width=40 + ((i + seed) * 31) % 120,
                height=40 + ((i + seed) * 17) % 120,
                declared=(i * seed * 29) % 5000,
                remote=bool((i + seed) % 2)))
        return {"tariffs": tariffs, "dispatch_rules": rules, "shipments": shipments}

    def test_matches_brute_force_oracle(self):
        for seed in range(60):
            with self.subTest(seed=seed):
                data = self.build(seed)
                report = plan(data)
                expected = oracle(data)
                if expected is None:
                    self.assertFalse(report["feasible"])
                    self.assertIsNone(report["total_cents"])
                    self.assertEqual(report["assignments"], [])
                    self.assertEqual(report["groups"], [])
                    continue
                (total, opened, vector), group_view = expected
                self.assertTrue(report["feasible"])
                self.assertEqual(report["total_cents"], total)
                self.assertEqual(len(report["groups"]), opened)
                ids = sorted(s["id"] for s in data["shipments"])
                actual_vector = tuple(next(a["service"] for a in report["assignments"]
                                           if a["id"] == sid) for sid in ids)
                self.assertEqual(actual_vector, vector)
                actual_groups = sorted(
                    ((g["service"], g["zone"]), tuple(g["member_ids"]))
                    for g in report["groups"])
                self.assertEqual(actual_groups, group_view)
                # Candidate evidence completeness for every declared candidate.
                declared = {(s["id"], service) for s in data["shipments"]
                            for service in s["services"]}
                self.assertEqual({(c["id"], c["service"]) for c in report["candidates"]},
                                 declared)


    def test_matches_brute_force_oracle_at_full_scale(self):
        # Twelve parcels (the supported maximum) keeps the independent oracle
        # at 2^12 combos while exercising the production solver at its full
        # input scale, with loose capacity so feasible optima (not just
        # infeasibility) are compared.  Three/four-service combinatorics are
        # covered by the small-seed oracle test above.
        for seed in range(8):
            with self.subTest(seed=seed):
                services = ["A", "B"]
                data = {
                    "tariffs": [tariff(s, "Z1",
                                       base=(seed * 11) % 200,
                                       per_step=(seed * 7 + 3) % 180,
                                       fuel=(seed * 97) % 1500,
                                       insurance=(seed * 23) % 250,
                                       free=(seed * 13) % 300)
                                for s in services],
                    "dispatch_rules": [rule(s, "Z1", pieces=12,
                                            grams=20000,
                                            minimum=(seed * 61) % 700)
                                       for s in services],
                    "shipments": [
                        parcel(f"P{i:02d}", services,
                               weight=100 + ((i + 1) * (seed + 2) * 137) % 1200,
                               length=50 + ((i + seed) * 53) % 200,
                               width=40 + ((i + seed) * 31) % 100,
                               height=40 + ((i + seed) * 17) % 100,
                               declared=(i * seed * 29) % 4000,
                               remote=bool((i + seed) % 2))
                        for i in range(12)],
                }
                report = plan(data)
                (total, opened, vector), group_view = oracle(data)
                self.assertTrue(report["feasible"])
                self.assertEqual(report["total_cents"], total)
                self.assertEqual(len(report["groups"]), opened)
                ids = sorted(s["id"] for s in data["shipments"])
                actual_vector = tuple(next(a["service"] for a in report["assignments"]
                                           if a["id"] == sid) for sid in ids)
                self.assertEqual(actual_vector, vector)
                actual_groups = sorted(
                    ((g["service"], g["zone"]), tuple(g["member_ids"]))
                    for g in report["groups"])
                self.assertEqual(actual_groups, group_view)


if __name__ == "__main__":
    unittest.main()
