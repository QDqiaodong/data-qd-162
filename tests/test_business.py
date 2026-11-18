import json
import unittest
from pathlib import Path
from parcel_rating.model import analyze, half_up

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return json.loads((ROOT / 'fixtures/input.json').read_text())


class ParcelRatingTests(unittest.TestCase):
    def test_fixture_exact_rating(self):
        report = analyze(fixture())
        p = report['shipments'][0]
        self.assertEqual((p['dimensional_weight_grams'], p['chargeable_weight_grams']), ('4800', 5000))
        self.assertEqual((p['base_cents'], p['fuel_cents'], p['insurance_cents'], p['total_cents']), (775, 66, 500, 1341))
        self.assertEqual(report['total_cents'], 1742)

    def test_oversize_is_reported_not_charged(self):
        data = fixture()
        data['shipments'][0]['length_mm'] = 2001
        report = analyze(data)
        self.assertEqual(report['shipments'][0]['status'], 'oversize')
        self.assertEqual(report['shipments'][0]['total_cents'] if 'total_cents' in report['shipments'][0] else 0, 0)

    def test_actual_weight_dominates(self):
        data = fixture()
        data['shipments'][0].update(weight_grams=10000, length_mm=100, width_mm=100, height_mm=100)
        self.assertEqual(analyze(data)['shipments'][0]['chargeable_weight_grams'], 10000)

    def test_rounds_up_to_step(self):
        data = fixture()
        data['shipments'][1]['weight_grams'] = 1001
        self.assertEqual(analyze(data)['shipments'][1]['weight_steps'], 3)

    def test_half_up_tie(self):
        from fractions import Fraction
        self.assertEqual(half_up(Fraction(1, 2)), 1)
        self.assertEqual(half_up(Fraction(3, 2)), 2)

    def test_insurance_threshold(self):
        data = fixture()
        data['shipments'][0]['declared_cents'] = 50000
        self.assertEqual(analyze(data)['shipments'][0]['insurance_cents'], 0)

    def test_missing_tariff_rejected(self):
        data = fixture()
        data['shipments'][0]['zone'] = 'UNKNOWN'
        with self.assertRaisesRegex(ValueError, 'tariff'):
            analyze(data)

    def test_duplicate_package_id_rejected(self):
        data = fixture()
        data['shipments'][1]['id'] = data['shipments'][0]['id']
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            analyze(data)

    def test_positive_dimensions_required(self):
        data = fixture()
        data['shipments'][0]['width_mm'] = 0
        with self.assertRaises(ValueError):
            analyze(data)

    def test_remote_flag_is_boolean(self):
        data = fixture()
        data['shipments'][0]['remote'] = 1
        with self.assertRaisesRegex(ValueError, 'boolean'):
            analyze(data)
