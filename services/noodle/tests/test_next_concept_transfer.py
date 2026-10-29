"""Checked artifacts transfer without a model and rollback cannot overwrite edits."""

import shutil
import sqlite3

import pytest
from test_next_workspace import atomic, concept, publish, store  # noqa: F401

from noodle_agent.next.concepts.maintenance_bundle import enqueue, prepare, rollback
from noodle_agent.next.contracts import Candidate, MapEntry, Scope, Section
from noodle_agent.next.editor import edit_section
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.repository import Conflict, one
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store

ACCEPTANCE = {'engineering_passed': True, 'semantic_passed': True, 'lifecycle_passed': True,
              'scope': 'EXPLICIT_OFFLINE_FIXTURE'}


def copy_library(source, target):
    shutil.copytree(source.root, target, ignore=shutil.ignore_patterns('noodle.db*'))
    with sqlite3.connect(source.root / 'noodle.db') as original, sqlite3.connect(target / 'noodle.db') as copied:
        original.backup(copied)
    return Store(target)


def generated_revision(library, initial, markdown):
    candidate = frozen_candidate(library, initial['revision_id'])
    sections = [candidate.sections[0].model_copy(update={'markdown': markdown}), *candidate.sections[1:]]
    return publish(library, candidate.model_copy(update={'sections': sections}), initial['revision_id'])


@pytest.mark.parametrize('later_edit', [False, True])
def test_checked_transfer_normal_publication_identity_and_fenced_rollback(store, tmp_path, later_edit):  # noqa: F811
    origin, _ = atomic(store)
    initial = concept(store, origin)
    source = copy_library(store, tmp_path / 'candidate')
    try:
        original_atomic = store.get_note(origin['note_id'])
        generated_revision(source, initial, '梯度是方向导数的线性表示。\n\n### 复习框架')
        candidate = frozen_candidate(source, source.get_note(initial['note_id'])['revision']['id'])
        new_id = source.create_note('CONCEPT', '新的阅读对象', domain='机器学习')
        added = candidate.model_copy(update={'note_id': new_id, 'title': '新的阅读对象'})
        publish(source, added)
        root_id = source.content.create_note('MOC', '测试体系', creation_key='knowledge-root:fixture')
        scope = Scope(includes=['导航'])
        publish(source, Candidate(note_id=root_id, title='测试体系', scope=scope,
            sections=[Section(section_id='intro', title='阅读入口', question='阅读', scope=scope, markdown='按目录阅读。')],
            map_entries=[MapEntry(entry_id='new-concept', title='新的阅读对象', target_note_id=new_id)]))
        bundle = prepare(source, store, acceptance=ACCEPTANCE)
        assert len(bundle['publications']) == 3
        job = enqueue(store, bundle)
        app = Application(store, index_mode='KEYWORD_ONLY', job_kinds=('APPLY_CONCEPT_MAINTENANCE',))
        result = app.run_once('transfer')
        assert result['status'] == 'SUCCEEDED', result
        receipt = result['result']
        assert receipt['model_calls'] == 0
        assert frozen_candidate(store, store.get_note(new_id)['revision']['id']).fingerprint == added.fingerprint
        assert frozen_candidate(store, store.get_note(initial['note_id'])['revision']['id']).fingerprint == candidate.fingerprint
        assert store.get_note(root_id)['map_entries'][0]['target_note_id'] == new_id
        assert store.get_note(origin['note_id'])['markdown'] == original_atomic['markdown']
        assert enqueue(store, bundle) == job and app.run_once('repeat') is None
        if later_edit:
            current = store.get_note(initial['note_id'])
            edit_section(store, initial['note_id'], expected_revision=current['revision']['id'],
                         section_id='s1', markdown='作者后续修改，禁止回滚覆盖。')
            with pytest.raises(Conflict, match='Post-transfer edit|Atomic input changes'):
                rollback(store, bundle, receipt)
            assert '作者后续修改' in store.get_note(initial['note_id'])['markdown']
        else:
            assert rollback(store, bundle, receipt)['status'] == 'ROLLED_BACK'
            assert store.get_note(initial['note_id'])['revision']['id'] == initial['revision_id']
            with store.db.engine.connect() as c:
                assert one(c, 'SELECT visibility FROM notes WHERE id=:id', id=new_id)['visibility'] == 'WITHDRAWN'
                assert c.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
            assert rollback(store, bundle, receipt)['status'] == 'ALREADY_ROLLED_BACK'
    finally:
        source.close()


def test_changed_affected_head_is_rejected_before_any_transfer_write(store, tmp_path):  # noqa: F811
    origin, _ = atomic(store)
    initial = concept(store, origin)
    source = copy_library(store, tmp_path / 'candidate')
    try:
        generated_revision(source, initial, '梯度是方向导数的线性表示。\n\n### 新的摘要')
        bundle = prepare(source, store, acceptance=ACCEPTANCE)
        edit_section(store, initial['note_id'], expected_revision=initial['revision_id'], section_id='s0', markdown='用户新编辑。')
        enqueue(store, bundle)
        result = Application(store, index_mode='KEYWORD_ONLY', job_kinds=('APPLY_CONCEPT_MAINTENANCE',)).run_once('cas')
        assert result['status'] != 'SUCCEEDED' and result['error'] == 'Conflict'
        assert '用户新编辑' in store.get_note(initial['note_id'])['markdown']
    finally:
        source.close()


def test_reroute_claims_inputs_and_retirement_transfer_and_rollback_together(store, tmp_path):  # noqa: F811
    from test_next_entity_concepts import Agent, drain, publish_atomic

    from noodle_agent.next.concepts.maintenance import request_replan
    from noodle_agent.next.repository import rows

    original_source = store.add_source('分类模型使用伯努利输出。'.encode(), '分类', kind='AUTHOR')
    identity = publish_atomic(store, original_source, '分类模型', 'Bernoulli', '分类模型使用伯努利输出。')
    drain(Application(store, agent=Agent(), index_mode='KEYWORD_ONLY'))
    revision = store.get_note(identity)['revision']['id']
    with store.db.engine.connect() as c:
        old_route = dict(one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision))
    source = copy_library(store, tmp_path / 'candidate')

    class CorrectingAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            if operation == 'next.entity_plan':
                return {'assignments': [{'atomic': a['ref'], 'target': 'N0', 'action': 'CREATE',
                    'object_match': 'SAME_OBJECT', 'outline_action': 'REVISE', 'reason': '模型与分布是不同对象'}
                    for a in user['content']['atomics']],
                    'new_entities': [{'ref': 'N0', 'identity_key': 'classifier', 'title': '分类模型',
                        'domain': 'ml', 'description': '条件概率分类模型'}]}
            return super().structured_json(operation, user, **kwargs)

    try:
        request_replan(source, [revision], 'fixture-transfer-reroute')
        drain(Application(source, agent=CorrectingAgent(), index_mode='KEYWORD_ONLY'))
        bundle = prepare(source, store, acceptance=ACCEPTANCE)
        assert len(bundle['plans']) == 1 and len(bundle['routes']) == 1
        assert bundle['retired'] == [old_route['concept_id']]
        enqueue(store, bundle)
        result = Application(store, index_mode='KEYWORD_ONLY', job_kinds=('APPLY_CONCEPT_MAINTENANCE',)).run_once('transfer')
        assert result['status'] == 'SUCCEEDED', result
        with store.db.engine.connect() as c:
            current = dict(one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision))
            assert current['concept_id'] != old_route['concept_id']
            assert c.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
            assert len(rows(c, "SELECT * FROM contribution_decisions WHERE status='ACCEPTED'")) == 1
        assert store.get_note(identity)['revision']['id'] == revision
        assert rollback(store, bundle, result['result'])['status'] == 'ROLLED_BACK'
        with store.db.engine.connect() as c:
            assert dict(one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision)) == old_route
            assert c.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    finally:
        source.close()
