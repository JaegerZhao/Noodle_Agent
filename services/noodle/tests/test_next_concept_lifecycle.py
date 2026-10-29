"""Repeated Atomic removal/restoration must maintain the same publication lineage."""

import pytest
from test_next_entity_concepts import Agent, course, drain, store  # noqa: F401

from noodle_agent.next.eligibility import eligible
from noodle_agent.next.navigation import current_notes
from noodle_agent.next.repository import InvalidKnowledge, rows
from noodle_agent.next.retrieval.contracts import Query
from noodle_agent.next.runtime import Application


def test_repeated_withdraw_restore_updates_concept_and_index_each_time(store):  # noqa: F811
    _, identities = course(store)
    agent = Agent()
    app = Application(store, agent=agent, index_mode='KEYWORD_ONLY')
    drain(app)
    atomic = store.get_note(identities[1])
    revision = atomic['revision']['id']
    pca_id = next(n['id'] for n in current_notes(store, kind='CONCEPT')['items'] if n['title'] == 'PCA')
    initial_core = store.get_note(pca_id)['markdown']
    plans = sum(op == 'next.entity_plan' for op, _ in agent.calls)
    for iteration in range(2):
        assert not store.withdraw_note(atomic['id'], expected_revision=revision)['unchanged']
        assert store.withdraw_note(atomic['id'], expected_revision=revision)['unchanged']
        # Even the still-active previous index must not retrieve withdrawn input.
        assert atomic['id'] not in {h['note_id'] for h in app.searcher.search(Query(query='最大方差方向', retrieval_mode='KEYWORD_ONLY', rerank='OFF'))['hits']}
        drain(app)
        assert '最大方差方向' not in store.get_note(pca_id)['markdown']
        assert not store.restore_note(atomic['id'], expected_revision=revision)['unchanged']
        assert store.restore_note(atomic['id'], expected_revision=revision)['unchanged']
        drain(app)
        assert store.get_note(atomic['id'])['revision']['id'] == revision
        assert '最大方差方向' in store.get_note(pca_id)['markdown']
        with store.db.engine.connect() as c:
            assert len(rows(c, "SELECT id FROM events WHERE event_type='NOTE_WITHDRAWN'")) == iteration + 1
            assert len(rows(c, "SELECT id FROM events WHERE event_type='NOTE_RESTORED'")) == iteration + 1
            assert atomic['id'] in {n['id'] for n in eligible(c)}
    assert sum(op == 'next.entity_plan' for op, _ in agent.calls) == plans
    assert store.get_note(pca_id)['markdown'] == initial_core


def test_restore_refuses_invalid_source_and_rolls_back_visibility(store):  # noqa: F811
    source, identities = course(store)
    note = store.get_note(identities[0])
    store.withdraw_note(note['id'], expected_revision=note['revision']['id'])
    store.set_source_deleted(source['source_id'], True)
    with pytest.raises(InvalidKnowledge, match='cannot restore'):
        store.restore_note(note['id'], expected_revision=note['revision']['id'])
    with store.db.engine.connect() as c:
        assert rows(c, 'SELECT visibility FROM notes WHERE id=:id', id=note['id'])[0]['visibility'] == 'WITHDRAWN'
        assert not rows(c, "SELECT id FROM events WHERE event_type='NOTE_RESTORED'")
