"""Offline, repeatable reading/figure audit. Never invokes workers or providers."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def state(root):
    with sqlite3.connect((root / 'noodle.db').as_uri() + '?mode=ro', uri=True) as c:
        return {'calls': c.execute('SELECT count(*) FROM provider_calls').fetchone()[0],
                'tokens': c.execute('SELECT sum(total_tokens) FROM provider_calls').fetchone()[0],
                'jobs': c.execute('SELECT id,status,attempts FROM jobs ORDER BY id').fetchall(),
                'heads': c.execute('SELECT id,current_revision_id FROM notes ORDER BY id').fetchall()}


def audit(root):
    from noodle_agent.next.figure_bindings import display_projection
    from noodle_agent.next.repository import rows
    from noodle_agent.next.source_structure import source_structure
    from noodle_agent.next.store import Store
    before = state(root)
    store = Store(root)
    try:
        with store.db.engine.connect() as c:
            notes = rows(c, "SELECT id FROM notes WHERE current_revision_id IS NOT NULL AND visibility='ACTIVE'")
            sources = rows(c, "SELECT current_revision_id FROM sources WHERE kind='PDF' AND deleted_at IS NULL")
        records = []
        for item in notes:
            raw = store.get_note(item['id'])
            projected = display_projection(store, raw)
            assert hashlib.sha256(raw['markdown'].encode()).hexdigest() == raw['revision']['markdown_sha256']
            records.append({'note_id': raw['id'], 'title': raw['title'], 'kind': raw['kind'],
                            'revision_id': raw['revision']['id'], 'raw_sha256': raw['revision']['markdown_sha256'],
                            'display_projection': projected.get('display_projection')})
        structures = []
        for source in sources:
            value = source_structure(store, source['current_revision_id'])
            structures.append(value)
            for item in value['notes']:
                note = store.get_note(item['note_id'])
                assert {s['section_id'] for s in item['section_anchors']} == {s['section_id'] for s in note['sections']}
                ids = {n['node_id'] for n in value['outline']['nodes']}
                assert all(a['node_id'] is None or a['node_id'] in ids for a in item['section_anchors'])
        after = state(root)
        assert before == after, 'Read-only audit altered jobs, heads or model usage'
        return {'policy': 'reading-refinement-audit-v1', 'before': before, 'after': after,
                'notes': records, 'sources': structures}
    finally:
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.library.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'notes': len(result['notes']), 'corrected_notes': sum(bool(n['display_projection']) for n in result['notes']),
                      'calls': result['after']['calls'], 'tokens': result['after']['tokens'], 'output': str(args.output)}, ensure_ascii=False))
