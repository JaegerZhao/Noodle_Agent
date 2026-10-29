"""Object confusion and sparse contributions exercise the production routing path."""

import json
from dataclasses import replace

import pytest
from test_next_entity_concepts import Agent, store  # noqa: F401

from noodle_agent.config import Settings
from noodle_agent.next.concepts.coverage import core_coverage
from noodle_agent.next.concepts.entity_context import apply_outline
from noodle_agent.next.concepts.entity_contracts import RoutingDecision
from noodle_agent.next.concepts.entity_routing import validate_plan
from noodle_agent.next.concepts.routing_protocol import expand
from noodle_agent.next.concepts.selection import contributions
from noodle_agent.next.contracts import Candidate, EntityHints, Review, Scope, Section
from noodle_agent.next.repository import one, rows
from noodle_agent.next.runtime import Application


def card():
    return {'id': 'atomic', 'revision': 'r1', 'title': '逻辑回归',
            'sections': [{'id': 'definition'}, {'id': 'example'}],
            'concept_outline': {'concept_key': 'logistic', 'concept_title': '逻辑回归'}}


def catalog():
    return [{'id': 'bernoulli-note', 'title': '伯努利分布', 'domain': 'ml',
             'definition': {'identity_key': 'bernoulli'}}]


@pytest.mark.parametrize('match', ['USES', 'DIFFERENT_OBJECT', 'UNCERTAIN'])
def test_related_objects_cannot_be_accepted_as_reuse(match):
    decision = RoutingDecision.model_validate({'assignments': [{'atomic': 'A0',
        'target': 'C0', 'action': 'REUSE', 'object_match': match, 'reason': '模型使用分布描述输出'}]})
    plan = expand(decision, [card()], catalog())
    apply_outline(plan, [card()], {}, catalog())
    validate_plan(plan, [card()], catalog())
    assert plan._unresolved_atomics == {'atomic': 'OBJECT_MATCH_DOES_NOT_ALLOW_REUSE'}
    assert not plan.entities


def test_explicit_outline_revision_is_preserved_instead_of_forced_rename():
    source = card()
    source['concept_outline']['concept_key'] = 'bernoulli'
    decision = RoutingDecision.model_validate({'assignments': [{'atomic': 'A0',
        'target': 'N0', 'action': 'CREATE', 'object_match': 'SAME_OBJECT',
        'outline_revision_reason': '模型与分布是两个独立对象', 'reason': '建立分类模型的核心框架'}],
        'new_entities': [{'ref': 'N0', 'identity_key': 'logistic', 'title': '逻辑回归',
                          'domain': 'ml', 'description': '用于分类的条件概率模型'}]})
    plan = expand(decision, [source], catalog())
    apply_outline(plan, [source], {}, catalog())
    validate_plan(plan, [source], catalog())
    assert plan.routes['atomic'].entity_key == 'logistic'
    assert plan.routes['atomic'].binding['source_hint_key'] == 'bernoulli'
    assert plan.routes['atomic'].binding['version']
    assert source['concept_outline']['concept_key'] == 'bernoulli'


def test_outline_variant_reuses_a_valid_object_without_creating_narrow_entity():
    source = card()
    source['concept_outline']['concept_key'] = 'gradient-boosting'
    existing = [{'id': 'boost', 'title': '提升方法', 'domain': 'ml', 'definition': {'identity_key': 'boosting'}}]
    decision = RoutingDecision.model_validate({'assignments': [{'atomic': 'A0',
        'target': 'C0', 'action': 'REUSE', 'object_match': 'INTERNAL_VARIANT',
        'reason': '作为提升方法中的内部变体'}]})
    plan = expand(decision, [source], existing)
    apply_outline(plan, [source], {}, existing)
    assert plan.routes['atomic'].entity_key == 'boosting'
    assert len(plan.entities) == 1 and plan.entities[0].reuse_id == 'boost'


def test_coverage_uses_current_body_and_marks_budget_or_invalid_support_unknown():
    item = {'revision_id': 'r', 'scope': {'includes': ['not actually covered']},
            'sections': [{'section_id': 'core', 'title': '框架', 'markdown': '定义\n边界条件'},
                         {'section_id': 'pending', 'title': '尚未写作', 'markdown': '不可当作核心', 'planned': True}]}
    result = core_coverage(item)
    assert result['complete'] and '边界条件' in result['text']
    assert 'not actually covered' not in result['text'] and '不可当作核心' not in result['text']
    assert core_coverage(item, limit=2)['coverage_unknown']
    assert core_coverage({**item, 'valid': False})['text'] == ''


def test_sparse_selection_replaces_primary_only_for_selected_sections():
    value = {'route': {'reason': '默认核心', 'selected_contributions': [
        {'entity_key': 'secondary', 'section_ids': ['example'], 'purpose': 'FURTHER_READING', 'reason': '实现示例'}]},
        'mapping': {'secondary': 'c2'}}
    result = list(contributions(value, [{'section_id': 'definition'}, {'section_id': 'example'}], 'c1'))
    assert [(c, p) for c, p, _, _ in result] == [('c1', 'CORE_DELTA'), ('c2', 'FURTHER_READING')]


def test_fragment_selection_separates_two_objects_inside_one_atomic_section():
    source = card()
    source['sections'] = [{'id': 'article'}]
    source['fragments'] = [{'fragment_id': 'em-proof', 'section_id': 'article'},
                           {'fragment_id': 'blackbox-proof', 'section_id': 'article'}]
    raw = {'assignments': [{'atomic': 'A0', 'target': 'N0', 'action': 'CREATE', 'object_match': 'SAME_OBJECT',
        'reason': '混合 Atomic 以 EM 为主', 'selections': [
            {'target': 'N1', 'fragments': ['F1'], 'purpose': 'CORE_DELTA', 'reason': '黑箱优化片段归独立对象'}]}],
        'new_entities': [{'ref': 'N0', 'identity_key': 'em', 'title': 'EM', 'domain': 'ml', 'description': '潜变量估计'},
                         {'ref': 'N1', 'identity_key': 'blackbox', 'title': '黑箱优化', 'domain': 'ml', 'description': '无导数优化'}]}
    plan = expand(RoutingDecision.model_validate(raw), [source], [])
    validate_plan(plan, [source], [])
    route = plan.routes['atomic'].model_dump()
    value = {'route': route, 'mapping': {'blackbox': 'c2'}}
    selected = list(contributions(value, [{'fragment_id': 'em-proof', 'section_id': 'article'},
                                        {'fragment_id': 'blackbox-proof', 'section_id': 'article'}], 'c1'))
    assert [(c, f['fragment_id']) for c, _, f, _ in selected] == [('c1', 'em-proof'), ('c2', 'blackbox-proof')]


def test_legacy_collected_scope_is_not_sent_as_identity_evidence():
    from noodle_agent.next.concepts.routing_protocol import packet
    item = {'id': 'distribution', 'title': '伯努利分布', 'domain': '概率论', 'summary': '旧摘要混入逻辑回归',
        'definition': {'scope': {'includes': ['逻辑回归', 'IRLS', '优化分类模型']}},
        'sections': [{'section_id': 'core', 'title': '知识框架', 'markdown': '分布定义与逻辑回归应用'}]}
    candidate = packet([], [item])['candidates'][0]
    assert candidate['identity_boundary']['status'] == 'LEGACY_UNRESOLVED'
    assert candidate['scope']['includes'] == ['伯努利分布']
    assert candidate['description'] == '伯努利分布'
    assert candidate['core_coverage']['complete']
    assert candidate['core_coverage']['text'] == '分布定义与逻辑回归应用'
    assert candidate['core_coverage']['identity_ownership'] == 'UNRESOLVED'
    assert candidate['core_coverage']['identity_use'] == 'DO_NOT_INFER_IDENTITY_FROM_INCLUDED_CONTENT'


@pytest.mark.parametrize('distributed', [False, True])
def test_same_fragment_multiple_targets_persists_once_per_target_and_retries_without_calls(store, distributed):  # noqa: F811
    source = store.add_source(b'EM estimates latent variables. Black box inference uses generic estimators.', '混合章', kind='AUTHOR')
    identity = store.content.create_note('ATOMIC', 'EM 与黑盒推断', creation_key=f"atomic:{source['revision_id']}:mixed")
    sections = []
    for key, body in [('em', 'EM estimates latent variables.'), ('blackbox', 'Black box inference uses generic estimators.')]:
        sections.append(Section(section_id=key, title=key, question=key, scope=Scope(includes=[key]),
            markdown=body, evidence_ids=[store.add_evidence(source['revision_id'], body)]))
    value = Candidate(note_id=identity, title='EM 与黑盒推断', summary='两个不同对象及其联系',
        scope=Scope(includes=['EM', '黑盒推断']), sections=sections,
        entity_hints=EntityHints(subject='EM', subjects=['EM', '黑盒推断'], context='推断', intent='EXPLAIN'))
    store.publish(value, expected_revision=None, review=Review(candidate_sha256=value.fingerprint,
        policy_version=value.policy_version, reviewer='fixture', passed=True))

    class MixedAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            if operation == 'next.entity_write':
                return {'chapters': {s['key']: {'markdown': '\n\n'.join(f['text'] for f in s['fragments'])}
                                     for s in user['content']['chapters']}}
            if operation == 'next.entity_review':
                return {'chapters': {s['key']: {'supported_fragment_ids': [f['fragment_id'] for f in s['fragments']],
                                              'checked_image_ids': [], 'findings': []}
                                     for s in user['content']['chapters']}}
            assert operation == 'next.entity_plan'
            self.calls.append(operation)
            return {'assignments': {'A0': {'action': 'DISTRIBUTE' if distributed else 'CREATE', 'target': None if distributed else 'N0', 'object_match': 'SAME_OBJECT',
                'reason': 'EM 主归属，黑盒推断小节单独贡献', 'selections': [
                    *([{'target': 'N0', 'sections': ['S0'], 'purpose': 'CORE_DELTA', 'reason': 'EM 核心'}] if distributed else []),
                    {'target': 'N1', 'sections': ['S1'], 'purpose': 'CORE_DELTA', 'reason': '另一对象的核心'},
                    {'target': 'N0', 'sections': ['S1'], 'purpose': 'FURTHER_READING', 'reason': '仅比较阅读'}]}},
                'new_entities': [{'ref': 'N0', 'identity_key': 'em', 'title': 'EM', 'domain': 'ml', 'description': '潜变量估计'},
                                 {'ref': 'N1', 'identity_key': 'black-box', 'title': '黑盒推断', 'domain': 'ml', 'description': '通用推断'}]}

    agent = MixedAgent()
    settings = replace(Settings.from_env(store.root), embedding_api_key=None, llm_api_key=None,
                       vision_api_key=None, mineru_api_token=None)
    app = Application(store, agent=agent, settings=settings,
                      index_mode='KEYWORD_ONLY', job_kinds=('ANALYZE_ATOMIC',))
    assert app.run_once('mixed')['status'] == 'SUCCEEDED'
    with store.db.engine.connect() as c:
        decisions = rows(c, 'SELECT * FROM contribution_decisions ORDER BY concept_id,fragment_id')
        events = rows(c, "SELECT * FROM events WHERE event_type='CONCEPT_REBUILD_REQUESTED'")
        assert c.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    assert len(decisions) == 3 and len({d['concept_id'] for d in decisions}) == 2
    assert len(events) == 2
    assert sorted(json.loads(d['decision_json'])['purpose'] for d in decisions) == ['CORE_DELTA', 'CORE_DELTA', 'FURTHER_READING']
    # A second event uses the same accepted route and frozen selection; no model call.
    with store.repository.transaction() as c:
        store.repository.event(c, 'TEST_REPLAY', identity, None, {}, key='test-selection-replay')
        event = one(c, "SELECT id FROM events WHERE dedupe_key='test-selection-replay'")['id']
    revision = store.get_note(identity)['revision']['id']
    store.jobs.enqueue(event, 'ANALYZE_ATOMIC', {'entity_id': identity, 'revision_id': revision})
    assert app.run_once('repeat')['status'] == 'SUCCEEDED'
    with store.db.engine.connect() as c:
        assert len(rows(c, 'SELECT * FROM contribution_decisions')) == 3
        assert len(rows(c, "SELECT * FROM events WHERE event_type='CONCEPT_REBUILD_REQUESTED'")) == 2
    assert len(agent.calls) == 1
    writer = Application(store, agent=agent, settings=settings,
                         index_mode='KEYWORD_ONLY', job_kinds=('BUILD_CONCEPT',))
    for _ in range(4):
        outcome = writer.run_once('publish-selection')
        if outcome is None:
            break
        assert outcome['status'] == 'SUCCEEDED', outcome
    with store.db.engine.connect() as c:
        concepts = rows(c, "SELECT id,title FROM notes WHERE kind='CONCEPT'")
    bodies = {n['title']: store.get_note(n['id']) for n in concepts}
    assert 'Black box inference' not in bodies['EM']['markdown']
    assert 'EM estimates' not in bodies['黑盒推断']['markdown']
    assert 'Black box inference' in bodies['黑盒推断']['markdown']
    assert bodies['EM']['revision']['summary'] != '潜变量估计'


def test_distribution_unselected_context_has_no_primary_object_or_composite_declaration():
    source = card()
    decision = RoutingDecision.model_validate({'assignments': [{'atomic': 'A0', 'action': 'DISTRIBUTE',
        'target': None, 'reason': '多个对象独立贡献，算例留在 Atomic', 'selections': [
            {'target': 'N0', 'sections': ['S0'], 'purpose': 'CORE_DELTA', 'reason': '定义'}]}],
        'new_entities': [{'ref': 'N0', 'title': '逻辑回归', 'identity_key': 'logistic', 'domain': 'ml', 'description': '分类模型'},
                         {'ref': 'N1', 'title': '未使用声明', 'identity_key': 'unused', 'domain': 'ml', 'description': '无贡献'}]})
    plan = expand(decision, [source], [])
    validate_plan(plan, [source], [])
    assert not plan._protocol_issues and not plan._unresolved_atomics
    assert len(plan.entities) == 1 and plan.routes['atomic'].entity_key is None
    result = list(contributions({'route': plan.routes['atomic'].model_dump(), 'mapping': {'logistic': 'c'}},
        [{'section_id': 'definition'}, {'section_id': 'example'}], None))
    assert [(target, purpose) for target, purpose, _, _ in result] == [('c', 'CORE_DELTA'), (None, 'CONTEXT_ONLY')]


def test_keep_action_does_not_treat_explanatory_reason_as_an_outline_revision():
    source = card()
    existing = [{'id': 'canonical', 'title': '逻辑回归', 'domain': 'ml', 'definition': {'identity_key': 'binary-logistic'}}]
    decision = RoutingDecision.model_validate({'assignments': [{'atomic': 'A0', 'action': 'CREATE', 'target': 'N0',
        'object_match': 'SAME_OBJECT', 'outline_action': 'KEEP', 'outline_revision_reason': '无需修订', 'reason': '同一模型'}],
        'new_entities': [{'ref': 'N0', 'title': '逻辑回归', 'domain': 'ml', 'identity_key': 'logistic', 'description': '分类'}]})
    plan = expand(decision, [source], existing)
    apply_outline(plan, [source], {'same_source_bindings': [{'resolved_binding': {
        'source_hint_key': 'logistic', 'entity_key': 'binary-logistic', 'outline_action': 'KEEP',
        'outline_revision_reason': '无需修订'}}]}, existing)
    assert plan.routes['atomic'].entity_key == 'binary-logistic'


def test_same_key_across_domains_persists_separate_claims_and_routes(store):  # noqa: F811
    from test_next_entity_concepts import drain, publish_atomic

    source = store.add_source('图论树是无环连通图。机器学习树按条件进行分类。'.encode(), '树', kind='AUTHOR')
    identities = [publish_atomic(store, source, '图论树', '图论', '图论树是无环连通图。'),
                  publish_atomic(store, source, '机器学习树', '机器学习', '机器学习树按条件进行分类。')]

    class DomainAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            if operation == 'next.entity_plan':
                cards = user['content']['atomics']
                return {'assignments': [{'atomic': a['ref'], 'target': f'N{i}', 'action': 'CREATE',
                    'object_match': 'SAME_OBJECT', 'reason': '按各自领域建立对象'} for i, a in enumerate(cards)],
                    'new_entities': [{'ref': f'N{i}', 'title': '树', 'domain': a['hints']['subject'],
                        'identity_key': 'tree', 'description': a['hints']['subject']} for i, a in enumerate(cards)]}
            return super().structured_json(operation, user, **kwargs)

    drain(Application(store, agent=DomainAgent(), index_mode='KEYWORD_ONLY'))
    with store.db.engine.connect() as c:
        claims = rows(c, 'SELECT domain,identity_key,concept_id FROM concept_identity_claims')
        routes = rows(c, 'SELECT concept_id,route_json FROM atomic_entity_routes')
        assert {r['identity_key'] for r in claims} == {'tree'}
        assert {r['domain'] for r in claims} == {'图论', '机器学习'}
        assert len({r['concept_id'] for r in routes}) == 2
    for identity in identities:
        with store.db.engine.connect() as c:
            domain = one(c, 'SELECT c.domain FROM atomic_entity_routes r JOIN notes a ON a.current_revision_id=r.atomic_revision_id JOIN notes c ON c.id=r.concept_id WHERE a.id=:id', id=identity)['domain']
        assert domain in store.get_note(identity)['title']


def test_partial_support_removal_does_not_offer_unsupported_old_core_as_writer_evidence(store):  # noqa: F811
    from test_next_entity_concepts import drain, publish_atomic

    source = store.add_source('模型使用基础映射。扩展采用 IRLS 工作响应。'.encode(), '分类模型', kind='AUTHOR')
    publish_atomic(store, source, '基础', '分类模型', '模型使用基础映射。')
    extension = publish_atomic(store, source, '扩展', '分类模型', '扩展采用 IRLS 工作响应。')

    class CoreAgent(Agent):
        def __init__(self):
            super().__init__()
            self.packets = []

        def structured_json(self, operation, user, **kwargs):
            content = user['content']
            if operation == 'next.entity_plan':
                return {'assignments': {a['ref']: {'action': 'CREATE', 'target': 'N0', 'object_match': 'SAME_OBJECT', 'reason': '共同对象'} for a in content['atomics']},
                    'new_entities': [{'ref': 'N0', 'identity_key': 'classifier', 'title': '分类模型', 'domain': 'ml', 'description': '分类模型'}]}
            if operation == 'next.entity_write':
                self.packets.append(content['chapters'])
                return {'chapters': {s['key']: {'markdown': (s.get('accepted_core') or {}).get('markdown', '') + '\n\n' + '\n'.join(f['text'] for f in s['fragments'])} for s in content['chapters']}}
            if operation == 'next.entity_review':
                return {'chapters': {s['key']: {'supported_fragment_ids': [f['fragment_id'] for f in s['fragments']], 'checked_image_ids': [], 'findings': []} for s in content['chapters']}}
            raise AssertionError(operation)

    agent = CoreAgent()
    settings = replace(Settings.from_env(store.root), embedding_api_key=None, llm_api_key=None, vision_api_key=None, mineru_api_token=None)
    app = Application(store, agent=agent, settings=settings, index_mode='KEYWORD_ONLY',
                      job_kinds=('ANALYZE_ATOMIC', 'BUILD_CONCEPT', 'RECONCILE_DEPENDENCIES'))
    drain(app)
    with store.db.engine.connect() as c:
        identity = one(c, "SELECT id FROM notes WHERE kind='CONCEPT'")['id']
    assert 'IRLS' in store.get_note(identity)['markdown']
    revision = store.get_note(extension)['revision']['id']
    store.withdraw_note(extension, expected_revision=revision)
    drain(app)
    assert 'IRLS' not in store.get_note(identity)['markdown']
    last = agent.packets[-1][0]
    assert last['previous'] is None and last['accepted_core'] is None
    assert last['core_rebuild_reason'] == 'SUPPORT_OR_PROOF_CHANGED'
    store.restore_note(extension, expected_revision=revision)
    drain(app)
    assert 'IRLS' in store.get_note(identity)['markdown']
