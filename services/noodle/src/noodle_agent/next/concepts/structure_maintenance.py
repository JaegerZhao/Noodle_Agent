"""One model decision per bounded structure packet; deterministic versioned apply."""

import json
from typing import Literal

from pydantic import Field
from sqlalchemy import text

from ..contracts import Candidate, Contract, Link, MapEntry, Review, Scope, Section, canonical, sha
from ..jobs import require_lease
from ..navigation import frozen_candidate
from ..relation_overrides import key
from ..repository import Conflict, now, one, rows


class Change(Contract):
    edge_id: str
    action: Literal['KEEP', 'REMOVE', 'REPLACE']
    relation: Literal['TOPIC', 'USES', 'RELATED_TO', 'COMPARES_WITH', 'VARIANT_OF'] = 'RELATED_TO'
    direction: Literal['OUTGOING', 'INCOMING'] = 'OUTGOING'
    reason: str = Field(min_length=1)


class Changes(Contract):
    changes: list[Change]


class Root(Contract):
    key: Literal['machine-learning', 'deep-learning']
    members: list[str]


class Roots(Contract):
    roots: list[Root]


def request(store, owners, run_id, *, roots=False):
    with store.repository.transaction() as c:
        store.repository.event(c, 'CONCEPT_STRUCTURE_MAINTENANCE_REQUESTED', 'concept-structure', None,
            {'owners': owners, 'run_id': run_id, 'roots': roots}, key='concept-structure:' + run_id)


def maintain(store, lease, calls):
    payload = lease.input['payload']
    return root_membership(store, lease, calls) if payload.get('roots') else revise_relations(store, lease, calls)


def revise_relations(store, lease, calls):
    owners, edges, source_ids = {}, {}, set()
    for identity in lease.input['payload']['owners']:
        try:
            note = store.get_note(identity)
        except ValueError:
            continue
        candidate = frozen_candidate(store, note['revision']['id'])
        owners[identity] = {'note': note, 'candidate': candidate}
        with store.db.engine.connect() as c:
            owned = rows(c, 'SELECT id FROM links WHERE from_revision_id=:r ORDER BY rowid', r=note['revision']['id'])
        for row, link in zip(owned, candidate.links, strict=True):
            if link.origin != 'AUTHOR' and link.navigation_role:
                edges[row['id']] = {'owner': identity, 'link': link}
                source_ids.update(link.support_revision_ids)
    if not edges:
        return {'status': 'NO_STRUCTURE_CHANGES', 'changed': []}
    inputs = []
    from .entity_routing import card_for
    with store.db.engine.connect() as c:
        for revision in sorted(source_ids):
            row = one(c, 'SELECT n.*,r.id revision_id FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE r.id=:r', r=revision)
            if row and store.content.valid_revision(c, revision):
                inputs.append(card_for(store, row))
        targets = {r['id']: {'id': r['id'], 'title': r['title'], 'domain': r['domain']} for r in rows(c, "SELECT id,title,domain FROM notes WHERE kind='CONCEPT'")}
    packet = {'objects': [targets[i] for i in sorted({e['owner'] for e in edges.values()} | {e['link'].target_note_id for e in edges.values()})],
        'atomic_cards': inputs, 'edges': [{'edge_id': eid, 'owner_note_id': e['owner'], **e['link'].model_dump()} for eid, e in edges.items()]}
    def valid(value):
        if len(value.changes) != len(edges) or {v.edge_id for v in value.changes} != set(edges):
            raise ValueError('Return one explained decision for every input edge_id')
    calls.begin_unit(2, key='structure-revision:' + sha(canonical(packet)))
    decision = calls.run('next.concept_maintenance_structure',
        '核对已有知识目录边，不写知识正文。每条edge_id恰好给KEEP、REMOVE或REPLACE及简短原因。'
        'TOPIC仅用于明确主题包含或学习范围中的上下位；应用场景、计算工具、使用方法不能冒充目录父子。'
        '使用关系用USES，方法变体用VARIANT_OF，普通联系用RELATED_TO；不因要减少独立节点而猜关系。'
        'direction按证据owner到target理解：OUTGOING表示owner使用target或owner是上位主题，INCOMING表示反向。'
        '只用已给来源卡片与对象，不将旧basis当作已证明事实；证据不足以改判则KEEP并说明待定。'
        '不能改端点、不能创建新知识，REMOVE仅移除该关系，保留正文与来源。', packet, Changes, check=valid, use_pro_model=False, max_attempts=2)
    changed, skipped = [], []
    by_owner = {}
    for item in decision.changes:
        if item.action != 'KEEP':
            by_owner.setdefault(edges[item.edge_id]['owner'], []).append(item)
    for identity, changes in by_owner.items():
        original = owners[identity]['candidate']
        base = owners[identity]['note']['revision']['id']
        replacement_by_key, operations = {}, []
        for change in changes:
            old = edges[change.edge_id]['link']
            replacement = None if change.action == 'REMOVE' else old.model_copy(update={
                'relation': 'LINKS_TO' if change.relation == 'TOPIC' else change.relation,
                'navigation_role': ('TOPIC_CHILD' if change.direction == 'OUTGOING' else 'TOPIC_PARENT') if change.relation == 'TOPIC' else None,
                'semantic_direction': change.direction if change.relation != 'TOPIC' else None,
                'basis': change.reason, 'origin': 'SYSTEM'})
            digest = key(identity, old)
            replacement_by_key[digest] = replacement
            operations.append({'key': digest, 'edge_id': change.edge_id, 'reason': change.reason,
                               'replacement': replacement.model_dump() if replacement else None})
        links = [replacement_by_key.get(key(identity, link), link) for link in original.links]
        links = list(dict.fromkeys(canonical(l.model_dump()) for l in links if l is not None))
        updated = original.model_copy(update={'links': [Link.model_validate(json.loads(v)) for v in links]})
        with store.db.engine.connect() as c:
            proof = one(c, 'SELECT storage_ref,sha256 FROM build_artifacts WHERE storage_ref=:ref', ref=f'artifacts/builds/{base}.json')
        old_report = json.loads(store.read(proof['storage_ref'], proof['sha256']))['review']['report']
        review = Review(candidate_sha256=updated.fingerprint, policy_version=updated.policy_version,
            reviewer='program.structure_revision_acceptance', passed=True,
            report={**old_report, 'metadata_only': True, 'prior_revision': base,
                    'structure_revision': {'run_id': lease.input['payload']['run_id'], 'operations': operations}})
        try:
            with store.repository.transaction() as c:
                require_lease(c, lease)
                for operation in operations:
                    c.execute(text('INSERT INTO relation_overrides(owner_note_id,relation_key,operation_json,source_revision_id,run_id,created_at) VALUES(:o,:k,:v,:r,:run,:at) ON CONFLICT(owner_note_id,relation_key) DO UPDATE SET operation_json=:v,source_revision_id=:r,run_id=:run,created_at=:at'),
                        {'o': identity, 'k': operation['key'], 'v': canonical(operation), 'r': base, 'run': lease.input['payload']['run_id'], 'at': now()})
                def accepted(connection, revision, identity=identity, base=base):
                    connection.execute(text("UPDATE contribution_decisions SET applied_revision_id=:r WHERE concept_id=:id AND status='ACCEPTED' AND applied_revision_id=:old"), {'r': revision, 'id': identity, 'old': base})
                changed.append(store.publisher.publish(updated, expected_revision=base, review=review, lease=lease, accepted=accepted, _connection=c))
        except (ValueError, Conflict) as exc:
            skipped.append({'owner': identity, 'reason': str(exc)})
    return {'status': 'STRUCTURE_MAINTAINED', 'changed': changed, 'decisions': decision.model_dump(), 'skipped': skipped}


def root_membership(store, lease, calls):
    from ..navigation import current_notes
    notes = current_notes(store, kind='CONCEPT', limit=100000)['items']
    allowed = {n['id'] for n in notes}
    packet = {'roots': {'machine-learning': '机器学习', 'deep-learning': '深度学习'},
              'objects': [{'id': n['id'], 'title': n['title'], 'domain': n['domain'], 'summary': n['summary'][:250]} for n in notes]}
    def valid(value):
        if {r.key for r in value.roots} != set(packet['roots']) or len(value.roots) != 2:
            raise ValueError('Return both requested roots exactly once')
        if any(not set(root.members) <= allowed or len(set(root.members)) != len(root.members) for root in value.roots):
            raise ValueError('Root members must be unique current Concept IDs')
    decision = calls.run('next.concept_maintenance_roots',
        '为用户建立机器学习和深度学习两个阅读体系，只选择现有知识对象ID，不生成正文。'
        '机器学习可包含必要数学基础与学习方法；深度学习聚焦神经网络、训练、表示与相关应用及必要基础。'
        '同一对象可以同时属于两个体系，按适合阅读的顺序排列；不要为了全覆盖把所有统计和线性代数对象都复制到每个体系。',
        packet, Roots, check=valid, use_pro_model=False, max_attempts=2)
    result = []
    for root in decision.roots:
        title = packet['roots'][root.key]
        identity = store.content.create_note('MOC', title, creation_key='knowledge-root:' + root.key, lease=lease)
        with store.db.engine.connect() as c:
            base = one(c, 'SELECT current_revision_id FROM notes WHERE id=:id', id=identity)['current_revision_id']
            ref = f'artifacts/builds/{base}.json'
            proof = one(c, 'SELECT sha256 FROM build_artifacts WHERE storage_ref=:r', r=ref) if base else None
        if proof and json.loads(store.read(ref, proof['sha256']))['review'].get('reviewer') != 'program.root_membership_acceptance':
            result.append({'note_id': identity, 'revision_id': base, 'protected_user_structure': True})
            continue
        from ..knowledge_graph import snapshot_data
        from ..knowledge_tree import forest
        _, nodes, edges = snapshot_data(store)
        layout = forest(root.key, nodes, edges, set(root.members))
        targets = {entry['entry_id']: entry['concept_id'] for entry in layout}
        entries = [MapEntry(entry_id='member-' + entry['concept_id'],
            parent_entry_id='member-' + targets[entry['parent_entry_id']] if entry['parent_entry_id'] else None,
            title=entry['title'], target_note_id=entry['concept_id']) for entry in layout]
        scope = Scope(includes=['知识体系阅读导航'])
        value = Candidate(note_id=identity, title=title, summary=title+'知识体系', scope=scope,
            sections=[Section(section_id='intro', title='阅读入口', question='如何浏览知识体系', scope=scope,
                              markdown='按目录浏览知识点；同一知识点在不同体系中共享正文。')], map_policy='MANUAL', map_entries=entries)
        if base and frozen_candidate(store, base).fingerprint == value.fingerprint:
            result.append({'note_id': identity, 'revision_id': base, 'unchanged': True})
            continue
        result.append(store.publish(value, expected_revision=base, review=Review(candidate_sha256=value.fingerprint,
            policy_version=value.policy_version, reviewer='program.root_membership_acceptance', passed=True,
            report={'root_membership': decision.model_dump(), 'run_id': lease.input['payload']['run_id']}), lease=lease))
    return {'status': 'ROOTS_MAINTAINED', 'roots': result}
