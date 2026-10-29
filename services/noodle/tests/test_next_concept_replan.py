"""Explicit maintenance replaces accepted routing with preserved historical receipts."""

import json

import pytest
from test_next_entity_concepts import Agent, drain, publish_atomic, store  # noqa: F401

from noodle_agent.next.concepts.maintenance import request_replan
from noodle_agent.next.repository import Conflict, one, rows
from noodle_agent.next.runtime import Application


@pytest.mark.parametrize('exclude_old_target',[False,True])
def test_replan_retains_old_contributions_and_atomic_head_and_is_idempotent(store,exclude_old_target):  # noqa: F811
    source = store.add_source('分类模型使用伯努利输出。'.encode(), '分类', kind='AUTHOR')
    identity = publish_atomic(store, source, '分类模型', 'Bernoulli', '分类模型使用伯努利输出。')
    first = Application(store, agent=Agent(), index_mode='KEYWORD_ONLY')
    drain(first)
    atomic = store.get_note(identity)
    revision = atomic['revision']['id']
    with store.db.engine.connect() as c:
        prior = dict(one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision))
        old_decisions = [dict(r) for r in rows(c, 'SELECT * FROM contribution_decisions')]
    context={'kind':'CORRECT_OLD_IDENTITY','excluded_candidate_ids':[prior['concept_id']]} if exclude_old_target else None

    class CorrectingAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            if operation == 'next.entity_plan':
                if exclude_old_target:
                    assert user['content']['candidates']==[]
                    assert user['content']['source_context']['maintenance_goal']==context
                self.calls.append((operation, kwargs))
                return {'assignments': [{'atomic': a['ref'], 'target': 'N0', 'action': 'CREATE',
                    'object_match': 'SAME_OBJECT', 'reason': '分类模型与输出分布是不同知识对象',
                    'outline_revision_reason': '修订旧归属'} for a in user['content']['atomics']],
                    'new_entities': [{'ref': 'N0', 'identity_key': 'classifier', 'title': '分类模型',
                                      'domain': 'ml', 'description': '以条件概率进行分类的模型'}]}
            return super().structured_json(operation, user, **kwargs)

    request_replan(store, [revision], 'fixture-replan',resolution_context=context)
    agent = CorrectingAgent()
    drain(Application(store, agent=agent, index_mode='KEYWORD_ONLY'))
    assert store.get_note(identity)['revision']['id'] == revision
    assert store.get_note(identity)['markdown'] == atomic['markdown']
    with store.db.engine.connect() as c:
        current = one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision)
        assert current['concept_id'] != prior['concept_id']
        assert json.loads(current['route_json'])['maintenance_run_id'] == 'fixture-replan'
        for decision in old_decisions:
            saved = dict(one(c, 'SELECT * FROM contribution_decisions WHERE id=:id', id=decision['id']))
            assert saved == {**decision, 'status': 'SKIPPED'}
        assert one(c, 'SELECT status FROM note_revisions WHERE id=(SELECT current_revision_id FROM notes WHERE id=:id)', id=prior['concept_id'])['status'] == 'WITHDRAWN'
    assert request_replan(store, [revision], 'fixture-replan',resolution_context=context)['reused']
    with pytest.raises(Conflict,match='intent changed'):
        request_replan(store,[revision],'fixture-replan',resolution_context={'reason':'different intent'})
    with pytest.raises(Conflict,match='Only current targets'):
        request_replan(store,[revision],'bad-exclusion',resolution_context={'excluded_candidate_ids':['unrelated']})
    assert sum(op == 'next.entity_plan' for op, _ in agent.calls) == 1
