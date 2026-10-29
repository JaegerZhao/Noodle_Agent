"""Navigation uses the publication rules in bulk and reflects withdrawal immediately."""

from test_next_workspace import atomic, concept, store  # noqa: F401

from noodle_agent.next.navigation import current_notes, refresh_maps
from noodle_agent.next.repository import rows


def test_removed_source_map_exits_navigation_and_returns_only_when_restored(store):  # noqa: F811
    from test_next_workspace import publish

    from noodle_agent.next.contracts import Candidate, Scope, Section
    _, source=atomic(store)
    identity=store.content.create_note('MOC','来源目录',creation_key=f"source-map:{source['revision_id']}:source-sections")
    scope=Scope(includes=['导航'])
    publish(store,Candidate(note_id=identity,title='来源目录',scope=scope,sections=[Section(section_id='overview',title='导航',question='如何阅读？',scope=scope,markdown='来源阅读入口。')]))
    assert identity in {n['id'] for n in current_notes(store)['items']}
    store.set_source_deleted(source['source_id'],True)
    assert identity not in {n['id'] for n in current_notes(store)['items']}
    store.set_source_deleted(source['source_id'],False)
    assert identity in {n['id'] for n in current_notes(store)['items']}


def reference(workspace):
    with workspace.db.engine.connect() as c:
        return {n['id'] for n in rows(c,'SELECT * FROM notes') if n['current_revision_id'] and workspace.content.valid_revision(c,n['current_revision_id'])}


def test_bulk_navigation_preserves_validity_and_does_not_recurse(store, monkeypatch):  # noqa: F811
    a, source = atomic(store)
    concept(store,a)
    refresh_maps(store)
    expected=reference(store)
    with monkeypatch.context() as m:
        m.setattr(store.content,'valid_revision',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Per-note recursion')))
        assert {n['id'] for n in current_notes(store)['items']}==expected
    store.set_source_deleted(source['source_id'],True)
    expected=reference(store)
    result=current_notes(store)
    assert {n['id'] for n in result['items']}==expected
    assert all(n['kind']=='MOC' for n in result['items'])


def test_bulk_navigation_keeps_search_paging_and_policy_guards(store):  # noqa: F811
    atomic(store,'甲梯度');atomic(store,'乙梯度')
    result=current_notes(store,kind='ATOMIC',q='梯度',offset=1,limit=1)
    assert result['total']==2 and len(result['items'])==1
    assert current_notes(store,kind='CONCEPT')['total']==0
    store.repository.activate_policy('new-policy',expected_policy=store.repository.policy_version)
    assert current_notes(store)['total']==0
