"""Projection batching and reversible reclamation, without provider requests."""

from pathlib import Path

import pytest
from test_next_rag_effectiveness import note

from noodle_agent.next.backup import restore_package
from noodle_agent.next.projection_batch import run_batch
from noodle_agent.next.repository import Conflict
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.retrieval.retention import apply, plan
from noodle_agent.next.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'library')
    yield value
    value.close()


def enqueue(store, key):
    with store.repository.transaction() as c:
        store.repository.event(c, 'TEST', key, None, {}, key=key)
        event = c.exec_driver_sql('SELECT id FROM events WHERE dedupe_key=?', (key,)).scalar_one()
    return store.jobs.enqueue(event, 'UPDATE_MAPS', {})


def test_batch_never_acknowledges_events_arriving_after_projection_started(store):
    first = enqueue(store, 'first')
    second = enqueue(store, 'second')
    lease = store.jobs.claim('test', kind='UPDATE_MAPS')
    extra = second if lease.job_id == first else first
    late = []
    def operation():
        late.append(enqueue(store, 'late'))
        return {'status': 'READY'}
    assert run_batch(store, lease, 'UPDATE_MAPS', operation)['coalesced_jobs'] == 1
    with store.db.engine.connect() as c:
        statuses = dict(c.exec_driver_sql('SELECT id,status FROM jobs').all())
    assert statuses[extra] == 'SUCCEEDED' and statuses[late[0]] == 'PENDING'


def test_batch_failure_releases_other_claims_for_retry(store):
    enqueue(store, 'first')
    enqueue(store, 'second')
    lease = store.jobs.claim('test', kind='UPDATE_MAPS')
    with pytest.raises(ValueError):
        run_batch(store, lease, 'UPDATE_MAPS', lambda: (_ for _ in ()).throw(ValueError('failure')))
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM jobs WHERE status='RETRY'").scalar_one() == 1


def generations(store):
    result = []
    for i in range(4):
        note(store, f'Topic{i}', f'Knowledge evidence {i}.')
        result.append(Indexer(store).build('KEYWORD_ONLY')['generation'])
    with store.repository.transaction() as c:
        for i, identity in enumerate(result):
            c.exec_driver_sql('UPDATE index_generations SET created_at=? WHERE id=?',
                             (f'2000-01-0{i+1}T00:00:00+00:00', identity))
    return result


def test_retention_keeps_head_and_pins_and_recovers_all_content(store, tmp_path):
    ids = generations(store)
    preview = plan(store, keep=1, protected=[ids[0]])
    assert preview['candidate_count'] == 2
    assert {ids[0], ids[-1]} <= {v['generation'] for v in preview['retained']}
    with store.db.engine.connect() as c:
        old_fts = c.exec_driver_sql('SELECT count(*) FROM knowledge_fts').scalar_one()
    result = apply(store, preview, keep=1, protected=[ids[0]])
    assert len(result['removed']) == 2
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql('SELECT count(*) FROM knowledge_fts').scalar_one() < old_fts
        assert c.exec_driver_sql('SELECT count(*) FROM note_revisions').scalar_one() == 4
    destination = tmp_path / 'restored'
    restore_package(Path(result['recovery_package']).read_bytes(), destination)
    restored = Store(destination)
    try:
        with restored.db.engine.connect() as c:
            assert c.exec_driver_sql('SELECT count(*) FROM index_generations').scalar_one() == 4
            assert c.exec_driver_sql('SELECT count(*) FROM knowledge_fts').scalar_one() == old_fts
            assert c.exec_driver_sql('SELECT generation FROM index_heads').scalar_one() == ids[-1]
    finally:
        restored.close()


def test_retention_rejects_changed_preview_and_active_workers(store):
    generations(store)
    preview = plan(store, keep=1)
    with pytest.raises(Conflict, match='inputs changed'):
        apply(store, {**preview, 'heads': []}, keep=1)
    enqueue(store, 'running')
    store.jobs.claim('worker', kind='UPDATE_MAPS')
    with pytest.raises(Conflict, match='Pause active'):
        apply(store, preview, keep=1)
