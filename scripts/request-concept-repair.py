"""Enqueue located Concept findings; the normal model/lease/publisher writes a new version."""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'services/noodle/src'))
from noodle_agent.next.concepts.coordinator import request_repair
from noodle_agent.next.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path)
    parser.add_argument('request', type=Path, help='JSON: note_id, expected_revision, findings (located quotes/issues, never replacement prose)')
    parser.add_argument('--enqueue', action='store_true')
    args = parser.parse_args()
    value = json.loads(args.request.read_text(encoding='utf-8'))
    if not args.enqueue:
        print(json.dumps({'action':'PREVIEW', **value}, ensure_ascii=False))
        return
    store = Store(args.library)
    try:
        result = request_repair(store, value['note_id'], value['expected_revision'], value['findings'])
        print(json.dumps(result, ensure_ascii=False))
    finally:
        store.close()


if __name__ == '__main__':
    main()
