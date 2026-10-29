"""Coverage is transferable only when the reviewed logical graph is unchanged."""

import pytest
from test_next_concept_transfer import copy_library
from test_next_workspace import atomic, concept, store  # noqa: F401

from noodle_agent.next.concepts.replay_promotion import transfer_structure_coverage
from noodle_agent.next.organization import coverage, organization_graph
from noodle_agent.next.repository import Conflict, one


def test_transferred_current_graph_receipt_and_mismatch_rejection(store,tmp_path):  # noqa: F811
    origin,_=atomic(store)
    concept(store,origin)
    with store.repository.transaction() as c:
        store.repository.event(c,'ORGANIZATION_REVIEW_REQUESTED','concept-organization',None,{},key='single-root-proof')
        event=one(c,"SELECT id FROM events WHERE dedupe_key='single-root-proof'")['id']
    store.jobs.enqueue(event,'REVIEW_ORGANIZATION',{'entity_id':'concept-organization','event_id':event,'revision_id':None,'payload':{}})
    lease=store.jobs.claim('fixture',kind='REVIEW_ORGANIZATION')
    store.jobs.finish(lease,{'scope':'ALL_CURRENT_CONCEPTS','policy':'organization-essential-v1',
        'reviewed_graph_snapshot':organization_graph(store)['snapshot']})
    target=copy_library(store,tmp_path/'target')
    try:
        with target.repository.transaction() as c:
            c.exec_driver_sql("UPDATE jobs SET result_json='{}' WHERE kind='REVIEW_ORGANIZATION'")
        value=transfer_structure_coverage(store,target,{'bundle_sha256':'fixture','identity_rebase':{'mapping':{}}})
        assert value['status']=='COMPLETE' and coverage(target)['status']=='COMPLETE'
        concept(target,origin,title='另一个独立知识对象')
        with pytest.raises(Conflict,match='differs'):
            transfer_structure_coverage(store,target,{'bundle_sha256':'fixture-new','identity_rebase':{'mapping':{}}})
    finally:
        target.close()
