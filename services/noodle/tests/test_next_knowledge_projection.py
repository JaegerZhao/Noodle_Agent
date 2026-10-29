"""Tree completeness and graph provenance remain independent of display budgets."""

from types import SimpleNamespace

from test_next_workspace import atomic, publish, store  # noqa: F401

from noodle_agent.next.contracts import MapEntry, Section, sha
from noodle_agent.next.editor import edit_section
from noodle_agent.next.graph_projection import graph, markdown_adjacency
from noodle_agent.next.knowledge_roots import tree
from noodle_agent.next.knowledge_tree import forest
from noodle_agent.next.navigation import frozen_candidate


def test_forest_does_not_expand_shared_objects_repeatedly_or_cross_hidden_domain():
    nodes = {str(i): SimpleNamespace(title=str(i)) for i in range(500)}
    edge = lambda a, b: SimpleNamespace(source=a, target=b, navigation_role='TOPIC_CHILD')
    edges = [edge('0', str(i)) for i in range(1, 300)] + [edge('400', str(i)) for i in range(1, 300)]
    projected = forest('all', nodes, edges, set(nodes))
    primary = [e for e in projected if not e['reference']]
    assert len(primary) == len({e['concept_id'] for e in projected}) == 500
    assert all(e['child_count'] == 0 for e in projected if e['reference'])
    assert next(e for e in projected if e['concept_id'] == '1')['parent_entry_id'] is None
    assert len(next(e for e in projected if e['concept_id'] == '1')['alternative_parent_ids']) == 2
    scoped = forest('domain', nodes, [edge('0', '1'), edge('1', '2')], {'0', '2'})
    assert all(e['parent_entry_id'] is None for e in scoped)
    moved = forest('all', nodes, [edge('400', '1')], set(nodes))
    assert {e['concept_id']: e['entry_id'] for e in moved if not e['reference']} == {e['concept_id']: e['entry_id'] for e in projected if not e['reference']}


def test_default_concept_graph_preserves_an_explicit_atomic_center_inside_scope(store):  # noqa: F811
    note,_=atomic(store)
    value=graph(store,note['note_id'],mode='semantic')
    assert [n['id'] for n in value['nodes']]==[note['note_id']]
    assert graph(store,mode='semantic')['nodes']==[]


def test_personal_tree_reports_atomic_kind_and_withdrawal_without_deleting_entry(store):  # noqa: F811
    note, _ = atomic(store)
    identity = store.content.create_note('MOC', '我的体系', creation_key='knowledge-root:fixture')
    from noodle_agent.next.contracts import Candidate, Scope
    value = Candidate(note_id=identity, title='我的体系', scope=Scope(includes=['阅读']), sections=[
        Section(section_id='intro', title='阅读目录', question='如何浏览', scope=Scope(includes=['阅读']), markdown='个人知识目录。')],
        map_policy='MANUAL', map_entries=[MapEntry(entry_id='atomic-entry', title='来源笔记', target_note_id=note['note_id'])])
    publish(store, value)
    before = tree(store, identity, limit=1)
    assert before['entries'][0]['target_kind'] == 'ATOMIC'
    assert before['entries'][0]['concept_id'] is None
    store.withdraw_note(note['note_id'], expected_revision=note['revision_id'])
    after = tree(store, identity, limit=1)
    assert after['entries'][0]['availability'] == 'WITHDRAWN'
    assert not after['entries'][0]['available'] and after['entries'][0]['entry_id'] == 'atomic-entry'
    assert before['snapshot'] != after['snapshot']


def test_forest_uses_nearest_parent_when_other_parent_is_its_ancestor():
    nodes = {n: SimpleNamespace(title=n) for n in ['a', 'b', 'c', 'other']}
    edge = lambda a, b: SimpleNamespace(source=a, target=b, navigation_role='TOPIC_CHILD')
    result = forest('all', nodes, [edge('a', 'b'), edge('b', 'c'), edge('a', 'c')], set(nodes))
    by_id = {e['concept_id']: e for e in result}
    assert by_id['c']['parent_entry_id'] == by_id['b']['entry_id']
    assert by_id['c']['depth'] == 2
    assert by_id['c']['alternative_parent_ids'] == ['a']
    cyclic = forest('all', nodes, [edge('a', 'b'), edge('b', 'a'), edge('a', 'c'), edge('b', 'c')], set(nodes))
    assert len(cyclic) == len(nodes)
    assert next(e for e in cyclic if e['concept_id']=='c')['parent_entry_id'] is None


def test_graph_preserves_two_anchors_and_ignores_export_derived_footer(store):  # noqa: F811
    a, _ = atomic(store, '链接起点')
    b, _ = atomic(store, '链接目标')
    edit_section(store, a['note_id'], expected_revision=a['revision_id'], section_id='s0',
        markdown='阅读 [[链接目标#条件]] 和 [[链接目标#原理]]。')
    body = b'[[real]]\n<!-- noodle:derived -->\n[[derived]]'
    store.put('artifacts/adjacency-fixture.md', body)
    assert [r['target'] for r in markdown_adjacency(str(store.root), 'artifacts/adjacency-fixture.md', sha(body))] == ['real']
    result = graph(store, layers=['DOCUMENT_LINK'], connected_only=True)
    assert {e['target_anchor'] for e in result['edges']} == {'条件', '原理'}
    assert len(result['edges']) == 2 and len({e['edge_id'] for e in result['edges']}) == 2
    page = graph(store, layers=['DOCUMENT_LINK'], limit=1)
    assert page['boundary_edges'] and page['boundary_nodes']
    old = frozen_candidate(store, store.get_note(b['note_id'])['revision']['id'])
    publish(store, old.model_copy(update={'summary': '正文摘要变化'}), store.get_note(b['note_id'])['revision']['id'])
    assert graph(store, layers=['DOCUMENT_LINK'])['snapshot'] == result['snapshot']
    assert graph(store, layers=['DOCUMENT_LINK'])['content_snapshot'] != result['content_snapshot']


def test_provenance_has_original_source_and_isolated_center_remains_visible(store):  # noqa: F811
    note, source = atomic(store)
    lone = graph(store, center=note['note_id'], connected_only=True, layers=['DOCUMENT_LINK'])
    assert [n['id'] for n in lone['nodes']] == [note['note_id']]
    origins = graph(store, center=note['note_id'], mode='provenance', hops=2)
    raw = next(n for n in origins['nodes'] if n['kind']=='SOURCE')
    assert raw['source_revision_id'] == source['revision_id']
    assert any(e['source']==note['note_id'] and e['target']==raw['id'] for e in origins['edges'])


def test_graph_search_includes_unpaginated_matches(store):  # noqa: F811
    atomic(store, '甲节点')
    target, _ = atomic(store, '最后一个节点', parts=('独立目标。',))
    result = graph(store, q='最后一个', limit=1)
    assert [n['id'] for n in result['nodes']] == [target['note_id']]
