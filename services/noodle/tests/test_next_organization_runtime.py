"""Partition budgets, stable graph scheduling and current replacement proof."""

import json

import pytest
from test_next_source_sections import prepared  # noqa: F401
from test_next_workspace import atomic, concept

from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.convergence import convergence_report
from noodle_agent.next.events import dispatch
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.organization import coverage, dependencies, organization_graph, scope_covers
from noodle_agent.next.organization_v2 import review
from noodle_agent.next.store import Store
from noodle_agent.next.task_control import park, request_pause


def enqueue(store, key):
    with store.repository.transaction() as c:
        store.repository.event(c,'ORGANIZATION_REVIEW_REQUESTED','concept-organization',None,
            {'source_revision':key},key='organization-fixture:'+key)
    dispatch(store)


def test_unchanged_topology_does_not_turn_local_coverage_into_global_proof():
    local={'scope':'CHANGED_CONCEPT_NEIGHBORHOOD','concept_ids':['a','b']}
    assert scope_covers(local,['a'])
    assert not scope_covers(local,[])
    assert not scope_covers(local,['c'])
    assert scope_covers({'scope':'ALL_CURRENT_CONCEPTS'},[])


@pytest.mark.parametrize('same_root',[False,True])
def test_each_semantic_packet_has_its_own_durable_budget_and_cache(tmp_path,same_root):
    store=Store(tmp_path/'budget')
    class Agent:
        name='offline-organization'
        count=0
        def structured_json(self,**kwargs):
            self.count+=1
            return {'links':[]}
    try:
        enqueue(store,'source')
        lease=store.jobs.claim('test',kind='REVIEW_ORGANIZATION')
        agent=Agent()
        calls=Calls(store,agent,Checkpoints(store.repository,lease),policy='structured-check-v2',review_policy='essential-v3')
        packets=[]
        nodes={f'c{i}':{} for i in range(3)}
        for i in range(3):
            seed='c0' if same_root else f'c{i}'
            packet={'concepts':[{'id':seed,'revision':'version'}],'roots':[seed],
                'atomic_cards':[{'revision':f'atomic{i}'}]}
            packets.append(packet)
            plan,_=review(store,calls,packet,nodes,{},[seed],set())
            assert plan.links==[]
        assert agent.count==3
        review(store,calls,packets[0],nodes,{},packets[0]['roots'],set())
        assert agent.count==3
        with store.db.engine.connect() as c:
            assert c.exec_driver_sql("SELECT count(DISTINCT input_sha256) FROM build_artifacts WHERE kind='MODEL_ATTEMPT'").scalar_one()==3
    finally:
        store.close()


def test_global_graph_waits_for_unrelated_live_generation_but_not_parked_work(prepared):  # noqa: F811
    store,lease,_=prepared
    other={'input_json':json.dumps({'payload':{'source_revision':'another-source'}})}
    with store.db.engine.connect() as c:
        assert lease.job_id in dependencies(c,other)
    request_pause(store,lease.job_id)
    with store.db.engine.connect() as c:
        assert lease.job_id in dependencies(c,other)
    park(store,lease)
    with store.db.engine.connect() as c:
        assert dependencies(c,other)==[]


def test_global_organization_is_serial_across_workers(tmp_path):
    store=Store(tmp_path/'serial')
    try:
        enqueue(store,'a');enqueue(store,'b')
        first=store.jobs.claim('one',kind='REVIEW_ORGANIZATION')
        assert first and store.jobs.claim('two',kind='REVIEW_ORGANIZATION') is None
        store.jobs.finish(first,{})
        assert store.jobs.claim('two',kind='REVIEW_ORGANIZATION') is not None
    finally:
        store.close()


def test_current_full_graph_receipt_covers_history_without_erasing_failures(tmp_path):
    store=Store(tmp_path/'proof')
    try:
        enqueue(store,'old')
        old=store.jobs.claim('one',kind='REVIEW_ORGANIZATION')
        store.jobs.fail(old,{'code':'OLD_PARTITION_BUDGET'},retry=False)
        enqueue(store,'new')
        new=store.jobs.claim('two',kind='REVIEW_ORGANIZATION')
        store.jobs.finish(new,{'scope':'ALL_CURRENT_CONCEPTS','reviewed_graph_snapshot':organization_graph(store)['snapshot']})
        assert coverage(store)['status']=='COMPLETE'
        result=convergence_report(store)
        assert result['passed'] and result['superseded_jobs'][0]['job_id']==old.job_id
        with store.db.engine.connect() as c:
            assert c.exec_driver_sql('SELECT status FROM jobs WHERE id=?',(old.job_id,)).scalar_one()=='REVIEW'
        published,_=atomic(store)
        concept(store,published)
        assert coverage(store)['status']=='WAITING'
        assert coverage(store)['pending'] == 0
        assert coverage(store)['failed'] == 0
        assert coverage(store)['historical_failed'] == 1
        assert convergence_report(store,require_idle=False)['superseded_jobs']==[]
    finally:
        store.close()


def test_local_completion_is_not_a_full_graph_proof(tmp_path):
    store=Store(tmp_path/'local-proof')
    try:
        enqueue(store,'local')
        lease=store.jobs.claim('one',kind='REVIEW_ORGANIZATION')
        store.jobs.finish(lease,{'scope':'CHANGED_CONCEPT_NEIGHBORHOOD',
            'reviewed_graph_snapshot':organization_graph(store)['snapshot']})
        result=coverage(store)
        assert result['status']!='COMPLETE'
        assert result['local_status']=='COMPLETE' and result['pending']==0
        assert result['local_covering_job_id']==lease.job_id
        published,_=atomic(store)
        concept(store,published)
        assert coverage(store)['local_status']=='WAITING'
    finally:
        store.close()
