"""Regression cases from real paper assets, source removal and cost overruns."""

import json
from types import SimpleNamespace

import pytest

from noodle_agent.next.assembly_generation import SourceSectionPipeline, apply_source_patch
from noodle_agent.next.attempts import charge
from noodle_agent.next.contracts import canonical
from noodle_agent.next.cost_policy import model_choice
from noodle_agent.next.events import dispatch
from noodle_agent.next.figure_bindings import bind_markdown, catalogue, retain_paper_figures
from noodle_agent.next.metrics import task_clock
from noodle_agent.next.section_contracts import DraftPatch, SourceDraft, bounded_source_draft
from noodle_agent.next.store import Store


def test_paper_figure_and_table_numbers_are_distinct_and_exact_paths_compile():
    blocks = [{'id':key,'page':i+1,'asset_ref':f'assets/{key}.jpg','text':'',
        'metadata':{'mineru':{field:[caption]}}} for i,(key,field,caption) in enumerate([
            ('architecture','image_caption','Figure 1: Model architecture'),
            ('bleu','table_caption','Table 2: Translation quality'),
            ('ablation','table_caption','Table 3: Ablations')])]
    figures = catalogue({'blocks':blocks})
    assert [(f['kind'],f['number']) for f in figures] == [('FIGURE','1'),('TABLE','2'),('TABLE','3')]
    assert bind_markdown('![模型架构](figure:../assets/architecture.jpg)',figures) == '![模型架构](../assets/architecture.jpg)'
    assert bind_markdown('![表2 BLEU](../assets/ablation.jpg)',figures) == '![表2 BLEU](../assets/bleu.jpg)'
    assert bind_markdown('![表2 BLEU](../assets/unknown.jpg)',figures) == '![表2 BLEU](../assets/unknown.jpg)'
    draft = SourceDraft(title='论文',sections=[{'key':'method','title':'方法','markdown':'正文'}],
        assignments={'u1':'method'},omissions={'u2':'无新增内容'})
    diagnostics=[]
    result = retain_paper_figures(draft,figures,[{'unit_id':'u1','block_id':'text','text':'方法'},
        {'unit_id':'u2','block_id':'architecture','text':'Figure 1'}],diagnostics)
    assert result.assignments['u2']=='method' and not result.omissions
    assert result.sections[0].markdown.count('architecture.jpg')==1
    assert 'bleu.jpg' not in result.sections[0].markdown
    assert retain_paper_figures(result,figures,[{'unit_id':'u2','block_id':'architecture','text':'Figure 1'}],[]).sections==result.sections


def test_source_ranges_expand_without_gaps_and_reject_overlap():
    contract = bounded_source_draft(['u1','u2','u3','u4'],ranges=True)
    value = {'title':'方法','sections':[{'key':'s','title':'机制','markdown':'正文'}],
        'source_ranges':[{'start_unit':'u1','end_unit':'u3','section_key':'s','omission_reason':''},
                         {'start_unit':'u4','end_unit':'u4','section_key':'','omission_reason':'版权'}]}
    result=contract.model_validate(value)
    assert result.assignments=={'u1':'s','u2':'s','u3':'s'} and result.omissions=={'u4':'版权'}
    assert 'assignments' not in contract.model_json_schema()['properties']
    assert 'source_ranges' not in result.model_dump()
    value['source_ranges'][1]['start_unit']='u3'
    with pytest.raises(ValueError,match='Overlapping'):
        contract.model_validate(value)
    value['source_ranges'][1]['start_unit']='u4'
    value['source_ranges'][0]['end_unit']='u2'
    with pytest.raises(ValueError,match='gaps'):
        contract.model_validate(value)


@pytest.mark.parametrize('duplicate_top', [False, True])
def test_nested_range_response_compiles_without_dropping_real_evidence(duplicate_top):
    contract=bounded_source_draft(['u1','u2'],ranges=True)
    local={'start_unit':'u1','end_unit':'u2','section_key':None,'omission_reason':None}
    value={'title':'方法','sections':[{'key':'s','title':'机制','markdown':'正文','source_ranges':[local]}]}
    if duplicate_top:
        value['source_ranges']=[{**local,'section_key':'s','omission_reason':''}]
    result=contract.model_validate(value)
    assert result.assignments=={'u1':'s','u2':'s'}
    assert value['sections'][0]['source_ranges']==[local]  # raw receipt stays immutable
    value['sections'][0]['source_ranges']=[{**local,'section_key':'other'}]
    with pytest.raises(ValueError,match='different section'):
        contract.model_validate(value)


def test_null_nested_range_metadata_does_not_trigger_another_writer_call():
    contract=bounded_source_draft(['u1'],ranges=True)
    value={'title':'方法','sections':[{'key':'s','title':'机制','markdown':'正文','source_ranges':None}],
           'source_ranges':[{'start_unit':'u1','end_unit':'u1','section_key':'s','omission_reason':''}]}
    assert contract.model_validate(value).assignments=={'u1':'s'}


def test_paper_registered_table_is_displayed_only_once_across_sections():
    figures=catalogue({'blocks':[{'id':'t3','page':9,'asset_ref':'assets/ablation.jpg','text':'Table 3: Ablations'}]})
    draft=SourceDraft(title='实验',sections=[{'key':key,'title':key,'markdown':'文字\n![表3](../assets/ablation.jpg)'} for key in ['a','b']],
        assignments={'u1':'a','u2':'b'})
    diagnostics=[]
    result=retain_paper_figures(draft,figures,[],diagnostics)
    assert sum(s.markdown.count('ablation.jpg') for s in result.sections)==1
    assert all('文字' in s.markdown for s in result.sections)
    assert any(d['code']=='PAPER_DUPLICATE_FIGURE_REMOVED' for d in diagnostics)
    assert all('ablation.jpg' in s.markdown for s in draft.sections)


def test_range_endpoint_can_include_only_a_proven_navigation_hole():
    contract=bounded_source_draft(['u89','u90','u91','u93'],ranges=True,
        source_order=['prior-topic','u89','u90','u91','u92','u93','next-topic'],navigation_ids=['u92'])
    value={'title':'实验','sections':[{'key':'s','title':'训练','markdown':'正文'}],
        'source_ranges':[{'start_unit':'u89','end_unit':'u92','section_key':'s','omission_reason':''},
                         {'start_unit':'u93','end_unit':'u93','section_key':'s','omission_reason':''}]}
    assert contract.model_validate(value).assignments=={'u89':'s','u90':'s','u91':'s','u93':'s'}
    value['source_ranges'][0]['start_unit']='prior-topic'
    with pytest.raises(ValueError,match='substantive'):
        contract.model_validate(value)
    value['source_ranges'][0]['start_unit']='not-in-source'
    with pytest.raises(ValueError,match='real ordered'):
        contract.model_validate(value)


def test_removed_duplicate_requires_explicit_new_generation_and_keeps_history(tmp_path):
    store=Store(tmp_path/'library')
    try:
        first=store.add_source(b'%PDF-test','slides')
        store.sources.set_source_deleted(first['source_id'],True,expected_revision=first['revision_id'])
        same=store.add_source(b'%PDF-test','slides')
        assert same['duplicate'] and same['deleted']
        new=store.add_source(b'%PDF-test','slides',regenerate_removed=True)
        assert new['regenerated'] and new['revision_id']!=first['revision_id'] and new['source_id']==first['source_id']
        with store.db.engine.connect() as c:
            assert not store.sources.source_valid(c,first['revision_id'])
            assert store.sources.source_valid(c,new['revision_id'])
            assert c.exec_driver_sql('SELECT count(*) FROM source_revisions').scalar_one()==2
        dispatch(store)
        with store.db.engine.connect() as c:
            jobs=c.exec_driver_sql("SELECT input_json FROM jobs WHERE kind='GENERATE_ATOMICS'").scalars().all()
        assert new['revision_id'] in [json.loads(j)['revision_id'] for j in jobs]
    finally:
        store.close()


def test_cost_threshold_is_one_observation_and_never_rejects_valid_work(tmp_path):
    store=Store(tmp_path/'library')
    try:
        store.add_source(b'%PDF-test','slides')
        dispatch(store)
        lease=store.jobs.claim('test',kind='GENERATE_ATOMICS')
        for _ in range(5):
            charge(store,lease,'same-unit','next.section_write',2)
        with store.db.engine.connect() as c:
            assert c.exec_driver_sql("SELECT count(*) FROM build_artifacts WHERE kind='MODEL_ATTEMPT'").scalar_one()==5
            assert c.exec_driver_sql("SELECT count(*) FROM build_artifacts WHERE kind='COST_OBSERVATION'").scalar_one()==1
    finally:
        store.close()


def test_attempt_clock_excludes_restart_gap_and_later_maintenance():
    def e(stage,minute,detail=None):
        return {'stage':stage,'created_at':f'2026-10-27T00:{minute:02d}:00+00:00','detail_json':canonical(detail or {})}
    job={'status':'SUCCEEDED','created_at':'2026-10-27T00:00:00+00:00'}
    events=[e('execution.attempt-start',1),e('execution.attempt-end',3),e('execution.attempt-start',10),
        e('execution.attempt-end',12),e('atomic-index-ready',13,{'scope_key':'document'}),e('maintenance',30)]
    clock=task_clock(job,events,None,[{'created_at':'2026-10-27T00:30:00+00:00'}],{'state':'RUN'},'2026-10-27T01:00:00+00:00')
    assert clock['active_ms']==240000 and clock['notes_ready_wall_ms']==780000
    assert clock['waiting_ms']==540000 and not clock['ticking']


def test_real_draft_compilation_uses_ranges_and_retains_a_missing_paper_architecture():
    figures=catalogue({'blocks':[{'id':'f1','page':1,'asset_ref':'assets/model.jpg','text':'Figure 1: Architecture'}]})
    units=[{'unit_id':'u1','block_id':'text','page':1,'kind':'TEXT','text':'Transformer 的方法。'},
        {'unit_id':'u2','block_id':'f1','page':1,'kind':'IMAGE','text':'Figure 1','asset_ref':'../assets/model.jpg'}]
    raw={'title':'方法','sections':[{'key':'s','title':'机制','markdown':'注意力组成模型。'}],
        'source_ranges':[{'start_unit':'u1','end_unit':'u2','section_key':'s','omission_reason':''}]}
    def call(stage,operation,instruction,inputs,contract,validator):
        assert 'source_ranges' in contract.model_json_schema()['properties']
        return validator(contract.model_validate(raw))
    generation=SimpleNamespace(store=None,profile={'review_policy':'essential-v3','content_policy':'concise-foundation-v1',
        'evidence_encoding':'source-ranges-v1'},writing_policy='中文笔记',call=call)
    pipeline=SourceSectionPipeline(generation);pipeline.document_profile={'kind':'PAPER'}
    record=pipeline.draft(SimpleNamespace(key='p'),['u1','u2'],{'u1':'e1','u2':'e2'},[],[],
        {'units':units,'image_paths':['../assets/model.jpg'],'figures':figures})
    assert record['draft']['assignments']=={'u1':'s','u2':'s'}
    assert 'model.jpg' in record['draft']['sections'][0]['markdown']
    old=SourceDraft.model_validate(record['draft'])
    repaired=apply_source_patch(old,DraftPatch(sections=[{'key':'s','markdown':'一次定点修复。'}]),['u1','u2'],['../assets/model.jpg'],
        figures=figures,units=units,material_kind='PAPER')
    assert 'model.jpg' in repaired.sections[0].markdown
    assert 'model.jpg' in old.sections[0].markdown


def test_unchanged_checked_math_does_not_escalate_a_small_concept_delta(monkeypatch):
    monkeypatch.setenv('NOODLE_NEXT_PRO_USAGE', 'selective')
    proof='证明与推导：$$'+r'\frac{p(x,y)}{p(x)} + '*25+'$$'
    packet={'chapters':[{'accepted_core':{'markdown':proof},'markdown':proof+'\n\n新增适用范围说明。'}]}
    assert model_choice('next.entity_review',packet,True)==(False,'routine-single-review')
    packet['chapters'][0]['markdown']+='\n\n'+proof.replace('p(x,y)','p(z,y)')
    assert model_choice('next.entity_review',packet,True)[0]
