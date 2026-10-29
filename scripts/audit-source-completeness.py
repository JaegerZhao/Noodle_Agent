"""Read-only audit of a frozen source plan, published bodies and index coverage.

This checks recorded coverage, not the semantic completeness of every statement.
It never generates, repairs, parses a PDF or calls a provider.
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/noodle/src'))

from noodle_agent.next.pipeline_state import readiness, source_plan
from noodle_agent.next.repository import one, rows
from noodle_agent.next.store import Store


def audit(store, revision):
    job, plan, checkpoints = source_plan(store, revision)
    if not plan:
        raise ValueError('Source has no frozen source-section plan')
    with store.db.engine.connect() as c:
        published = {n['creation_key'].rsplit(':', 1)[-1]: dict(n) for n in rows(c,
            "SELECT n.*,r.markdown_ref,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id "
            "WHERE n.kind='ATOMIC' AND n.visibility='ACTIVE' AND substr(n.creation_key,1,length(:p))=:p",
            p=f'atomic:{revision}:source-section:')}
    excluded = {u['unit_id'] for u in plan.get('layout_exclusions', [])}
    resolved, deferred = set(), set()
    for cp in checkpoints:
        if cp['stage'].startswith('source.publish.'):
            receipt = json.loads(store.read(cp['artifact_ref'], cp['artifact_sha256']))
            resolved.update(receipt.get('resolved_plan_keys', []))
            excluded.update(receipt.get('omissions', {}))
            deferred.update(v['plan_key'] for v in receipt.get('deferred', []))
    deferred.difference_update(resolved)
    results = []
    for item in plan['plans']:
        key, expected = item['key'], set(item['unit_ids'])
        note = published.get(key)
        result = {'key': key, 'title': item['title'], 'pages': item['pages'], 'expected_units': len(expected)}
        if not note:
            status = 'OMITTED' if key in resolved and expected <= excluded else 'DEFERRED' if key in deferred else 'NOT_GENERATED'
            result.update(status=status, missing_units=sorted(expected - excluded))
        else:
            body = store.read(note['markdown_ref'], note['markdown_sha256']).decode('utf-8')
            with store.db.engine.connect() as c:
                valid = store.content.valid_revision(c, note['current_revision_id'])
                sections = rows(c, 'SELECT * FROM sections WHERE revision_id=:r ORDER BY position', r=note['current_revision_id'])
                proof = one(c, "SELECT * FROM build_artifacts WHERE storage_ref=:r LIMIT 1", r=f"artifacts/builds/{note['current_revision_id']}.json")
            report = json.loads(store.read(proof['storage_ref'], proof['sha256']))['review']['report'] if proof else {}
            accounted = set(report.get('units', [])) | set(report.get('omissions', {}))
            empty = [s['section_id'] for s in sections if not re.sub(r'^#{1,6}\s+.*$', '', body[s['start_offset']:s['end_offset']], flags=re.MULTILINE).strip()]
            missing = sorted(expected - accounted - excluded)
            result.update(status='PUBLISHED' if valid and sections and not empty and not missing else 'CONTENT_GAP',
                note_id=note['id'], revision_id=note['current_revision_id'], eligible=valid,
                sections=len(sections), empty_sections=empty, missing_units=missing,
                explicit_omissions=len(expected & set(report.get('omissions', {}))))
        results.append(result)
    by_key = {v['key']: v for v in results}
    chapters = []
    for chapter in plan.get('chapter_tasks', []):
        members = [by_key[k] for k in chapter['plan_keys']]
        chapters.append({'title': chapter['title'], 'planned': len(members),
            'published': sum(v['status'] == 'PUBLISHED' for v in members),
            'omitted': sum(v['status'] == 'OMITTED' for v in members),
            'gaps': [v['title'] for v in members if v['status'] not in {'PUBLISHED', 'OMITTED'}]})
    units_cp = next((v for v in checkpoints if v['stage'] == 'units'), None)
    units = json.loads(store.read(units_cp['artifact_ref'], units_cp['artifact_sha256']))['units'] if units_cp else []
    planned_units = {u for p in plan['plans'] for u in p['unit_ids']}
    unplanned = sorted({u['unit_id'] for u in units} - planned_units - excluded)
    state = readiness(store, revision)
    gaps = [v for v in results if v['status'] not in {'PUBLISHED', 'OMITTED'}]
    return {'schema_version': 'source-completeness-audit-v1', 'recorded_at': datetime.now(UTC).isoformat(),
        'source_revision_id': revision, 'job_status': job['status'], 'planned': len(results),
        'published': sum(v['status'] == 'PUBLISHED' for v in results), 'gaps': gaps,
        'chapters': chapters, 'units_outside_plan_and_explicit_exclusions': unplanned,
        'readiness': state, 'items': results,
        'content_complete': not gaps and not unplanned,
        'notes_and_index_ready': not gaps and not unplanned and state['notes_status'] == 'READY'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path)
    parser.add_argument('revision')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    store = Store(args.library)
    try:
        result = audit(store, args.revision)
    finally:
        store.close()
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({
        'recorded_at': result['recorded_at'], 'job_status': result['job_status'],
        'planned': result['planned'], 'published': result['published'],
        'gaps': [{k: v[k] for k in ('key', 'title', 'status')} for v in result['gaps']],
        'chapters': result['chapters'],
        'unplanned_units': len(result['units_outside_plan_and_explicit_exclusions']),
        'content_complete': result['content_complete'],
        'notes_and_index_ready': result['notes_and_index_ready'],
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
