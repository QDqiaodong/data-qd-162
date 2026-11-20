"""Schema v6 release evaluation tests: linked acceptance, validation and an
independent oracle differential.

The oracle in this module never calls the production release code.  It
re-derives the rated baseline with the shared v1 ``analyze`` interface (the
contract defines rated/total_cents by that interface), then computes
selection, grouping and the membership-vector equivalence classes directly
with per-customer boolean vectors -- the representation the production code
is forbidden to use -- so agreement is a genuine cross-check.
"""
import copy
import unittest
from random import Random
from unittest import mock

from parcel_rating import release
from parcel_rating.model import analyze
from parcel_rating.release import evaluate


def tariff(service, zone, per_step=100, base=0, girth=2000, remote=0, fuel=0,
           insurance=0, free=0, step=500):
    return {"service": service, "zone": zone, "base_cents": base,
            "step_grams": step, "per_step_cents": per_step,
            "divisor_cm3_per_kg": 10 ** 9, "max_girth_mm": girth,
            "remote_cents": remote, "fuel_bp": fuel,
            "insurance_bp": insurance, "insurance_free_cents": free}


def ship(pid, service, zone, weight=500, length=100, width=100, height=100,
         declared=0, remote=False):
    return {"id": pid, "service": service, "zone": zone,
            "weight_grams": weight, "length_mm": length, "width_mm": width,
            "height_mm": height, "declared_cents": declared, "remote": remote}


def batch(tariffs, shipments, customers):
    return {"tariffs": tariffs, "shipments": shipments, "customers": customers}


def acceptance_batch():
    """Service S in zones X/Y/Z from customers A/B/C; service T in X from A,B."""
    return batch(
        [tariff("S", "X"), tariff("S", "Y"), tariff("S", "Z"),
         tariff("T", "X")],
        [ship("P1", "S", "X"), ship("P2", "S", "Y"), ship("P3", "S", "Z"),
         ship("P4", "T", "X"), ship("P5", "T", "X")],
        {"P1": "A", "P2": "B", "P3": "C", "P4": "A", "P5": "B"})


def oracle(min_customers, requests, batches):
    """Independent reference: boolean membership vectors over all rows."""
    rated = []
    for b in batches:
        result = analyze({"tariffs": b["tariffs"], "shipments": b["shipments"]})
        where = {row["id"]: (row["service"], row["zone"])
                 for row in b["shipments"]}
        for row in result["shipments"]:
            if row["status"] == "rated":
                service, zone = where[row["id"]]
                rated.append((service, zone, b["customers"][row["id"]],
                              row["total_cents"]))
    published = []  # list of frozensets of customers, one per published row
    out = []
    for req in requests:
        selected = [r for r in rated
                    if (req["services"] is None or r[0] in req["services"])
                    and (req["zones"] is None or r[1] in req["zones"])]
        if not selected:
            out.append({"id": req["id"], "status": "empty", "level": None,
                        "rows": []})
            continue
        outcome = None
        for level in req["levels"]:
            groups = {}
            for service, zone, customer, cents in selected:
                key = {"service_zone": (service, zone),
                       "service": (service,), "zone": (zone,),
                       "total": ()}[level]
                group = groups.setdefault(key, [0, 0, set()])
                group[0] += 1
                group[1] += cents
                group[2].add(customer)
            trial_rows = [frozenset(group[2]) for group in groups.values()]
            all_rows = published + trial_rows
            vectors = {}
            for index, row in enumerate(all_rows):
                for customer in row:
                    vectors.setdefault(customer, set()).add(index)
            classes = {}
            for customer, hits in vectors.items():
                vector = tuple(i in hits for i in range(len(all_rows)))
                classes[vector] = classes.get(vector, 0) + 1
            if all(size >= min_customers for size in classes.values()):
                published.extend(trial_rows)
                rows = [{"key": list(key), "customer_count": len(group[2]),
                         "shipment_count": group[0], "total_cents": group[1]}
                        for key, group in sorted(groups.items())]
                outcome = {"id": req["id"], "status": "published",
                           "level": level, "rows": rows}
                break
        if outcome is None:
            outcome = {"id": req["id"], "status": "withheld", "level": None,
                       "rows": []}
        out.append(outcome)
    return out


class JointAcceptanceTests(unittest.TestCase):
    """The three linked acceptance scenarios from the task."""

    def test_threshold_two_sequence_and_replay(self):
        requests = [
            {"id": "R1", "services": ["S"], "zones": None,
             "levels": ["zone", "total"]},
            {"id": "R2", "services": ["T"], "zones": None,
             "levels": ["total"]},
            {"id": "R3", "services": ["S"], "zones": None,
             "levels": ["zone", "total"]},
        ]
        result = evaluate(2, requests, [acceptance_batch()])
        self.assertEqual([r["status"] for r in result],
                         ["published", "withheld", "published"])
        self.assertEqual(result[0]["level"], "total")
        self.assertEqual(result[0]["rows"], [{"key": [], "customer_count": 3,
                                              "shipment_count": 3,
                                              "total_cents": 300}])
        # R2 has two customers of its own yet stays withheld: publishing its
        # total row would isolate C as a one-customer difference set.
        self.assertEqual(result[1], {"id": "R2", "status": "withheld",
                                     "level": None, "rows": []})
        # The rejected R2 trial must not pollute history: R3, selecting the
        # same data as the published R1, still publishes at total.
        self.assertEqual(result[2]["status"], "published")
        self.assertEqual(result[2]["rows"], result[0]["rows"])

    def test_swapped_request_order_changes_outcome(self):
        first = {"id": "R2", "services": ["T"], "zones": None,
                 "levels": ["total"]}
        second = {"id": "R1", "services": ["S"], "zones": None,
                  "levels": ["zone", "total"]}
        result = evaluate(2, [first, second], [acceptance_batch()])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(result[0]["rows"], [{"key": [], "customer_count": 2,
                                              "shipment_count": 2,
                                              "total_cents": 200}])
        # With T published first, both S granularities fail.
        self.assertEqual(result[1], {"id": "R1", "status": "withheld",
                                     "level": None, "rows": []})

    def test_joint_two_row_release_and_total_fallback(self):
        # One request publishes two rows at once: {A,B,C} and {A,B,D}.
        b = batch(
            [tariff("S1", "Z"), tariff("S2", "Z")],
            [ship("P1", "S1", "Z"), ship("P2", "S1", "Z"), ship("P3", "S1", "Z"),
             ship("P4", "S2", "Z"), ship("P5", "S2", "Z"), ship("P6", "S2", "Z")],
            {"P1": "A", "P2": "B", "P3": "C", "P4": "A", "P5": "B", "P6": "D"})
        # Both rows have three customers, yet the grouping isolates C and D.
        only_service = [{"id": "Q", "services": None, "zones": None,
                         "levels": ["service"]}]
        self.assertEqual(evaluate(2, only_service, [b]),
                         [{"id": "Q", "status": "withheld", "level": None,
                           "rows": []}])
        # With total declared as fallback the four-customer row publishes.
        with_fallback = [{"id": "Q", "services": None, "zones": None,
                          "levels": ["service", "total"]}]
        result = evaluate(2, with_fallback, [b])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(result[0]["level"], "total")
        self.assertEqual(result[0]["rows"], [{"key": [], "customer_count": 4,
                                              "shipment_count": 6,
                                              "total_cents": 600}])

    def test_multi_shipment_customer_cannot_fill_threshold(self):
        # K ships three parcels across two batches; customer_count still
        # counts K once per row, so the threshold-2 run withholds.
        b1 = batch([tariff("S", "Z")], [ship("P1", "S", "Z")], {"P1": "K"})
        b2 = batch([tariff("S", "Z")], [ship("P2", "S", "Z"),
                                        ship("P3", "S", "Z")],
                   {"P2": "K", "P3": "K"})
        requests = [{"id": "Q", "services": None, "zones": None,
                     "levels": ["service_zone", "service", "zone", "total"]}]
        result = evaluate(2, requests, [b1, b2])
        self.assertEqual(result[0]["status"], "withheld")
        # With a real second customer W the row publishes, and K's three
        # parcels still count fully in shipments and cents.
        b3 = batch([tariff("S", "Z")], [ship("P4", "S", "Z")], {"P4": "W"})
        result = evaluate(2, requests, [b1, b2, b3])
        row = result[0]["rows"][0]
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(row["customer_count"], 2)
        self.assertEqual(row["shipment_count"], 4)
        self.assertEqual(row["total_cents"], 400)


class RatingAndSelectionTests(unittest.TestCase):
    def test_exact_amounts_across_different_rate_batches(self):
        # Same service/zone, different tariffs per batch; v1 exact amounts.
        t1 = tariff("S", "Z", per_step=100)
        t2 = tariff("S", "Z", per_step=200, remote=50, fuel=1000)
        b1 = batch([t1], [ship("P1", "S", "Z")], {"P1": "A"})
        b2 = batch([t2], [ship("P2", "S", "Z", remote=True)], {"P2": "B"})
        # Independent expectation straight from the v1 interface.
        cents1 = analyze({"tariffs": [t1], "shipments": b1["shipments"]})
        cents2 = analyze({"tariffs": [t2], "shipments": b2["shipments"]})
        self.assertEqual((cents1["total_cents"], cents2["total_cents"]),
                         (100, 275))
        requests = [{"id": "Q", "services": None, "zones": None,
                     "levels": ["total"]}]
        result = evaluate(2, requests, [b1, b2])
        self.assertEqual(result[0]["rows"], [
            {"key": [], "customer_count": 2, "shipment_count": 2,
             "total_cents": 375}])

    def test_oversize_shipments_never_enter_statistics(self):
        # X's only parcel is oversize: X must not appear in any row, and a
        # filter that matches no rated parcel reports empty.
        big = ship("P0", "S", "Z", length=3000)  # girth 3400 > 2000
        b = batch([tariff("S", "Z")],
                  [big, ship("P1", "S", "Z"), ship("P2", "S", "Z")],
                  {"P0": "X", "P1": "A", "P2": "B"})
        empty = evaluate(2, [{"id": "Q", "services": None, "zones": ["W"],
                              "levels": ["total"]}], [b])
        self.assertEqual(empty[0]["status"], "empty")
        result = evaluate(2, [{"id": "Q", "services": ["S"], "zones": ["Z"],
                               "levels": ["total"]}], [b])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(result[0]["rows"], [
            {"key": [], "customer_count": 2, "shipment_count": 2,
             "total_cents": 200}])

    def test_empty_status_for_unknown_but_legal_selection(self):
        b = batch([tariff("S", "Z")], [ship("P1", "S", "Z"),
                                       ship("P2", "S", "Z")],
                  {"P1": "A", "P2": "B"})
        requests = [{"id": "Q", "services": ["UNKNOWN"], "zones": None,
                     "levels": ["total"]}]
        self.assertEqual(evaluate(2, requests, [b]),
                         [{"id": "Q", "status": "empty", "level": None,
                           "rows": []}])

    def test_row_key_order_and_shape(self):
        # Every cell holds the same two customers, so service_zone publishes.
        b = batch(
            [tariff("B", "Z2"), tariff("A", "Z2"), tariff("A", "Z1")],
            [ship("P1", "B", "Z2"), ship("P2", "A", "Z2"),
             ship("P3", "A", "Z1"), ship("P4", "A", "Z1"),
             ship("P5", "B", "Z2"), ship("P6", "A", "Z2")],
            {"P1": "C1", "P2": "C1", "P3": "C1", "P4": "C2", "P5": "C2",
             "P6": "C2"})
        requests = [{"id": "Q", "services": None, "zones": None,
                     "levels": ["service_zone"]}]
        result = evaluate(2, requests, [b])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual([row["key"] for row in result[0]["rows"]],
                         [["A", "Z1"], ["A", "Z2"], ["B", "Z2"]])
        for row in result[0]["rows"]:
            self.assertEqual(set(row), {"key", "customer_count",
                                        "shipment_count", "total_cents"})
        self.assertEqual([row["customer_count"] for row in result[0]["rows"]],
                         [2, 2, 2])

    def test_same_named_rows_are_distinct_observations(self):
        # R1 publishes row key [S1] = {A,B} via zone Z1.  R2's row key is
        # also [S1] but covers {C} alone: as a distinct observation it adds
        # a one-customer class and must withhold (collapsing same-key rows
        # into one observation would union them into a passing {A,B,C}).
        tariffs = [tariff("S1", "Z1"), tariff("S1", "Z2")]
        b = batch(tariffs,
                  [ship("P1", "S1", "Z1"), ship("P2", "S1", "Z1"),
                   ship("P3", "S1", "Z2")],
                  {"P1": "A", "P2": "B", "P3": "C"})
        requests = [
            {"id": "R1", "services": None, "zones": ["Z1"],
             "levels": ["service"]},
            {"id": "R2", "services": None, "zones": ["Z2"],
             "levels": ["service"]},
            {"id": "R3", "services": None, "zones": ["Z1"],
             "levels": ["service"]},
        ]
        result = evaluate(2, requests, [b])
        self.assertEqual([r["status"] for r in result],
                         ["published", "withheld", "published"])
        self.assertEqual(result[0]["rows"], [
            {"key": ["S1"], "customer_count": 2, "shipment_count": 2,
             "total_cents": 200}])
        self.assertEqual(result[2]["rows"], result[0]["rows"])

    def test_customer_in_multiple_rows_of_one_report(self):
        # A and B each ship in two different cells, so both appear in two
        # rows of the same service_zone report; their shared pattern keeps
        # them in one equivalence class of exactly two.
        b = batch(
            [tariff("S", "X"), tariff("S", "Y")],
            [ship("P1", "S", "X"), ship("P2", "S", "X"),
             ship("P3", "S", "Y"), ship("P4", "S", "Y")],
            {"P1": "A", "P2": "B", "P3": "A", "P4": "B"})
        requests = [{"id": "Q", "services": None, "zones": None,
                     "levels": ["service_zone"]}]
        result = evaluate(2, requests, [b])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(result[0]["rows"], [
            {"key": ["S", "X"], "customer_count": 2, "shipment_count": 2,
             "total_cents": 200},
            {"key": ["S", "Y"], "customer_count": 2, "shipment_count": 2,
             "total_cents": 200}])
        # A probe isolating just one of the cells adds nothing new: its rows
        # are already exact classes, so it publishes too.
        probe = [{"id": "Q2", "services": None, "zones": ["X"],
                  "levels": ["service_zone"]}]
        result = evaluate(2, requests + probe, [b])
        self.assertEqual(result[1]["status"], "published")

    def test_cross_cutting_levels_narrow_classes(self):
        # R1 publishes service rows {A,B,C,D} and {E,F,G,H}; R2 publishes
        # cross-cutting zone rows, narrowing classes to {A,B}, {C,D}, {E,F},
        # {G,H}; a later probe aligned with one class still publishes.
        tariffs = [tariff("S1", "Z1"), tariff("S1", "Z2"), tariff("S2", "Z1"),
                   tariff("S2", "Z2")]
        customers = {}
        shipments = []
        names = ["A", "B", "C", "D", "E", "F", "G", "H"]
        plan = [("S1", "Z1"), ("S1", "Z1"), ("S1", "Z2"), ("S1", "Z2"),
                ("S2", "Z1"), ("S2", "Z1"), ("S2", "Z2"), ("S2", "Z2")]
        for i, ((service, zone), customer) in enumerate(zip(plan, names)):
            shipments.append(ship("P%d" % i, service, zone))
            customers["P%d" % i] = customer
        b = batch(tariffs, shipments, customers)
        requests = [
            {"id": "R1", "services": None, "zones": None,
             "levels": ["service"]},
            {"id": "R2", "services": None, "zones": None, "levels": ["zone"]},
            {"id": "R3", "services": ["S1"], "zones": ["Z1"],
             "levels": ["total"]},
        ]
        result = evaluate(2, requests, [b])
        self.assertEqual([r["status"] for r in result],
                         ["published", "published", "published"])
        self.assertEqual(result[2]["rows"], [
            {"key": [], "customer_count": 2, "shipment_count": 2,
             "total_cents": 200}])


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, min_customers, requests, batches):
        with self.assertRaises(ValueError):
            evaluate(min_customers, requests, batches)

    def valid_batch(self):
        return batch([tariff("S", "Z")], [ship("P1", "S", "Z")], {"P1": "A"})

    def valid_requests(self):
        return [{"id": "Q", "services": None, "zones": None,
                 "levels": ["total"]}]

    def test_header_validation(self):
        b = [self.valid_batch()]
        self.assert_invalid(1, self.valid_requests(), b)
        self.assert_invalid(101, self.valid_requests(), b)
        self.assert_invalid(True, self.valid_requests(), b)
        self.assert_invalid(2.0, self.valid_requests(), b)
        self.assert_invalid("2", self.valid_requests(), b)
        self.assert_invalid(2, "not-a-list", b)
        self.assert_invalid(2, [{"id": "Q"}], b)  # missing fields
        extra = [dict(self.valid_requests()[0], levelz=["total"])]
        self.assert_invalid(2, extra, b)  # unknown field

    def test_request_validation(self):
        b = [self.valid_batch()]
        base = self.valid_requests()[0]
        self.assert_invalid(2, [dict(base, id=""), ], b)
        self.assert_invalid(2, [base, base], b)  # duplicate request id
        self.assert_invalid(2, [dict(base, services=[])], b)
        self.assert_invalid(2, [dict(base, services=["S", "S"])], b)
        self.assert_invalid(2, [dict(base, services=[" S"])], b)
        self.assert_invalid(2, [dict(base, zones=[1])], b)
        self.assert_invalid(2, [dict(base, levels=[])], b)
        self.assert_invalid(2, [dict(base, levels=["total", "total"])], b)
        self.assert_invalid(2, [dict(base, levels=["customer"])], b)
        self.assert_invalid(2, [dict(base, levels=[["total"]])], b)
        too_many = [dict(base, id="R%d" % i) for i in range(25)]
        self.assert_invalid(2, too_many, b)
        okay = [dict(base, id="R%d" % i) for i in range(24)]
        self.assertEqual(len(evaluate(2, okay, b)), 24)

    def test_batch_validation(self):
        req = self.valid_requests()
        self.assert_invalid(2, req, [{"tariffs": [], "shipments": []}])
        self.assert_invalid(2, req, [dict(self.valid_batch(), extra=1)])
        self.assert_invalid(2, req, [batch([tariff("S", "Z")],
                                           [ship("P1", "S", "Z")],
                                           {"P1": "A", "PX": "B"})])
        self.assert_invalid(2, req, [batch([tariff("S", "Z")],
                                           [ship("P1", "S", "Z")], {})])
        self.assert_invalid(2, req, [batch([tariff("S", "Z")],
                                           [ship("P1", "S", "Z")],
                                           {"P1": "  "})])
        self.assert_invalid(2, req, [batch([tariff("S", "Z")],
                                           [ship("P1", "S", "Z")],
                                           {"P1": 7})])
        # v1-invalid data: unknown service tariff, bad numbers, duplicates.
        self.assert_invalid(2, req, [batch([], [ship("P1", "S", "Z")],
                                           {"P1": "A"})])
        self.assert_invalid(2, req, [batch([tariff("S", "Z")],
                                           [ship("P1", "S", "Z", weight=0)],
                                           {"P1": "A"})])
        self.assert_invalid(2, req, [batch([tariff("S", "Z")],
                                           [ship("P1", "S", "Z"),
                                            ship("P1", "S", "Z")],
                                           {"P1": "A"})])
        # Duplicate shipment id across batches.
        self.assert_invalid(2, req, [self.valid_batch(), self.valid_batch()])
        # customers must be an object, not an array.
        self.assert_invalid(2, req, [{"tariffs": [tariff("S", "Z")],
                                      "shipments": [ship("P1", "S", "Z")],
                                      "customers": ["A"]}])

    def test_service_and_zone_limits_count_rated_only(self):
        req = self.valid_requests()
        tariffs = [tariff("S%d" % i, "Z") for i in range(13)]
        shipments = [ship("P%d" % i, "S%d" % i, "Z") for i in range(13)]
        customers = {"P%d" % i: "C%d" % i for i in range(13)}
        self.assert_invalid(2, req, [batch(tariffs, shipments, customers)])
        zones = [tariff("S", "Z%d" % i) for i in range(13)]
        shipments = [ship("P%d" % i, "S", "Z%d" % i) for i in range(13)]
        self.assert_invalid(2, req, [batch(zones, shipments, customers)])
        # Twelve is fine; a thirteenth service that is only oversize is too.
        tariffs = [tariff("S%d" % i, "Z") for i in range(12)]
        tariffs.append(tariff("BIG", "Z", girth=100))
        shipments = [ship("P%d" % i, "S%d" % i, "Z") for i in range(12)]
        shipments.append(ship("PX", "BIG", "Z"))
        customers = {"P%d" % i: "C%d" % i for i in range(12)}
        customers["PX"] = "CX"
        result = evaluate(2, req, [batch(tariffs, shipments, customers)])
        self.assertEqual(result[0]["status"], "published")

    def test_shipment_limit(self):
        req = self.valid_requests()
        with mock.patch.object(release, "SHIPMENT_LIMIT", 3):
            batches = [batch([tariff("S", "Z")],
                             [ship("P%d" % i, "S", "Z") for i in range(2)],
                             {"P%d" % i: "C%d" % i for i in range(2)})
                       for _ in range(2)]
            self.assert_invalid(2, req, batches)
            self.assertEqual(evaluate(2, req, batches[:1])[0]["status"],
                             "published")


class OracleDifferentialTests(unittest.TestCase):
    """Randomized agreement between production and the independent oracle."""

    def make_sample(self, rng):
        services = ["S%d" % i for i in range(rng.randint(1, 4))]
        zones = ["Z%d" % i for i in range(rng.randint(1, 3))]
        customers = ["C%d" % i for i in range(rng.randint(1, 8))]
        tariffs = [tariff(s, z, per_step=rng.choice([0, 100, 250]),
                          girth=rng.choice([550, 2000]))
                   for s in services for z in zones]
        batches = []
        serial = 0
        for _ in range(rng.randint(1, 5)):
            shipments = []
            mapping = {}
            for _ in range(rng.randint(0, 8)):
                pid = "P%d" % serial
                serial += 1
                service = rng.choice(services)
                zone = rng.choice(zones)
                shipments.append(ship(pid, service, zone,
                                      weight=rng.choice([100, 500, 1500]),
                                      length=rng.choice([100, 900]),
                                      remote=rng.choice([False, True])))
                mapping[pid] = rng.choice(customers)
            needed = {(s["service"], s["zone"]) for s in shipments}
            chosen = [t for t in tariffs if (t["service"], t["zone"]) in needed]
            chosen += [t for t in tariffs
                       if (t["service"], t["zone"]) not in needed
                       and rng.random() < 0.3]
            batches.append(batch(chosen, shipments, mapping))
        requests = []
        for i in range(rng.randint(1, 6)):
            def pick(options):
                if rng.random() < 0.4:
                    return None
                count = rng.randint(1, len(options))
                return rng.sample(options, count)
            levels = rng.sample(["service_zone", "service", "zone", "total"],
                                rng.randint(1, 4))
            requests.append({"id": "R%d" % i, "services": pick(services),
                             "zones": pick(zones), "levels": levels})
        return rng.randint(2, 4), requests, batches

    def test_differential(self):
        for seed in range(300):
            rng = Random(seed)
            min_customers, requests, batches = self.make_sample(rng)
            with self.subTest(seed=seed):
                self.assertEqual(
                    evaluate(min_customers, requests, batches),
                    oracle(min_customers, requests, batches))

    def test_reorder_invariance(self):
        for seed in range(40):
            rng = Random(1000 + seed)
            min_customers, requests, batches = self.make_sample(rng)
            expected = evaluate(min_customers, requests, batches)
            shuffled = []
            for b in batches:
                perm = list(range(len(b["shipments"])))
                rng.shuffle(perm)
                shipments = [b["shipments"][i] for i in perm]
                mapping = {pid: b["customers"][pid] for pid in
                           rng.sample(list(b["customers"]),
                                      len(b["customers"]))}
                tariffs = list(b["tariffs"])
                rng.shuffle(tariffs)
                shuffled.append(batch(tariffs, shipments, mapping))
            rng.shuffle(shuffled)
            requests2 = []
            for req in requests:
                req = dict(req)
                if req["services"] is not None:
                    req["services"] = list(req["services"])
                    rng.shuffle(req["services"])
                if req["zones"] is not None:
                    req["zones"] = list(req["zones"])
                    rng.shuffle(req["zones"])
                requests2.append(req)
            with self.subTest(seed=seed):
                self.assertEqual(expected, evaluate(min_customers, requests2,
                                                    shuffled))

    def test_production_does_not_mutate_inputs(self):
        rng = Random(7)
        min_customers, requests, batches = self.make_sample(rng)
        frozen = copy.deepcopy(batches)
        evaluate(min_customers, requests, batches)
        self.assertEqual(batches, frozen)


if __name__ == "__main__":
    unittest.main()
