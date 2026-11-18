import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from parcel_rating.model import temporal

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'parcel_rating'


def revision(rid, seq, start, end, supers=(), per_step=100, girth=2000):
    return {"revision_id": rid, "known_seq": seq, "start": start, "end": end,
            "tariff": {"service": "S", "zone": "Z", "base_cents": 0,
                       "step_grams": 500, "per_step_cents": per_step,
                       "divisor_cm3_per_kg": 10 ** 9, "max_girth_mm": girth,
                       "remote_cents": 0, "fuel_bp": 0, "insurance_bp": 0,
                       "insurance_free_cents": 0},
            "supersedes": list(supers)}


def shipment(pid, t):
    return {"id": pid, "service": "S", "zone": "Z", "weight_grams": 500,
            "length_mm": 600, "width_mm": 100, "height_mm": 100,
            "declared_cents": 0, "remote": False, "shipped_at": t}


def sample(as_of=4):
    return {
        "revisions": [
            revision("A", 1, 0, 100, per_step=100, girth=2000),
            revision("B", 2, 20, 80, ["A"], per_step=200, girth=800),
            revision("C", 3, 40, 60, ["A"], per_step=300, girth=2000),
            revision("D", 4, 50, 70, ["B", "C"], per_step=400, girth=2000),
        ],
        "shipments": [shipment("P0", 45), shipment("P1", 55),
                      shipment("P2", 75), shipment("P3", 80)],
        "as_of_seq": as_of, "window_start": 0, "window_end": 100,
    }


class TemporalCliTests(unittest.TestCase):
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
            completed = self.invoke(source, output, '--temporal')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 3)
            self.assertEqual(set(report), {'schema_version', 'source_sha256', 'result'})
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(report['result'], temporal(sample()))
            self.assertEqual(set(report['result']),
                             {'timelines', 'shipments', 'rated_count',
                              'oversize_count', 'missing_count',
                              'ambiguous_count', 'settled_total_cents',
                              'total_cents'})
            self.assertEqual(json.loads(completed.stdout), report)

    def test_fixture_runs_through_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures' / 'temporal_input.json'
            completed = self.invoke(source, output, '--temporal')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 3)
            self.assertEqual(report['result'],
                             temporal(json.loads(source.read_text())))

    def test_temporal_and_plan_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.write(temporary, 'in.json', sample())
            output = Path(temporary) / 'report.json'
            completed = self.invoke(source, output, '--temporal', '--plan')
            self.assertEqual(completed.returncode, 2)
            self.assertIn('--plan', completed.stderr + completed.stdout)
            self.assertFalse(output.exists())

    def test_invalid_input_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            output.write_text('previous report')
            valid = sample()
            variants = [
                {},
                {**valid, 'extra': 1},
                {**valid, 'as_of_seq': True},
                {**valid, 'revisions': [{**valid['revisions'][0],
                                         'supersedes': ['MISSING']}]},
                {**valid, 'shipments': [{**valid['shipments'][0],
                                         'shipped_at': 100}]},
            ]
            for variant in variants:
                source = self.write(temporary, 'bad.json', variant)
                completed = self.invoke(source, output, '--temporal')
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(output.read_text(), 'previous report')

    def test_collision_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.write(temporary, 'in.json', sample())
            self.assertEqual(self.invoke(source, source, '--temporal').returncode, 2)
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            link = Path(temporary) / 'link.json'
            link.symlink_to(target)
            completed = self.invoke(source, link, '--temporal')
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_permutation_changes_sha_not_result(self):
        import copy, random
        with tempfile.TemporaryDirectory() as temporary:
            rng = random.Random(3)
            first = sample()
            second = copy.deepcopy(first)
            rng.shuffle(second['revisions'])
            rng.shuffle(second['shipments'])
            for row in second['revisions']:
                rng.shuffle(row['supersedes'])
            source_a = self.write(temporary, 'a.json', first)
            source_b = self.write(temporary, 'b.json', second)
            out_a = Path(temporary) / 'a.json.report'
            out_b = Path(temporary) / 'b.json.report'
            self.assertEqual(self.invoke(source_a, out_a, '--temporal').returncode, 0)
            self.assertEqual(self.invoke(source_b, out_b, '--temporal').returncode, 0)
            report_a = json.loads(out_a.read_text())
            report_b = json.loads(out_b.read_text())
            self.assertEqual(report_a['result'], report_b['result'])
            self.assertNotEqual(report_a['source_sha256'], report_b['source_sha256'])

    def test_legacy_entries_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures' / 'input.json'
            completed = self.invoke(source, output)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(report['result']['total_cents'], 1742)

            plan_output = Path(temporary) / 'plan.json'
            plan_source = ROOT / 'fixtures' / 'plan_input.json'
            completed = self.invoke(plan_source, plan_output, '--plan')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(plan_output.read_text())['schema_version'], 2)


if __name__ == '__main__':
    unittest.main()
