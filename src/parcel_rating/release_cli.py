"""Console entry point for the schema v6 offline release evaluation.

Independent from ``parcel-rating`` and ``parcel-register``: this module never
changes the older command lines or their ``run`` signatures.  Usage::

    parcel-rate-release --source FILE --output FILE

The source is a UTF-8 JSON Lines file (header line plus one batch object per
line).  On success the report ``{schema_version: 6, source_sha256, result}``
is published atomically and printed to stdout with the same parsed value.
Any input or I/O failure exits 2 and preserves an existing report.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .common import save
from .release import run

DESCRIPTION = 'Offline parcel-rate release evaluation (schema v6)'


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True,
                        help='UTF-8 JSON Lines release input')
    parser.add_argument('--output', type=Path, required=True,
                        help='report destination (atomically replaced)')
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
