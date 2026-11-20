"""Schema v7 carrier-rate price-impact evaluation.

A prospective tariff table is compared with the historical one **without
changing any historical quote and without producing any charge**: every
comparable shipment is priced against both tables at the same corrected
weight, and the report exposes the resulting per-shipment and per-scale-batch
price-change intervals together with their witnesses.

Corrected weight for a shipment with original scale reading ``w`` is
``w + delta`` where one integer ``delta`` is shared by every shipment of a
scale batch.  Feasible ``delta`` values form a closed integer interval: the
declared batch interval intersected with every reference-object interval
``[true_min - reading, true_max - reading]`` and with the v1 actual-weight
range for every shipment of the batch (including oversize ones).  An empty
intersection is a legal business conclusion: the batch is ``conflict`` and its
amounts are unknown, while every other batch is still reported in full.

Cost growth is governed by rating breakpoints, never by grams: a v1 tariff's
total is a step function of corrected weight whose steps change only at
``ceil(W / step_grams)`` transitions.  A shipment's price difference is
therefore piecewise constant with at most ``width/step_old +
width/step_new + 1`` pieces (``width`` is bounded by the +/-20000 declared
range, ``step_grams`` is at least 250).  Batch totals are obtained by an
event sweep over the union of those breakpoints; the shipment/correction
Cartesian product is never materialized and no per-gram loop is used.
"""
from fractions import Fraction

from .common import ident, number, require, rows
from .model import (ceil_fraction, half_up, parse_tariff_row,
                    _parse_parcel_common)

IMPACT_FIELDS = 'old_tariffs new_tariffs scale_batches shipments'
IMPACT_SHIPMENT_FIELDS = ('id service zone weight_grams length_mm width_mm '
                          'height_mm declared_cents remote scale_batch_id')
BATCH_FIELDS = ('scale_batch_id min_delta_grams max_delta_grams references')
REFERENCE_FIELDS = 'reference_id reading_grams true_min_grams true_max_grams'

TARIFF_LIMIT = 500
SHIPMENT_LIMIT = 10000
BATCH_LIMIT = 10000
REFERENCE_LIMIT = 16
DELTA_LIMIT = 20000
WEIGHT_MAX = 10 ** 9
MIN_STEP_GRAMS = 250


# ---------------------------------------------------------------------------
# Validation (complete input validation happens before any business result)
# ---------------------------------------------------------------------------

def _parse_tariffs(value):
    """Validate one tariff table with the unchanged v1 tariff contract."""
    tariffs = {}
    for row in rows(value, TARIFF_LIMIT):
        key, tariff = parse_tariff_row(row)
        if key in tariffs:
            raise ValueError('duplicate tariff')
        if tariff['step'] < MIN_STEP_GRAMS:
            raise ValueError('pilot tariffs require step_grams >= 250')
        tariffs[key] = tariff
    return tariffs


def _parse_batches(value):
    batches = {}
    for row in rows(value, BATCH_LIMIT):
        require(row, BATCH_FIELDS)
        batch_id = ident(row['scale_batch_id'])
        if batch_id in batches:
            raise ValueError('duplicate scale batch id')
        lower = number(row['min_delta_grams'], -DELTA_LIMIT, DELTA_LIMIT)
        upper = number(row['max_delta_grams'], -DELTA_LIMIT, DELTA_LIMIT)
        if lower > upper:
            raise ValueError('scale batch lower delta exceeds upper delta')
        raw_references = row['references']
        if (not isinstance(raw_references, list)
                or not 1 <= len(raw_references) <= REFERENCE_LIMIT):
            raise ValueError('references must contain 1..16 records')
        references = []
        reference_ids = set()
        for reference in raw_references:
            require(reference, REFERENCE_FIELDS)
            reference_id = ident(reference['reference_id'])
            if reference_id in reference_ids:
                raise ValueError('duplicate reference id within scale batch')
            reference_ids.add(reference_id)
            reading = number(reference['reading_grams'], 1, WEIGHT_MAX)
            true_min = number(reference['true_min_grams'], 1, WEIGHT_MAX)
            true_max = number(reference['true_max_grams'], 1, WEIGHT_MAX)
            if true_min > true_max:
                raise ValueError('reference true_min exceeds true_max')
            references.append({'reading_grams': reading,
                               'true_min_grams': true_min,
                               'true_max_grams': true_max})
        batches[batch_id] = {'min_delta_grams': lower,
                             'max_delta_grams': upper,
                             'references': references}
    return batches


def _parse_shipments(value, old_tariffs, new_tariffs, batches):
    shipments = []
    seen = set()
    for row in rows(value, SHIPMENT_LIMIT):
        require(row, IMPACT_SHIPMENT_FIELDS)
        shipment_id = ident(row['id'])
        if shipment_id in seen:
            raise ValueError('duplicate shipment id')
        seen.add(shipment_id)
        service = ident(row['service'])
        zone = ident(row['zone'])
        key = (service, zone)
        if key not in old_tariffs or key not in new_tariffs:
            raise ValueError('no matching service-zone tariff in both tables')
        batch_id = ident(row['scale_batch_id'])
        if batch_id not in batches:
            raise ValueError('shipment references unknown scale batch')
        shipments.append({'id': shipment_id, 'service': service, 'zone': zone,
                          'scale_batch_id': batch_id,
                          'parcel': _parse_parcel_common(row)})
    return shipments


# ---------------------------------------------------------------------------
# Exact v1 rating as a step function of corrected weight
# ---------------------------------------------------------------------------

def _profile(parcel, tariff):
    """Constant-ish rating profile for one tariff.

    Returns ``None`` for an oversize parcel (girth alone decides that, and it
    is independent of corrected weight).  Otherwise returns the integer pieces
    of the v1 per-piece formula; :func:`_total` evaluates it at any corrected
    weight using the same arithmetic as :func:`parcel_rating.model.rate_parcel`.
    """
    weight, length, width, height, declared, remote = parcel
    girth = length + 2 * (width + height)
    if girth > tariff['girth']:
        return None
    # ceil(dimensional_weight / step) is the tariff's base step count; actual
    # chargeable steps are max(this, ceil(corrected_weight / step)).
    fixed_steps = ceil_fraction(Fraction(length * width * height,
                                         tariff['divisor'] * tariff['step']))
    constant_base = tariff['base'] + (tariff['remote'] if remote else 0)
    insured = max(0, declared - tariff['free'])
    insurance = half_up(Fraction(insured * tariff['insurance'], 10000))
    return {'step': tariff['step'], 'rate': tariff['rate'],
            'constant': constant_base, 'fixed_steps': fixed_steps,
            'fuel': tariff['fuel'], 'insurance': insurance}


def _total(profile, corrected_weight):
    steps = max(profile['fixed_steps'],
                (corrected_weight + profile['step'] - 1) // profile['step'])
    base = profile['constant'] + steps * profile['rate']
    fuel = (2 * base * profile['fuel'] + 10000) // 20000
    return base + fuel + profile['insurance']


def _transitions(profile, reading, low_weight, high_weight):
    """Corrected-weight deltas (> 0 relative to ``low_weight``) where the
    tariff's step count changes, inside ``[low_weight, high_weight]``."""
    step = profile['step']
    steps = max(profile['fixed_steps'],
                (low_weight + step - 1) // step)
    while True:
        boundary = steps * step + 1
        if boundary > high_weight:
            return
        yield boundary - reading
        steps += 1


def _segments(old, new, reading, lower, upper):
    """Piecewise-constant ``new_total - old_total`` over ``[lower, upper]``.

    Returns a list of ``(start_delta, diff_cents)`` ascending by
    ``start_delta``; each entry's value holds until the next entry starts.
    """
    low_weight = reading + lower
    high_weight = reading + upper
    starts = {lower}
    starts.update(_transitions(old, reading, low_weight, high_weight))
    starts.update(_transitions(new, reading, low_weight, high_weight))
    ordered = sorted(starts)
    return [(delta, _total(new, reading + delta) - _total(old, reading + delta))
            for delta in ordered]


# ---------------------------------------------------------------------------
# Business evaluation
# ---------------------------------------------------------------------------

def _feasible_interval(batch, members):
    """Final integer correction interval before the conflict decision."""
    lower = batch['min_delta_grams']
    upper = batch['max_delta_grams']
    for reference in batch['references']:
        reading = reference['reading_grams']
        lower = max(lower, reference['true_min_grams'] - reading)
        upper = min(upper, reference['true_max_grams'] - reading)
    for shipment in members:
        weight = shipment['parcel'][0]
        lower = max(lower, 1 - weight)
        upper = min(upper, WEIGHT_MAX - weight)
    return lower, upper


def _amounts(min_value, max_value, min_at, max_at):
    return {'min_cents': min_value, 'max_cents': max_value,
            'min_at_grams': min_at, 'max_at_grams': max_at}


def evaluate(data):
    """Return the schema v7 price-impact result without mutating ``data``."""
    require(data, IMPACT_FIELDS)
    old_tariffs = _parse_tariffs(data['old_tariffs'])
    new_tariffs = _parse_tariffs(data['new_tariffs'])
    batches = _parse_batches(data['scale_batches'])
    shipments = _parse_shipments(
        data['shipments'], old_tariffs, new_tariffs, batches)

    grouped = {batch_id: [] for batch_id in batches}
    for shipment in shipments:
        grouped[shipment['scale_batch_id']].append(shipment)
    for batch_id, members in grouped.items():
        if not members:
            raise ValueError('declared scale batch has no shipments')

    shipment_rows = []
    batch_rows = []
    portfolio_complete = True

    for batch_id in sorted(batches):
        batch = batches[batch_id]
        members = grouped[batch_id]
        lower, upper = _feasible_interval(batch, members)
        conflict = lower > upper
        if conflict:
            portfolio_complete = False
            for shipment in members:
                old = _profile(shipment['parcel'],
                               old_tariffs[shipment['service'],
                                           shipment['zone']])
                new = _profile(shipment['parcel'],
                               new_tariffs[shipment['service'],
                                           shipment['zone']])
                shipment_rows.append({
                    'id': shipment['id'],
                    'scale_batch_id': batch_id,
                    'old_status': 'oversize' if old is None else 'rated',
                    'new_status': 'oversize' if new is None else 'rated',
                    'delta_cents': None,
                })
            batch_rows.append({
                'scale_batch_id': batch_id,
                'status': 'conflict',
                'lower_delta_grams': lower,
                'upper_delta_grams': upper,
                'net_change_cents': None,
                'increase_cents': None,
            })
            continue

        # Event sweep for the batch totals: each shipment contributes its
        # piecewise-constant difference; changes are accumulated per delta so
        # memory is bounded by the number of distinct breakpoints, not by
        # shipments x corrections.
        events = {}
        net_at_lower = 0
        increase_at_lower = 0
        for shipment in members:
            old = _profile(shipment['parcel'],
                           old_tariffs[shipment['service'],
                                       shipment['zone']])
            new = _profile(shipment['parcel'],
                           new_tariffs[shipment['service'],
                                       shipment['zone']])
            if old is None or new is None:
                # Oversize on either side: both verdicts are kept, but the
                # ticket never enters a price difference or an exposure sum.
                shipment_rows.append({
                    'id': shipment['id'],
                    'scale_batch_id': batch_id,
                    'old_status': 'oversize' if old is None else 'rated',
                    'new_status': 'oversize' if new is None else 'rated',
                    'delta_cents': None,
                })
                continue

            segments = _segments(old, new, shipment['parcel'][0],
                                 lower, upper)
            min_value = max_value = None
            min_at = max_at = lower
            previous = None
            for delta, value in segments:
                positive = value if value > 0 else 0
                if min_value is None or value < min_value:
                    min_value, min_at = value, delta
                if max_value is None or value > max_value:
                    max_value, max_at = value, delta
                if previous is None:
                    net_at_lower += value
                    increase_at_lower += positive
                else:
                    previous_value, previous_positive = previous
                    entry = events.get(delta)
                    if entry is None:
                        entry = events[delta] = [0, 0]
                    entry[0] += value - previous_value
                    entry[1] += positive - previous_positive
                previous = (value, positive)
            shipment_rows.append({
                'id': shipment['id'],
                'scale_batch_id': batch_id,
                'old_status': 'rated',
                'new_status': 'rated',
                'delta_cents': _amounts(min_value, max_value, min_at, max_at),
            })

        net_min = net_max = net_at_lower
        net_min_at = net_max_at = lower
        increase_min = increase_max = increase_at_lower
        increase_min_at = increase_max_at = lower
        current_net = net_at_lower
        current_increase = increase_at_lower
        for delta in sorted(events):
            change_net, change_increase = events[delta]
            current_net += change_net
            current_increase += change_increase
            if current_net < net_min:
                net_min, net_min_at = current_net, delta
            if current_net > net_max:
                net_max, net_max_at = current_net, delta
            if current_increase < increase_min:
                increase_min, increase_min_at = current_increase, delta
            if current_increase > increase_max:
                increase_max, increase_max_at = current_increase, delta

        batch_rows.append({
            'scale_batch_id': batch_id,
            'status': 'feasible',
            'lower_delta_grams': lower,
            'upper_delta_grams': upper,
            'net_change_cents': _amounts(net_min, net_max,
                                         net_min_at, net_max_at),
            'increase_cents': _amounts(increase_min, increase_max,
                                       increase_min_at, increase_max_at),
        })

    shipment_rows.sort(key=lambda row: row['id'])
    batch_rows.sort(key=lambda row: row['scale_batch_id'])

    if portfolio_complete:
        net_min = sum(row['net_change_cents']['min_cents']
                      for row in batch_rows)
        net_max = sum(row['net_change_cents']['max_cents']
                      for row in batch_rows)
        increase_min = sum(row['increase_cents']['min_cents']
                           for row in batch_rows)
        increase_max = sum(row['increase_cents']['max_cents']
                           for row in batch_rows)
        portfolio = {
            'status': 'complete',
            'net_change_cents': {'min_cents': net_min, 'max_cents': net_max},
            'increase_cents': {'min_cents': increase_min,
                               'max_cents': increase_max},
        }
    else:
        portfolio = {'status': 'incomplete', 'net_change_cents': None,
                     'increase_cents': None}

    return {'shipments': shipment_rows, 'scale_batches': batch_rows,
            'portfolio': portfolio}
