"""Real book gap regressions; no external calls or generated-file edits."""

import json

import pytest
from test_next_workflow_readiness import corpus  # noqa: F401

from noodle_agent.adapters.knowledge.common import _loads_structured_json
from noodle_agent.next.attempts import charge
from noodle_agent.next.generation import Generation
from noodle_agent.next.generation_contracts import Draft, DraftSection
from noodle_agent.next.markdown_checks import normalize_math_markup
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.section_contracts import bounded_source_draft


def draft(markdown):
    return Draft(title='卷积', summary='卷积定义', sections=[DraftSection(key='s', title='卷积',
        question='卷积是什么', scope={'includes': ['卷积']}, markdown=markdown, units=['u'])])


def test_formula_brackets_are_not_markdown_resource_links():
    text = r'卷积定义为 $[f \circledast g](\boldsymbol{z})=0$。'
    assert Generation.checked_draft(draft(text), {'u'}, set()).sections[0].markdown == text
    with pytest.raises(QualityFailure, match='Unapproved Markdown'):
        Generation.checked_draft(draft(text + '\n[外部](https://example.invalid)'), {'u'}, set())


def test_math_code_format_is_compiled_and_actual_code_stays_intact():
    body = '概率为 `$p(y)=1$`。\n\n```python\nvalue = "$p$"\n```'
    actual = Generation.checked_draft(draft(body), {'u'}, set()).sections[0].markdown
    assert '概率为 $p(y)=1$' in actual
    assert 'value = "$p$"' in actual


def test_spam_punctuation_features_are_literal_code_not_broken_math():
    from noodle_agent.next.assembly_generation import checked_source_draft
    from noodle_agent.next.section_contracts import SourceDraft
    body = '垃圾邮件特征包括 `;`、`.`、`[`、`!`、`$` 和 `#` 的出现频率。'
    candidate = SourceDraft.model_validate({'title':'随机森林', 'sections':[{'key':'s','title':'垃圾邮件分类','markdown':body}],
        'assignments':{'u':'s'},'omissions':{}})
    assert checked_source_draft(candidate, {'u'}, set()).sections[0].markdown == body


def test_single_source_page_identifier_is_bound_without_guessing_across_images(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from noodle_agent.adapters.vision import DoubaoVisionProvider
    provider = object.__new__(DoubaoVisionProvider)
    provider.model_name = 'doubao-seed-2-1-turbo-260628'
    provider.system_prompt, provider.prompt_version = 'fixture', 'fixture'
    provider.reasoning_effort, provider.max_output_tokens = 'low', 900
    provider.client, provider._record = MagicMock(), MagicMock()
    provider.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({'images':[{'asset_id':'asset_1','caption':'树状图'}]})))])
    path = tmp_path / 'page.png';path.write_bytes(b'fixture-image')
    result = provider.analyze(path, external_block_id='page-748', context={'extraction_mode':'SOURCE_PAGE'})
    assert result.external_block_id == 'page-748' and result.caption == '树状图'
    assert provider._record.call_args.kwargs['normalizations'][0]['to'] == 'page-748'
    with pytest.raises(ValueError, match='asset IDs'):
        provider.analyze(path, external_block_id='crop-1', context={})
    with pytest.raises(ValueError, match='asset IDs'):
        provider.analyze_batch([{'image_path':path, 'external_block_id':k, 'context':{'extraction_mode':'SOURCE_PAGE'}} for k in ['page-1','page-2']])


def test_failed_ocr_table_replays_exact_response_without_another_paid_call(request, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from noodle_agent.adapters.vision import DoubaoVisionProvider
    from noodle_agent.next.audit import ProviderAudit

    store, _source, lease, _journal, _profile, _ = request.getfixturevalue('corpus')
    provider = object.__new__(DoubaoVisionProvider)
    provider.model_name = 'doubao-seed-2-1-turbo-260628'
    provider.system_prompt, provider.prompt_version = 'fixture', 'fixture'
    provider.reasoning_effort, provider.max_output_tokens = 'low', 900
    provider.client, provider.telemetry = MagicMock(), ProviderAudit(store.repository, lease)
    raw = json.dumps({'images':[{'asset_id':'page-803', 'ocr_text':[['Symbol','Meaning'],[r'$\infty$','Infinity']]}]})
    provider.client.chat.completions.create.return_value = SimpleNamespace(id='fixture-response',usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    path = tmp_path / 'page.png';path.write_bytes(b'fixture-image')
    context = {'extraction_mode':'SOURCE_PAGE','evidence_policy':'native-delta-v1','native_text':'原始文字'}
    with patch('noodle_agent.adapters.vision.text_items', side_effect=lambda v: v if v is not None else []), pytest.raises(ValueError):
        provider.analyze(path, external_block_id='page-803', context=context)
    result = provider.analyze(path, external_block_id='page-803', context=context)
    assert result.ocr_text == ['Symbol\tMeaning', r'$\infty$' + '\tInfinity']
    assert provider.client.chat.completions.create.call_count == 1
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM provider_calls WHERE kind='VISION_CALL'").scalar_one() == 1
    assert len(list(store.root.glob('artifacts/format-recoveries/*.json'))) == 1


def test_optional_navigation_skips_only_invalid_new_edges_and_keeps_roots():
    from noodle_agent.next.organization_v2 import OrganizationProposal, compile_proposal
    nodes = {k:{} for k in ['a','b','c','d']}
    supported = {k:{'u'+k} for k in nodes}
    proposal = OrganizationProposal.model_validate({'links':[
        {'parent_note_id':'b','child_note_id':'a','support_revision_ids':['ua'],'reason':'循环候选'},
        {'parent_note_id':'a','child_note_id':'c','support_revision_ids':['uc'],'reason':'有效子主题'},
        {'parent_note_id':'unknown','child_note_id':'d','support_revision_ids':['ud'],'reason':'未知父节点'},
        {'parent_note_id':'c','child_note_id':'d','support_revision_ids':['ua'],'reason':'证据不属于两端'}]})
    diagnostics = []
    links, remaining = compile_proposal(proposal,nodes,supported,['a','c','d'],{('a','b')},diagnostics=diagnostics)
    assert len(links) == 1 and links[0].owner_note_id == 'c' and links[0].target_note_id == 'a'
    assert remaining == ['a','d']
    assert [v['code'] for v in diagnostics] == ['CYCLIC_NAVIGATION_EDGE_SKIPPED','INVALID_NAVIGATION_ENDPOINT_SKIPPED','UNOWNED_NAVIGATION_SUPPORT_SKIPPED']
    with pytest.raises(ValueError, match='cycle'):
        compile_proposal(proposal,nodes,supported,['a','c','d'],{('a','b')})


def test_breve_escape_and_extra_closing_group_recover_only_math_markup():
    assert _loads_structured_json(r'{"markdown":"$\breve{w}$"}')['markdown'] == r'$\breve{w}$'
    body = '$' + '\x08reve{w}' + '$；' + r'$\boldsymbol{\Lambda}}=I$'
    compiled = normalize_math_markup(body)
    assert compiled == r'$\breve{w}$；$\boldsymbol{\Lambda}=I$'
    assert normalize_math_markup('原文 } 和 \x08reve 不在公式内') == '原文 } 和 \x08reve 不在公式内'
    Generation.checked_draft(draft(body), {'u'}, set())


def test_writer_schema_does_not_suggest_extrapolating_global_unit_numbers():
    contract = bounded_source_draft(['u07643', 'u07644', 'u07647'])
    for field in ('assignments', 'omissions'):
        schema = contract.model_json_schema()['properties'][field]
        assert schema['propertyNames']['enum'] == ['u07643', 'u07644', 'u07647']
        assert schema['maxProperties'] == 3


@pytest.mark.parametrize('same_timestamp', [False, True])
def test_cost_observations_survive_retry_and_explicit_recovery_starts_a_new_window(request, monkeypatch, same_timestamp):
    store, _source, lease, _journal, _profile, _ = request.getfixturevalue('corpus')
    if same_timestamp:
        from noodle_agent.next.repository import now
        stamp = now()
        for module in ('attempts', 'jobs', 'repository'):
            monkeypatch.setattr('noodle_agent.next.' + module + '.now', lambda: stamp)
    charge(store, lease, 'atomic:a', 'next.section_write', 1)
    charge(store, lease, 'atomic:a', 'next.section_write', 1)
    store.jobs.fail(lease, {'code': 'TEMPORARY'}, retry=True)
    second = store.jobs.claim('automatic', kind='GENERATE_ATOMICS')
    charge(store, second, 'atomic:a', 'next.section_write', 1)
    store.jobs.fail(second, {'code': 'STOPPED'}, retry=False)
    store.jobs.retry_review(lease.job_id)
    third = store.jobs.claim('recovery', kind='GENERATE_ATOMICS')
    charge(store, third, 'atomic:a', 'next.section_write', 1)
    charge(store, third, 'atomic:a', 'next.section_write', 1)
    with store.db.engine.connect() as c:
        attempts = c.exec_driver_sql(
            "SELECT storage_ref,sha256 FROM build_artifacts WHERE kind='MODEL_ATTEMPT' ORDER BY rowid"
        ).all()
        observations = c.exec_driver_sql(
            "SELECT count(*) FROM build_artifacts WHERE kind='COST_OBSERVATION'"
        ).scalar_one()
    assert [json.loads(store.read(ref, digest))['attempt'] for ref, digest in attempts] == [1, 2, 3, 1, 2]
    assert observations == 2
