import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from parcel_rating.model import plan

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'parcel_rating'


def tariff(service, zone, base=0, per_step=0, fuel=0, girth=3000):
    return {"service": service, "zone": zone, "base_cents": base, "step_grams": 500,
            "per_step_cents": per_step, "divisor_cm3_per_kg": 5000,
            "max_girth_mm": girth, "remote_cents": 0, "fuel_bp": fuel,
            "insurance_bp": 0, "insurance_free_cents": 0}


def rule(service, zone, pieces=12, grams=10 ** 12, minimum=0):
    return {"service": service, "zone": zone, "max_pieces": pieces,
            "max_chargeable_grams": grams, "minimum_base_cents": minimum}


def parcel(pid, services, zone="Z1", weight=500, length=100, width=100,
           height=100):
    return {"id": pid, "services": list(services), "zone": zone,
            "weight_grams": weight, "length_mm": length, "width_mm": width,
            "height_mm": height, "declared_cents": 0, "remote": False}


class PlanCliTests(unittest.TestCase):
    def invoke(self, source, output, planning=True):
        args = [sys.executable, '-m', MODULE + '.cli']
        if planning:
            args.append('--plan')
        args += ['--source', str(source), '--output', str(output)]
        return subprocess.run(args, capture_output=True, text=True, timeout=30)

    def write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def sample(self):
        return {
            "tariffs": [tariff("A", "Z1", base=100, fuel=50),
                        tariff("B", "Z1", per_step=200, fuel=100)],
            "dispatch_rules": [rule("A", "Z1", pieces=2, grams=1000, minimum=1000),
                               rule("B", "Z1", pieces=2, grams=1000)],
            "shipments": [parcel("P1", ("A", "B")), parcel("P2", ("A", "B"))],
        }

    def test_plan_report_envelope_and_result_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.write(temporary, 'in.json', self.sample())
            output = Path(temporary) / 'report.json'
            completed = self.invoke(source, output)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 2)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(set(report['result']),
                             {'feasible', 'candidates', 'assignments', 'groups',
                              'total_cents'})
            # The CLI result is exactly the Python interface result.
            self.assertEqual(report['result'], plan(self.sample()))
            self.assertEqual(json.loads(completed.stdout), report)

    def test_fixture_plan_runs_through_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures' / 'plan_input.json'
            completed = self.invoke(source, output)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 2)
            self.assertTrue(report['result']['feasible'])
            self.assertEqual(report['result'], plan(json.loads(source.read_text())))

    def test_infeasible_plan_exits_zero_with_null_total(self):
        data = self.sample()
        data['dispatch_rules'][0]['max_pieces'] = 0  # invalid bound -> must be 2 instead
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = self.write(temporary, 'in.json', data)
            self.assertEqual(self.invoke(source, output).returncode, 2)
            self.assertFalse(output.exists())
        # A structurally valid but unallocatable request exits 0.
        data = self.sample()
        for candidate in data['tariffs']:
            candidate['max_girth_mm'] = 100
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = self.write(temporary, 'in.json', data)
            completed = self.invoke(source, output)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text())['result']
            self.assertFalse(result['feasible'])
            self.assertIsNone(result['total_cents'])
            self.assertEqual(result['assignments'], [])
            self.assertEqual(result['groups'], [])
            self.assertEqual(len(result['candidates']), 4)

    def test_invalid_plan_input_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            output.write_text('previous report')
            valid = self.sample()
            invalid_variants = [
                {},
                {'tariffs': [], 'shipments': [], 'dispatch_rules': [], 'extra': 1},
                {'tariffs': valid['tariffs'], 'shipments': valid['shipments'],
                 'dispatch_rules': [rule('A', 'ZZ')]},
                {'tariffs': valid['tariffs'], 'dispatch_rules': valid['dispatch_rules'],
                 'shipments': [{**parcel('P1', ('A', 'B')), 'services': []}]},
                {'tariffs': valid['tariffs'], 'dispatch_rules': valid['dispatch_rules'],
                 'shipments': [{**parcel('P1', ('A', 'B'))},
                               {**parcel('P1', ('A',))}]},
            ]
            for variant in invalid_variants:
                source = self.write(temporary, 'bad.json', variant)
                completed = self.invoke(source, output)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(output.read_text(), 'previous report')

    def test_permutation_keeps_result_but_sha_tracks_raw_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = self.sample()
            second = json.loads(json.dumps(first))
            second['shipments'] = list(reversed(second['shipments']))
            second['tariffs'] = list(reversed(second['tariffs']))
            second['dispatch_rules'] = list(reversed(second['dispatch_rules']))
            for shipment in second['shipments']:
                shipment['services'] = list(reversed(shipment['services']))
            source_a = self.write(temporary, 'a.json', first)
            source_b = self.write(temporary, 'b.json', second)
            out_a = Path(temporary) / 'a_report.json'
            out_b = Path(temporary) / 'b_report.json'
            self.assertEqual(self.invoke(source_a, out_a).returncode, 0)
            self.assertEqual(self.invoke(source_b, out_b).returncode, 0)
            report_a = json.loads(out_a.read_text())
            report_b = json.loads(out_b.read_text())
            self.assertEqual(report_a['result'], report_b['result'])
            self.assertEqual(report_a['source_sha256'],
                             hashlib.sha256(source_a.read_bytes()).hexdigest())
            self.assertEqual(report_b['source_sha256'],
                             hashlib.sha256(source_b.read_bytes()).hexdigest())
            self.assertNotEqual(report_a['source_sha256'], report_b['source_sha256'])

    def test_plan_rejects_symlink_and_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.write(temporary, 'in.json', self.sample())
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            link = Path(temporary) / 'link.json'
            link.symlink_to(target)
            self.assertEqual(self.invoke(source, link).returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')
            self.assertEqual(self.invoke(source, source).returncode, 2)

    def test_legacy_mode_remains_schema_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures' / 'input.json'
            completed = self.invoke(source, output, planning=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(report['result']['total_cents'], 1742)


if __name__ == '__main__':
    unittest.main()
