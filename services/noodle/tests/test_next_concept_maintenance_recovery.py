"""Recovery keeps source objects coherent and cannot silently claim a global review."""

from noodle_agent.next.concepts.maintenance_run import source_batches
from noodle_agent.next.concepts.routing_budget import bounded_packet
from noodle_agent.next.organization import neighborhood


def test_source_batches_follow_outline_order_and_keep_shared_object_together():
    members = [{'note_id': identity, 'source_revision_id': 'source', 'chapter_key': chapter,
                'outline_key': key, 'source_position': position}
               for identity, chapter, key, position in [('random-a', '10', 'multi', 3),
                   ('random-z', '10', 'binary', 0), ('random-b', '13', 'mlp', 5),
                   ('random-c', '10', 'binary', 1), ('random-d', '10', 'multi', 2)]]
    cards = {m['note_id']: {'id': m['note_id']} for m in members}
    batches = source_batches(members, cards, limit=2)
    assert [[m['note_id'] for m in b] for b in batches] == [
        ['random-z', 'random-c'], ['random-d', 'random-a'], ['random-b']]
    assert {m['note_id'] for b in batches for m in b} == set(cards)


def test_compact_packet_keeps_resolved_binding_even_with_a_different_canonical_key():
    cards = [{'id': 'a', 'title': '扩展', 'description': '扩展', 'hints': {}, 'sections': [],
              'concept_outline': {'concept_key': 'source-alias'}}]
    catalog = [{'id': str(i), 'title': str(i), 'domain': 'ml', 'aliases': [], 'sections': [],
                'definition': {'identity_key': 'canonical-' + str(i)}} for i in range(30)]
    context = {'same_source_bindings': [{'concept_ref': 'C29', 'resolved_binding': {
        'source_hint_key': 'source-alias', 'entity_key': 'canonical-29'}}]}
    selected, packet, receipt = bounded_packet(cards, catalog, context, budget=1600)
    assert receipt['compact'] and len(selected) < len(catalog)
    assert any(c['id'] == '29' for c in selected)
    binding = packet['source_context']['same_source_bindings'][0]
    assert packet['candidates'][int(binding['concept_ref'][1:])]['name'] == '29'


def test_neighborhood_excludes_unrelated_source_evidence_and_retains_neighbors():
    graph = {'snapshot': 's', 'nodes': [{'id': str(i), 'title': '无关对象' + str(i), 'domain': 'ml'} for i in range(12)],
             'edges': [{'source': '0', 'target': '11', 'relation': 'USES'}]}
    result = neighborhood(graph, {'0'})
    assert result['snapshot'] == 's'
    assert {'0', '11'} <= {n['id'] for n in result['nodes']}
    assert len(result['nodes']) < len(graph['nodes'])
    assert result['edges'] == graph['edges']


def test_routing_reserves_relevant_checked_coverage_before_unrelated_identity_cards():
    cards=[{'id':'atomic','title':'核 SVM','description':'支持向量机的核技巧与软间隔',
        'hints':{'subject':'支持向量机'},'sections':[]}]
    core='核技巧与软间隔已包含于支持向量机框架。'*35
    catalog=[{'id':'svm','title':'支持向量机','domain':'机器学习','aliases':['SVM'],
        'definition':{'identity_key':'svm'},'sections':[{'title':'知识框架','markdown':core}]}]
    catalog += [{'id':str(i),'title':'无关对象'+str(i),'domain':'其他','aliases':[],
        'definition':{'identity_key':'other'+str(i)},'sections':[]} for i in range(100)]
    selected,payload,receipt=bounded_packet(cards,catalog,{},budget=5000)
    target=next(c for c in payload['candidates'] if c['name']=='支持向量机')
    assert receipt['coverage_priority'] and 'svm' in receipt['coverage_preserved_ids']
    assert target['core_coverage']['text']==core and target['core_coverage']['complete']
    assert target['identity_boundary']['status']=='LEGACY_UNRESOLVED'
    assert target['identity_boundary']['scope']['includes']==['支持向量机']
    assert len(selected)<len(catalog)


def test_routing_soft_budget_expands_for_required_core_without_filling_unrelated_cards():
    cards=[{'id':'a','title':'SVM','description':'支持向量机', 'hints':{},'sections':[]}]
    core='核技巧、硬间隔和软间隔属于同一方法。'*100
    catalog=[{'id':'svm','title':'SVM','domain':'ml','aliases':[],
        'definition':{'identity_key':'svm','_source_role':'FOUNDATION'},
        'sections':[{'title':'知识框架','markdown':core}]}]
    catalog += [{'id':str(i),'title':'无关对象'+str(i),'domain':'其他','aliases':[],
        'definition':{'identity_key':'other'+str(i)},'sections':[]} for i in range(40)]
    _,payload,receipt=bounded_packet(cards,catalog,{},budget=1600,maximum=6000)
    candidate=next(c for c in payload['candidates'] if c['name']=='SVM')
    assert candidate['core_coverage']['text']==core
    assert receipt['expanded_for_relevant_evidence']
    assert receipt['target_tokens'] < receipt['tokens'] <= receipt['maximum_tokens']
    assert payload['source_context']['checked_foundation_candidates'][0]['ref']==candidate['ref']
    assert receipt['catalog_selected'] < 10


def test_exact_identity_reuse_does_not_merge_objects_via_a_generic_alias():
    from noodle_agent.next.concepts.entity_contracts import EntityPlan
    from noodle_agent.next.concepts.object_normalization import normalize
    data = {'entities': [{'identity_key': 'mlp', 'title': '多层感知机', 'question': 'MLP', 'description': '多层前馈模型',
        'domain': 'ml', 'scope': {'includes': ['MLP']}, 'aliases': ['MLP', '前馈网络']}],
        'routes': {'a': {'entity_key': 'mlp', 'reason': 'MLP'}}}
    plan = EntityPlan.model_validate(data)
    catalog = [{'id': 'canonical-mlp', 'title': '多层感知机', 'domain': 'ml', 'definition': {'identity_key': 'multilayer_perceptron'}},
        {'id': 'feedforward', 'title': '前馈网络', 'domain': 'ml', 'definition': {'identity_key': 'feedforward'}}]
    normalize(plan, catalog)
    assert len(plan.entities) == 1 and plan.entities[0].reuse_id == 'canonical-mlp'
    assert plan.routes['a'].entity_key == 'multilayer_perceptron'
    assert plan.entities[0].aliases == ['MLP']
    assert {n['code'] for n in plan._normalizations} == {'EXACT_CANONICAL_OBJECT_REUSED', 'AMBIGUOUS_ALIAS_OMITTED'}


def test_format_maintenance_preserves_atomic_evidence_and_is_idempotent(tmp_path):
    from test_next_workspace import atomic, concept

    from noodle_agent.next.concepts.format_maintenance import apply
    from noodle_agent.next.navigation import frozen_candidate
    from noodle_agent.next.store import Store
    store = Store(tmp_path / 'library')
    try:
        source, _ = atomic(store, title='MLP', parts=('# MLP\n\n**核心机制**\n\n多层非线性映射。',))
        generated = concept(store, source, title='MLP')
        before = frozen_candidate(store, generated['revision_id'])
        first = apply(store, tmp_path)
        assert len(first['changed']) == 1 and first['model_calls'] == 0
        after = frozen_candidate(store, store.get_note(generated['note_id'])['revision']['id'])
        assert after.sections[0].markdown.startswith('### 核心机制')
        assert after.sections[0].inputs == before.sections[0].inputs
        assert after.sections[0].evidence_ids == before.sections[0].evidence_ids
        assert store.get_note(source['note_id'])['revision']['id'] == source['revision_id']
        assert apply(store, tmp_path)['changed'] == []
    finally:
        store.close()


def test_same_storage_key_and_title_in_different_domains_keep_distinct_targets():
    from noodle_agent.next.concepts.entity_context import apply_outline
    from noodle_agent.next.concepts.entity_contracts import RoutingDecision
    from noodle_agent.next.concepts.entity_routing import validate_plan
    from noodle_agent.next.concepts.routing_protocol import expand

    cards = [{'id': f'a{i}', 'revision': f'r{i}', 'title': '树', 'sections': [{'id': 'main'}]} for i in range(2)]
    catalog = [{'id': f'c{i}', 'title': '树', 'domain': domain, 'aliases': [],
                'definition': {'identity_key': 'tree'}} for i, domain in enumerate(['图论', '机器学习'])]
    decision = RoutingDecision.model_validate({'assignments': [{'atomic': f'A{i}', 'target': f'C{i}',
        'action': 'REUSE', 'object_match': 'SAME_OBJECT', 'reason': '按领域分别复用'} for i in range(2)]})
    plan = expand(decision, cards, catalog)
    apply_outline(plan, cards, {}, catalog)
    validate_plan(plan, cards, catalog)
    assert len(plan.entities) == 2 and {e.reuse_id for e in plan.entities} == {'c0', 'c1'}
    assert {e.storage_identity_key for e in plan.entities} == {'tree'}
    assert len({r.entity_key for r in plan.routes.values()}) == 2


def test_new_objects_with_same_key_in_different_domains_have_distinct_routes():
    from noodle_agent.next.concepts.entity_contracts import RoutingDecision
    from noodle_agent.next.concepts.routing_protocol import expand

    cards = [{'id': f'a{i}', 'title': '树', 'sections': [{'id': 'main'}]} for i in range(2)]
    decision = RoutingDecision.model_validate({'assignments': [{'atomic': f'A{i}', 'target': f'N{i}',
        'action': 'CREATE', 'reason': '两个领域的不同对象'} for i in range(2)],
        'new_entities': [{'ref': f'N{i}', 'title': '树', 'domain': d, 'identity_key': 'tree', 'description': d}
            for i, d in enumerate(['图论', '机器学习'])]})
    plan = expand(decision, cards, [])
    assert len({e.identity_key for e in plan.entities}) == 2
    assert {e.storage_identity_key for e in plan.entities} == {'tree'}
