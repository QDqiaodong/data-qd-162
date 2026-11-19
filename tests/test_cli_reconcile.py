import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from parcel_rating.model import reconcile

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'parcel_rating'


def shipment(sid, total_cents, per_step=100, oversize=False):
    if oversize:
        return {"id": sid, "service": "ground", "zone": "Z1",
                "weight_grams": 500, "length_mm": 1000,
                "width_mm": 300, "height_mm": 300,
                "declared_cents": 0, "remote": False}
    steps = max(1, total_cents // per_step)
    return {"id": sid, "service": "ground", "zone": "Z1",
            "weight_grams": steps * 500, "length_mm": 100,
            "width_mm": 100, "height_mm": 100,
            "declared_cents": 0, "remote": False}


def tariff(per_step=100, girth=1000):
    return {"service": "ground", "zone": "Z1", "base_cents": 0,
            "step_grams": 500, "per_step_cents": per_step,
            "divisor_cm3_per_kg": 10 ** 9, "max_girth_mm": girth,
            "remote_cents": 0, "fuel_bp": 0, "insurance_bp": 0,
            "insurance_free_cents": 0}


def debit(did, gross, candidates):
    return {"debit_id": did, "gross_cents": gross,
            "shipment_candidates": list(candidates)}


def credit(cid, cents, candidates):
    return {"credit_id": cid, "cents": cents,
            "debit_candidates": list(candidates)}


def sample(c2_cents=50):
    return {
        "rating": {"tariffs": [tariff()],
                   "shipments": [shipment("A", 100), shipment("B", 200),
                                 shipment("X", 0, oversize=True)]},
        "debits": [debit("D1", 250, ["A", "B"]),
                   debit("D2", 150, ["A", "B"])],
        "credits": [credit("C1", 50, ["D1", "D2"]),
                    credit("C2", c2_cents, ["D1"])],
    }


class ReconcileCliTests(unittest.TestCase):
    def invoke(self, source, output, *flags):
        args = [sys.executable, '-m', MODULE + '.cli', *flags,
                '--source', str(source), '--output', str(output)]
        return subprocess.run(args, capture_output=True, text=True, timeout=60)

    def write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_envelope_sha_and_interface_equivalence(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.write(temporary, 'in.json', sample())
            output = Path(temporary) / 'report.json'
            completed = self.invoke(source, output, '--reconcile')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 4)
            self.assertEqual(set(report),
                             {'schema_version', 'source_sha256', 'result'})
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(report['result'], reconcile(sample()))
            self.assertEqual(set(report['result']),
                             {'expected_shipments', 'excluded_oversize_ids',
                              'expected_total_cents', 'net_debit_cents',
                              'reconciled', 'debit_support', 'credit_support'})
            self.assertEqual(json.loads(completed.stdout), report)

    def test_demo_fixture_runs_through_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures' / 'reconcile_input.json'
            completed = self.invoke(source, output, '--reconcile')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 4)
            self.assertTrue(report['result']['reconciled'])
            self.assertEqual(report['result']['excluded_oversize_ids'], ['X'])
            self.assertEqual(report['result'],
                             reconcile(json.loads(source.read_text())))

    def test_unclosed_but_legal_exits_zero_with_blocked_support(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = self.write(temporary, 'in.json', sample(c2_cents=60))
            completed = self.invoke(source, output, '--reconcile')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text())['result']
            self.assertFalse(result['reconciled'])
            self.assertEqual(result['net_debit_cents'], 290)
            self.assertEqual(result['expected_total_cents'], 300)
            self.assertTrue(all(row['status'] == 'blocked'
                                for row in result['debit_support']))
            self.assertTrue(all(row['status'] == 'blocked'
                                for row in result['credit_support']))

    def test_modes_are_pairwise_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.write(temporary, 'in.json', sample())
            output = Path(temporary) / 'report.json'
            for flags in (('--reconcile', '--plan'),
                          ('--reconcile', '--temporal')):
                completed = self.invoke(source, output, *flags)
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(output.exists())

    def test_invalid_input_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            output.write_text('previous report')
            valid = sample()
            variants = [
                {},
                {**valid, 'extra': 1},
                {'rating': valid['rating'], 'debits': valid['debits']},
                # credit references a debit that was never declared
                {**valid, 'credits': [credit('C1', 50, ['GHOST'])]},
                # debit candidate references the oversize (non-payable) id
                {**valid,
                 'debits': [debit('D1', 250, ['X']), debit('D2', 150, ['A'])]},
                # bool/float/string money rejected
                {**valid, 'debits': [debit('D1', True, ['A']),
                                     debit('D2', 150, ['A'])]},
                {**valid, 'credits': [credit('C1', 50.0, ['D1'])]},
                # thirteen shipments exceed the narrowed cap
                {**valid, 'rating': {
                    'tariffs': [tariff()],
                    'shipments': [shipment(f"S{i:02d}", 100) for i in range(13)]}},
            ]
            for variant in variants:
                source = self.write(temporary, 'bad.json', variant)
                completed = self.invoke(source, output, '--reconcile')
                self.assertEqual(completed.returncode, 2,
                                 (completed.stderr, variant))
                self.assertEqual(output.read_text(), 'previous report')

    def test_collision_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.write(temporary, 'in.json', sample())
            self.assertEqual(
                self.invoke(source, source, '--reconcile').returncode, 2)
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            link = Path(temporary) / 'link.json'
            link.symlink_to(target)
            completed = self.invoke(source, link, '--reconcile')
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_permutation_changes_sha_not_result(self):
        import copy, random
        with tempfile.TemporaryDirectory() as temporary:
            rng = random.Random(11)
            first = sample()
            second = copy.deepcopy(first)
            rng.shuffle(second['debits'])
            rng.shuffle(second['credits'])
            rng.shuffle(second['rating']['shipments'])
            for d in second['debits']:
                rng.shuffle(d['shipment_candidates'])
            for c in second['credits']:
                rng.shuffle(c['debit_candidates'])
            source_a = self.write(temporary, 'a.json', first)
            source_b = self.write(temporary, 'b.json', second)
            out_a = Path(temporary) / 'a.json.report'
            out_b = Path(temporary) / 'b.json.report'
            self.assertEqual(
                self.invoke(source_a, out_a, '--reconcile').returncode, 0)
            self.assertEqual(
                self.invoke(source_b, out_b, '--reconcile').returncode, 0)
            report_a = json.loads(out_a.read_text())
            report_b = json.loads(out_b.read_text())
            self.assertEqual(report_a['result'], report_b['result'])
            self.assertNotEqual(report_a['source_sha256'],
                                report_b['source_sha256'])

    def test_legacy_entries_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures' / 'input.json'
            completed = self.invoke(source, output)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output.read_text())['schema_version'], 1)

            plan_output = Path(temporary) / 'plan.json'
            completed = self.invoke(ROOT / 'fixtures' / 'plan_input.json',
                                    plan_output, '--plan')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(plan_output.read_text())['schema_version'], 2)

            temporal_output = Path(temporary) / 'temporal.json'
            completed = self.invoke(ROOT / 'fixtures' / 'temporal_input.json',
                                    temporal_output, '--temporal')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(temporal_output.read_text())['schema_version'], 3)


if __name__ == '__main__':
    unittest.main()
