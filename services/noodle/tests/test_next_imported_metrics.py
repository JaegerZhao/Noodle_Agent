"""Historical display snapshots never become fresh provider consumption."""

from test_next_workspace import store  # noqa: F401

from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.imported_metrics import overlay
from noodle_agent.next.repository import identifier, insert, now


def test_original_clock_overlay_keeps_current_usage_and_receipt_provenance_separate(store):  # noqa: F811
    ref='artifacts/imported-generation-metrics/original.json'
    original={'clock':{'active_ms':216475,'clock_basis':'RECORDED_EXECUTION_ATTEMPTS'},
        'model_usage':[{'kind':'LLM_CALL','total_tokens':252593}],
        'stages':[{'stage':'parse','wall_ms':52000,'total_tokens':0}]}
    data=canonical(original)
    store.put(ref,data.encode())
    with store.repository.transaction() as c:
        insert(c,'build_artifacts',id=identifier(),job_id=None,kind='IMPORTED_GENERATION_METRICS',
            storage_ref=ref,sha256=sha(data),policy_version=store.repository.policy_version,input_sha256='fixture',created_at=now())
    value={'job':{'id':'original'},'summary':{'active_ms':None,'calls':3,'total_tokens':100,'model_usage':[]},
        'stages':[{'stage':'parse','status':'WAITING'},{'stage':'concept','total_tokens':100}]}
    result=overlay(store,value)
    assert result['summary']['active_ms']==216475 and not result['summary']['ticking']
    assert result['summary']['total_tokens']==100 and result['summary']['calls']==3
    assert result['summary']['original_model_usage']==original['model_usage']
    assert result['stages'][0]['wall_ms']==52000 and result['stages'][1]['total_tokens']==100
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one()==0
    fresh={'job':{'id':'new-generation'},'summary':{'active_ms':1200,'ticking':True},'stages':[]}
    assert overlay(store,fresh)['summary']=={'active_ms':1200,'ticking':True}
