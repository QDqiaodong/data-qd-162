"""Schema v3: authoritative tariff-revision timelines and point-in-time rating.

A request carries tariff *revisions* with half-open effective intervals
``[start, end)`` and ``supersedes`` edges that express authority precedence
without deleting any parent interval.  Visibility is fixed by a knowledge
cutoff ``as_of_seq`` (a revision is visible iff ``known_seq <= as_of_seq``);
ancestry is the transitive closure of the supersedes graph over the visible
revisions, computed before any interval filtering.  At every point in time the
*frontier* is the set of visible covering revisions that are not themselves
ancestors of another covering revision:

* an empty frontier is ``missing``,
* a single-id frontier is ``resolved``,
* any multi-id frontier is ``ambiguous`` -- identical tariff payloads never
  collapse the identity of distinct revisions.

Timelines are produced as exact segment partitions of the integer request
window (no per-time-unit enumeration), and every shipment reuses the frontier
of the timeline segment containing its ``shipped_at`` (no per-shipment graph
traversal).  Resolved shipments are rated with the shared v1 per-piece rules
from :mod:`parcel_rating.model`.
"""
from bisect import bisect_right

from .common import ident, number, require, rows
from .model import parse_tariff_row, rate_parcel, _parse_parcel_common

TEMPORAL_FIELDS = ('revisions shipments as_of_seq window_start window_end')
REVISION_FIELDS = ('revision_id known_seq start end tariff supersedes')
TEMPORAL_SHIPMENT_FIELDS = ('id service zone weight_grams length_mm width_mm '
                            'height_mm declared_cents remote shipped_at')
TIME_MAX = 10 ** 15
REVISION_LIMIT = 500
SHIPMENT_LIMIT = 10000
EDGE_LIMIT = 20000


def _time(value):
    """Integer time coordinate in 0..10^15; bool is explicitly rejected."""
    if type(value) is not int or not 0 <= value <= TIME_MAX:
        raise ValueError('time coordinate must be an integer in 0..10^15')
    return value


def _parse(data):
    require(data, TEMPORAL_FIELDS)
    as_of_seq = number(data['as_of_seq'], 0, TIME_MAX)
    window_start = _time(data['window_start'])
    window_end = _time(data['window_end'])
    if window_start >= window_end:
        raise ValueError('window_start must be strictly less than window_end')

    revisions = {}
    all_keys = set()
    edge_total = 0
    for row in rows(data['revisions'], REVISION_LIMIT):
        require(row, REVISION_FIELDS)
        revision_id = ident(row['revision_id'])
        if revision_id in revisions:
            raise ValueError('duplicate revision id: ' + revision_id)
        known_seq = number(row['known_seq'], 1, TIME_MAX)
        start = _time(row['start'])
        end = _time(row['end'])
        if start >= end:
            raise ValueError('revision start must be strictly less than end')
        key, tariff = parse_tariff_row(row['tariff'])
        parents = row['supersedes']
        if not isinstance(parents, list):
            raise ValueError('supersedes must be an array')
        if len(parents) != len(set(parents)):
            raise ValueError('duplicate supersedes parent id')
        parent_ids = []
        for parent in parents:
            parent_ids.append(ident(parent))
        if revision_id in parent_ids:
            raise ValueError('revision cannot supersede itself')
        edge_total += len(parent_ids)
        if edge_total > EDGE_LIMIT:
            raise ValueError('supersedes edge total exceeds 20000')
        revisions[revision_id] = {
            'id': revision_id, 'known_seq': known_seq, 'start': start,
            'end': end, 'key': key, 'tariff': tariff, 'parents': parent_ids,
        }
        all_keys.add(key)

    # Global structural validation over the whole request, including revisions
    # that are not yet visible at as_of_seq.
    for revision in revisions.values():
        for parent_id in revision['parents']:
            parent = revisions.get(parent_id)
            if parent is None:
                raise ValueError('unknown supersedes parent id: ' + parent_id)
            if parent['key'] != revision['key']:
                raise ValueError('supersedes parent must share service and zone')
            if not parent['known_seq'] < revision['known_seq']:
                raise ValueError('parent known_seq must be strictly smaller')

    seen_shipment_ids = set()
    shipments = []
    for row in rows(data['shipments'], SHIPMENT_LIMIT):
        require(row, TEMPORAL_SHIPMENT_FIELDS)
        shipment_id = ident(row['id'])
        if shipment_id in seen_shipment_ids:
            raise ValueError('duplicate shipment id')
        seen_shipment_ids.add(shipment_id)
        service, zone = ident(row['service']), ident(row['zone'])
        shipped_at = _time(row['shipped_at'])
        if not window_start <= shipped_at < window_end:
            raise ValueError('shipped_at must lie within [window_start, window_end)')
        parcel = _parse_parcel_common(row)
        key = (service, zone)
        all_keys.add(key)
        shipments.append({'id': shipment_id, 'key': key,
                          'shipped_at': shipped_at, 'parcel': parcel})
    return revisions, shipments, as_of_seq, window_start, window_end, all_keys


def _ancestors(visible):
    """Transitive ancestor bitmasks over visible revisions.

    Edges always point from a child to a parent with a strictly smaller
    known_seq, so processing visible revisions in ascending known_seq makes
    every parent's mask final before the child is computed.  The graph is
    built over *all* visible revisions first -- never after clipping records
    to the window -- so an intermediate node whose interval never covers the
    evaluation point still keeps its descendants reachable from their
    ancestors.
    """
    ordered = sorted(visible.values(), key=lambda r: (r['known_seq'], r['id']))
    index = {r['id']: i for i, r in enumerate(ordered)}
    own = [1 << i for i in range(len(ordered))]
    ancestors = [0] * len(ordered)
    for i, revision in enumerate(ordered):
        mask = 0
        for parent_id in revision['parents']:
            j = index.get(parent_id)
            if j is not None:  # visible parents only; structure was validated
                mask |= own[j] | ancestors[j]
        ancestors[i] = mask
    for i, mask in enumerate(ancestors):
        ancestors[i] = mask | own[i]
    return ordered, ancestors


def _frontier(cover_mask, ordered, ancestors):
    """Covering revisions that no other covering revision dominates.

    A covering revision X is removed when it is an ancestor of some other
    covering revision Y (Y transitively supersedes X).  The dominated set is
    the union, over every covering record Y, of Y's ancestor bits with Y's
    own bit excluded -- one OR pass over the covering set, never a pairwise
    scan.  A dominated record entering or leaving coverage can therefore
    leave the frontier untouched.
    """
    dominated = 0
    bits = cover_mask
    while bits:
        j = (bits & -bits).bit_length() - 1
        dominated |= ancestors[j] & ~(1 << j)
        bits &= bits - 1
    frontier_mask = cover_mask & ~dominated
    frontier = []
    while frontier_mask:
        j = (frontier_mask & -frontier_mask).bit_length() - 1
        frontier.append(ordered[j]['id'])
        frontier_mask &= frontier_mask - 1
    return tuple(sorted(frontier))  # Unicode code-point order on revision ids


def _build_timeline(key, visible_by_key, window_start, window_end, ordered, ancestors):
    """Exact segment partition of [window_start, window_end).

    Only visible revision boundaries inside the window are breakpoints; no
    time unit is enumerated.  Covering sets and frontiers are evaluated once
    per raw interval, and adjacent raw intervals are merged when status and
    frontier are identical (coverage-only changes, including a dominated
    record entering or leaving, merge away).
    """
    records = visible_by_key.get(key, ())
    breaks = {window_start, window_end}
    for revision in records:
        if revision['start'] > window_start:
            breaks.add(min(revision['start'], window_end))
        if revision['end'] < window_end:
            breaks.add(max(revision['end'], window_start))
    points = sorted(breaks)
    raw = []
    for left, right in zip(points, points[1:]):
        if left >= right:
            continue
        cover = 0
        for revision in records:
            if revision['start'] <= left < revision['end']:
                cover |= 1 << revision['index']
        frontier = _frontier(cover, ordered, ancestors)
        status = ('missing' if not frontier else
                  'resolved' if len(frontier) == 1 else 'ambiguous')
        raw.append((left, right, status, frontier))

    segments = []
    for left, right, status, frontier in raw:
        if (segments and segments[-1]['end'] == left
                and segments[-1]['status'] == status
                and tuple(segments[-1]['frontier']) == frontier):
            segments[-1]['end'] = right
        else:
            segments.append({'start': left, 'end': right, 'status': status,
                             'frontier': list(frontier),
                             'revision_id': frontier[0] if status == 'resolved' else None})
    return segments


def temporal(data):
    """Authoritative revision timeline and point-in-time rating (schema v3).

    Returns the canonical ``result`` object and never mutates ``data``.
    """
    (revisions, shipments, as_of_seq, window_start,
     window_end, all_keys) = _parse(data)

    visible = {rid: r for rid, r in revisions.items() if r['known_seq'] <= as_of_seq}
    ordered, ancestors = _ancestors(visible)
    for i, revision in enumerate(ordered):
        revision['index'] = i
    visible_by_key = {}
    for revision in ordered:
        visible_by_key.setdefault(revision['key'], []).append(revision)

    timelines = []
    timeline_by_key = {}
    segment_ends_by_key = {}
    for key in sorted(all_keys):
        segments = _build_timeline(key, visible_by_key, window_start,
                                   window_end, ordered, ancestors)
        entry = {'service': key[0], 'zone': key[1], 'segments': segments}
        timelines.append(entry)
        timeline_by_key[key] = segments
        segment_ends_by_key[key] = [segment['end'] for segment in segments]

    rated_count = oversize_count = missing_count = ambiguous_count = 0
    settled_total = 0
    shipment_results = []
    for shipment in sorted(shipments, key=lambda s: s['id']):
        key = shipment['key']
        segments = timeline_by_key[key]
        # One cached end list per key and a binary search per shipment: no
        # shipment ever walks the revision graph or the boundary list.
        position = bisect_right(segment_ends_by_key[key], shipment['shipped_at'])
        segment = segments[position]
        frontier = tuple(segment['frontier'])
        base_result = {
            'id': shipment['id'], 'service': shipment['key'][0],
            'zone': shipment['key'][1], 'shipped_at': shipment['shipped_at'],
            'status': segment['status'], 'frontier': list(frontier),
            'revision_id': segment['revision_id'],
        }
        if segment['status'] != 'resolved':
            if segment['status'] == 'missing':
                missing_count += 1
            else:
                ambiguous_count += 1
            shipment_results.append(base_result)
            continue

        revision = visible[frontier[0]]
        status, detail = rate_parcel(shipment['parcel'], revision['tariff'])
        if status == 'oversize':
            oversize_count += 1
            base_result['status'] = 'oversize'
            base_result.update(detail)  # oversize evidence: girth_mm only
        else:
            rated_count += 1
            base_result['status'] = 'rated'
            settled_total += detail['total_cents']
            base_result.update(detail)
        shipment_results.append(base_result)

    unresolved = missing_count + ambiguous_count
    return {
        'timelines': timelines,
        'shipments': shipment_results,
        'rated_count': rated_count,
        'oversize_count': oversize_count,
        'missing_count': missing_count,
        'ambiguous_count': ambiguous_count,
        'settled_total_cents': settled_total,
        'total_cents': None if unresolved else settled_total,
    }
