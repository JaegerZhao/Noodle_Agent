"""Folder metadata and versioned navigation survive a complete recovery."""

import json

from test_next_workspace import atomic, concept, store  # noqa: F401

from noodle_agent.next.backup import recovery_zip, restore_package
from noodle_agent.next.source_library import folder_write, list_sources, update_metadata
from noodle_agent.next.store import Store
from noodle_agent.next.vault import portable_files


def test_folders_display_titles_media_and_graph_survive_restore(store, tmp_path):  # noqa: F811
    origin, source = atomic(store)
    core = concept(store, origin)
    folder = folder_write(store, '机器学习课程')
    update_metadata(store, source['source_id'], {'folder_id':folder['id'],'display_title':'第一课'}, 0)
    exported = portable_files(store)
    workspace = json.loads(exported['.noodle-workspace.json'])
    assert workspace['folders'][0]['name'] == '机器学习课程'
    assert workspace['sources'][0]['display_title'] == '第一课'
    assert any(n['id']==core['note_id'] for n in workspace['graph']['nodes'])
    report = restore_package(recovery_zip(store), tmp_path/'restored')
    assert report['integrity']=='ok'
    restored = Store(tmp_path/'restored')
    try:
        assert list_sources(restored,folder=folder['id'])['items'][0]['display_title']=='第一课'
        assert restored.get_note(core['note_id'])['markdown']==store.get_note(core['note_id'])['markdown']
        assert json.loads(portable_files(restored)['.noodle-workspace.json'])==workspace
    finally:
        restored.close()
