"""Reproducible scale experiment for schema v7 (10000 shipments).

Generates a deterministic input with several scale batches, wide correction
intervals, mixed tariff step sizes and varied original scale readings, runs
the real CLI subprocess, verifies it against the library entry point and
records the elapsed time.  The document must finish quickly because the
implementation never enumerates grams; a per-gram scan would need 10^4 x 10^4
evaluations here.
"""
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'parcel_rating.cli'


def _tariff(service, zone, base, step, per, girth):
    return {'service': service, 'zone': zone, 'base_cents': base,
            'step_grams': step, 'per_step_cents': per,
            'divisor_cm3_per_kg': 5000, 'max_girth_mm': girth,
            'remote_cents': 10, 'fuel_bp': 850, 'insurance_bp': 100,
            'insurance_free_cents': 5000}


def build_scale_source(path, seed=20261120):
    """Write the deterministic 10000-shipment input; return expectations."""
    rng = random.Random(seed)
    services = ['SVC%d' % i for i in range(4)]
    zones = ['ZN%d' % i for i in range(3)]
    old, new = [], []
    for service in services:
        for zone in zones:
            old.append(_tariff(service, zone, 50, rng.choice([250, 500, 1000]),
                               rng.choice([40, 100, 200]), 3000))
            new.append(_tariff(service, zone, 0, rng.choice([250, 500, 1000]),
                               rng.choice([40, 100, 200]), 2800))
    batches, shipments = [], []
    batch_count = 20
    per_batch = 500
    for b in range(batch_count):
        bid = 'SB-%02d' % b
        references = []
        for r in range(8):
            reading = rng.randint(200, 6000)
            references.append({
                'reference_id': 'REF-%d' % r, 'reading_grams': reading,
                'true_min_grams': max(1, reading + rng.randint(-8, 5)),
                'true_max_grams': reading + rng.randint(8, 18)})
        batches.append({'scale_batch_id': bid, 'min_delta_grams': -500,
                        'max_delta_grams': 500, 'references': references})
        for m in range(per_batch):
            shipments.append({
                'id': 'SHP-%02d-%04d' % (b, m),
                'service': rng.choice(services), 'zone': rng.choice(zones),
                'weight_grams': rng.randint(200, 9000),
                'length_mm': rng.randint(100, 400),
                'width_mm': rng.randint(100, 300),
                'height_mm': rng.randint(100, 250),
                'declared_cents': rng.randint(0, 100000),
                'remote': rng.random() < 0.25, 'scale_batch_id': bid})
    document = {'old_tariffs': old, 'new_tariffs': new,
                'scale_batches': batches, 'shipments': shipments}
    path.write_text(json.dumps(document), encoding='utf-8')
    return document


class ScaleImpactTests(unittest.TestCase):
    def test_reproducible_10000_shipment_run(self):
        from parcel_rating.impact import evaluate
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'scale.json'
            output = Path(temporary) / 'report.json'
            document = build_scale_source(source)
            expected = evaluate(document)
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, '-m', MODULE, '--impact', '--source',
                 str(source), '--output', str(output)],
                capture_output=True, text=True, timeout=120)
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(result.stdout))
            self.assertEqual(report['schema_version'], 7)
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(report['result'], expected)
            self.assertEqual(len(report['result']['shipments']), 10000)
            self.assertEqual(len(report['result']['scale_batches']), 20)
            self.assertEqual(report['result']['portfolio']['status'],
                             'complete')
            # No per-gram enumeration: 10000 tickets x ~1000 corrections
            # would be 10^7 evaluations; the sweep must stay far below that.
            self.assertLess(elapsed, 30.0)
            metrics = {
                'shipments': len(report['result']['shipments']),
                'scale_batches': len(report['result']['scale_batches']),
                'elapsed_seconds': round(elapsed, 3),
                'portfolio_increase_cents':
                    report['result']['portfolio']['increase_cents'],
                'portfolio_net_change_cents':
                    report['result']['portfolio']['net_change_cents'],
            }
            run_dir = ROOT / 'run'
            run_dir.mkdir(exist_ok=True)
            (run_dir / 'impact_scale_metrics.json').write_text(
                json.dumps(metrics, indent=2, sort_keys=True) + '\n')
            print('\nimpact scale metrics:', json.dumps(metrics, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
