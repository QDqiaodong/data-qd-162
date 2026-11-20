"""Real subprocess CLI tests for ``parcel-rating --impact`` (schema_version 7).

Covers report/stdio identity, provenance, repeat stability, failure cases
that must preserve an existing report, source/output collisions, symlinks,
the 8 MB limit, mutual exclusivity and regression of the older modes.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures/impact_input.json'


def invoke(*args, timeout=60):
    return subprocess.run([sys.executable, '-m', 'parcel_rating.cli', *args],
                          capture_output=True, text=True, timeout=timeout)


def write(path, document):
    path.write_text(json.dumps(document), encoding='utf-8')


def tariff(service='S', zone='Z', base=0, step=500, per=100):
    return {'service': service, 'zone': zone, 'base_cents': base,
            'step_grams': step, 'per_step_cents': per,
            'divisor_cm3_per_kg': 10 ** 9, 'max_girth_mm': 2000,
            'remote_cents': 0, 'fuel_bp': 0, 'insurance_bp': 0,
            'insurance_free_cents': 0}


def ship(pid, weight, batch='B1'):
    return {'id': pid, 'service': 'S', 'zone': 'Z', 'weight_grams': weight,
            'length_mm': 100, 'width_mm': 100, 'height_mm': 100,
            'declared_cents': 0, 'remote': False, 'scale_batch_id': batch}


def batch(bid, lower, upper, references):
    return {'scale_batch_id': bid, 'min_delta_grams': lower,
            'max_delta_grams': upper, 'references': references}


def reference(rid, reading, true_min, true_max):
    return {'reference_id': rid, 'reading_grams': reading,
            'true_min_grams': true_min, 'true_max_grams': true_max}


def valid_document():
    return {'old_tariffs': [tariff(base=50)],
            'new_tariffs': [tariff(base=0, step=1000, per=200)],
            'scale_batches': [batch('B1', 0, 1,
                                    [reference('R', 1000, 1000, 1001)])],
            'shipments': [ship('P', 500)]}


class ImpactCliTests(unittest.TestCase):
    def test_fixture_report_stdout_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            first = invoke('--impact', '--source', str(FIXTURE),
                           '--output', str(output))
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(first.stdout))
            self.assertEqual(report['schema_version'], 7)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
            self.assertEqual(set(report), {'schema_version', 'source_sha256',
                                           'result'})
            original = output.read_bytes()
            self.assertEqual(invoke('--impact', '--source', str(FIXTURE),
                                    '--output', str(output)).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_named_business_scenario_via_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            write(source, {
                'old_tariffs': [tariff(base=50, step=500, per=100)],
                'new_tariffs': [tariff(base=0, step=1000, per=200)],
                'scale_batches': [batch('B1', 0, 1,
                                        [reference('R', 1000, 1000, 1001)])],
                'shipments': [ship('P', 500), ship('Q', 1000)]})
            result = invoke('--impact', '--source', str(source),
                            '--output', str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['result']['portfolio']['increase_cents'],
                             {'min_cents': 50, 'max_cents': 50})
            self.assertEqual(report['result']['portfolio']['net_change_cents'],
                             {'min_cents': 0, 'max_cents': 0})

    def test_conflict_exits_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            write(source, {
                'old_tariffs': [tariff()], 'new_tariffs': [tariff()],
                'scale_batches': [batch('B1', -5, 5,
                                        [reference('A', 1000, 1010, 1010),
                                         reference('B', 1000, 980, 980)])],
                'shipments': [ship('P', 1000)]})
            result = invoke('--impact', '--source', str(source),
                            '--output', str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report['result']['scale_batches'][0]['status'],
                             'conflict')
            self.assertEqual(report['result']['portfolio']['status'],
                             'incomplete')

    def test_invalid_inputs_preserve_existing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            write(source, valid_document())
            self.assertEqual(invoke('--impact', '--source', str(source),
                                    '--output', str(output)).returncode, 0)
            original = output.read_bytes()

            cases = {}
            document = valid_document()
            document['extra'] = 1
            cases['extra field'] = json.dumps(document)
            document = valid_document()
            document['old_tariffs'][0]['step_grams'] = 249
            cases['sub-250 step'] = json.dumps(document)
            document = valid_document()
            document['shipments'][0]['weight_grams'] = True
            cases['boolean weight'] = json.dumps(document)
            document = valid_document()
            document['shipments'][0]['scale_batch_id'] = 'MISSING'
            cases['bad batch reference'] = json.dumps(document)
            document = valid_document()
            document['scale_batches'].append(
                batch('B2', 0, 0, [reference('R', 1000, 1000, 1000)]))
            cases['empty declared batch'] = json.dumps(document)
            document = valid_document()
            document['scale_batches'][0]['min_delta_grams'] = 20001
            cases['out of delta bound'] = json.dumps(document)
            document = valid_document()
            document['shipments'].append(dict(ship('P', 700)))
            cases['duplicate shipment id'] = json.dumps(document)
            cases['duplicate key'] = json.dumps(valid_document()).replace(
                '"shipments"', '"shipments": [], "shipments"', 1)
            cases['malformed'] = '{"old_tariffs": [}'
            cases['empty object'] = '{}'
            cases['NaN'] = json.dumps(valid_document()).replace(
                '"declared_cents": 0', '"declared_cents": NaN', 1)

            for label, text in cases.items():
                with self.subTest(label=label):
                    source.write_text(text)
                    result = invoke('--impact', '--source', str(source),
                                    '--output', str(output))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(output.read_bytes(), original)

    def test_missing_source_preserves_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            output.write_text('prior')
            result = invoke('--impact', '--source',
                            str(Path(temporary) / 'absent.json'),
                            '--output', str(output))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_text(), 'prior')

    def test_oversize_source_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'big.json'
            output = Path(temporary) / 'report.json'
            document = valid_document()
            document['pad'] = 'x' * 8_000_100
            source.write_text(json.dumps(document))
            self.assertEqual(invoke('--impact', '--source', str(source),
                                    '--output', str(output)).returncode, 2)
            self.assertFalse(output.exists())

    def test_source_output_collision_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            write(source, valid_document())
            original = source.read_bytes()
            self.assertEqual(invoke('--impact', '--source', str(source),
                                    '--output', str(source)).returncode, 2)
            self.assertEqual(source.read_bytes(), original)
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            output = Path(temporary) / 'report.json'
            output.symlink_to(target)
            self.assertEqual(invoke('--impact', '--source', str(source),
                                    '--output', str(output)).returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_array_reordering_is_invariant(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / 'first.json'
            second = Path(temporary) / 'second.json'
            output = Path(temporary) / 'report.json'
            document = valid_document()
            document['scale_batches'].append(
                batch('B2', 0, 0, [reference('R2', 900, 900, 900)]))
            document['shipments'].append(ship('Q', 1000, 'B2'))
            write(first, document)
            shuffled = json.loads(json.dumps(document))
            shuffled['old_tariffs'].reverse()
            shuffled['new_tariffs'].reverse()
            shuffled['scale_batches'].reverse()
            shuffled['shipments'].reverse()
            for item in shuffled['scale_batches']:
                item['references'].reverse()
            write(second, shuffled)
            self.assertEqual(invoke('--impact', '--source', str(first),
                                    '--output', str(output)).returncode, 0)
            baseline = json.loads(output.read_text())['result']
            self.assertEqual(invoke('--impact', '--source', str(second),
                                    '--output', str(output)).returncode, 0)
            self.assertEqual(json.loads(output.read_text())['result'], baseline)

    def test_impact_is_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            for other in ('--plan', '--temporal', '--reconcile'):
                with self.subTest(other=other):
                    result = invoke('--impact', other, '--source',
                                    str(FIXTURE), '--output', str(output))
                    self.assertEqual(result.returncode, 2)

    def test_older_modes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            cases = {
                '--plan': ('fixtures/plan_input.json', 2),
                '--temporal': ('fixtures/temporal_input.json', 3),
                '--reconcile': ('fixtures/reconcile_input.json', 4),
            }
            for flag, (fixture, schema) in cases.items():
                with self.subTest(flag=flag):
                    result = invoke(flag, '--source', str(ROOT / fixture),
                                    '--output', str(output))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(output.read_text())['schema_version'],
                                     schema)
            result = invoke('--source', str(ROOT / 'fixtures/input.json'),
                            '--output', str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text())['schema_version'], 1)


if __name__ == '__main__':
    unittest.main()
