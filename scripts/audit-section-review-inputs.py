"""Estimate section review inputs from immutable real responses; no model/network calls."""

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/noodle/src'))

from noodle_agent.next.assembly_generation import SourceSectionPipeline
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.section_contracts import bounded_source_draft
from noodle_agent.next.section_review import section_packet, section_records
from noodle_agent.next.segmentation import tokens


def audit(library, revision):
    root = library.resolve()
    with sqlite3.connect((root / 'noodle.db').as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        job = connection.execute("SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=? ORDER BY created_at DESC LIMIT 1", (revision,)).fetchone()
        records = connection.execute("SELECT stage,artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=? AND state='DONE' ORDER BY created_at DESC", (job['id'],)).fetchall()
        observed = connection.execute("SELECT count(*),sum(input_tokens),sum(output_tokens) FROM provider_calls WHERE root_job_id=? AND operation='next.assembly_review'", (job['id'],)).fetchone()

    def load(row):
        raw = (root / row['artifact_ref']).read_bytes()
        if sha(raw) != row['artifact_sha256']:
            raise ValueError('Frozen checkpoint hash differs')
        return json.loads(raw)

    units = load(next(r for r in records if r['stage'] == 'units'))['units']
    plan = load(next(r for r in records if r['stage'].startswith('source.sections.plan')))
    navigation = {u['unit_id'] for u in plan['layout_exclusions'] if u['category'] == 'NAVIGATION'}
    contexts = [load(r) for r in records if r['stage'].startswith('page-context.')]
    pipeline = object.__new__(SourceSectionPipeline)
    pipeline.g = SimpleNamespace(profile={'checking_policy': 'single-check-v1'})
    rows, errors = [], []
    for topic in plan['plans']:
        receipt = next((r for r in records if r['stage'].startswith('source.write.' + topic['key'] + '.') and not r['stage'].endswith('.error')), None)
        if not receipt:
            continue
        try:
            response = load(receipt)
            draft = bounded_source_draft(topic['unit_ids'], ranges='source_ranges' in response,
                source_order=[u['unit_id'] for u in units], navigation_ids=navigation).model_validate(response)
        except ValueError as exc:
            errors.append({'plan_key': topic['key'], 'detail': str(exc)[:300]})
            continue
        selected = set(topic['unit_ids'])
        item = {'key': topic['key'], 'draft': draft.model_dump(), 'evidence': {},
                'inputs': {'units': [u for u in units if u['unit_id'] in selected], 'image_paths': []},
                'contexts': [c for c in contexts if c['page'] in topic['pages']]}
        local = section_records(item)
        rows.append({'title': topic['title'], 'sections': len(local),
                     'whole_atomic_packet_tokens': tokens(canonical(pipeline.make_packet([item]))),
                     'section_packet_tokens': [tokens(canonical(section_packet(pipeline, s)[0])) for s in local]})
    before = [r['whole_atomic_packet_tokens'] for r in rows]
    after = [v for r in rows for v in r['section_packet_tokens']]
    return {'revision': revision, 'provider_calls': 0, 'library_mutations': 0,
            'basis': 'Token estimates cover payload JSON only; repeated prompts/schema and output are additional. Recorded provider usage is historical, not a new run.',
            'historical_review': {'calls': observed[0], 'input_tokens': observed[1], 'output_tokens': observed[2]},
            'replayed_atomics': len(rows), 'sections': len(after),
            'whole_atomic_packet_tokens': {'median': statistics.median(before), 'maximum': max(before), 'sum': sum(before)} if before else {},
            'section_packet_tokens': {'median': statistics.median(after), 'maximum': max(after), 'sum': sum(after)} if after else {},
            'atomics': rows, 'unreplayed_responses': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.library, args.revision), ensure_ascii=False, indent=2))
