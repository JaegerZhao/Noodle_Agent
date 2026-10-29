"""User organization must not rewrite knowledge; lifecycle batches are durable."""

import pytest
from fastapi.testclient import TestClient
from test_next_workspace import atomic, store  # noqa: F401

from noodle_agent.next.api import create_app
from noodle_agent.next.repository import Conflict, identifier
from noodle_agent.next.runtime import Application
from noodle_agent.next.source_library import (
    batch,
    folder_delete,
    folder_write,
    list_sources,
    update_metadata,
)


def test_metadata_folders_natural_sort_and_no_generation(store):  # noqa: F811
    note, source = atomic(store, '第10份课件')
    atomic(store, '第2份课件', parts=('第二份资料。',))
    with store.db.engine.connect() as c:
        epoch = c.exec_driver_sql('SELECT validity_epoch FROM storage_contract').scalar_one()
        events = c.exec_driver_sql('SELECT count(*) FROM events').scalar_one()
    folder = folder_write(store, '机器学习')
    moved = update_metadata(store, source['source_id'], {'folder_id': folder['id'], 'display_title': '第10份课件（新名称）'}, 0)
    assert moved['metadata_version'] == 1
    assert list_sources(store, folder=folder['id'])['total'] == 1
    assert list_sources(store, sort='title_asc')['items'][0]['title'] == '第2份课件'
    assert store.get_note(note['note_id'])['revision']['id'] == note['revision_id']
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql('SELECT validity_epoch FROM storage_contract').scalar_one() == epoch
        assert c.exec_driver_sql('SELECT count(*) FROM events').scalar_one() == events
    folder_delete(store, folder['id'], 1)
    assert list_sources(store, folder='unfiled')['total'] == 2
    with pytest.raises(Conflict):
        update_metadata(store, source['source_id'], {'display_title': '旧界面覆盖'}, 1)


def test_batch_conflict_idempotency_and_single_lifecycle_event(store):  # noqa: F811
    a, first = atomic(store)
    _, second = atomic(store, '第二份', parts=('第二份资料。',))
    operation = identifier()
    items = [{'source_id': first['source_id'], 'expected_revision': first['revision_id']},
             {'source_id': second['source_id'], 'expected_revision': identifier()}]
    result = batch(store, operation, 'delete', items)
    assert result['succeeded'] == 1
    with store.db.engine.connect() as c:
        assert not store.content.valid_revision(c, a['revision_id'])
    assert batch(store, operation, 'delete', items) == result
    with pytest.raises(Conflict):
        batch(store, operation, 'restore', items)
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM events WHERE event_type='SOURCES_STATE_CHANGED'").scalar_one() == 1
    restored = batch(store, identifier(), 'restore', [items[0]])
    assert restored['succeeded'] == 1
    with store.db.engine.connect() as c:
        assert store.content.valid_revision(c, a['revision_id'])


def test_folder_api_compatibility_and_partial_metadata(store):  # noqa: F811
    _, source = atomic(store)
    with TestClient(create_app(application=Application(store))) as client:
        assert isinstance(client.get('/internal/next/sources').json(), list)
        folder = client.post('/internal/next/source-folders', json={'name': '课件'}).json()
        assert client.post('/internal/next/source-folders', json={'name': '课件'}).status_code == 409
        path = '/internal/next/sources/' + source['source_id'] + '/metadata'
        assert client.patch(path, json={'expected_metadata_version': 0, 'folder_id': folder['id']}).status_code == 200
        assert client.patch(path, json={'expected_metadata_version': 1, 'display_title': '新名称'}).status_code == 200
        item = client.get('/internal/next/sources?limit=10').json()['items'][0]
        assert item['folder_id'] == folder['id'] and item['display_title'] == '新名称'
        assert client.get('/internal/next/source-statuses').status_code == 200


def test_reimport_date_and_sort_follow_current_revision_without_overwriting_original_time(store):  # noqa: F811
    import fitz
    with fitz.open() as pdf:
        pdf.new_page()
        content = pdf.tobytes()
    source = store.add_source(content, 'Reimport.pdf')
    with store.repository.transaction() as c:
        c.exec_driver_sql("UPDATE sources SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (source['source_id'],))
    atomic(store, 'Another source')
    store.set_source_deleted(source['source_id'], True)
    regenerated = store.add_source(content, 'Reimport.pdf', regenerate_removed=True)
    items = list_sources(store, sort='created_desc')['items']
    assert items[0]['id'] == source['source_id']
    assert items[0]['created_at'] == '2020-01-01T00:00:00+00:00'
    with store.db.engine.connect() as c:
        stamp = c.exec_driver_sql('SELECT created_at FROM source_revisions WHERE id=?', (regenerated['revision_id'],)).scalar_one()
    assert items[0]['imported_at'] == stamp


def test_waiting_upload_write_does_not_block_health_requests(store, monkeypatch):  # noqa: F811
    import asyncio
    import threading

    import fitz
    import httpx

    entered, release = threading.Event(), threading.Event()
    def slow_write(*args, **kwargs):
        entered.set()
        if not release.wait(4):
            raise TimeoutError('Fixture upload did not run in a background thread')
        return {'source_id': 'fixture', 'revision_id': 'fixture-revision'}
    monkeypatch.setattr(store, 'add_source', slow_write)
    with fitz.open() as pdf:
        pdf.new_page()
        content = pdf.tobytes()

    async def exercise():
        app = create_app(application=Application(store))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            upload = asyncio.create_task(client.post('/internal/next/sources', files={'file': ('fixture.pdf', content, 'application/pdf')}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert not upload.done()
                response = await asyncio.wait_for(client.get('/health'), timeout=1)
                assert response.status_code == 200
                assert not upload.done()
            finally:
                release.set()
                response = await upload
            assert response.status_code == 200
    asyncio.run(exercise())


def test_removed_file_confirmation_probe_does_not_wait_for_a_background_writer(store):  # noqa: F811
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    import fitz

    with fitz.open() as pdf:
        pdf.new_page()
        content = pdf.tobytes()
    original = store.add_source(content, 'Source.pdf')
    store.set_source_deleted(original['source_id'], True)
    with sqlite3.connect(store.root / 'noodle.db') as writer, ThreadPoolExecutor(max_workers=1) as pool:
        writer.execute('BEGIN IMMEDIATE')
        future = pool.submit(store.add_source, content, 'Source.pdf')
        try:
            result = future.result(timeout=1)
        finally:
            writer.rollback()
        assert result['duplicate'] and result['deleted']
    with store.db.engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM source_revisions').scalar_one() == 1


def test_source_reconcile_only_schedules_related_concepts_even_after_source_deletion(store, monkeypatch):  # noqa: F811
    from test_next_workspace import concept

    from noodle_agent.next.concepts import coordinator
    from noodle_agent.next.events import dispatch
    from noodle_agent.next.repository import insert

    a, source = atomic(store, '第一份', parts=('第一份来源定义。',))
    b, _ = atomic(store, '无关来源', parts=('另一份来源定义。',))
    first = concept(store, a, title='相关知识')
    unrelated = concept(store, b, title='无关知识')
    with store.repository.transaction() as connection:
        for note in [first, unrelated]:
            insert(connection, 'concept_work_state', concept_id=note['note_id'], desired_generation=0,
                desired_input_hash='fixture', status='READY')
    store.set_source_deleted(source['source_id'], True)
    dispatch(store)
    with store.db.engine.connect() as connection:
        cause=connection.exec_driver_sql("SELECT id FROM jobs WHERE kind='RECONCILE_DEPENDENCIES' AND json_extract(input_json,'$.entity_id')=? ORDER BY created_at DESC LIMIT 1", (source['source_id'],)).scalar_one()
        assert coordinator.source_affected_concepts(connection, cause)=={first['note_id']}
    scheduled=[]
    monkeypatch.setattr(coordinator, 'schedule', lambda store,connection,identity,cause:scheduled.append(identity))
    assert coordinator.reconcile(store,cause)=={'reconciled':1,'scope':'SOURCE'}
    assert scheduled==[first['note_id']]
    scheduled.clear()
    assert coordinator.reconcile(store)['scope']=='LIBRARY'
    assert set(scheduled)=={first['note_id'],unrelated['note_id']}
