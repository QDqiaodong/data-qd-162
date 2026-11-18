from fractions import Fraction
from .common import require, ident, number, rows

TARIFF_FIELDS = ('service zone base_cents step_grams per_step_cents '
                 'divisor_cm3_per_kg max_girth_mm remote_cents fuel_bp '
                 'insurance_bp insurance_free_cents')
RULE_FIELDS = 'service zone max_pieces max_chargeable_grams minimum_base_cents'
PLAN_SHIPMENT_FIELDS = ('id services zone weight_grams length_mm width_mm '
                        'height_mm declared_cents remote')
RULE_LIMIT = 4
PLAN_SHIPMENT_LIMIT = 12


def ceil_fraction(value):
    return -(-value.numerator // value.denominator)


def half_up(value):
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def parse_tariff_row(row):
    """Validate one complete tariff and return ((service, zone), internal tariff)."""
    require(row, TARIFF_FIELDS)
    service, zone = ident(row['service']), ident(row['zone'])
    return (service, zone), {
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


def _parse_tariffs(data):
    tariffs = {}
    for row in rows(data['tariffs'], 500):
        key, tariff = parse_tariff_row(row)
        if key in tariffs:
            raise ValueError('duplicate tariff')
        tariffs[key] = tariff
    return tariffs


def _parse_rules(data, tariffs):
    rules = {}
    for row in rows(data['dispatch_rules'], RULE_LIMIT):
        require(row, RULE_FIELDS)
        service, zone = ident(row['service']), ident(row['zone'])
        key = (service, zone)
        if key in rules:
            raise ValueError('duplicate dispatch rule')
        if key not in tariffs:
            raise ValueError('dispatch rule has no matching service-zone tariff')
        rules[key] = {
            'max_pieces': number(row['max_pieces'], 1, 12),
            'max_chargeable_grams': number(row['max_chargeable_grams'], 1, 10**12),
            'minimum_base_cents': number(row['minimum_base_cents'], 0, 10**15),
        }
    return rules


def _parse_parcel_common(row):
    weight = number(row['weight_grams'], 1, 10**9)
    length = number(row['length_mm'], 1, 10**7)
    width = number(row['width_mm'], 1, 10**7)
    height = number(row['height_mm'], 1, 10**7)
    declared = number(row['declared_cents'], 0, 10**12)
    if type(row['remote']) is not bool:
        raise ValueError('remote must be boolean')
    return weight, length, width, height, declared, row['remote']


def _evaluate_candidate(shipment_id, service, zone, parcel, tariff):
    """Per-piece rating for one candidate; mirrors analyze() exactly."""
    weight, length, width, height, declared, remote = parcel
    girth = length + 2 * (width + height)
    evidence = {'id': shipment_id, 'service': service, 'zone': zone,
                'status': 'eligible', 'girth_mm': girth}
    if girth > tariff['girth']:
        evidence['status'] = 'oversize'
        return evidence, None
    volume_cm3 = Fraction(length * width * height, 1_000)
    dimensional_grams = volume_cm3 * 1000 / tariff['divisor']
    chargeable = (ceil_fraction(max(Fraction(weight), dimensional_grams) / tariff['step'])
                  * tariff['step'])
    steps = chargeable // tariff['step']
    base = tariff['base'] + steps * tariff['rate'] + (tariff['remote'] if remote else 0)
    insured_value = max(0, declared - tariff['free'])
    insurance = half_up(Fraction(insured_value * tariff['insurance'], 10000))
    evidence['chargeable_weight_grams'] = chargeable
    evidence['base_cents'] = base
    evidence['insurance_cents'] = insurance
    detail = {'chargeable': chargeable, 'base': base, 'insurance': insurance}
    return evidence, detail


def _empty_result():
    return {'feasible': True, 'candidates': [], 'assignments': [],
            'groups': [], 'total_cents': 0}


def _infeasible_result(candidates):
    return {'feasible': False,
            'candidates': sorted(candidates, key=lambda c: (c['id'], c['service'])),
            'assignments': [], 'groups': [], 'total_cents': None}


def _solve(shipment_ids, eligible, rule_keys, rules, tariffs):
    """Exact optimum over full group bills via memoized subset DP.

    Assignment = one (service, zone) group subset per dispatch rule.  Every
    feasible subset of a rule's eligible parcels is enumerated once, its
    capacity is checked on summed chargeable weight, and the whole-group bill
    (minimum base, single group fuel rounding, summed insurance) is computed
    once.  Memoized search over (rule index, covered bitmask) explores every
    legal partition, so the result is a global optimum -- never a heuristic.
    Tie objective: (total cents, nonempty groups, Unicode service vector).
    """
    n = len(shipment_ids)
    full = (1 << n) - 1
    r_count = len(rule_keys)

    # Eligibility bitmask and per-rule, per-parcel rating details.
    key_to_index = {key: g for g, key in enumerate(rule_keys)}
    elig_mask = [0] * r_count
    detail = [[None] * n for _ in range(r_count)]
    for i, sid in enumerate(shipment_ids):
        for service, info in eligible[sid].items():
            g = key_to_index[(service, info['zone'])]
            elig_mask[g] |= 1 << i
            detail[g][i] = info

    # Every feasible subset option per rule, with its finalized group bill.
    options = []
    for g, key in enumerate(rule_keys):
        rule = rules[key]
        fuel_bp = tariffs[key]['fuel']
        bucket = []
        sub = elig_mask[g]
        while True:
            pieces = sub.bit_count()
            if pieces <= rule['max_pieces']:
                weight_sum = base_sum = ins_sum = 0
                bits = sub
                while bits:
                    j = (bits & -bits).bit_length() - 1
                    info = detail[g][j]
                    weight_sum += info['chargeable']
                    base_sum += info['base']
                    ins_sum += info['insurance']
                    bits &= bits - 1
                if weight_sum <= rule['max_chargeable_grams']:
                    if pieces == 0:
                        bucket.append((0, 0, 0, 0, 0, 0, 0, 0, 0))
                    else:
                        billed = max(base_sum, rule['minimum_base_cents'])
                        fuel = half_up(Fraction(billed * fuel_bp, 10000))
                        bucket.append((sub, pieces, weight_sum, base_sum, ins_sum,
                                       billed, billed - base_sum, fuel,
                                       billed + fuel + ins_sum))
            if sub == 0:
                break
            sub = (sub - 1) & elig_mask[g]
        # Largest subsets first reaches full cover quickly; correctness does
        # not depend on order because every option is still examined.
        bucket.sort(key=lambda option: option[0], reverse=True)
        options.append(bucket)

    suffix_union = [0] * (r_count + 1)
    for g in range(r_count - 1, -1, -1):
        suffix_union[g] = suffix_union[g + 1] | elig_mask[g]

    memo = {}

    def solve(rule_index, covered):
        key = (rule_index, covered)
        if key in memo:
            return memo[key]
        remaining = full ^ covered
        if remaining & ~suffix_union[rule_index]:
            memo[key] = None
            return None
        if rule_index == r_count:
            blank = tuple([None] * n)
            memo[key] = (0, 0, blank, 0) if remaining == 0 else None
            return memo[key]

        best = None
        is_last = rule_index == r_count - 1
        service = rule_keys[rule_index][0]
        for option in options[rule_index]:
            sub = option[0]
            if sub & covered:
                continue
            if is_last and sub != remaining:
                continue
            child = solve(rule_index + 1, covered | sub)
            if child is None:
                continue
            cost = option[8] + child[0]
            opened = (1 if sub else 0) + child[1]
            # Unicode code-point lexicographic order over parcels in id
            # order.  Child vectors already carry every position decided by
            # later rules; this rule fills its own subset positions.
            vector = list(child[2])
            bits = sub
            while bits:
                j = (bits & -bits).bit_length() - 1
                vector[j] = service
                bits &= bits - 1
            candidate = (cost, opened, tuple(vector), sub)
            if best is None or candidate[:3] < best[:3]:
                best = candidate
        memo[key] = best
        return best

    decision = solve(0, 0)
    if decision is None:
        return None

    # Reconstruct the chosen subset of every rule.
    chosen = {}
    covered = 0
    for g in range(r_count):
        sub = memo[(g, covered)][3]
        if sub:
            chosen[g] = sub
        covered |= sub
    return decision, chosen


def plan(data):
    require(data, 'tariffs shipments dispatch_rules')
    tariffs = _parse_tariffs(data)
    rules = _parse_rules(data, tariffs)
    rule_keys = sorted(rules)

    candidates = []
    eligible = {}
    seen_ids = set()
    for row in rows(data['shipments'], PLAN_SHIPMENT_LIMIT):
        require(row, PLAN_SHIPMENT_FIELDS)
        sid = ident(row['id'])
        if sid in seen_ids:
            raise ValueError('duplicate shipment id')
        seen_ids.add(sid)
        zone = ident(row['zone'])
        service_values = row['services']
        if not isinstance(service_values, list) or not service_values:
            raise ValueError('services must be a nonempty array')
        services = []
        service_seen = set()
        for value in service_values:
            service = ident(value)
            if service in service_seen:
                raise ValueError('duplicate candidate service')
            service_seen.add(service)
            if (service, zone) not in rules:
                raise ValueError('candidate service has no matching dispatch rule')
            services.append(service)
        parcel = _parse_parcel_common(row)
        per_services = {}
        for service in services:
            tariff = tariffs[(service, zone)]
            evidence, detail = _evaluate_candidate(sid, service, zone, parcel, tariff)
            candidates.append(evidence)
            if detail is not None:
                per_services[service] = dict(detail, zone=zone)
        eligible[sid] = per_services

    if not seen_ids:
        result = _empty_result()
    else:
        shipment_ids = sorted(seen_ids)
        solution = _solve(shipment_ids, eligible, rule_keys, rules, tariffs)
        if solution is None:
            result = _infeasible_result(candidates)
        else:
            decision, chosen = solution
            assignments = []
            groups = []
            total = 0
            for g, sub in sorted(chosen.items()):
                key = rule_keys[g]
                service, zone = key
                member_ids = [shipment_ids[j] for j in range(len(shipment_ids))
                              if sub & (1 << j)]
                weight_sum = base_sum = ins_sum = 0
                for member in member_ids:
                    info = eligible[member][service]
                    weight_sum += info['chargeable']
                    base_sum += info['base']
                    ins_sum += info['insurance']
                    assignments.append({'id': member, 'service': service, 'zone': zone})
                rule = rules[key]
                billed = max(base_sum, rule['minimum_base_cents'])
                fuel = half_up(Fraction(billed * tariffs[key]['fuel'], 10000))
                group_total = billed + fuel + ins_sum
                total += group_total
                groups.append({
                    'service': service, 'zone': zone,
                    'member_ids': member_ids, 'piece_count': len(member_ids),
                    'chargeable_weight_grams': weight_sum,
                    'subtotal_base_cents': base_sum,
                    'billed_base_cents': billed,
                    'minimum_topup_cents': billed - base_sum,
                    'fuel_cents': fuel,
                    'insurance_cents': ins_sum,
                    'total_cents': group_total,
                })
            if total != decision[0]:
                raise AssertionError('internal billing mismatch')
            groups.sort(key=lambda group: (group['service'], group['zone']))
            assignments.sort(key=lambda assignment: assignment['id'])
            result = {
                'feasible': True,
                'candidates': sorted(candidates, key=lambda c: (c['id'], c['service'])),
                'assignments': assignments,
                'groups': groups,
                'total_cents': total,
            }
    return result


def rate_parcel(parcel, tariff):
    """Rate one parcel against one complete tariff using the exact v1 rules.

    Returns ('oversize', {'girth_mm': girth}) or
    ('rated', {all v1 rated evidence fields except id/service/zone}).
    Shared by analyze() and the temporal frontier rating so the per-piece
    formulas (girth, dimensional weight, step ceiling, remote, fuel and
    half-up insurance) have a single implementation.
    """
    weight, length, width, height, declared, remote = parcel
    girth = length + 2 * (width + height)
    if girth > tariff['girth']:
        return 'oversize', {'girth_mm': girth}
    volume_cm3 = Fraction(length * width * height, 1_000)
    dimensional_grams = volume_cm3 * 1000 / tariff['divisor']
    chargeable = (ceil_fraction(max(Fraction(weight), dimensional_grams) / tariff['step'])
                  * tariff['step'])
    steps = chargeable // tariff['step']
    base = tariff['base'] + steps * tariff['rate'] + (tariff['remote'] if remote else 0)
    fuel = half_up(Fraction(base * tariff['fuel'], 10000))
    insured_value = max(0, declared - tariff['free'])
    insurance = half_up(Fraction(insured_value * tariff['insurance'], 10000))
    return 'rated', {
        'actual_weight_grams': weight,
        'dimensional_weight_grams': str(dimensional_grams),
        'chargeable_weight_grams': chargeable,
        'weight_steps': steps,
        'girth_mm': girth,
        'base_cents': base,
        'fuel_cents': fuel,
        'insurance_cents': insurance,
        'total_cents': base + fuel + insurance,
    }


def analyze(data):
    require(data, 'tariffs shipments')
    tariffs = _parse_tariffs(data)
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
        status, detail = rate_parcel(_parse_parcel_common(row), tariff)
        shipments.append({'id': sid, 'status': status, **detail})
    rated = [s for s in shipments if s['status'] == 'rated']
    return {'shipments': sorted(shipments, key=lambda s: s['id']), 'rated_count': len(rated),
            'oversize_count': len(shipments) - len(rated), 'total_cents': sum(s['total_cents'] for s in rated)}


def temporal(data):
    """Schema v3 authoritative revision timeline; implementation lives in
    :mod:`parcel_rating.temporal`. Imported lazily to keep the module graph
    acyclic (the temporal module reuses v1 rating helpers from this module)."""
    from .temporal import temporal as _temporal
    return _temporal(data)
