"""Schema v5: SQLite pre-deposit and shipment-manifest register.

The register is the durable book of fact for two linked responsibilities:

* **Pre-deposits.** ``fund`` adds integer-cent balances to named accounts;
  ``amend`` replaces or cancels not-yet-shipped parcels as one batch.  For
  every account the invariant is
  ``available = funded - held - spent >= 0`` where ``held`` equals exactly the
  sum of frozen quotes of its currently held parcels.
* **Manifest lists.** Every non-shipped parcel belongs to exactly one open
  manifest.  ``seal`` atomically closes manifests, moving their members' frozen
  quotes from ``held`` to ``spent`` and freezing members and amounts forever:
  sealed parcels can never be replaced, moved or cancelled.

All rating reuses the unchanged v1 per-piece :func:`parcel_rating.model.analyze`
contract -- the register never recomputes an accepted quote with a later
tariff.  Money is arbitrary-precision Python integer cents; SQLite stores money
as canonical decimal text so neither 64-bit overflow nor floating point can
corrupt a balance or aggregate.  Every successful request is one serialized
SQLite transaction (the commit is the point of effect) and stores an immutable
receipt plus its canonical request bytes, making retries idempotent.
"""
import copy
import json
import os
import sqlite3
from collections import defaultdict

from .common import ident, require
from .model import analyze

DB_FORMAT = 'parcel-register'
DB_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 5

FUND_CENTS_MAX = 10 ** 15
ENTRY_LIMIT = 1000
UPSERT_LIMIT = 1000
CANCEL_LIMIT = 1000
MANIFEST_LIMIT = 1000
BUSY_TIMEOUT_MS = 20000

SHIPMENT_FIELDS = ('id service zone weight_grams length_mm width_mm '
                   'height_mm declared_cents remote')
UPSERT_FIELDS = 'account_id manifest_id shipment tariff'
FUND_ENTRY_FIELDS = 'account_id cents'


class RegisterError(ValueError):
    """A format, business, version or database-schema error."""


def _integer(value, minimum, maximum, label):
    """Strict integer check: ``bool`` is never accepted as an integer."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise RegisterError(label + ' must be an integer in '
                            + str(minimum) + '..' + str(maximum))
    return value


def _bounded_array(value, limit, label, allow_empty=True):
    if not isinstance(value, list) or len(value) > limit or (not allow_empty and not value):
        raise RegisterError(label + ' must be an array within the declared bound')
    return value


def _unique_ids(values, label):
    seen = set()
    result = []
    for value in values:
        item = ident(value)
        if item in seen:
            raise RegisterError('duplicate ' + label + ': ' + item)
        seen.add(item)
        result.append(item)
    return result


def _validate_request(data):
    """Parse and validate one request without touching the database.

    Returns ``(action, request_id, expected_version, parsed)`` where parsed
    carries freshly built structures; the caller's ``data`` is never mutated.
    """
    if not isinstance(data, dict):
        raise RegisterError('request must be a JSON object')
    action = data.get('action')
    if action not in ('fund', 'amend', 'seal'):
        raise RegisterError('action must be one of fund, amend, seal')
    if action == 'fund':
        require(data, 'request_id expected_version action entries')
    elif action == 'amend':
        require(data, 'request_id expected_version action upserts cancels')
    else:
        require(data, 'request_id expected_version action manifest_ids')
    request_id = ident(data['request_id'])
    expected_version = data['expected_version']
    if type(expected_version) is not int or expected_version < 0:
        raise RegisterError('expected_version must be a non-negative integer')

    if action == 'fund':
        entries_raw = _bounded_array(data['entries'], ENTRY_LIMIT, 'entries',
                                     allow_empty=False)
        entries = []
        accounts = set()
        for row in entries_raw:
            require(row, FUND_ENTRY_FIELDS)
            account_id = ident(row['account_id'])
            if account_id in accounts:
                raise RegisterError('duplicate fund account: ' + account_id)
            accounts.add(account_id)
            cents = _integer(row['cents'], 1, FUND_CENTS_MAX, 'fund cents')
            entries.append({'account_id': account_id, 'cents': cents})
        parsed = {'entries': entries}
    elif action == 'amend':
        upserts_raw = _bounded_array(data['upserts'], UPSERT_LIMIT, 'upserts')
        cancels_raw = _bounded_array(data['cancels'], CANCEL_LIMIT, 'cancels')
        if not upserts_raw and not cancels_raw:
            raise RegisterError('amend needs at least one upsert or cancel')
        upserts = []
        shipment_ids = set()
        for row in upserts_raw:
            require(row, UPSERT_FIELDS)
            account_id = ident(row['account_id'])
            manifest_id = ident(row['manifest_id'])
            # Defensive copies guarantee the caller's data is never mutated
            # or retained by reference in any persisted structure.
            shipment = copy.deepcopy(row['shipment'])
            tariff = copy.deepcopy(row['tariff'])
            require(shipment, SHIPMENT_FIELDS)
            shipment_id = ident(shipment['id'])
            if shipment_id in shipment_ids:
                raise RegisterError('duplicate upsert shipment id: ' + shipment_id)
            shipment_ids.add(shipment_id)
            # The single-parcel analyze call enforces every v1 shipment and
            # tariff field, identifier, numeric bound and boolean rule, the
            # service/zone match, and produces the frozen v1 evidence row.
            rating = analyze({'tariffs': [tariff], 'shipments': [shipment]})
            quote = rating['shipments'][0]
            if quote['status'] == 'oversize':
                raise RegisterError('oversize shipment cannot be registered: '
                                    + shipment_id)
            upserts.append({
                'account_id': account_id,
                'manifest_id': manifest_id,
                'shipment': shipment,
                'tariff': tariff,
                'quote': quote,
            })
        cancels = _unique_ids(cancels_raw, 'cancel id')
        if shipment_ids.intersection(cancels):
            raise RegisterError('a shipment id cannot be both upserted and cancelled')
        parsed = {'upserts': upserts, 'cancels': cancels}

    else:
        manifest_ids = _unique_ids(
            _bounded_array(data['manifest_ids'], MANIFEST_LIMIT,
                           'manifest_ids', allow_empty=False),
            'manifest id')
        parsed = {'manifest_ids': manifest_ids}

    return action, request_id, expected_version, parsed


def _canonical(action, request_id, expected_version, parsed):
    """Canonical request identity (array order-insensitive, key-order-insensitive).

    Collection arrays are sorted by the governing id (account id, shipment id,
    manifest id or cancel id); nested object key order and JSON whitespace do
    not matter because nested structures are re-serialized with sorted keys.
    Every other field, including expected_version, participates in identity.
    """
    normalized = {
        'action': action,
        'request_id': request_id,
        'expected_version': expected_version,
    }
    if action == 'fund':
        normalized['entries'] = sorted(
            ({'account_id': e['account_id'], 'cents': e['cents']}
             for e in parsed['entries']),
            key=lambda e: e['account_id'])
    elif action == 'amend':
        normalized['upserts'] = sorted(
            ({'account_id': u['account_id'],
              'manifest_id': u['manifest_id'],
              'shipment': u['shipment'],
              'tariff': u['tariff']}
             for u in parsed['upserts']),
            key=lambda u: u['shipment']['id'])
        normalized['cancels'] = sorted(parsed['cancels'])
    else:
        normalized['manifest_ids'] = sorted(parsed['manifest_ids'])
    return json.dumps(normalized, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _database_path(database):
    if not isinstance(database, (str, os.PathLike)):
        raise RegisterError('database must be a filesystem path')
    return os.fspath(database)


def _connect(database, create):
    path = _database_path(database)
    if not create and not os.path.exists(path):
        raise RegisterError('database does not exist: ' + path)
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as error:
        raise RegisterError('cannot open database: ' + str(error)) from error
    conn.execute('PRAGMA busy_timeout=' + str(BUSY_TIMEOUT_MS))
    return conn


def _table_names(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _init_schema(conn):
    # Individual execute() calls (never executescript(), which would issue an
    # implicit COMMIT and tear down the surrounding BEGIN IMMEDIATE write
    # lock) so schema creation participates in the single serialized
    # transaction and only becomes visible when the request commits.
    conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)",
                 (DB_FORMAT,))
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(DB_SCHEMA_VERSION),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', '0')")
    conn.execute('CREATE TABLE accounts ('
                 'account_id TEXT PRIMARY KEY, funded_cents TEXT NOT NULL)')
    conn.execute('CREATE TABLE manifests ('
                 'manifest_id TEXT PRIMARY KEY, status TEXT NOT NULL, '
                 'sealed_version INTEGER)')
    conn.execute('CREATE TABLE bookings ('
                 'id TEXT PRIMARY KEY, account_id TEXT NOT NULL, '
                 'manifest_id TEXT NOT NULL, status TEXT NOT NULL, '
                 'shipment_json TEXT NOT NULL, tariff_json TEXT NOT NULL, '
                 'quote_json TEXT NOT NULL, total_cents TEXT NOT NULL)')
    conn.execute('CREATE TABLE requests ('
                 'request_id TEXT PRIMARY KEY, canonical_json TEXT NOT NULL, '
                 'receipt_json TEXT NOT NULL, version INTEGER NOT NULL)')


def _verify_schema(conn):
    tables = _table_names(conn)
    if not tables:
        raise RegisterError('database is not a parcel-register database')
    required = {'meta', 'accounts', 'manifests', 'bookings', 'requests'}
    if not required.issubset(tables):
        raise RegisterError('database is not a parcel-register database')
    marker = dict(conn.execute('SELECT key, value FROM meta'))
    if marker.get('format') != DB_FORMAT:
        raise RegisterError('database is not a parcel-register database')
    if marker.get('schema_version') != str(DB_SCHEMA_VERSION):
        raise RegisterError('unsupported parcel-register schema version: '
                            + str(marker.get('schema_version')))


def _ensure_schema(conn):
    """Create the schema on a brand-new empty database, else verify the marker."""
    tables = _table_names(conn)
    if not tables:
        _init_schema(conn)
    else:
        _verify_schema(conn)


def _load_state(conn):
    accounts = {aid: int(funded) for aid, funded in
                conn.execute('SELECT account_id, funded_cents FROM accounts')}
    manifests = {mid: (status, sealed_version) for mid, status, sealed_version
                 in conn.execute(
                     'SELECT manifest_id, status, sealed_version FROM manifests')}
    bookings = {}
    for bid, account_id, manifest_id, status, shipment_json, tariff_json, \
            quote_json, total in conn.execute(
                'SELECT id, account_id, manifest_id, status, shipment_json, '
                'tariff_json, quote_json, total_cents FROM bookings'):
        bookings[bid] = {
            'id': bid,
            'account_id': account_id,
            'manifest_id': manifest_id,
            'status': status,
            'shipment': json.loads(shipment_json),
            'tariff': json.loads(tariff_json),
            'quote': json.loads(quote_json),
            'total': int(total),
        }
    version = int(conn.execute(
        "SELECT value FROM meta WHERE key='version'").fetchone()[0])
    return accounts, manifests, bookings, version


def _snapshot(accounts, manifests, bookings, version):
    held = defaultdict(int)
    spent = defaultdict(int)
    for booking in bookings.values():
        if booking['status'] == 'held':
            held[booking['account_id']] += booking['total']
        elif booking['status'] == 'shipped':
            spent[booking['account_id']] += booking['total']
    account_rows = []
    for account_id in sorted(accounts):
        funded = accounts[account_id]
        held_cents = held.get(account_id, 0)
        spent_cents = spent.get(account_id, 0)
        account_rows.append({
            'account_id': account_id,
            'funded_cents': funded,
            'held_cents': held_cents,
            'spent_cents': spent_cents,
            'available_cents': funded - held_cents - spent_cents,
        })
    booking_rows = []
    for bid in sorted(bookings):
        booking = bookings[bid]
        booking_rows.append({
            'id': bid,
            'account_id': booking['account_id'],
            'manifest_id': booking['manifest_id'],
            'status': booking['status'],
            'shipment': booking['shipment'],
            'tariff': booking['tariff'],
            'quote': booking['quote'],
        })
    manifest_rows = []
    for manifest_id in sorted(manifests):
        status, sealed_version = manifests[manifest_id]
        member_ids = sorted(
            bid for bid, booking in bookings.items()
            if booking['manifest_id'] == manifest_id
            and booking['status'] != 'cancelled')
        total = sum(bookings[bid]['total'] for bid in member_ids)
        manifest_rows.append({
            'manifest_id': manifest_id,
            'status': status,
            'member_ids': member_ids,
            'total_cents': total,
            'sealed_version': sealed_version,
        })
    return {
        'version': version,
        'accounts': account_rows,
        'bookings': booking_rows,
        'manifests': manifest_rows,
    }


def _persist_booking(conn, booking):
    conn.execute(
        'INSERT OR REPLACE INTO bookings (id, account_id, manifest_id, status, '
        'shipment_json, tariff_json, quote_json, total_cents) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (booking['id'], booking['account_id'], booking['manifest_id'],
         booking['status'],
         json.dumps(booking['shipment'], sort_keys=True, ensure_ascii=False),
         json.dumps(booking['tariff'], sort_keys=True, ensure_ascii=False),
         json.dumps(booking['quote'], sort_keys=True, ensure_ascii=False),
         str(booking['total'])))


def _apply_fund(conn, accounts, parsed):
    for entry in parsed['entries']:
        account_id = entry['account_id']
        if account_id in accounts:
            accounts[account_id] += entry['cents']
            conn.execute('UPDATE accounts SET funded_cents=? WHERE account_id=?',
                         (str(accounts[account_id]), account_id))
        else:
            accounts[account_id] = entry['cents']
            conn.execute('INSERT INTO accounts (account_id, funded_cents) '
                         'VALUES (?, ?)',
                         (account_id, str(entry['cents'])))


def _apply_amend(conn, accounts, manifests, bookings, parsed):
    upserts = parsed['upserts']
    cancels = parsed['cancels']

    # Reference validation against the current committed state.
    for upsert in upserts:
        account_id = upsert['account_id']
        manifest_id = upsert['manifest_id']
        shipment_id = upsert['shipment']['id']
        if account_id not in accounts:
            raise RegisterError('unknown account: ' + account_id)
        if manifest_id in manifests and manifests[manifest_id][0] != 'open':
            raise RegisterError('target manifest is not open: ' + manifest_id)
        if shipment_id in bookings:
            current = bookings[shipment_id]
            # Terminal identity is permanent and is checked before any
            # manifest state so re-registering a shipped/cancelled id always
            # reports the identity violation.
            if current['status'] != 'held':
                raise RegisterError(
                    'cancelled or shipped shipment id cannot be re-registered: '
                    + shipment_id)
            old_manifest = manifests.get(current['manifest_id'])
            if old_manifest is None or old_manifest[0] != 'open':
                raise RegisterError(
                    'source manifest is not open: ' + current['manifest_id'])
    for shipment_id in cancels:
        current = bookings.get(shipment_id)
        if current is None or current['status'] != 'held':
            raise RegisterError('cancel must reference a currently held parcel: '
                                + shipment_id)

    # Provisional state after the whole batch; funds are judged on this final
    # state, never per input row, so cross-account swaps cannot fail midway.
    provisional = {bid: dict(booking) for bid, booking in bookings.items()}
    provisional_manifests = dict(manifests)
    for upsert in upserts:
        shipment = upsert['shipment']
        shipment_id = shipment['id']
        manifest_id = upsert['manifest_id']
        if manifest_id not in provisional_manifests:
            provisional_manifests[manifest_id] = ('open', None)
        provisional[shipment_id] = {
            'id': shipment_id,
            'account_id': upsert['account_id'],
            'manifest_id': manifest_id,
            'status': 'held',
            'shipment': shipment,
            'tariff': upsert['tariff'],
            'quote': upsert['quote'],
            'total': upsert['quote']['total_cents'],
        }
    for shipment_id in cancels:
        provisional[shipment_id]['status'] = 'cancelled'

    held = defaultdict(int)
    spent = defaultdict(int)
    for booking in provisional.values():
        if booking['status'] == 'held':
            held[booking['account_id']] += booking['total']
        elif booking['status'] == 'shipped':
            spent[booking['account_id']] += booking['total']
    for account_id, funded in accounts.items():
        if funded - held.get(account_id, 0) - spent.get(account_id, 0) < 0:
            raise RegisterError(
                'insufficient available funds for account: ' + account_id)

    # All checks passed: persist the batch.  New open manifests first.
    for manifest_id, state in provisional_manifests.items():
        if manifest_id not in manifests:
            conn.execute('INSERT INTO manifests (manifest_id, status, '
                         'sealed_version) VALUES (?, ?, ?)',
                         (manifest_id, state[0], state[1]))
    for upsert in upserts:
        _persist_booking(conn, provisional[upsert['shipment']['id']])
    for shipment_id in cancels:
        conn.execute('UPDATE bookings SET status=? WHERE id=?',
                     ('cancelled', shipment_id))
    return provisional_manifests, provisional


def _apply_seal(conn, manifests, bookings, parsed, new_version):
    for manifest_id in parsed['manifest_ids']:
        state = manifests.get(manifest_id)
        if state is None:
            raise RegisterError('unknown manifest: ' + manifest_id)
        if state[0] != 'open':
            raise RegisterError('manifest is not open: ' + manifest_id)
    for manifest_id in parsed['manifest_ids']:
        conn.execute('UPDATE manifests SET status=?, sealed_version=? '
                     'WHERE manifest_id=?',
                     ('sealed', new_version, manifest_id))
        member_ids = [bid for bid, booking in bookings.items()
                      if booking['manifest_id'] == manifest_id
                      and booking['status'] == 'held']
        for shipment_id in member_ids:
            conn.execute('UPDATE bookings SET status=? WHERE id=?',
                         ('shipped', shipment_id))
            bookings[shipment_id]['status'] = 'shipped'
        manifests[manifest_id] = ('sealed', new_version)


def apply(database, data):
    """Apply one validated request and return its immutable receipt.

    The receipt is ``{request_id, version, snapshot}`` where version is the
    database version after this request's first successful commit and snapshot
    is the complete state at that point in time.  A repeated request with the
    same id and canonical content returns the original receipt even when the
    database has since advanced; the same id with different content is
    rejected.  The caller's ``data`` is never mutated.
    """
    action, request_id, expected_version, parsed = _validate_request(data)
    canonical = _canonical(action, request_id, expected_version, parsed)
    conn = _connect(database, create=True)
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                _ensure_schema(conn)
                # Idempotency precedes every version and state check.
                existing = conn.execute(
                    'SELECT canonical_json, receipt_json FROM requests '
                    'WHERE request_id=?', (request_id,)).fetchone()
                if existing is not None:
                    if existing[0] != canonical:
                        raise RegisterError(
                            'request_id reused with different canonical '
                            'content: ' + request_id)
                    conn.commit()
                    return json.loads(existing[1])

                accounts, manifests, bookings, version = _load_state(conn)
                if expected_version != version:
                    raise RegisterError(
                        'version conflict: expected ' + str(expected_version)
                        + ' but database is at version ' + str(version))
                new_version = version + 1

                if action == 'fund':
                    _apply_fund(conn, accounts, parsed)
                elif action == 'amend':
                    manifests, bookings = _apply_amend(
                        conn, accounts, manifests, bookings, parsed)
                else:
                    _apply_seal(conn, manifests, bookings, parsed, new_version)

                conn.execute("UPDATE meta SET value=? WHERE key='version'",
                             (str(new_version),))
                current_snapshot = _snapshot(
                    accounts, manifests, bookings, new_version)
                receipt = {'request_id': request_id,
                           'version': new_version,
                           'snapshot': current_snapshot}
                conn.execute(
                    'INSERT INTO requests (request_id, canonical_json, '
                    'receipt_json, version) VALUES (?, ?, ?, ?)',
                    (request_id, canonical,
                     json.dumps(receipt, sort_keys=True, ensure_ascii=False),
                     new_version))
                conn.commit()
                return receipt
            except Exception:
                # The single BEGIN IMMEDIATE transaction owns every change,
                # including schema DDL on a brand-new database: rollback
                # leaves no business fact and no successful request record
                # (an empty database file may remain, which the contract
                # permits).  The file is never unlinked because a concurrent
                # connection could otherwise keep writing to an orphaned
                # inode invisible at the database path.
                conn.rollback()
                raise
        except sqlite3.Error as error:
            raise RegisterError('database error: ' + str(error)) from error
    finally:
        conn.close()


def snapshot(database):
    """Return the current committed state snapshot without modifying anything.

    All rows are read inside one transaction so the four sections come from a
    single read snapshot.  A missing path is an error -- this call never
    creates a database -- and foreign or unsupported databases are rejected.
    """
    conn = _connect(database, create=False)
    try:
        try:
            conn.execute('PRAGMA query_only=1')
            conn.execute('BEGIN')
            try:
                _verify_schema(conn)
                accounts, manifests, bookings, version = _load_state(conn)
                result = _snapshot(accounts, manifests, bookings, version)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
        except sqlite3.Error as error:
            raise RegisterError('database error: ' + str(error)) from error
    finally:
        conn.close()
