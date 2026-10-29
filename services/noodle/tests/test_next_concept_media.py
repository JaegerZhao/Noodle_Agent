"""Media additions preserve prose and never need another content review."""

from test_next_workspace import atomic, store  # noqa: F401

from noodle_agent.next.concepts.entity_contracts import Chapter, RoutingDecision
from noodle_agent.next.concepts.entity_writing import bind_images, checked_images, retained_assets
from noodle_agent.next.concepts.fragments import fragments
from noodle_agent.next.concepts.media import candidates, maintain
from noodle_agent.next.concepts.routing_protocol import expand
from noodle_agent.next.concepts.selection import contributions
from noodle_agent.next.contracts import sha


def image_fragments(library):
    data = b'fixture-image-content'
    ref = 'assets/' + sha(data) + '.png'
    library.put(ref, data)
    note, _ = atomic(library, parts=('结构由编码器和解码器组成。\n\n![模型架构](../'+ref+')',))
    return fragments(library, library.get_note(note['note_id']))


class MediaCalls:
    def __init__(self):
        self.calls = []

    def run(self, operation, instruction, payload, contract, check=None, **kwargs):
        self.calls.append(operation)
        assert kwargs['use_pro_model'] is False and kwargs['max_attempts'] == 1
        value = contract.model_validate({'selected': [{'image_id': next(iter(payload['figures'])),
            'caption': '编码器与解码器结构', 'after_heading': '要素与联系', 'reason': '直接解释模型组件'}]})
        check(value)
        return value


def test_key_figure_addition_preserves_text_and_reuses_media(store):  # noqa: F811
    evidence = image_fragments(store)
    original = '一句话定义：编码器与解码器模型。\n\n### 要素与联系\n\n组件相互连接。'
    calls = MediaCalls()
    body, report = maintain(store, calls, '模型', original, evidence)
    assert body.count('![') == 1 and report['text_rewritten'] is False
    assert '一句话定义：编码器与解码器模型。' in body and '组件相互连接。' in body
    assert report['selected'][0]['sources'] and len(calls.calls) == 1
    again, receipt = maintain(store, calls, '模型', body, evidence, report)
    assert again == body and not receipt['called'] and len(calls.calls) == 1


def test_page_context_is_never_a_core_figure(store, monkeypatch):  # noqa: F811
    evidence = image_fragments(store)
    ref = next(iter(candidates(store, evidence).values()))['asset_ref'].removeprefix('../')
    monkeypatch.setattr('noodle_agent.next.concepts.media.page_assets', lambda store: {ref})
    assert not candidates(store, evidence)
    calls = MediaCalls()
    assert maintain(store, calls, '模型', '真实正文。', evidence)[0] == '真实正文。'
    assert not calls.calls


def test_only_current_supported_old_figures_enter_incremental_packet(store):  # noqa: F811
    evidence=image_fragments(store)
    ref=next(iter(candidates(store,evidence).values()))['asset_ref']
    old='已有正文。\n\n![旧核心图]('+ref+')'
    assert len(retained_assets(store,evidence,old))==1
    assert retained_assets(store,[],old)=={}
    unsupported='../assets/'+'f'*64+'.png'
    assert retained_assets(store,evidence,'![历史图]('+unsupported+')')=={}


def test_text_delta_and_media_delta_are_independent_in_routing():
    wire = RoutingDecision.model_validate({'assignments': [{'atomic': 'A0', 'target': 'N0', 'action': 'CREATE',
        'contribution_mode': 'EVIDENCE', 'text_contribution': 'CORE_DELTA', 'media_contribution': 'KEY_FIGURE',
        'reason': '同对象但补充关键变体'}], 'new_entities': [{'ref': 'N0', 'title': '决策树', 'domain': '机器学习',
        'identity_key': 'decision_tree', 'description': '树模型'}]})
    plan = expand(wire, [{'id': 'atomic', 'sections': [{'id': 's', 'title': '变体'}]}], [])
    route = plan.routes['atomic']
    assert route.media_contribution == 'KEY_FIGURE'
    actual = list(contributions({'route': route.model_dump(), 'mapping': {}}, [{'section_id': 's', 'fragment_id': 'f'}], 'target'))
    assert actual[0][1] == 'CORE_DELTA'


def test_explicit_use_is_compiled_even_when_model_omits_markdown_reference():
    ref = '../assets/'+'a'*64+'.png'
    assets = {'diagram': {'asset_ref': ref, 'caption': '模型架构'}}
    value = Chapter(markdown='### 核心结构\n\n正文不变。', images={'diagram': {'action': 'USE', 'reason': '解释组件', 'after_heading': '核心结构'}})
    result = bind_images(value, assets)
    checked_images(result, assets)
    assert ref in result.markdown and result.markdown.count('正文不变。') == 1


def test_uses_identity_with_explicit_evidence_is_distribution_not_false_reuse():
    decision=RoutingDecision.model_validate({'assignments':[{'atomic':'A0','target':'C0','action':'REUSE',
        'object_match':'USES','reason':'解释已有方法的局限','selections':[{'target':'C0','sections':['S0'],
        'purpose':'SUPPORT_ONLY','reason':'局限性证据'}]}]})
    cards=[{'id':'source','sections':[{'id':'s'}]}]
    catalog=[{'id':'old','title':'已有方法','domain':'领域','aliases':[],'definition':{'identity_key':'method'}}]
    plan=expand(decision,cards,catalog)
    assert not plan._unresolved_atomics
    assert plan.routes['source'].entity_key is None
    assert plan.routes['source'].selected_contributions[0].entity_key
    assert plan._normalizations[0]['code']=='EXPLICIT_EVIDENCE_DISTRIBUTION'
