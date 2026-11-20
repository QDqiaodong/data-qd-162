"""Real CLI tests for the schema v6 parcel-rate-release tool, including the
400k-shipment scale experiment with hot/cold customer grouping, cross-batch
repeat customers and alternating granularity attempts, run under the 128 MiB
process-memory budget with the actual resource usage recorded.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'parcel_rating.release_cli'
MIB = 1024
MEMORY_LIMIT_MIB = 128


def invoke(source, output, timeout=30):
    return subprocess.run([sys.executable, '-m', MODULE, '--source',
                           str(source), '--output', str(output)],
                          capture_output=True, text=True, timeout=timeout)


HEADER = {"min_customers": 2, "requests": [
    {"id": "R1", "services": ["S"], "zones": None,
     "levels": ["zone", "total"]},
    {"id": "R2", "services": ["T"], "zones": None, "levels": ["total"]},
    {"id": "R3", "services": ["S"], "zones": None,
     "levels": ["zone", "total"]},
]}


def tariff(service, zone, per_step=100):
    return {"service": service, "zone": zone, "base_cents": 0,
            "step_grams": 500, "per_step_cents": per_step,
            "divisor_cm3_per_kg": 10 ** 9, "max_girth_mm": 2000,
            "remote_cents": 0, "fuel_bp": 0, "insurance_bp": 0,
            "insurance_free_cents": 0}


def ship(pid, service, zone, **overrides):
    row = {"id": pid, "service": service, "zone": zone, "weight_grams": 500,
           "length_mm": 100, "width_mm": 100, "height_mm": 100,
           "declared_cents": 0, "remote": False}
    row.update(overrides)
    return row


def sample_batch():
    return {
        "tariffs": [tariff("S", "X"), tariff("S", "Y"), tariff("S", "Z"),
                    tariff("T", "X")],
        "shipments": [ship("P1", "S", "X"), ship("P2", "S", "Y"),
                      ship("P3", "S", "Z"), ship("P4", "T", "X"),
                      ship("P5", "T", "X")],
        "customers": {"P1": "A", "P2": "B", "P3": "C", "P4": "A", "P5": "B"},
    }


def write_jsonl(path, objects):
    with open(path, 'w', encoding='utf-8') as stream:
        for obj in objects:
            stream.write(json.dumps(obj, ensure_ascii=False) + '\n')


class CliReleaseTests(unittest.TestCase):
    def test_fixture_report_stdout_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures/release_input.jsonl'
            first = invoke(source, output)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(first.stdout))
            self.assertEqual(report['schema_version'], 6)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual([item['status'] for item in report['result']],
                             ['published', 'withheld', 'published'])
            original = output.read_bytes()
            self.assertEqual(invoke(source, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_invalid_inputs_preserve_existing_report(self):
        good = [HEADER, sample_batch()]
        cases = {
            'blank line': '\n',
            'trailing blank': json.dumps(HEADER) + '\n' +
                              json.dumps(sample_batch()) + '\n\n',
            'duplicate key': '{"min_customers": 2, "min_customers": 2, '
                             '"requests": []}\n',
            'nonfinite': '{"min_customers": NaN, "requests": []}\n',
            'bad utf-8': None,  # written as bytes below
            'not an object': '[1, 2]\n',
            'unknown header field': '{"min_customers": 2, "requests": [], '
                                    '"x": 1}\n',
            'late illegal batch': json.dumps(HEADER) + '\n' +
                                  json.dumps(sample_batch()) + '\n' +
                                  json.dumps({"tariffs": [tariff("S", "X")],
                                              "shipments": [ship("P9", "S", "X",
                                                                 weight=0)],
                                              "customers": {"P9": "A"}}) + '\n',
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'input.jsonl'
            write_jsonl(source, good)
            self.assertEqual(invoke(source, output).returncode, 0)
            original = output.read_bytes()
            for label, text in cases.items():
                with self.subTest(label=label):
                    if text is None:
                        source.write_bytes(b'{"min_customers": 2, "requests":'
                                           b' []}\n\xff\xfe{}\n')
                    else:
                        source.write_text(text)
                    result = invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(output.read_bytes(), original)

    def test_empty_file_and_missing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'empty.jsonl'
            source.write_text('')
            self.assertEqual(invoke(source, output).returncode, 2)
            self.assertFalse(output.exists())
            self.assertEqual(invoke(Path(temporary) / 'absent.jsonl',
                                    output).returncode, 2)
            self.assertFalse(output.exists())

    def test_source_output_collision_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.jsonl'
            write_jsonl(source, [HEADER, sample_batch()])
            original = source.read_bytes()
            self.assertEqual(invoke(source, source).returncode, 2)
            self.assertEqual(source.read_bytes(), original)
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            output = Path(temporary) / 'report.json'
            output.symlink_to(target)
            self.assertEqual(invoke(source, output).returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_oversize_line_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'big.jsonl'
            padding = 'x' * (8_000_100)
            source.write_text(json.dumps(HEADER) + '\n')
            with open(source, 'a', encoding='utf-8') as stream:
                stream.write('{"tariffs": [], "shipments": [], "customers":'
                             ' {}, "pad": "%s"}\n' % padding)
            self.assertEqual(invoke(source, output).returncode, 2)
            self.assertFalse(output.exists())


def build_scale_source(path):
    """Write the deterministic 400k-shipment experiment source.

    Forty batches of 10000 parcels each; per-batch tariffs differ so exact
    amounts vary by batch; 200 hot customers recur in every batch while cold
    customers ship rarely; one (SVCX, ZNX) cell holds exactly two lone
    customers; about one percent of parcels are oversize.  The header is
    written first and batches are streamed so the generating process keeps a
    low memory high-water mark (a forked child inherits the parent's
    maxrss, which would corrupt the child measurement).
    """
    rng = Random(20261120)
    services = ['SVC%d' % i for i in range(6)]
    zones = ['ZN%d' % i for i in range(4)]
    hot = ['HOT-%03d' % i for i in range(200)]
    cold_pool = 120_000
    batches = 40
    per_batch = 10_000
    expected_rated = 0
    expected_cents = 0
    expected_customers = set()
    requests = [{'id': 'R00', 'services': None, 'zones': None,
                 'levels': ['total']}]
    level_plans = [['service_zone', 'total'], ['zone', 'total'],
                   ['service', 'total'], ['total']]
    for i in range(1, 24):
        if i == 3:
            requests.append({'id': 'R03', 'services': ['NOPE'],
                             'zones': None, 'levels': ['total']})
            continue
        if i == 4:
            requests.append({'id': 'R04', 'services': ['SVCX'],
                             'zones': None,
                             'levels': ['service_zone', 'service', 'zone',
                                        'total']})
            continue
        service_pick = [None, services[:2], services[2:4], services[4:],
                        None, services[:3]][i % 6]
        zone_pick = [None, zones[:2], zones[2:], None][i % 4]
        requests.append({'id': 'R%02d' % i, 'services': service_pick,
                         'zones': zone_pick,
                         'levels': level_plans[i % len(level_plans)]})
    header = {'min_customers': 5, 'requests': requests}
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(header, ensure_ascii=False) + '\n')
        for b in range(batches):
            tariffs = [tariff(s, z, per_step=100 + b)
                       for s in services for z in zones]
            tariffs.append(tariff('SVCX', 'ZNX', per_step=100 + b))
            shipments = []
            customers = {}
            for i in range(per_batch):
                pid = 'SHP-%02d-%05d' % (b, i)
                if b == 0 and i < 2:
                    service, zone, customer = 'SVCX', 'ZNX', 'LONE-%d' % (i + 1)
                else:
                    service = services[i % 6]
                    zone = zones[i % 4]
                    if rng.random() < 0.075:
                        customer = hot[rng.randrange(len(hot))]
                    else:
                        customer = 'CLD-%06d' % rng.randrange(cold_pool)
                oversize = rng.random() < 0.01
                shipments.append(ship(pid, service, zone,
                                      length_mm=3000 if oversize else 100))
                customers[pid] = customer
                if not oversize:
                    expected_rated += 1
                    expected_cents += 100 + b
                    expected_customers.add(customer)
            stream.write(json.dumps({'tariffs': tariffs,
                                     'shipments': shipments,
                                     'customers': customers},
                                    ensure_ascii=False) + '\n')
    return {'rated': expected_rated, 'cents': expected_cents,
            'customers': expected_customers, 'requests': requests,
            'header': header}


class ScaleReleaseTests(unittest.TestCase):
    """The reproducible 400k-shipment experiment under the memory budget."""

    def test_scale_memory_and_cross_check(self):
        from parcel_rating.release import evaluate
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'scale.jsonl'
            output = Path(temporary) / 'report.json'
            expected = build_scale_source(source)
            # The child reports its own peak RSS: a forked process inherits
            # the parent's maxrss high-water mark, so RUSAGE_CHILDREN from
            # this (already busy) test process could not be trusted.
            bootstrap = (
                'import resource, sys;'
                'from parcel_rating.release_cli import main;'
                'rc = main(["--source", sys.argv[1], "--output", sys.argv[2]]);'
                'print("PEAK_RSS_KIB=%d"'
                ' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'
                ' file=sys.stderr);'
                'sys.exit(rc)')
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, '-c', bootstrap, str(source), str(output)],
                capture_output=True, text=True, timeout=900)
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            marker = 'PEAK_RSS_KIB='
            peak_line = [line for line in result.stderr.splitlines()
                         if line.startswith(marker)]
            self.assertEqual(len(peak_line), 1, result.stderr)
            peak_kib = int(peak_line[0][len(marker):])
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(result.stdout))
            self.assertEqual(report['schema_version'], 6)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            items = report['result']
            self.assertEqual([item['id'] for item in items],
                             ['R%02d' % i for i in range(24)])
            first = items[0]
            self.assertEqual(first['status'], 'published')
            self.assertEqual(first['level'], 'total')
            self.assertEqual(first['rows'], [
                {'key': [],
                 'customer_count': len(expected['customers']),
                 'shipment_count': expected['rated'],
                 'total_cents': expected['cents']}])
            self.assertEqual(items[3]['status'], 'empty')
            self.assertEqual(items[4]['status'], 'withheld')
            # Full cross-check: the library entry point agrees on the file.
            with open(source, 'rb') as stream:
                header = json.loads(stream.readline())
                batches = (json.loads(line) for line in stream)
                reference = evaluate(header['min_customers'],
                                     header['requests'], batches)
            self.assertEqual(items, reference)
            # Record the actual resource usage of the experiment.
            metrics = {'shipments': 400_000, 'rated': expected['rated'],
                       'customers': len(expected['customers']),
                       'requests': 24, 'elapsed_seconds': round(elapsed, 3),
                       'cli_peak_rss_kib': peak_kib,
                       'memory_limit_mib': MEMORY_LIMIT_MIB,
                       'status_histogram': {
                           status: sum(1 for item in items
                                       if item['status'] == status)
                           for status in ('published', 'empty', 'withheld')}}
            run_dir = ROOT / 'run'
            run_dir.mkdir(exist_ok=True)
            (run_dir / 'release_scale_metrics.json').write_text(
                json.dumps(metrics, indent=2, sort_keys=True) + '\n')
            print('\nscale experiment metrics:', json.dumps(metrics,
                                                            sort_keys=True))
            self.assertLess(peak_kib, MEMORY_LIMIT_MIB * MIB)


if __name__ == '__main__':
    unittest.main()
