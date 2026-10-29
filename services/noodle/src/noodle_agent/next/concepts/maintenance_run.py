"""Bounded incremental maintenance over an independently prepared library."""

import json
import time
from collections import Counter

from ..contracts import canonical, sha
from ..repository import Conflict, rows
from ..segmentation import tokens


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def source_batches(members, cards, *, budget=9000, limit=24):
    """Keep source order and a planned object together across execution packets."""
    groups = {}
    for member in sorted(members, key=lambda m: (m['source_revision_id'], m.get('source_position', 0), m['note_id'])):
        key = (member['source_revision_id'], member.get('chapter_key'), member.get('outline_key') or member['note_id'])
        groups.setdefault(key, []).append(member)
    batches, current = [], []
    for group in groups.values():
        same_chapter = not current or (group[0]['source_revision_id'], group[0].get('chapter_key')) == (current[0]['source_revision_id'], current[0].get('chapter_key'))
        trial = [*current, *group]
        if current and (not same_chapter or len(trial) > limit or tokens(canonical([cards[m['note_id']] for m in trial])) > budget):
            batches.append(current)
            current = []
        for member in group:
            trial = [*current, member]
            if current and (len(trial) > limit or tokens(canonical([cards[m['note_id']] for m in trial])) > budget):
                batches.append(current)
                current = []
            current.append(member)
    if current:
        batches.append(current)
    return batches


def plan(store, run, baseline_sha256, *, atomic_ids=None):
    destination = run / 'change-plan.json'
    if destination.exists():
        saved = json.loads(destination.read_text(encoding='utf-8'))
        if saved.get('planning_version') == 4 and (atomic_ids is None or set(atomic_ids) == set(saved.get('requested_atomics') or [])):
            return saved
        budget = run / 'call-budget.json'
        if budget.exists() and json.loads(budget.read_text(encoding='utf-8')).get('calls'):
            if atomic_ids is None:
                return saved  # Recorded plans remain readable; never silently replan a paid run.
            raise Conflict('Cannot replace an already executing plan')
        archive = run / ('change-plan-before-budget-' + sha(canonical(saved))[:12] + '.json')
        if not archive.exists():
            destination.rename(archive)
    with store.db.engine.connect() as c:
        claims = {r['concept_id']: json.loads(r['definition_json']) for r in rows(c, 'SELECT * FROM concept_identity_claims')}
        atomics = rows(c, "SELECT n.*,r.plan_hash,r.route_json,r.concept_id FROM notes n JOIN atomic_entity_routes r ON r.atomic_revision_id=n.current_revision_id WHERE n.kind='ATOMIC' AND n.visibility='ACTIVE' ORDER BY n.creation_key")
        checkpoints = rows(c, "SELECT json_extract(j.input_json,'$.revision_id') source_revision,k.artifact_ref,k.artifact_sha256 FROM job_checkpoints k JOIN jobs j ON j.id=k.job_id WHERE j.kind='GENERATE_ATOMICS' AND k.stage LIKE 'source.sections.plan%' AND k.state='DONE' ORDER BY k.created_at")
        ambiguous = [r['owner'] for r in rows(c, "SELECT n.id owner,count(*) count FROM links l JOIN notes n ON n.current_revision_id=l.from_revision_id WHERE n.kind='CONCEPT' AND json_extract(l.basis_json,'$.navigation_role') IS NOT NULL GROUP BY n.id HAVING count(*)>=4 ORDER BY count(*) DESC,n.id")]
    plans = {}
    for checkpoint in checkpoints:
        data = json.loads(store.read(checkpoint['artifact_ref'], checkpoint['artifact_sha256']))
        plans[checkpoint['source_revision']] = {p['key']: {**p, '_position': i} for i, p in enumerate(data.get('plans', []))}
    members, cards = [], {}
    from .entity_routing import card_for
    for row in atomics:
        parts = (row['creation_key'] or '').split(':')
        if len(parts) < 4 or parts[0] != 'atomic' or parts[2] != 'source-section':
            continue
        source, key = parts[1], ':'.join(parts[3:])
        source_plan = plans.get(source, {}).get(key, {})
        hint = source_plan.get('concept_outline') or {}
        card = card_for(store, {**dict(row), 'revision_id': row['current_revision_id']})
        card['concept_outline'] = hint
        cards[row['id']] = card
        members.append({'note_id': row['id'], 'revision_id': row['current_revision_id'],
            'title': row['title'], 'source_revision_id': source, 'chapter_key': source_plan.get('chapter_key'),
            'source_position': source_plan.get('_position', 0),
            'outline_key': hint.get('concept_key'), 'concept_id': row['concept_id'],
            'identity_key': claims.get(row['concept_id'], {}).get('identity_key'), 'plan_hash': row['plan_hash']})
    seeds = [m for m in members if m['outline_key'] and m['outline_key'] != m['identity_key']]
    seed_keys = {(m['source_revision_id'], m['outline_key']) for m in seeds}
    targets = {m['concept_id'] for m in seeds}
    orphaned_claims = {target for target in targets if not any(m['concept_id'] == target and m['outline_key'] == m['identity_key'] for m in members)}
    chapters = {(m['source_revision_id'], m['chapter_key']) for m in members if m['concept_id'] in orphaned_claims and m['chapter_key']}
    selected = [m for m in members if m['concept_id'] in targets or (m['source_revision_id'], m['outline_key']) in seed_keys
                or (m['source_revision_id'], m['chapter_key']) in chapters]
    if atomic_ids is not None:
        selected = [m for m in members if m['note_id'] in set(atomic_ids)]
        if {m['note_id'] for m in selected} != set(atomic_ids):
            raise Conflict('Requested Atomic is not a current, routed frozen input')
        targets = {m['concept_id'] for m in selected if m['concept_id']}
        ambiguous = []
    batches = source_batches(selected, cards)
    value = {'policy': 'incremental-concept-maintenance-v1', 'planning_version': 4, 'baseline_sha256': baseline_sha256,
        'requested_atomics': sorted(atomic_ids) if atomic_ids is not None else None,
        'run_id': run.name, 'seed_diagnostics': seeds, 'atomics': selected,
        'batches': [[m['revision_id'] for m in batch] for batch in batches],
        'target_concepts': sorted(targets), 'scope_rule': 'outline mismatch is a review candidate, not proof of error; include existing target supports and shared planned objects',
        'structure_owner_batches': [ambiguous[i:i+4] for i in range(0, len(ambiguous), 4)],
        'root_templates': {'machine-learning': '机器学习', 'deep-learning': '深度学习'},
        'budget': {'llm_calls': 48, 'estimated_input_tokens': 450000, 'observed_output_tokens': 140000,
                   'embedding_missing_texts': 200},
        'forbidden_jobs': ['GENERATE_ATOMICS', 'REPAIR_ATOMIC'], 'reuse': 'FROZEN_ATOMICS_ONLY',
        'provider_baseline': None}
    with store.db.engine.connect() as c:
        value['provider_baseline'] = c.exec_driver_sql('SELECT COALESCE(max(rowid),0) FROM provider_calls').scalar_one()
    value['plan_sha256'] = sha(canonical(value))
    write(destination, value)
    return value


class Budget:
    def __init__(self, store, run, frozen):
        self.store, self.path, self.frozen = store, run / 'call-budget.json', frozen
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'calls': 0, 'estimated_input_tokens': 0, 'operations': []}

    def charge(self, request):
        operation = request['operation']
        if not operation.startswith(('next.entity_', 'next.organization_', 'next.concept_maintenance_')):
            raise Conflict('Maintenance attempted an unplanned model operation: ' + operation)
        estimate = tokens(canonical(request.get('user', {}))) + tokens(request.get('system', ''))
        budget = self.frozen['budget']
        with self.store.db.engine.connect() as c:
            outputs = c.exec_driver_sql('SELECT COALESCE(sum(output_tokens),0) FROM provider_calls WHERE rowid>?', (self.frozen['provider_baseline'],)).scalar_one()
        if self.state['calls'] >= budget['llm_calls'] or self.state['estimated_input_tokens'] + estimate > budget['estimated_input_tokens'] or outputs >= budget['observed_output_tokens']:
            self.state.setdefault('cost_diagnosis', {'policy':'OBSERVE_CONTINUE','at':time.time(),
                'analysis':'Cumulative cost threshold exceeded; continue bounded operations without rejection.',
                'operation_counts':dict(Counter(r['operation'] for r in self.state['operations'])),
                'identical_request_counts':dict(Counter(r['request_sha256'] for r in self.state['operations'] if r.get('request_sha256'))),
                'thresholds':budget, 'observed_output_tokens':outputs})
        self.state['calls'] += 1
        self.state['estimated_input_tokens'] += estimate
        self.state['operations'].append({'operation': operation, 'estimated_input_tokens': estimate,
            'request_sha256':sha(canonical(request)), 'at': time.time()})
        write(self.path, self.state)


def apply(store, run, frozen, *, allow_models=False):
    if not allow_models:
        raise ValueError('Use --allow-models for the explicitly planned Concept-only run')
    from dataclasses import replace

    from ...config import Settings
    from ..provider_limits import call_guard
    from ..runtime import Application
    from .maintenance import request_replan

    for index, batch in enumerate(frozen['batches']):
        request_replan(store, batch, frozen['run_id'] + ':batch:' + str(index))
    from .structure_maintenance import request
    for index, owners in enumerate(frozen.get('structure_owner_batches', [])):
        request(store, owners, frozen['run_id'] + ':structure:' + str(index))
    settings = replace(Settings.from_env(), vision_api_key=None, mineru_api_token=None,
                       embedding_api_key=None, llm_max_retries=0)
    budget = Budget(store, run, frozen)
    token = call_guard.set(budget.charge)
    # Coalesce all reroutes before any core build, then only drain the newly
    # requested Concept work. Imported historical REVIEW jobs are never retried.
    started, outcomes = time.perf_counter(), []
    try:
        for stage, kinds in [('routing', ('ANALYZE_ATOMIC',)), ('cores', ('BUILD_CONCEPT', 'RECONCILE_DEPENDENCIES')),
                      ('relations', ('MAINTAIN_CONCEPT_STRUCTURE',)), ('organization', ('REVIEW_ORGANIZATION',)),
                      ('roots', ('MAINTAIN_CONCEPT_STRUCTURE',)), ('maps', ('UPDATE_MAPS',))]:
            if stage == 'organization':
                affected = set(frozen['target_concepts'])
                with store.repository.transaction() as c:
                    for batch in frozen['batches']:
                        for revision in batch:
                            row = rows(c, 'SELECT route_json FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision)[0]
                            value = json.loads(row['route_json'])
                            route = value['route']
                            keys = {route.get('entity_key'), *(s['entity_key'] for s in route.get('selected_contributions', []))} - {None}
                            affected.update(value['mapping'][key] for key in keys)
                    store.repository.event(c, 'ORGANIZATION_REVIEW_REQUESTED', 'concept-organization', None,
                        {'source_revision': frozen['atomics'][0]['source_revision_id'] if frozen['atomics'] else None,
                         'concept_ids': sorted(affected)}, key='maintenance-organization:' + frozen['run_id'])
            if stage == 'roots':
                request(store, [], frozen['run_id'] + ':roots', roots=True)
            app = Application(store, settings=settings, index_mode='KEYWORD_ONLY', job_kinds=kinds)
            for _ in range(300):
                outcome = app.run_once('concept-maintenance:' + frozen['run_id'])
                if outcome is None:
                    break
                outcomes.append(outcome)
                with (run / 'timeline.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'elapsed_seconds': time.perf_counter() - started, **outcome}, ensure_ascii=False) + '\n')
                if outcome.get('error') in {'AuthenticationError', 'PermissionDeniedError'}:
                    return {'status': 'PROVIDER_BLOCKED', 'outcomes': outcomes}
            else:
                raise Conflict('Maintenance did not settle within its program step budget')
    finally:
        call_guard.reset(token)
    result = {'status': 'RECORDED', 'elapsed_seconds': time.perf_counter() - started, 'outcomes': outcomes,
              'model_budget': budget.state}
    write(run / 'apply-results.json', result)
    return result


def report(store, run, frozen):
    baseline = json.loads((run / 'baseline.json').read_text(encoding='utf-8'))['baseline']
    atomic_heads = {n['id']: n['current_revision_id'] for n in baseline['heads'] if n['kind'] == 'ATOMIC'}
    with store.db.engine.connect() as c:
        usage = [dict(r) for r in rows(c, "SELECT kind,operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(elapsed_ms) elapsed_ms FROM provider_calls WHERE rowid>:base GROUP BY kind,operation,model,status", base=frozen['provider_baseline'])]
        atomic_writes = 0
        for row in rows(c, "SELECT n.id,r.ordinal,r.id revision_id FROM notes n JOIN note_revisions r ON r.note_id=n.id WHERE n.kind='ATOMIC'"):
            base = atomic_heads.get(row['id'])
            previous = rows(c, 'SELECT ordinal FROM note_revisions WHERE id=:r', r=base) if base else []
            atomic_writes += int(not previous or row['ordinal'] > previous[0]['ordinal'])
    value = {'usage': usage, 'new_calls': sum(r['calls'] for r in usage), 'new_atomic_writes': atomic_writes}
    write(run / 'usage.json', value)
    return value
