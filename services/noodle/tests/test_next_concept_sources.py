"""Source groups deduplicate Atomic support and preserve explicit history."""

import pytest
from test_next_workspace import atomic, concept, store  # noqa: F401

from noodle_agent.next.concept_sources import sources
from noodle_agent.next.repository import InvalidKnowledge


def test_source_groups_deduplicate_sections_and_keep_historical_provenance(store):  # noqa: F811
    origin,source=atomic(store)
    core=concept(store,origin)
    data=sources(store,core['note_id'])
    assert len(data['groups'])==1
    group=data['groups'][0]
    assert group['source_revision_id']==source['revision_id']
    assert len(group['atomics'])==1 and len(group['atomics'][0]['section_ids'])==2
    store.set_source_deleted(source['source_id'],True)
    with pytest.raises(InvalidKnowledge):
        sources(store,core['note_id'])
    history=sources(store,core['note_id'],core['revision_id'])
    assert not history['groups'][0]['available']
    assert history['groups'][0]['atomics'][0]['revision_id']==origin['revision_id']
