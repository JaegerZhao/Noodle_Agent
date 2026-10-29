"""Real HTTP publication and worker propagation in a disposable knowledge library."""
from fastapi.testclient import TestClient
from test_next_maintenance import Agent, atomic, current_concept, drain

from noodle_agent.next.api import create_app
from noodle_agent.next.retrieval.contracts import Query
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


def test_section_edit_http_fences_revision_then_updates_concept_and_index(tmp_path):
    store = Store(tmp_path/'isolated-agent-edit')
    try:
        app = Application(store, agent=Agent(), index_mode='KEYWORD_ONLY')
        _, identity, original = atomic(store, '梯度笔记', '梯度通过链式法则。')
        drain(app)
        old_concept = current_concept(store)
        note = store.get_note(identity)
        payload = {'expected_revision':original['revision_id'], 'section_id':note['sections'][0]['section_id'],
                   'markdown':'NEW 梯度通过链式法则，并解释局部导数。'}
        with TestClient(create_app(application=app)) as client:
            accepted = client.put('/internal/next/notes/'+identity+'/sections',json=payload)
            assert accepted.status_code == 200, accepted.text
            updated = store.get_note(identity)['revision']['id']
            assert updated != original['revision_id']
            # An identical replay is idempotent; a different stale write conflicts.
            assert client.put('/internal/next/notes/'+identity+'/sections',json=payload).status_code == 200
            assert store.get_note(identity)['revision']['id'] == updated
            assert client.put('/internal/next/notes/'+identity+'/sections',json={**payload,'markdown':'不同请求。'}).status_code == 409
        drain(app)
        concept = current_concept(store)
        assert concept['revision']['id'] != old_concept['revision']['id']
        with store.db.engine.connect() as c:
            inputs = [r[0] for r in c.exec_driver_sql('SELECT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=?',(concept['revision']['id'],))]
        assert updated in inputs and original['revision_id'] not in inputs
        hits = app.searcher.search(Query(query='梯度',retrieval_mode='KEYWORD_ONLY',rerank='OFF'))['hits']
        assert hits and any(h['revision_id']==updated for h in hits)
        assert all(h['revision_id']!=original['revision_id'] for h in hits)
    finally:
        store.close()
