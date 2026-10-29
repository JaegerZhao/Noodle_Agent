"""Stage receipts and read-only input freezing for the knowledge workspace rollout."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/noodle/src'))

from noodle_agent.next.concepts.replay_inputs import ordered_inputs
from noodle_agent.next.contracts import canonical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['freeze', 'status'])
    parser.add_argument('--base', type=Path, default=ROOT / 'data/knowledge-formal-20261025T043825Z')
    parser.add_argument('--run', type=Path, default=ROOT / '.local-only/concept-workspace-20261028')
    args = parser.parse_args()
    run = args.run.resolve()
    if not run.is_relative_to((ROOT / '.local-only').resolve()) or run == args.base.resolve():
        raise ValueError('Run must be isolated inside .local-only')
    run.mkdir(parents=True, exist_ok=True)
    if args.phase == 'freeze':
        value = ordered_inputs(args.base)
        path = run / 'input-manifest.json'
        if path.exists() and json.loads(path.read_text(encoding='utf8'))['input_sha256'] != value['input_sha256']:
            raise ValueError('Frozen input changed; inspect before creating a new run')
        path.write_text(canonical(value), encoding='utf8')
        (run / 'source-order.json').write_text(canonical([
            {**{k: v for k, v in s.items() if k != 'atomics'}, 'count': len(s['atomics']),
             'chapters': list(dict.fromkeys(n['chapter_title'] for n in s['atomics']))} for s in value['sources']]), encoding='utf8')
        baseline = {'base': str(args.base.resolve()), 'input_sha256': value['input_sha256'],
                    'frozen_at': datetime.now(UTC).isoformat(), 'model_calls': 0}
        (run / 'baseline.json').write_text(canonical(baseline), encoding='utf8')
        status = run / 'stage-status.json'
        if not status.exists():
            status.write_text(canonical({f'N{i}': {'status': 'RUNNING' if i == 0 else 'NOT_STARTED'} for i in range(9)}), encoding='utf8')
        print(json.dumps({'atomic_count': value['atomic_count'], 'input_sha256': value['input_sha256'],
                          'sources': [(s['title'], len(s['atomics'])) for s in value['sources']]}, ensure_ascii=False))
    else:
        print((run / 'stage-status.json').read_text(encoding='utf8'))


if __name__ == '__main__':
    main()
