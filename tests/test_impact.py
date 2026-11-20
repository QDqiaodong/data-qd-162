"""Schema v7 price-impact tests.

The independent oracle below never calls a production rating helper: it
re-implements the v1 per-piece arithmetic, enumerates every candidate
correction delta for small inputs, and derives every per-shipment and
per-batch extremum and witness directly.  Production event-sweep output must
agree with it exactly.
"""
import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path

from parcel_rating.impact import evaluate

ROOT = Path(__file__).resolve().parents[1]

WEIGHT_MAX = 10 ** 9


# ---------------------------------------------------------------------------
# Independent oracle: own arithmetic, exhaustive delta enumeration
# ---------------------------------------------------------------------------

def _ceil(value):
    return -(-value.numerator // value.denominator)


def _half_up(value):
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _oracle_rate(parcel, tariff):
    """Return ('oversize', None) or ('rated', total_cents)."""
    weight, length, width, height, declared, remote = parcel
    girth = length + 2 * (width + height)
    if girth > tariff['max_girth_mm']:
        return 'oversize', None
    dimensional = Fraction(length * width * height, tariff['divisor_cm3_per_kg'])
    chargeable = (_ceil(max(Fraction(weight), dimensional) / tariff['step_grams'])
                  * tariff['step_grams'])
    steps = chargeable // tariff['step_grams']
    base = (tariff['base_cents'] + steps * tariff['per_step_cents']
            + (tariff['remote_cents'] if remote else 0))
    fuel = _half_up(Fraction(base * tariff['fuel_bp'], 10000))
    insured = max(0, declared - tariff['insurance_free_cents'])
    insurance = _half_up(Fraction(insured * tariff['insurance_bp'], 10000))
    return 'rated', base + fuel + insurance


def _oracle_interval(batch, members):
    lower = batch['min_delta_grams']
    upper = batch['max_delta_grams']
    for reference in batch['references']:
        reading = reference['reading_grams']
        lower = max(lower, reference['true_min_grams'] - reading)
        upper = min(upper, reference['true_max_grams'] - reading)
    for member in members:
        lower = max(lower, 1 - member['weight_grams'])
        upper = min(upper, WEIGHT_MAX - member['weight_grams'])
    return lower, upper


def oracle(data):
    old = {(t['service'], t['zone']): t for t in data['old_tariffs']}
    new = {(t['service'], t['zone']): t for t in data['new_tariffs']}
    batches = {b['scale_batch_id']: b for b in data['scale_batches']}
    grouped = {bid: [] for bid in batches}
    for shipment in data['shipments']:
        grouped[shipment['scale_batch_id']].append(shipment)

    shipments_out = []
    batches_out = []
    complete = True
    for batch_id in sorted(batches):
        batch = batches[batch_id]
        members = grouped[batch_id]
        lower, upper = _oracle_interval(batch, members)
        infeasible = lower > upper
        if infeasible:
            complete = False
        names = []
        for shipment in members:
            names.append(shipment['id'])
            parcel = (shipment['weight_grams'], shipment['length_mm'],
                      shipment['width_mm'], shipment['height_mm'],
                      shipment['declared_cents'], shipment['remote'])
            key = (shipment['service'], shipment['zone'])
            old_status, _ = _oracle_rate(parcel, old[key])
            new_status, _ = _oracle_rate(parcel, new[key])
            delta = None
            if not infeasible and old_status == 'rated' and new_status == 'rated':
                values = []
                for d in range(lower, upper + 1):
                    corrected = parcel[0] + d
                    _, before = _oracle_rate((corrected,) + parcel[1:], old[key])
                    _, after = _oracle_rate((corrected,) + parcel[1:], new[key])
                    values.append((d, after - before))
                best_low = min(v for _, v in values)
                best_high = max(v for _, v in values)
                low_at = min(d for d, v in values if v == best_low)
                high_at = min(d for d, v in values if v == best_high)
                delta = {'min_cents': best_low, 'max_cents': best_high,
                         'min_at_grams': low_at, 'max_at_grams': high_at}
            shipments_out.append({
                'id': shipment['id'], 'scale_batch_id': batch_id,
                'old_status': old_status, 'new_status': new_status,
                'delta_cents': delta})
        if infeasible:
            batches_out.append({
                'scale_batch_id': batch_id, 'status': 'conflict',
                'lower_delta_grams': lower, 'upper_delta_grams': upper,
                'net_change_cents': None, 'increase_cents': None})
            continue
        nets, increases = [], []
        for d in range(lower, upper + 1):
            net = increase = 0
            for shipment in members:
                parcel = (shipment['weight_grams'] + d, shipment['length_mm'],
                          shipment['width_mm'], shipment['height_mm'],
                          shipment['declared_cents'], shipment['remote'])
                key = (shipment['service'], shipment['zone'])
                old_status, before = _oracle_rate(parcel, old[key])
                new_status, after = _oracle_rate(parcel, new[key])
                if old_status != 'rated' or new_status != 'rated':
                    continue
                net += after - before
                increase += max(0, after - before)
            nets.append((d, net))
            increases.append((d, increase))

        def extremes(values):
            low = min(v for _, v in values)
            high = max(v for _, v in values)
            return {'min_cents': low, 'max_cents': high,
                    'min_at_grams': min(d for d, v in values if v == low),
                    'max_at_grams': min(d for d, v in values if v == high)}

        batches_out.append({
            'scale_batch_id': batch_id, 'status': 'feasible',
            'lower_delta_grams': lower, 'upper_delta_grams': upper,
            'net_change_cents': extremes(nets),
            'increase_cents': extremes(increases)})

    shipments_out.sort(key=lambda row: row['id'])
    batches_out.sort(key=lambda row: row['scale_batch_id'])
    if not complete:
        portfolio = {'status': 'incomplete', 'net_change_cents': None,
                     'increase_cents': None}
    else:
        portfolio = {
            'status': 'complete',
            'net_change_cents': {
                'min_cents': sum(r['net_change_cents']['min_cents'] for r in batches_out),
                'max_cents': sum(r['net_change_cents']['max_cents'] for r in batches_out)},
            'increase_cents': {
                'min_cents': sum(r['increase_cents']['min_cents'] for r in batches_out),
                'max_cents': sum(r['increase_cents']['max_cents'] for r in batches_out)},
        }
    return {'shipments': shipments_out, 'scale_batches': batches_out,
            'portfolio': portfolio}


# ---------------------------------------------------------------------------
# Input builders
# ---------------------------------------------------------------------------

def tariff(service='S', zone='Z', base=0, step=500, per=100, divisor=10 ** 9,
           girth=2000, remote=0, fuel=0, insurance=0, free=0):
    return {'service': service, 'zone': zone, 'base_cents': base,
            'step_grams': step, 'per_step_cents': per,
            'divisor_cm3_per_kg': divisor, 'max_girth_mm': girth,
            'remote_cents': remote, 'fuel_bp': fuel,
            'insurance_bp': insurance, 'insurance_free_cents': free}


def ship(pid, weight, batch='B1', service='S', zone='Z', length=100,
         width=100, height=100, declared=0, remote=False):
    return {'id': pid, 'service': service, 'zone': zone,
            'weight_grams': weight, 'length_mm': length, 'width_mm': width,
            'height_mm': height, 'declared_cents': declared,
            'remote': remote, 'scale_batch_id': batch}


def reference(rid, reading, true_min, true_max):
    return {'reference_id': rid, 'reading_grams': reading,
            'true_min_grams': true_min, 'true_max_grams': true_max}


def batch(bid, lower, upper, references=None):
    if references is None:
        # A neutral reference that never narrows the declared interval.
        references = [reference('REF', 1000, 1000 + max(lower, -1000),
                                1000 + upper)]
    return {'scale_batch_id': bid, 'min_delta_grams': lower,
            'max_delta_grams': upper, 'references': references}


def input_document(old, new, batches, shipments):
    return {'old_tariffs': old, 'new_tariffs': new,
            'scale_batches': batches, 'shipments': shipments}


# ---------------------------------------------------------------------------
# Named acceptance scenarios
# ---------------------------------------------------------------------------

class ScenarioTests(unittest.TestCase):
    """The explicitly required business scenarios."""

    def setUp(self):
        # Base 50 cents + 100 per 500 g vs base 0 + 200 per 1000 g.
        self.old = tariff(base=50, step=500, per=100)
        self.new = tariff(base=0, step=1000, per=200)

    def test_shared_batch_net_and_exposure(self):
        data = input_document(
            [self.old], [self.new], [batch('B1', 0, 1)],
            [ship('P', 500), ship('Q', 1000)])
        result = evaluate(data)
        rows = {row['id']: row for row in result['shipments']}
        # delta 0 -> (+50, -50); delta 1 -> (-50, +50).
        self.assertEqual(
            (rows['P']['delta_cents']['min_cents'],
             rows['P']['delta_cents']['max_cents'],
             rows['P']['delta_cents']['min_at_grams'],
             rows['P']['delta_cents']['max_at_grams']),
            (-50, 50, 1, 0))
        self.assertEqual(
            (rows['Q']['delta_cents']['min_cents'],
             rows['Q']['delta_cents']['max_cents'],
             rows['Q']['delta_cents']['min_at_grams'],
             rows['Q']['delta_cents']['max_at_grams']),
            (-50, 50, 0, 1))
        entry = result['scale_batches'][0]
        # Net is identically zero; exposure is identically 50.
        self.assertEqual(entry['net_change_cents'],
                         {'min_cents': 0, 'max_cents': 0,
                          'min_at_grams': 0, 'max_at_grams': 0})
        self.assertEqual(entry['increase_cents'],
                         {'min_cents': 50, 'max_cents': 50,
                          'min_at_grams': 0, 'max_at_grams': 0})
        # The required traps: per-shipment extremes must not be summed and
        # net must not be clipped to nonnegative before the exposure sum.
        self.assertEqual(result['portfolio'],
                         {'status': 'complete',
                          'net_change_cents': {'min_cents': 0, 'max_cents': 0},
                          'increase_cents': {'min_cents': 50, 'max_cents': 50}})

    def test_pinned_to_one_only_q_increases(self):
        pinned = [reference('P-REF', 500, 501, 501),
                  reference('Q-REF', 1000, 1001, 1001)]
        data = input_document(
            [self.old], [self.new], [batch('B1', 0, 1, pinned)],
            [ship('P', 500), ship('Q', 1000)])
        result = evaluate(data)
        rows = {row['id']: row for row in result['shipments']}
        self.assertEqual(rows['P']['delta_cents'],
                         {'min_cents': -50, 'max_cents': -50,
                          'min_at_grams': 1, 'max_at_grams': 1})
        self.assertEqual(rows['Q']['delta_cents'],
                         {'min_cents': 50, 'max_cents': 50,
                          'min_at_grams': 1, 'max_at_grams': 1})
        entry = result['scale_batches'][0]
        self.assertEqual((entry['lower_delta_grams'], entry['upper_delta_grams']),
                         (1, 1))
        self.assertEqual(entry['increase_cents']['max_cents'], 50)
        self.assertEqual(entry['increase_cents']['max_at_grams'], 1)
        self.assertEqual(result['portfolio']['increase_cents'],
                         {'min_cents': 50, 'max_cents': 50})

    def test_split_batches_portfolio_exposure(self):
        data = input_document(
            [self.old], [self.new], [batch('B1', 0, 1), batch('B2', 0, 1)],
            [ship('P', 500, 'B1'), ship('Q', 1000, 'B2')])
        result = evaluate(data)
        self.assertEqual(result['portfolio']['increase_cents'],
                         {'min_cents': 0, 'max_cents': 100})
        self.assertEqual(result['portfolio']['net_change_cents'],
                         {'min_cents': -100, 'max_cents': 100})

    def test_wide_interval_interior_maximum_witness(self):
        data = input_document(
            [self.old], [self.new], [batch('B1', 0, 1000)], [ship('Q', 1000)])
        result = evaluate(data)
        row = result['shipments'][0]['delta_cents']
        self.assertEqual((row['min_cents'], row['max_cents']), (-50, 50))
        self.assertEqual(row['min_at_grams'], 0)
        self.assertEqual(row['max_at_grams'], 1)
        # Net reaches its extremes only at interior corrections too.
        entry = result['scale_batches'][0]
        self.assertEqual(entry['net_change_cents']['min_cents'], -50)
        self.assertEqual(entry['net_change_cents']['max_cents'], 50)
        self.assertEqual(entry['net_change_cents']['max_at_grams'], 1)

    def test_empty_input(self):
        result = evaluate(input_document([], [], [], []))
        self.assertEqual(result, {
            'shipments': [], 'scale_batches': [],
            'portfolio': {'status': 'complete',
                          'net_change_cents': {'min_cents': 0, 'max_cents': 0},
                          'increase_cents': {'min_cents': 0, 'max_cents': 0}}})

    def test_conflict_keeps_other_batches(self):
        old = tariff(base=10)
        data = input_document(
            [old], [old],
            [batch('B1', -5, 5, [reference('A', 1000, 1010, 1010),
                                 reference('B', 1000, 980, 980)]),
             batch('B2', 0, 0, [reference('C', 1000, 1000, 1000)])],
            [ship('P', 1000, 'B1'), ship('Q', 1000, 'B2')])
        result = evaluate(data)
        self.assertEqual([row['scale_batch_id'] for row in result['scale_batches']],
                         ['B1', 'B2'])
        conflict = result['scale_batches'][0]
        self.assertEqual(conflict['status'], 'conflict')
        self.assertEqual((conflict['lower_delta_grams'],
                          conflict['upper_delta_grams']), (10, -20))
        self.assertIsNone(conflict['net_change_cents'])
        self.assertIsNone(conflict['increase_cents'])
        self.assertEqual(result['shipments'][0]['delta_cents'], None)
        self.assertEqual(result['shipments'][0]['old_status'], 'rated')
        normal = result['scale_batches'][1]
        self.assertEqual(normal['status'], 'feasible')
        self.assertEqual(normal['increase_cents']['max_cents'], 0)
        self.assertEqual(result['portfolio']['status'], 'incomplete')
        self.assertIsNone(result['portfolio']['net_change_cents'])
        self.assertIsNone(result['portfolio']['increase_cents'])

    def test_oversize_verdicts_on_both_tables(self):
        # girth = 400 + 2*(300+200) = 1400: new table rejects above 1300.
        old = tariff(base=100, girth=2000)
        new = tariff(base=100, girth=1300)
        data = input_document(
            [old], [new], [batch('B1', 0, 0)],
            [ship('P', 1000, length=400, width=300, height=200)])
        result = evaluate(data)
        row = result['shipments'][0]
        self.assertEqual((row['old_status'], row['new_status']),
                         ('rated', 'oversize'))
        self.assertIsNone(row['delta_cents'])
        # A ticket oversize on one side is never a zero-cent quote.
        self.assertEqual(result['scale_batches'][0]['increase_cents']['max_cents'], 0)
        self.assertEqual(result['scale_batches'][0]['net_change_cents']['max_cents'], 0)

    def test_all_oversize_batch_is_zero_totals(self):
        strict = tariff(base=100, girth=100)
        data = input_document(
            [strict], [strict], [batch('B1', 0, 3)],
            [ship('P', 1000, length=400, width=300, height=200)])
        result = evaluate(data)
        entry = result['scale_batches'][0]
        self.assertEqual(entry['status'], 'feasible')
        self.assertEqual(entry['net_change_cents'],
                         {'min_cents': 0, 'max_cents': 0,
                          'min_at_grams': 0, 'max_at_grams': 0})
        self.assertEqual(entry['increase_cents'],
                         {'min_cents': 0, 'max_cents': 0,
                          'min_at_grams': 0, 'max_at_grams': 0})

    def test_shipment_weight_bounds_constrain_interval(self):
        # weight 2 with declared [-5,5]: corrected weight must stay >= 1.
        data = input_document(
            [self.old], [self.new], [batch('B1', -5, 5)],
            [ship('T', 2)])
        result = evaluate(data)
        entry = result['scale_batches'][0]
        self.assertEqual((entry['lower_delta_grams'],
                          entry['upper_delta_grams']), (-1, 5))

    def test_different_dimensional_weights_per_table(self):
        # Different divisors make the two tables' volume weights differ, so
        # the same corrected reading can fall in different step ceilings.
        old = tariff(base=0, step=500, per=100, divisor=5000, girth=10000)
        new = tariff(base=0, step=500, per=100, divisor=1000, girth=10000)
        data = input_document(
            [old], [new], [batch('B1', 0, 0)],
            [ship('P', 300, length=500, width=500, height=500)])
        result = evaluate(data)
        # volume 125000 cm3: old dim = 25000 g -> 50 steps; new dim = 125000 g
        # -> 250 steps. Both tables rate from their own dimensional weight.
        self.assertEqual(result['shipments'][0]['delta_cents']['min_cents'],
                         200 * 100)

    def test_fuel_half_up_boundary(self):
        # base 5 cents with fuel 5000 bp -> exactly 2.5 -> half-up 3, matching
        # the v1 rate. Differences therefore include the rounded fuel.
        old = tariff(base=1, step=500, per=2, fuel=5000)
        new = tariff(base=0, step=500, per=2, fuel=5000)
        data = input_document(
            [old], [new], [batch('B1', 0, 0)], [ship('P', 1000)])
        result = evaluate(data)
        # old base = 1 + 2*2 = 5 -> fuel half-up 3 -> total 8;
        # new base = 0 + 2*2 = 4 -> fuel 2 -> total 6.
        self.assertEqual(result['shipments'][0]['delta_cents']['min_cents'], -2)

    def test_delta_rolled_up_per_table_against_rate_parcel(self):
        # The report's extremes must be reproducible by the shared v1 per-piece
        # rating at the same corrected weight, using each table independently.
        from parcel_rating.model import rate_parcel
        old = tariff(base=37, step=250, per=19, divisor=4000, girth=2600,
                     remote=13, fuel=777, insurance=333, free=1000)
        new = tariff(base=11, step=1000, per=61, divisor=6000, girth=3000,
                     remote=7, fuel=1234, insurance=901, free=2000)
        data = input_document(
            [old], [new], [batch('B1', -50, 50)],
            [ship('P', 4000, length=250, width=180, height=120,
                  declared=9000, remote=True)])
        result = evaluate(data)
        row = result['shipments'][0]['delta_cents']
        self.assertTrue(row['min_cents'] < row['max_cents'])
        weights = {'old': old, 'new': new}
        for field, value in (('min_cents', row['min_cents']),
                             ('max_cents', row['max_cents'])):
            delta = row['min_at_grams' if field == 'min_cents' else 'max_at_grams']
            corrected = 4000 + delta
            totals = {}
            for name, table in weights.items():
                status, detail = rate_parcel(
                    (corrected, 250, 180, 120, 9000, True),
                    {'base': table['base_cents'], 'step': table['step_grams'],
                     'rate': table['per_step_cents'],
                     'divisor': table['divisor_cm3_per_kg'],
                     'girth': table['max_girth_mm'],
                     'remote': table['remote_cents'],
                     'fuel': table['fuel_bp'],
                     'insurance': table['insurance_bp'],
                     'free': table['insurance_free_cents']})
                self.assertEqual(status, 'rated')
                totals[name] = detail['total_cents']
            self.assertEqual(value, totals['new'] - totals['old'])


# ---------------------------------------------------------------------------
# Randomized exhaustive oracle differential test
# ---------------------------------------------------------------------------

class OracleDifferentialTests(unittest.TestCase):
    def test_random_documents_against_brute_force(self):
        import random
        rng = random.Random(20261120)
        for case in range(60):
            with self.subTest(case=case):
                old, new = [], []
                keys = [(s, z) for s in ('A', 'B') for z in ('X', 'Y')]
                for key in keys:
                    old.append(tariff(key[0], key[1],
                                      base=rng.choice([0, 10, 50, 120]),
                                      step=rng.choice([250, 500, 1000]),
                                      per=rng.choice([0, 40, 100, 200]),
                                      divisor=rng.choice([5000, 10 ** 9, 3333]),
                                      girth=rng.choice([500, 1300, 2000]),
                                      remote=rng.choice([0, 25]),
                                      fuel=rng.choice([0, 850, 1250]),
                                      insurance=rng.choice([0, 100, 250]),
                                      free=rng.choice([0, 5000])))
                    new.append(tariff(key[0], key[1],
                                      base=rng.choice([0, 10, 50, 120]),
                                      step=rng.choice([250, 500, 1000]),
                                      per=rng.choice([0, 40, 100, 200]),
                                      divisor=rng.choice([5000, 10 ** 9, 3333]),
                                      girth=rng.choice([500, 1300, 2000]),
                                      remote=rng.choice([0, 25]),
                                      fuel=rng.choice([0, 850, 1250]),
                                      insurance=rng.choice([0, 100, 250]),
                                      free=rng.choice([0, 5000])))
                batches, shipments = [], []
                batch_count = rng.randint(1, 3)
                for b in range(batch_count):
                    bid = 'B%d' % b
                    lower = rng.randint(-30, 10)
                    upper = lower + rng.randint(0, 40)
                    refs = []
                    for r in range(rng.randint(1, 3)):
                        reading = rng.randint(200, 2000)
                        lo = reading + rng.randint(-25, 15)
                        hi = lo + rng.randint(0, 25)
                        refs.append(reference('R%d' % r, reading,
                                              max(1, lo), max(lo, hi)))
                    batches.append(batch(bid, lower, upper, refs))
                    for m in range(rng.randint(1, 4)):
                        service, zone = rng.choice(keys)
                        shipments.append(ship(
                            'S%d-%d' % (b, m),
                            rng.randint(100, 3000), bid, service, zone,
                            length=rng.choice([100, 200, 400]),
                            width=rng.choice([100, 150, 300]),
                            height=rng.choice([100, 120, 200]),
                            declared=rng.choice([0, 4000, 9000]),
                            remote=rng.choice([False, True])))
                rng.shuffle(batches)
                rng.shuffle(shipments)
                document = input_document(old, new, batches, shipments)
                snapshot = copy.deepcopy(document)
                self.assertEqual(evaluate(document), oracle(document))
                self.assertEqual(document, snapshot)  # input never mutated


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def base(self):
        return input_document(
            [tariff()], [tariff(base=10)], [batch('B1', 0, 5)], [ship('P', 1000)])

    def assert_invalid(self, mutate):
        data = self.base()
        mutate(data)
        with self.assertRaises(ValueError):
            evaluate(data)

    def test_structural_errors(self):
        self.assert_invalid(lambda d: d.update(extra=1))
        self.assert_invalid(lambda d: d.pop('shipments'))
        self.assert_invalid(lambda d: d['shipments'][0].update(weight_grams=True))
        self.assert_invalid(lambda d: d['shipments'][0].update(remote=1))
        self.assert_invalid(lambda d: d['scale_batches'][0].update(min_delta_grams=True))
        self.assert_invalid(lambda d: d['old_tariffs'][0].update(step_grams=249))
        self.assert_invalid(lambda d: d['new_tariffs'][0].update(step_grams=249))
        self.assert_invalid(lambda d: d['old_tariffs'].append(tariff()))
        self.assert_invalid(lambda d: d['scale_batches'][0].update(min_delta_grams=6))
        self.assert_invalid(lambda d: d['scale_batches'][0].update(min_delta_grams=-20001))
        self.assert_invalid(lambda d: d['scale_batches'][0].update(max_delta_grams=20001))
        self.assert_invalid(lambda d: d['scale_batches'].append(batch('B1', 0, 1)))
        self.assert_invalid(lambda d: d['scale_batches'][0].update(references=[]))
        self.assert_invalid(lambda d: d['scale_batches'][0]['references'].extend(
            [reference('X%d' % i, 1000, 1000, 1000) for i in range(16)]))
        self.assert_invalid(lambda d: d['scale_batches'][0]['references'].append(
            reference('REF', 1000, 1000, 1000)))
        self.assert_invalid(lambda d: d['scale_batches'][0]['references'][0].update(
            true_min_grams=1000, true_max_grams=999))
        self.assert_invalid(lambda d: d['scale_batches'][0]['references'][0].update(
            reading_grams=0))
        self.assert_invalid(lambda d: d['scale_batches'][0]['references'][0].update(
            reading_grams=10 ** 9 + 1))

    def test_reference_and_shipment_errors(self):
        self.assert_invalid(lambda d: d['shipments'][0].update(scale_batch_id='NOPE'))
        self.assert_invalid(lambda d: d['shipments'][0].update(service='Q'))
        self.assert_invalid(lambda d: d['shipments'].append(ship('P', 900)))
        self.assert_invalid(lambda d: d['scale_batches'].append(batch('B2', 0, 1)))

    def test_missing_tariff_in_only_one_table(self):
        def mutate(data):
            data['new_tariffs'][0] = tariff(service='OTHER')
        self.assert_invalid(mutate)

    def test_empty_arrays_are_legal(self):
        self.assertEqual(evaluate(input_document([], [], [], []))['portfolio']['status'],
                         'complete')


if __name__ == '__main__':
    unittest.main()
