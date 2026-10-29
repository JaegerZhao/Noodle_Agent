"""Attach original timing evidence to a Concept-only replay without copying usage rows."""

import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/noodle/src'))
from noodle_agent.next.imported_metrics import capture
from noodle_agent.next.store import Store

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--target',type=Path,required=True)
args=parser.parse_args()
if not args.target.resolve().is_relative_to(ROOT/'.local-only'):
    raise ValueError('History display imports are restricted to isolated validation libraries')
source,target=Store(args.source.resolve()),Store(args.target.resolve())
try:
    print(json.dumps(capture(source,target),ensure_ascii=False))
finally:
    source.close()
    target.close()
