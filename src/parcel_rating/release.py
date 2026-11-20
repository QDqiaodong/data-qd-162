"""Schema v6 offline parcel-rate release evaluation.

One fixed audience receives a sequence of freight-summary reports.  This
module simulates that sequence in request order and, for each request, finds
the first declared aggregation granularity whose publication is safe: a
granularity is publishable only when every customer equivalence class induced
by all previously published rows plus the candidate rows contains at least
``min_customers`` distinct customers.  Two customers are equivalent iff their
member/not-member pattern over every published and candidate row is identical.

Input is UTF-8 JSON Lines: a header line with ``min_customers`` and the
ordered ``requests``, then one batch object per line carrying exactly
``tariffs``, ``shipments`` and ``customers``.  Tariffs and shipments follow
the v1 per-piece contract and per-batch limits; ``customers`` maps every
shipment id of the batch to its customer id.  Only shipments the v1 per-piece
interface judges ``rated`` contribute, with their ``total_cents``.

Resource discipline (128 MiB container, up to 400k shipments / 24 requests /
12 services / 12 zones among rated shipments): shipment-id and customer-id
identity maps live in a temporary SQLite database, each (service, zone) cell
keeps only a compact list of customer indices, raw shipment objects are
dropped after each batch line, and the release state is partition refinement
(one class id per customer) rather than a per-customer boolean vector over
all published and trial rows.
"""
import hashlib
import json
import os
import sqlite3
import tempfile

from .common import ident, number, require
from .model import analyze

REPORT_SCHEMA_VERSION = 6
LEVELS = ('service_zone', 'service', 'zone', 'total')
REQUEST_LIMIT = 24
SHIPMENT_LIMIT = 400_000
SERVICE_LIMIT = 12
ZONE_LIMIT = 12
LINE_LIMIT = 8_000_000
HEADER_FIELDS = 'min_customers requests'
BATCH_FIELDS = 'tariffs shipments customers'
CHUNK = 400


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _invalid(value):
    raise ValueError('nonfinite JSON number: ' + value)


def _json_object(raw):
    """Decode one JSONL line to a dict, rejecting blank lines and bad bytes."""
    if not raw.strip():
        raise ValueError('blank line')
    text = raw.decode('utf-8')  # strict; UnicodeDecodeError is a ValueError
    data = json.loads(text, object_pairs_hook=_unique, parse_constant=_invalid)
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object per line')
    return data


def _lines(stream, hasher):
    """Yield raw line bodies (without terminator) while hashing exact bytes."""
    while True:
        raw = stream.readline(LINE_LIMIT + 2)
        if raw == b'':
            return
        hasher.update(raw)
        body = raw[:-1] if raw.endswith(b'\n') else raw
        if len(body) > LINE_LIMIT:
            raise ValueError('JSON line exceeds 8 MB')
        yield body


def _selection(value):
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError('selection must be null or a nonempty array')
    seen = set()
    for item in value:
        ident(item)
        if item in seen:
            raise ValueError('duplicate selection item')
        seen.add(item)
    return frozenset(seen)


def _parse_requests(value):
    if not isinstance(value, list) or len(value) > REQUEST_LIMIT:
        raise ValueError('expected an array of at most 24 requests')
    seen = set()
    requests = []
    for item in value:
        require(item, 'id services zones levels')
        rid = ident(item['id'])
        if rid in seen:
            raise ValueError('duplicate request id')
        seen.add(rid)
        levels = item['levels']
        if not isinstance(levels, list) or not levels:
            raise ValueError('levels must be a nonempty array')
        for level in levels:
            if not isinstance(level, str) or level not in LEVELS:
                raise ValueError('unknown level')
        if len(set(levels)) != len(levels):
            raise ValueError('duplicate level')
        requests.append({'id': rid, 'services': _selection(item['services']),
                         'zones': _selection(item['zones']),
                         'levels': tuple(levels)})
    return requests


def _parse_header(obj):
    require(obj, HEADER_FIELDS)
    return number(obj['min_customers'], 2, 100), _parse_requests(obj['requests'])


class _Accumulator:
    """Streaming per-batch validation and (service, zone) cell aggregation.

    Shipment-id uniqueness and the customer-id registry are kept in a
    temporary SQLite database so the 400k-shipment identity maps never live
    on the Python heap.  Only compact per-cell counters and customer-index
    lists remain in memory; raw shipment objects are dropped per batch.
    """

    def __init__(self, directory):
        self.db = sqlite3.connect(os.path.join(directory, 'identity.db'))
        self.db.execute('PRAGMA journal_mode = MEMORY')
        self.db.execute('PRAGMA synchronous = OFF')
        self.db.execute('CREATE TABLE shipment_ids (id TEXT PRIMARY KEY)')
        self.db.execute('CREATE TABLE customers (name TEXT PRIMARY KEY)')
        self.shipment_count = 0
        self.services = set()
        self.zones = set()
        self.cells = {}

    def add_batch(self, obj):
        require(obj, BATCH_FIELDS)
        mapping = obj['customers']
        if not isinstance(mapping, dict):
            raise ValueError('customers must be an object')
        # The v1 per-piece interface validates tariffs and shipments fully
        # (per-batch limits, duplicates, missing tariffs, numeric bounds).
        rated = analyze({'tariffs': obj['tariffs'], 'shipments': obj['shipments']})
        rows = obj['shipments']
        self.shipment_count += len(rows)
        if self.shipment_count > SHIPMENT_LIMIT:
            raise ValueError('shipment limit exceeded')
        ids = [row['id'] for row in rows]
        if set(mapping) != set(ids):
            raise ValueError('customers must exactly cover the batch shipments')
        for customer in mapping.values():
            ident(customer)
        try:
            with self.db:
                self.db.executemany('INSERT INTO shipment_ids (id) VALUES (?)',
                                    [(sid,) for sid in ids])
        except sqlite3.IntegrityError:
            raise ValueError('duplicate shipment id') from None
        index = self._customer_index(mapping.values())
        where = {row['id']: (row['service'], row['zone']) for row in rows}
        for row in rated['shipments']:
            if row['status'] != 'rated':
                continue
            cell = where[row['id']]
            stats = self.cells.get(cell)
            if stats is None:
                stats = self.cells[cell] = [0, 0, []]
            stats[0] += 1
            stats[1] += row['total_cents']
            stats[2].append(index[mapping[row['id']]])
            self.services.add(cell[0])
            self.zones.add(cell[1])
        if len(self.services) > SERVICE_LIMIT or len(self.zones) > ZONE_LIMIT:
            raise ValueError('too many distinct services or zones')

    def _customer_index(self, names):
        """Resolve customer ids to stable integer indices via SQLite."""
        wanted = sorted(set(names))
        found = {}
        for start in range(0, len(wanted), CHUNK):
            part = wanted[start:start + CHUNK]
            marks = ','.join('?' * len(part))
            cursor = self.db.execute(
                'SELECT rowid, name FROM customers WHERE name IN (%s)' % marks,
                part)
            found.update((name, idx) for idx, name in cursor)
        missing = [name for name in wanted if name not in found]
        if missing:
            with self.db:
                self.db.executemany('INSERT INTO customers (name) VALUES (?)',
                                    [(name,) for name in missing])
            for start in range(0, len(missing), CHUNK):
                part = missing[start:start + CHUNK]
                marks = ','.join('?' * len(part))
                cursor = self.db.execute(
                    'SELECT rowid, name FROM customers WHERE name IN (%s)' % marks,
                    part)
                found.update((name, idx) for idx, name in cursor)
        return found

    def finish(self):
        return self.cells


def _build_rows(level, selected, cells):
    """Aggregate the selected cells into every nonempty row of one level."""
    groups = {}
    for cell in selected:
        if level == 'service_zone':
            key = cell
        elif level == 'service':
            key = (cell[0],)
        elif level == 'zone':
            key = (cell[1],)
        else:
            key = ()
        group = groups.get(key)
        if group is None:
            group = groups[key] = [0, 0, set()]
        stats = cells[cell]
        group[0] += stats[0]
        group[1] += stats[1]
        group[2].update(stats[2])
    rows = []
    for key in sorted(groups):
        count, cents, members = groups[key]
        rows.append({'key': list(key), 'customer_count': len(members),
                     'shipment_count': count, 'total_cents': cents,
                     '_members': members})
    return rows


class _Partition:
    """Customer equivalence classes under every published report row.

    ``cls`` maps a customer index to its class id; ``sizes`` maps a class id
    to its size.  A customer enters the structure the first time any
    published (or currently tried) row contains it.  A trial level refines
    the partition row by row; a failed trial is rolled back exactly so
    rejected rows never pollute the observable release history.
    """

    def __init__(self):
        self.cls = {}
        self.sizes = []

    def trial(self, rows):
        """Refine with every row; return the rollback log."""
        moved_customer = []
        moved_from = []
        deltas = []
        base = len(self.sizes)
        cls = self.cls
        sizes = self.sizes
        for row in rows:
            members = row['_members']
            tally = {}
            for customer in members:
                key = cls.get(customer, -1)
                tally[key] = tally.get(key, 0) + 1
            moves = {}
            for key, count in tally.items():
                if key == -1:
                    moves[key] = len(sizes)
                    sizes.append(count)
                else:
                    size = sizes[key]
                    if count == size:
                        continue
                    moves[key] = len(sizes)
                    sizes.append(count)
                    sizes[key] = size - count
                    deltas.append((key, count))
            if moves:
                for customer in members:
                    key = cls.get(customer, -1)
                    target = moves.get(key)
                    if target is not None:
                        cls[customer] = target
                        moved_customer.append(customer)
                        moved_from.append(key)
        return moved_customer, moved_from, deltas, base

    def rollback(self, moved_customer, moved_from, deltas, base):
        cls = self.cls
        sizes = self.sizes
        for customer, key in zip(reversed(moved_customer),
                                 reversed(moved_from)):
            if key == -1:
                del cls[customer]
            else:
                cls[customer] = key
        for key, count in deltas:
            sizes[key] += count
        del sizes[base:]


def _public_row(row):
    return {'key': row['key'], 'customer_count': row['customer_count'],
            'shipment_count': row['shipment_count'],
            'total_cents': row['total_cents']}


def _decide(min_customers, requests, cells):
    partition = _Partition()
    result = []
    for request in requests:
        services = request['services']
        zones = request['zones']
        selected = [cell for cell in cells
                    if (services is None or cell[0] in services)
                    and (zones is None or cell[1] in zones)]
        if not selected:
            result.append({'id': request['id'], 'status': 'empty',
                           'level': None, 'rows': []})
            continue
        outcome = None
        for level in request['levels']:
            rows = _build_rows(level, selected, cells)
            log = partition.trial(rows)
            if all(size >= min_customers for size in partition.sizes):
                outcome = {'id': request['id'], 'status': 'published',
                           'level': level,
                           'rows': [_public_row(row) for row in rows]}
                break
            partition.rollback(*log)
        if outcome is None:
            outcome = {'id': request['id'], 'status': 'withheld',
                       'level': None, 'rows': []}
        result.append(outcome)
    return result


def _evaluate_validated(min_customers, requests, batches):
    with tempfile.TemporaryDirectory(prefix='parcel-release-') as directory:
        accumulator = _Accumulator(directory)
        try:
            for batch in batches:
                accumulator.add_batch(batch)
            cells = accumulator.finish()
        finally:
            accumulator.db.close()
    return _decide(min_customers, requests, cells)


def evaluate(min_customers, requests, batches):
    """Validate the header values and every batch, then decide in order.

    ``batches`` is an iterable of parsed batch objects (a generator keeps the
    caller's memory bounded as well).  Returns the ``result`` array.
    """
    min_customers, requests = _parse_header(
        {'min_customers': min_customers, 'requests': requests})
    return _evaluate_validated(min_customers, requests, batches)


def run(source):
    """Read the JSONL source once, then return the schema v6 report."""
    hasher = hashlib.sha256()
    with open(source, 'rb') as stream:
        lines = _lines(stream, hasher)
        try:
            first = next(lines)
        except StopIteration:
            raise ValueError('empty input') from None
        min_customers, requests = _parse_header(_json_object(first))

        def batches():
            for raw in lines:
                yield _json_object(raw)

        result = _evaluate_validated(min_customers, requests, batches())
    return {'schema_version': REPORT_SCHEMA_VERSION,
            'source_sha256': hasher.hexdigest(), 'result': result}
