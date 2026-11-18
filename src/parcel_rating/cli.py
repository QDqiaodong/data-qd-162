DESCRIPTION='Dimensional-weight parcel rating with exact tariff and surcharge evidence'
import argparse, json, sys
from pathlib import Path
from .common import read, save
from .model import analyze, plan, temporal

def run(source, planning, temporal_mode):
    data,digest=read(source)
    if temporal_mode:
        result=temporal(data)
        schema=3
    elif planning:
        result=plan(data)
        schema=2
    else:
        result=analyze(data)
        schema=1
    return {'schema_version':schema,'source_sha256':digest,'result':result}

def main():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--plan',action='store_true',help='batch dispatch allocation mode (schema_version 2)')
    mode.add_argument('--temporal',action='store_true',help='authoritative revision timeline and point-in-time rating (schema_version 3)')
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
        report=run(args.source,args.plan,args.temporal);save(args.output,report)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
