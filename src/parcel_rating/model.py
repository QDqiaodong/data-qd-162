from fractions import Fraction
from .common import require, ident, number, rows


def ceil_fraction(value):
    return -(-value.numerator // value.denominator)


def half_up(value):
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def analyze(data):
    require(data, 'tariffs shipments')
    tariffs = {}
    for row in rows(data['tariffs'], 500):
        require(row, 'service zone base_cents step_grams per_step_cents divisor_cm3_per_kg max_girth_mm remote_cents fuel_bp insurance_bp insurance_free_cents')
        service, zone = ident(row['service']), ident(row['zone'])
        key = (service, zone)
        if key in tariffs:
            raise ValueError('duplicate tariff')
        tariffs[key] = {
            'base': number(row['base_cents'], 0, 10**12),
            'step': number(row['step_grams'], 1, 10**9),
            'rate': number(row['per_step_cents'], 0, 10**9),
            'divisor': number(row['divisor_cm3_per_kg'], 1, 10**9),
            'girth': number(row['max_girth_mm'], 1, 10**9),
            'remote': number(row['remote_cents'], 0, 10**12),
            'fuel': number(row['fuel_bp'], 0, 100000),
            'insurance': number(row['insurance_bp'], 0, 100000),
            'free': number(row['insurance_free_cents'], 0, 10**12),
        }
    shipments = []
    seen = set()
    for row in rows(data['shipments'], 10000):
        require(row, 'id service zone weight_grams length_mm width_mm height_mm declared_cents remote')
        sid = ident(row['id'])
        if sid in seen:
            raise ValueError('duplicate shipment id')
        seen.add(sid)
        tariff = tariffs.get((ident(row['service']), ident(row['zone'])))
        if tariff is None:
            raise ValueError('no matching service-zone tariff')
        weight = number(row['weight_grams'], 1, 10**9)
        length, width, height = (number(row[k], 1, 10**7) for k in ('length_mm', 'width_mm', 'height_mm'))
        declared = number(row['declared_cents'], 0, 10**12)
        if type(row['remote']) is not bool:
            raise ValueError('remote must be boolean')
        girth = length + 2 * (width + height)
        if girth > tariff['girth']:
            shipments.append({'id': sid, 'status': 'oversize', 'girth_mm': girth})
            continue
        volume_cm3 = Fraction(length * width * height, 1_000)
        dimensional_grams = volume_cm3 * 1000 / tariff['divisor']
        chargeable = ceil_fraction(max(Fraction(weight), dimensional_grams) / tariff['step']) * tariff['step']
        steps = chargeable // tariff['step']
        base = tariff['base'] + steps * tariff['rate'] + (tariff['remote'] if row['remote'] else 0)
        fuel = half_up(Fraction(base * tariff['fuel'], 10000))
        insured_value = max(0, declared - tariff['free'])
        insurance = half_up(Fraction(insured_value * tariff['insurance'], 10000))
        shipments.append({'id': sid, 'status': 'rated', 'actual_weight_grams': weight,
                          'dimensional_weight_grams': str(dimensional_grams), 'chargeable_weight_grams': chargeable,
                          'weight_steps': steps, 'girth_mm': girth, 'base_cents': base,
                          'fuel_cents': fuel, 'insurance_cents': insurance, 'total_cents': base + fuel + insurance})
    rated = [s for s in shipments if s['status'] == 'rated']
    return {'shipments': sorted(shipments, key=lambda s: s['id']), 'rated_count': len(rated),
            'oversize_count': len(shipments) - len(rated), 'total_cents': sum(s['total_cents'] for s in rated)}
