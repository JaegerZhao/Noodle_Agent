"""Regression coverage for source grouping, publication assets and frozen models."""

from noodle_agent.next.concepts.entity_context import apply_outline
from noodle_agent.next.concepts.entity_contracts import Entity, EntityPlan, Route
from noodle_agent.next.contracts import Scope
from noodle_agent.next.generation_contracts import Unit
from noodle_agent.next.learning_labels import merge_short_tails
from noodle_agent.next.publication_assets import (
    figure_paths,
    model_contexts,
    omit_unregistered_images,
    without_images,
)
from noodle_agent.next.section_contracts import Boundaries, Boundary


def unit(key, text='知识', **kwargs):
    return Unit(unit_id=key, block_id=key, page=1, start=0, end=len(text), text=text, kind='TEXT', **kwargs)


def test_page_proof_does_not_become_publishable_figure():
    units = [unit('a', asset_ref='assets/page.png'), unit('b', asset_ref='assets/plot.png')]
    contexts = [{'asset_ref':'assets/page.png','page':1,'text':'{"ocr_text":["公式"]}','evidence_id':'e'}]
    assert figure_paths(units, contexts) == ['../assets/plot.png']
    projected = model_contexts(contexts)
    assert 'asset_ref' not in projected[0]
    assert projected[0]['material']['ocr_text'] == ['公式']
    audit = []
    text = without_images('条件 $x>0$\n![页](../assets/page.png)\n![图](../assets/plot.png)', {'assets/page.png'}, audit)
    assert '$x>0$' in text and 'plot.png' in text and 'page.png' not in text
    assert audit[0]['code'] == 'CONTEXT_IMAGE_NOT_PUBLISHED'


def test_successful_partial_batch_keeps_full_source_object_identity():
    plan = EntityPlan(entities=[Entity(title='硬间隔',domain='ml',identity_key='hard',description='最大间隔',question='分类',scope=Scope(includes=['分类'])),
        Entity(title='软间隔',domain='ml',identity_key='soft',description='松弛变量',question='分类',scope=Scope(includes=['分类']))],
        routes={'a':Route(entity_key='hard',sections={'body':'principles'},reason='硬间隔'),
                'b':Route(entity_key='soft',sections={'body':'principles'},reason='软间隔')})
    cards=[{'id':'a','plan_key':'p1'},{'id':'b','plan_key':'p2'}]
    context={'topics':[{'plan_key':p,'concept_outline':{'concept_key':'svm','concept_title':'支持向量机','internal_topic':p}} for p in ['p1','p2']]}
    apply_outline(plan,cards,context)
    assert len(plan.entities)==1 and plan.entities[0].title=='支持向量机'
    assert {r.entity_key for r in plan.routes.values()}=={'svm'}
    assert plan.routes['b'].sections=={'body':'principles'}


def test_unknown_image_is_not_guessed_or_allowed_and_caption_reaches_normal_review():
    markdown='公式 $x>0$\n![分隔超平面](../assets/invented.jpg)\n![真实图](../assets/registered.jpg)\n```md\n![示例](../assets/example.jpg)\n```'
    diagnostics=[]
    normalized=omit_unregistered_images(markdown,['../assets/registered.jpg'],diagnostics)
    assert 'invented.jpg' not in normalized and '分隔超平面' in normalized
    assert '![真实图](../assets/registered.jpg)' in normalized
    assert '![示例](../assets/example.jpg)' in normalized
    assert '$x>0$' in normalized
    assert diagnostics==[{'code':'UNREGISTERED_IMAGE_OMITTED','asset_ref':'../assets/invented.jpg',
        'policy':'figure-publication-v1','reason':'No exact registered asset; no guessed replacement'}]


def test_short_tail_merges_without_crossing_chapters_or_losing_units():
    boundaries=Boundaries(sections=[Boundary(start_unit='a',title='定义',chapter_key='c',parent_title='主题',topic_key='object'),
        Boundary(start_unit='b',title='条件',chapter_key='c',parent_title='主题',topic_key='object'),
        Boundary(start_unit='d',title='下一章',chapter_key='d',parent_title='主题',topic_key='object')])
    result=merge_short_tails(boundaries,[unit('a'),unit('b'),unit('d')])
    assert [b.start_unit for b in result.sections]==['a','d']
    assert '条件' in result.sections[0].title


def test_parse_only_reuse_removes_legacy_vision_prose_without_mutating_origin():
    from noodle_agent.next.source_cache import parser_only_artifact
    original = {'blocks':[{'text':'MinerU 原始图注\n[视觉识别，需结合原图核对]\n旧模型解释',
        'visual':{'caption':'旧模型解释'},'evidence_origin':'DERIVED_VISION','bbox':[1,2,3,4]},
        {'text':'原始公式 $x>0$','kind':'EQUATION'},
        {'text':'[视觉识别，需结合原图核对]\n旧解释','visual':{},'asset_ref':'assets/empty-caption.png'}]}
    result, removed = parser_only_artifact(original)
    assert removed == 2 and result['blocks'][0]['text'] == 'MinerU 原始图注'
    assert result['blocks'][0]['bbox'] == [1,2,3,4]
    assert result['blocks'][1] == original['blocks'][1]
    assert 'visual' not in result['blocks'][0] and 'evidence_origin' not in result['blocks'][0]
    assert original['blocks'][0]['visual']['caption'] == '旧模型解释'
    assert result['blocks'][2]['text'] == '' and result['blocks'][2]['asset_ref'] == 'assets/empty-caption.png'


def test_repeated_title_is_removed_without_dropping_real_subheading_or_code():
    from noodle_agent.next.markdown_checks import without_repeated_heading
    assert without_repeated_heading('### 2.1 贝叶斯规则\n\n条件 $p(B)>0$\n### 推导\n内容', '贝叶斯规则') == '条件 $p(B)>0$\n### 推导\n内容'
    code='```python\n# 贝叶斯规则\nprint(1)\n```'
    assert without_repeated_heading(code,'贝叶斯规则') == code


def test_empty_diagnostic_envelope_is_compiled_but_real_findings_remain():
    from noodle_agent.next.concepts.entity_contracts import EntityPatch
    from noodle_agent.next.json_format import empty_diagnostic_envelope
    raw={'chapters':{'core':{'markdown':'核心 $x>0$','images':{}}},'findings':{}}
    decoded,audit=empty_diagnostic_envelope(raw,EntityPatch)
    assert EntityPatch.model_validate(decoded).chapters['core'].markdown=='核心 $x>0$'
    assert raw['findings']=={} and audit[0]['code']=='EMPTY_DIAGNOSTIC_ENVELOPE'
    real={'chapters':{},'findings':{'core':'缺失关键条件'}}
    assert empty_diagnostic_envelope(real,EntityPatch)==(real,[])
