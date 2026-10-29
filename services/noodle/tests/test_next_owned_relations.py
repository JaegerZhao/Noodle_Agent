"""Author edits must preserve system-owned direction, evidence and publication proof."""

import json

import pytest
from fastapi.testclient import TestClient
from test_next_workspace import atomic, concept, publish, store  # noqa: F401

from noodle_agent.next.api import create_app
from noodle_agent.next.contracts import Link
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.relations import CONTRACT, owned_links, update_author_links
from noodle_agent.next.repository import Conflict, InvalidKnowledge
from noodle_agent.next.runtime import Application


def setup_relations(store):  # noqa: F811
    a, _ = atomic(store)
    parent = concept(store, a, '上位问题')
    child = concept(store, a, '具体方法')
    candidate = frozen_candidate(store, child['revision_id'])
    system = Link(target_note_id=parent['note_id'], relation='LINKS_TO', status='CONFIRMED',
                  basis='来源明确支持父子关系', navigation_role='TOPIC_PARENT', position=3,
                  support_revision_ids=[a['revision_id']])
    result = publish(store, candidate.model_copy(update={'links': [system]}), child['revision_id'])
    return a, parent, result, system


def test_empty_author_roundtrip_preserves_parent_edge_and_head(store):  # noqa: F811
    _, _, child, system = setup_relations(store)
    packet = owned_links(store, child['note_id'])
    assert packet['links'][0]['owner_note_id'] == child['note_id']
    assert not packet['links'][0]['editable']
    assert packet['links'][0]['value']['navigation_role'] == 'TOPIC_PARENT'
    result = update_author_links(store, child['note_id'], child['revision_id'], [], editor_contract=CONTRACT)
    assert result['unchanged'] and result['revision_id'] == child['revision_id']
    assert frozen_candidate(store, result['revision_id']).links == [system]


def test_author_edit_and_remove_preserve_system_edge_and_checked_sections(store):  # noqa: F811
    atomic_note, _, child, system = setup_relations(store)
    previous = frozen_candidate(store, child['revision_id'])
    link = Link(target_note_id=atomic_note['note_id'], target_section_id='s0',
                from_section_id='s0', relation='LINKS_TO', status='CONFIRMED', basis='补充阅读')
    result = update_author_links(store, child['note_id'], child['revision_id'], [link], editor_contract=CONTRACT)
    updated = frozen_candidate(store, result['revision_id'])
    assert updated.sections == previous.sections
    assert updated.links[0] == system and updated.links[1].origin == 'AUTHOR'
    assert updated.links[1].target_section_id == 's0'
    proof = json.loads(store.path(f"artifacts/builds/{result['revision_id']}.json").read_text(encoding='utf-8'))
    assert proof['review']['report']['metadata_only']
    assert proof['review']['report']['prior_revision'] == child['revision_id']
    again = update_author_links(store, child['note_id'], result['revision_id'], [link], editor_contract=CONTRACT)
    assert again['unchanged']
    removed = update_author_links(store, child['note_id'], result['revision_id'], [], editor_contract=CONTRACT)
    assert frozen_candidate(store, removed['revision_id']).links == [system]
    assert store.get_note(child['note_id'])['eligible']


def test_stale_or_display_graph_payload_cannot_replace_relations(store):  # noqa: F811
    a, _, child, _ = setup_relations(store)
    link = Link(target_note_id=a['note_id'], relation='RELATED_TO', status='CONFIRMED', basis='阅读关联')
    update_author_links(store, child['note_id'], child['revision_id'], [link], editor_contract=CONTRACT)
    with pytest.raises(Conflict, match='revision changed'):
        update_author_links(store, child['note_id'], child['revision_id'], [], editor_contract=CONTRACT)
    with pytest.raises(Conflict, match='Reload'):
        update_author_links(store, child['note_id'], child['revision_id'], [], editor_contract=None)


def test_client_cannot_forge_system_support(store):  # noqa: F811
    _, _, child, system = setup_relations(store)
    with pytest.raises(InvalidKnowledge, match='read-only'):
        update_author_links(store, child['note_id'], child['revision_id'], [system], editor_contract=CONTRACT)


def test_api_roundtrip_uses_owner_not_graph_display_direction(store):  # noqa: F811
    _, _, child, system = setup_relations(store)
    app = Application(store, index_mode='KEYWORD_ONLY')
    with TestClient(create_app(application=app)) as client:
        url = '/internal/next/notes/' + child['note_id'] + '/links'
        packet = client.get(url).json()
        response = client.put(url, json={'expected_revision': packet['expected_revision'],
            'editor_contract': packet['editor_contract'], 'links': [e['value'] for e in packet['links'] if e['editable']]})
        assert response.status_code == 200 and response.json()['unchanged']
        assert client.put(url, json={'expected_revision': packet['expected_revision'], 'links': []}).status_code == 409
    assert frozen_candidate(store, child['revision_id']).links == [system]


def test_rebuild_preserves_distinct_author_sections_and_shared_system_endpoint():
    from noodle_agent.next.concepts.builder import retain_links
    generated = Link(target_note_id='target', relation='LINKS_TO', status='CONFIRMED', basis='系统支持', support_revision_ids=['r'])
    first = generated.model_copy(update={'origin': 'AUTHOR', 'support_revision_ids': [], 'target_section_id': 's1'})
    second = first.model_copy(update={'target_section_id': 's2'})
    assert retain_links([generated], [first, second], {'r'}) == [generated, first, second]
