"""Relations can change type without changing body, and remain corrected on rebuild."""

from dataclasses import replace

from test_next_entity_concepts import Agent, course, drain, store  # noqa: F401

from noodle_agent.config import Settings
from noodle_agent.next.concepts.structure_maintenance import request
from noodle_agent.next.graph_projection import graph
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.relation_overrides import apply_overrides
from noodle_agent.next.repository import rows
from noodle_agent.next.runtime import Application


def test_relation_reclassification_preserves_body_owner_and_future_override(store):  # noqa: F811
    course(store)
    drain(Application(store, agent=Agent(), index_mode='KEYWORD_ONLY'))
    with store.db.engine.connect() as c:
        edge = rows(c, "SELECT n.id owner,n.current_revision_id,l.id,l.target_note_id FROM links l JOIN notes n ON n.current_revision_id=l.from_revision_id WHERE json_extract(l.basis_json,'$.navigation_role') IS NOT NULL ORDER BY l.id")[0]
    before = frozen_candidate(store, edge['current_revision_id'])

    class RevisingAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            if operation == 'next.concept_maintenance_structure':
                return {'changes': [{'edge_id': e['edge_id'], 'action': 'REPLACE', 'relation': 'USES',
                    'direction': 'INCOMING', 'reason': '来源表达使用关系而非目录包含'} for e in user['content']['edges']]}
            return super().structured_json(operation, user, **kwargs)

    request(store, [edge['owner']], 'fixture-structure')
    settings = replace(Settings.from_env(store.root), embedding_api_key=None, llm_api_key=None)
    app = Application(store, settings=settings, agent=RevisingAgent(), index_mode='KEYWORD_ONLY', job_kinds=('MAINTAIN_CONCEPT_STRUCTURE',))
    outcome = app.run_once('revise')
    assert outcome['status'] == 'SUCCEEDED', outcome
    after = frozen_candidate(store, store.get_note(edge['owner'])['revision']['id'])
    assert after.sections == before.sections
    assert after.summary == before.summary
    assert apply_overrides(store, edge['owner'], before.links) == after.links
    assert all(link.relation == 'USES' and link.semantic_direction == 'INCOMING' for link in after.links)
    displayed = graph(store, layers=['SEMANTIC'])
    assert any(e['source'] == edge['target_note_id'] and e['target'] == edge['owner'] and e['origin_note_id'] == edge['owner'] for e in displayed['edges'])
