"""Startup manifests preserve eligibility without invoking readiness or models."""

import pytest
from fastapi.testclient import TestClient
from test_next_source_sections import Agent, generation, prepared  # noqa: F401

from noodle_agent.next.api import create_app
from noodle_agent.next.editor import edit_section
from noodle_agent.next.reading_manifest import manifest, note_anchors
from noodle_agent.next.runtime import Application


def test_manifest_is_lightweight_and_invalidates_on_edits_and_withdrawal(request, monkeypatch):
    store, lease, source = request.getfixturevalue('prepared')
    result = generation(store, lease, Agent()).run()
    monkeypatch.setattr('noodle_agent.next.pipeline_state.readiness', lambda *a, **k: pytest.fail('Readiness must not block startup'))
    value = manifest(store, source['revision_id'])
    assert len(value['notes']) == 2 and 'readiness' not in value
    assert manifest(store, source['revision_id'], version_only=True)['version'] == value['version']
    first = result['notes'][0]
    assert note_anchors(store, source['revision_id'], first['revision_id'])
    note = store.get_note(first['note_id'])
    edit_section(store, first['note_id'], expected_revision=first['revision_id'],
        section_id=note['sections'][0]['section_id'], markdown='作者补充这一知识点。')
    assert manifest(store, source['revision_id'])['version'] != value['version']
    assert len(manifest(store, source['revision_id'])['notes']) == 2
    store.sources.set_source_deleted(source['source_id'], True, expected_revision=source['revision_id'])
    with pytest.raises(ValueError):
        manifest(store, source['revision_id'])


def test_reader_api_keeps_anchors_scoped_and_version_endpoint_small(request):
    store, lease, source = request.getfixturevalue('prepared')
    result = generation(store, lease, Agent()).run()
    with TestClient(create_app(application=Application(store, index_mode='KEYWORD_ONLY'))) as client:
        path=f"/internal/next/source-revisions/{source['revision_id']}/reading"
        value=client.get(path,params={'view':'manifest'}).json()
        assert client.get(path,params={'view':'version'}).json() == {'version':value['version']}
        url='/internal/next/notes/'+result['notes'][0]['note_id']
        note=client.get(url,params={'reading_source_revision':source['revision_id']})
        assert note.status_code == 200 and note.json()['source_section_anchors']
        assert client.get(url,params={'reading_source_revision':'unrelated'}).status_code == 409
