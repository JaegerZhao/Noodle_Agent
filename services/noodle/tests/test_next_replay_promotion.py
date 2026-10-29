"""Cold-start ID rebasing preserves prose, old links, CAS and rollback."""

import json

from test_next_concept_transfer import ACCEPTANCE, copy_library
from test_next_workspace import atomic, concept, publish, store  # noqa: F401

from noodle_agent.next.concepts.maintenance_bundle import enqueue, rollback
from noodle_agent.next.concepts.replay_promotion import prepare, remap
from noodle_agent.next.contracts import canonical
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.repository import insert
from noodle_agent.next.runtime import Application


def test_replay_ids_reuse_existing_objects_and_rollback(store,tmp_path):  # noqa: F811
    origin,_=atomic(store)
    old=concept(store,origin)
    with store.repository.transaction() as c:
        insert(c,'concept_identity_claims',domain='机器学习',identity_key='gradient_descent',concept_id=old['note_id'],definition_json=canonical({'title':'梯度下降'}))
    source=copy_library(store,tmp_path/'replay')
    try:
        candidate=frozen_candidate(source,old['revision_id'])
        # A cold build has its own identity and checked revision, never the old ID.
        new=source.content.create_note('CONCEPT','梯度下降',domain='机器学习',creation_key='concept:机器学习:gradient_descent')
        publish(source,candidate.model_copy(update={'note_id':new,'summary':'冷启动验证'}))
        with source.repository.transaction() as c:
            c.exec_driver_sql('DELETE FROM concept_identity_claims')
            c.exec_driver_sql("UPDATE notes SET visibility='WITHDRAWN' WHERE id=?",(old['note_id'],))
            insert(c,'concept_identity_claims',domain='机器学习',identity_key='gradient_descent',concept_id=new,definition_json=json.dumps({'title':'梯度下降'}))
        bundle=prepare(source,store,acceptance=ACCEPTANCE,atomic_scope=[origin['note_id']])
        assert bundle['identity_rebase']['mapping'][new]==old['note_id']
        assert bundle['publications'][0]['proof']['candidate']['note_id']==old['note_id']
        assert bundle['publications'][0]['proof']['candidate']['sections'][0]['markdown']==candidate.sections[0].markdown
        enqueue(store,bundle)
        result=Application(store,index_mode='KEYWORD_ONLY',job_kinds=['APPLY_CONCEPT_MAINTENANCE']).run_once('replay-rebase')
        assert result['status']=='SUCCEEDED',result
        assert store.get_note(old['note_id'])['revision']['id']!=old['revision_id']
        assert rollback(store,bundle,result['result'])['status']=='ROLLED_BACK'
        assert store.get_note(old['note_id'])['revision']['id']==old['revision_id']
    finally:
        source.close()


def test_remap_does_not_rewrite_arbitrary_prose():
    assert remap({'old':'old','body':'old remains in prose'}, {'old':'new'})=={'new':'new','body':'old remains in prose'}
