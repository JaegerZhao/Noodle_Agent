"""Cost routing and evidence reuse without a provider connection."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import fitz
import pytest
from test_next_workflow_readiness import corpus  # noqa: F401

from noodle_agent.next.cost_policy import COMPACT_REVIEW, complex_reasoning, model_choice
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.structured_quality import AssemblyAssessment
from noodle_agent.next.visual_context import native_page_complete, page_contexts


@pytest.fixture(autouse=True)
def selective_policy_for_legacy_routing_tests(monkeypatch):
    monkeypatch.setenv('NOODLE_NEXT_PRO_USAGE', 'selective')


@pytest.mark.parametrize('operation', ['next.structure', 'next.structure_exclusion_review',
    'next.plan_labels', 'next.assembly_review', 'next.section_repair', 'next.local_finding_review',
    'next.entity_plan', 'next.entity_review', 'next.entity_repair', 'next.answer', 'next.answer_review'])
def test_default_flash_policy_cannot_silently_escalate(operation, monkeypatch):
    monkeypatch.setenv('NOODLE_NEXT_PRO_USAGE', 'disabled')
    monkeypatch.setenv('NOODLE_NEXT_COST_POLICY', 'legacy')
    assert model_choice(operation, {'prior_findings':['located math dispute']}, True) == (False, 'flash-only-policy')


def test_production_default_uses_pro_for_planning_and_identity_only(monkeypatch):
    monkeypatch.delenv('NOODLE_NEXT_PRO_USAGE')
    for kind in ['PAPER','BOOK','SLIDES','ARTICLE','BLOG','UNKNOWN']:
        for op in ['next.structure','next.structure_exclusion_review','next.plan_labels','next.entity_plan','next.concept_decide']:
            assert model_choice(op, {'material_profile':{'kind':kind}}, False)==(True,'planning-and-identity-pro')
    for op in ['next.assembly_review','next.section_write','next.structure_patch',
               'next.section_repair','next.entity_write','next.entity_plan_review','next.entity_review',
               'next.entity_repair','next.local_finding_review','next.answer','next.answer_review']:
        assert not model_choice(op, {'material_profile':{'kind':'PAPER'},'prior_findings':['math']}, True)[0]
    assert model_choice('next.entity_plan',{'atomics':[{'concept_outline':{'concept_key':'svm'}}]},False)[0]


@pytest.mark.parametrize('operation,pro', [('next.structure',True),('next.structure_exclusion_review',True),
    ('next.plan_labels',True),('next.entity_plan',True),('next.entity_review',False),('next.entity_repair',False)])
def test_requested_policy_reaches_real_model_boundary(operation, pro, monkeypatch):
    from noodle_agent.next.contracts import Contract
    monkeypatch.delenv('NOODLE_NEXT_PRO_USAGE')
    captured=[]
    class Decision(Contract):
        result:str
    class Agent:
        fast_model,pro_model='deepseek-v4-flash','deepseek-v4-pro'
        def structured_json(self,**kwargs):
            captured.append(kwargs)
            return {'result':'ok'}
    calls=Calls(SimpleNamespace(put=lambda *args:None),Agent())
    calls.run(operation,'Do the work',{'atomics':[{'concept_outline':{'concept_key':'svm'}}]},Decision,use_pro_model=not pro,max_attempts=1)
    assert len(captured)==1 and captured[0].get('use_pro_model',False)==pro
    assert captured[0]['user']['model']==('deepseek-v4-pro' if pro else 'deepseek-v4-flash')


def test_flash_only_real_call_uses_flash_and_keeps_a_single_complete_check(monkeypatch):
    monkeypatch.delenv('NOODLE_NEXT_PRO_USAGE')
    captured = []
    class Agent:
        fast_model, pro_model = 'deepseek-v4-flash', 'deepseek-v4-pro'
        def structured_json(self, **kwargs):
            captured.append(kwargs)
            return {'checked_plans':['a'], 'checked_omissions':[], 'findings':[], 'data_checks':[]}
    calls=Calls(SimpleNamespace(put=lambda *args:None), Agent(), review_policy='essential-v3')
    calls.run('next.assembly_review', 'Check', {'prior_findings':['math dispute']}, AssemblyAssessment,
              use_pro_model=True, max_attempts=1)
    assert len(captured)==1 and not captured[0].get('use_pro_model') and not captured[0].get('thinking')
    assert captured[0]['user']['model']=='deepseek-v4-flash'
    assert captured[0]['user']['model_selection_reason']=='flash-only-policy'
    assert COMPACT_REVIEW in captured[0]['system']


def test_ordinary_formulas_code_and_section_numbers_do_not_buy_pro():
    for text in ['## 3.3 标准差为 1.25', '$p(y|x)=p(x,y)/p(x)$', '```python\nx=1\n```']:
        assert not complex_reasoning(text)
        assert model_choice('next.assembly_review', {'assembly': [{'article_section': text}]}, True)[0] is False


def test_substantial_derivation_stays_pro_and_source_length_does_not_escalate():
    proof = '证明与推导：$$' + r'\frac{p(x,y)}{p(x)} + ' * 25 + '$$'
    assert complex_reasoning(proof)
    assert model_choice('next.entity_review', {'chapters': [{'markdown': proof}]}, True)[0]
    packet = {'assembly': [{'article_section': 'Ordinary definition.', 'source_units': [{'text': proof}]}]}
    assert not model_choice('next.assembly_review', packet, True)[0]


def test_planning_and_located_repairs_keep_requested_model(monkeypatch):
    assert not model_choice('next.plan_labels', {}, True)[0]
    assert not model_choice('next.structure_exclusion_review', {}, True)[0]
    assert not model_choice('next.structure', {'material_profile': {'kind': 'PAPER'}}, True)[0]
    for kind in ['BOOK', 'SLIDES', 'UNKNOWN']:
        assert model_choice('next.structure', {'material_profile': {'kind': kind}}, True)[0]
    assert model_choice('next.section_repair', {}, True)[0]
    assert model_choice('next.entity_repair', {}, True)[0]
    assert model_choice('next.local_finding_review', {}, True)[0]
    assert model_choice('next.entity_review', {'chapters': [{'markdown': 'Short correction', 'repair_findings': ['located issue']}]}, True)[0]
    assert model_choice('next.entity_plan', {'atomics': [{'plan_key': 'a'}],
        'source_context': {'topics': [{'plan_key': 'a', 'concept_outline': {'concept_key': 'svm'}}]}}, True)[0] is False
    assert model_choice('next.entity_plan', {'atomics': [{'plan_key': 'a'}]}, True)[0]
    monkeypatch.setenv('NOODLE_NEXT_COST_POLICY', 'legacy')
    assert model_choice('next.entity_review', {}, True)[0]
    assert model_choice('next.plan_labels', {}, True)[0]


@pytest.mark.parametrize('complex_case', [False, True])
def test_real_call_boundary_selects_model_but_keeps_one_review(complex_case):
    captured = []
    class Agent:
        fast_model = 'deepseek-flash'
        pro_model = 'deepseek-v4-pro'
        def structured_json(self, **kwargs):
            captured.append(kwargs)
            return {'checked_plans': ['a'], 'checked_omissions': [], 'findings': [], 'data_checks': []}
    store = SimpleNamespace(put=lambda *args: None)
    calls = Calls(store, Agent(), policy='structured-check-v2', review_policy='essential-v3')
    text = '证明：$$' + r'\frac{a}{b}' * 45 + '$$' if complex_case else '定义：$y=x$。'
    result = calls.run('next.assembly_review', 'Check', {'assembly': [{'article_section': text}]},
                       AssemblyAssessment, use_pro_model=True, max_attempts=1)
    assert result.checked_plans == ['a'] and len(captured) == 1
    assert captured[0].get('use_pro_model', False) is complex_case
    assert captured[0].get('thinking', False) is complex_case
    assert COMPACT_REVIEW in captured[0]['system']


def test_native_text_bypass_never_skips_images_or_corrupt_code():
    unit = SimpleNamespace(text='完整来源文字。' * 20 + '\n' + r'$\begin{matrix}a&b\\c&d\end{matrix}$', asset_ref=None)
    assert native_page_complete([unit])
    assert not native_page_complete([SimpleNamespace(text=unit.text, asset_ref='assets/figure.png')])
    assert not native_page_complete([SimpleNamespace(text='def broken(:\n' + 'a' * 100, asset_ref=None)])
    assert not native_page_complete([SimpleNamespace(text='\ufffd' * 100, asset_ref=None)])


def test_cost_upgrade_reuses_existing_page_recognition(request):
    store, source, _lease, journal, _, _ = request.getfixturevalue('corpus')
    from noodle_agent.next.contracts import sha
    with store.db.engine.connect() as c:
        blob, digest = c.exec_driver_sql('SELECT blob_ref,sha256 FROM source_revisions WHERE id=?', (source['revision_id'],)).one()
    with fitz.open(stream=store.read(blob, digest), filetype='pdf') as pdf:
        image = pdf[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes('png')
    inputs = {'revision_id': source['revision_id'], 'page': 1, 'image_sha256': sha(image),
              'policy': 'whole-page-source-v3', 'model': 'fixture-vision'}
    old = {'page': 1, 'text': 'prior valid evidence', 'evidence_id': 'old', 'asset_ref': 'assets/' + sha(image) + '.png'}
    journal.save('page-context.source-v3.1.' + sha(image)[:16], inputs, old)
    def forbidden(*args, **kwargs):
        raise AssertionError('Existing vision must never be purchased again')
    vision = SimpleNamespace(enabled=True, model_name='fixture-vision', analyze=forbidden)
    assert page_contexts(store, journal, vision, source['revision_id'], [], source_aligned=True, selected_pages=[1]) == [old]


def test_cohort_wait_is_scoped_and_has_freshness_ceiling(monkeypatch):
    from noodle_agent.next.concepts import coordinator
    now = datetime.now(UTC)
    job = {'created_at': now.isoformat()}
    producer = {'input_json': '{"revision_id":"src"}', 'chapter_id': 'ch', 'artifact_ref': 'plan', 'artifact_sha256': 'h'}
    monkeypatch.setattr(coordinator, 'rows', lambda *args, **kwargs: [producer])
    monkeypatch.setattr('noodle_agent.next.pipeline_state.artifact', lambda *args: {'plans': [
        {'key': 'a', 'chapter_key': 'ch', 'concept_outline': {'concept_key': 'svm'}},
        {'key': 'future', 'chapter_key': 'later', 'concept_outline': {'concept_key': 'svm'}}]})
    published = set()
    def query(c, sql, **kwargs):
        return {'identity_key': 'svm'} if 'concept_identity_claims' in sql else ({'found': 1} if kwargs['k'] in published else None)
    monkeypatch.setattr(coordinator, 'one', query)
    store = SimpleNamespace(root='fixture')
    assert not coordinator.cohort_ready(store, None, job, 'concept')
    published.add('atomic:src:source-section:a')
    assert coordinator.cohort_ready(store, None, job, 'concept')
    published.clear()
    assert coordinator.cohort_ready(store, None, {'created_at': (now - timedelta(seconds=181)).isoformat()}, 'concept')


def test_flash_alias_explicitly_disables_thinking_and_keeps_usage_details():
    from noodle_agent.adapters.knowledge.transport import TransportModule
    captured, receipts = [], []
    response = SimpleNamespace(id='r', choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{}'))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120,
            prompt_cache_hit_tokens=10, prompt_cache_miss_tokens=90, prompt_tokens_details=None,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=7)))
    def create(**kwargs):
        captured.append(kwargs)
        return response
    owner = SimpleNamespace(fast_model='deepseek-flash', name='fixture',
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        telemetry=SimpleNamespace(repository=None, record_llm=lambda **v: receipts.append(v)))
    assert TransportModule(owner)._json_call(system='check', user={'cost_policy': 'balanced-cost-v1'}, operation='next.entity_review') == {}
    assert captured[0]['extra_body']['thinking']['type'] == 'disabled'
    assert captured[0]['max_tokens'] == 8192
    assert receipts[0]['usage']['completion_tokens_details']['reasoning_tokens'] == 7
    assert receipts[0]['thinking_enabled'] is False


def test_native_delta_vision_disables_thinking_without_truncating_code_budget(tmp_path):
    import json
    from unittest.mock import MagicMock

    from noodle_agent.adapters.vision import DoubaoVisionProvider
    provider = object.__new__(DoubaoVisionProvider)
    provider.model_name = 'doubao-seed-2-1-turbo-260628'
    provider.system_prompt, provider.prompt_version = 'fixture', 'fixture'
    provider.reasoning_effort, provider.max_output_tokens = 'low', 900
    provider.client, provider._record = MagicMock(), MagicMock()
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'images': [{'asset_id': 'page-1', 'caption': '图中关系'}]})))])
    provider.client.chat.completions.create.return_value = response
    path = tmp_path / 'page.png'
    path.write_bytes(b'fixture-image')
    provider.analyze(path, external_block_id='page-1', context={'extraction_mode': 'SOURCE_PAGE', 'evidence_policy': 'native-delta-v1', 'native_text': '已有原文'})
    kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert kwargs['extra_body']['thinking']['type'] == 'disabled' and 'reasoning_effort' not in kwargs
    assert kwargs['max_tokens'] >= 4800
    assert 'native_text' in kwargs['messages'][1]['content'][0]['text']
    assert provider._record.call_args.kwargs['prompt_version'] == 'source-page-evidence-v2'
