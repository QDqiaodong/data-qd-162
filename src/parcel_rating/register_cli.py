"""Console entry point for the schema v5 SQLite parcel register.

Independent from ``parcel-rating``: this module never imports or changes the
v1..v4 command line and its ``run`` signature.  Two modes:

* apply one request::

      parcel-register --database DB --source FILE --output FILE

* read-only snapshot (``--source`` is forbidden here)::

      parcel-register --database DB --snapshot --output FILE

Database commit and report publication are deliberately two separate steps,
not a cross-file atomic transaction: the request is first committed (including
its immutable receipt) inside SQLite, then the report is published via the
shared atomic :func:`parcel_rating.common.save`.  If publication fails after a
successful commit the process exits 2 and names the committed request id and
version on stderr; replaying the identical request restores the original
receipt without double booking.
"""
import argparse
import json
import sys
from pathlib import Path

from .common import read, save
from .register import (REPORT_SCHEMA_VERSION, RegisterError, apply,
                       snapshot as take_snapshot)

DESCRIPTION = 'SQLite pre-deposit and shipment-manifest register (schema v5)'


def _publish(path, report):
    """Atomically write the report and return the exact bytes printed."""
    text = json.dumps(report, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, indent=2) + '\n'
    save(path, report)
    return text


def _preflight(database, source, output):
    """Reject symlinks and path collisions before any business write.

    The contract requires database and output symlinks to be refused up
    front; source/database/output path collisions are refused as well.  The
    source itself may legitimately be reached through a directory symlink and
    is therefore only compared by resolved path, never rejected as a link.
    """
    for label, path in (('database', database), ('output', output)):
        if Path(path).is_symlink():
            raise RegisterError(label + ' path cannot be a symlink')
    paths = [Path(database).resolve(), Path(output).resolve()]
    if source is not None:
        paths.append(Path(source).resolve())
    if len(set(paths)) != len(paths):
        raise RegisterError('source, database and output paths must differ')


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument('--source', type=Path,
                              help='request JSON to apply (mutually exclusive '
                                   'with --snapshot)')
    source_group.add_argument('--snapshot', action='store_true',
                              help='read-only current state snapshot; '
                                   '--source is forbidden in this mode')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        _preflight(args.database, args.source, args.output)
        if args.snapshot:
            result = take_snapshot(args.database)
            report = {'schema_version': REPORT_SCHEMA_VERSION,
                      'source_sha256': None,
                      'result': result}
            text = _publish(args.output, report)
            print(text, end='')
            return 0

        data, digest = read(args.source)
        receipt = apply(args.database, data)
        # The SQLite commit is the point of effect: from here on a publishing
        # failure cannot undo the committed request, and replaying the same
        # request restores this exact receipt without double booking.
        report = {'schema_version': REPORT_SCHEMA_VERSION,
                  'source_sha256': digest,
                  'result': receipt}
        try:
            text = _publish(args.output, report)
        except (OSError, ValueError) as error:
            print('report publication failed after commit: ' + str(error)
                  + '; committed request_id=' + receipt['request_id']
                  + ' version=' + str(receipt['version']), file=sys.stderr)
            return 2
        print(text, end='')
        return 0
    except (RegisterError, ValueError, TypeError, KeyError, OSError,
            OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
