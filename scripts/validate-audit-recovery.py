"""Replay book audit inputs offline; read-only snapshot, no models/publications."""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'services/noodle/src'))
from noodle_agent.next.assembly_generation import SourceSectionPipeline
from noodle_agent.next.concepts.analysis import catalogue
from noodle_agent.next.concepts.entity_context import source_context
from noodle_agent.next.concepts.entity_routing import cohort
from noodle_agent.next.concepts.routing_budget import bounded_packet
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.pipeline_state import readiness
from noodle_agent.next.review_packets import review_packets
from noodle_agent.next.source_sections import units_from_blocks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, default=REPO / '.local-only/book-system-audit-20261025/final')
    parser.add_argument('--library', type=Path, default=REPO / 'data/knowledge-formal-20261025T043825Z')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    def load(name):
        return json.loads((args.audit / name).read_text(encoding='utf-8'))
    data, notes = load('tables.json'), load('notes.json')
    def read(ref, digest):
        raw = (args.library / ref).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        return raw
    def value(row):
        return json.loads(read(row['artifact_ref'], row['artifact_sha256']))
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect((args.audit / 'snapshot.db').resolve().as_uri() + '?mode=ro', uri=True))
    store = SimpleNamespace(root=args.library.resolve(), read=read, path=lambda r: args.library / r,
        db=SimpleNamespace(engine=engine), content=SimpleNamespace(valid_revision=lambda c, r: bool(eligible(c, [r]))),
        sources=SimpleNamespace(source_valid=lambda c, r: True))
    try:
        with engine.connect() as c:
            catalog = catalogue(store, c)
        by_id = {n['id']: n for n in notes}
        routing = []
        for job in data['jobs']:
            if job['kind'] != 'ANALYZE_ATOMIC' or 'cohort exceeds planning budget' not in job['error_json']:
                continue
            batch = json.loads(job['input_json'])['payload']['routing_batch']
            note = by_id[batch['atomics'][0]['note_id']]
            revision, cards = cohort(store, {**note, 'revision': {'id': note['revision_id']}}, batch)
            with engine.connect() as c:
                context = source_context(store, c, revision, cards, catalog)
            _, payload, receipt = bounded_packet(cards, catalog, context)
            assert len(payload['atomics']) == len(cards) and receipt['tokens'] <= 22000
            routing.append({'job_id': job['id'], **receipt})
        ck = data['job_checkpoints']
        plan = value(next(c for c in ck if c['stage'].startswith('source.sections.plan')))
        parsed = value(next(c for c in ck if c['stage'] == 'parse.normalized'))
        units = {u.unit_id: u.model_dump(mode='json') for u in units_from_blocks(parsed['artifact'])}
        contexts = {v['page']: v for row in ck if row['stage'].startswith('page-context.source-v3.') for v in [value(row)]}
        failed = {}
        for row in ck:
            if row['stage'].startswith('source.publish.'):
                for item in value(row).get('deferred', []):
                    if 'exceeds review budget' in json.dumps(item):
                        failed[item['plan_key']] = item
        pipeline = SourceSectionPipeline(SimpleNamespace(store=None, profile={'checking_policy':'single-check-v1'}))
        reviewed = []
        for p in plan['plans']:
            if p['key'] not in failed:
                continue
            drafts = [row for row in ck if row['stage'].startswith('source.write.' + p['key'] + '.')]
            assert drafts, p['key']
            draft = value(drafts[-1])
            selected = [units[u] for u in p['unit_ids']]
            pages = {u['page'] for u in selected}
            record = {'key':p['key'], 'draft':draft, 'inputs':{'units':selected},
                'contexts':[contexts[n] for n in sorted(pages) if n in contexts]}
            packets, receipt = review_packets(pipeline.make_packet([record]))
            assert all(v['tokens'] <= 24000 for v in receipt['packets'])
            expected = ''.join(u['text'] for u in selected)
            actual = ''.join(u['text'] for packet in packets for a in packet['assembly'] for u in a['source_units'])
            assert actual == expected
            reviewed.append({'plan_key':p['key'], 'title':p['title'], **receipt})
        state = readiness(store, plan['source_revision_id'])
        assert state['concept_maintenance']['failed_routing_jobs'] >= len(routing)
        assert state['concept_maintenance']['status'] == 'PARTIAL'
        result = {'status':'OFFLINE_PASSED', 'model_calls':0, 'routing':routing, 'atomic_reviews':reviewed,
            'maintenance':state['concept_maintenance'], 'structure':state['knowledge_structure']}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'status':result['status'], 'routing_batches':len(routing), 'atomic_reviews':len(reviewed),
            'max_routing_tokens':max((r['tokens'] for r in routing), default=0),
            'atomic_partitions':[r['partitions'] for r in reviewed], 'model_calls':0}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
