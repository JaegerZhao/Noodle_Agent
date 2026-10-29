"""Action labels are redundant with declared targets; never change the target."""

import pytest

from noodle_agent.next.concepts.entity_contracts import RoutingDecision
from noodle_agent.next.concepts.entity_routing import validate_plan
from noodle_agent.next.concepts.routing_protocol import expand


@pytest.mark.parametrize('action,target,expected',[('CREATE','C0','REUSE'),('REUSE','N0','CREATE')])
def test_known_target_compiles_redundant_action_without_replanning(action,target,expected):
    cards=[{'id':'atomic','sections':[{'id':'body'}]}]
    catalog=[{'id':'existing','title':'Method','domain':'ML','definition':{'identity_key':'method'}}]
    raw={'assignments':[{'atomic':'A0','action':action,'target':target,'reason':'Explains this method'}]}
    if target=='N0':
        raw['new_entities']=[{'ref':'N0','title':'New method','domain':'ML','identity_key':'new-method','description':'A distinct supported method'}]
    decision=RoutingDecision.model_validate(raw)
    plan=expand(decision,cards,catalog)
    validate_plan(plan,cards,catalog)
    assert not plan._unresolved_atomics and not plan._protocol_issues
    assert plan._normalizations==[{'atomic':'A0','target':target,'code':'ACTION_FROM_DECLARED_TARGET','original_action':action,'action':expected}]
    assert decision.assignments[0].target==target and decision.assignments[0].action==action
    assert plan.entities[0].reuse_id==('existing' if target=='C0' else None)


@pytest.mark.parametrize('action,target,reason',[('CREATE','C9','UNKNOWN_TARGET'),('SOURCE_ONLY','C0','SOURCE_ONLY_HAS_TARGET')])
def test_unknown_or_semantically_contradictory_target_is_not_guessed(action,target,reason):
    decision=RoutingDecision.model_validate({'assignments':[{'atomic':'A0','action':action,'target':target,'reason':'Contradictory decision'}]})
    plan=expand(decision,[{'id':'atomic','sections':[{'id':'body'}]}],[{'id':'existing','title':'Method','domain':'ML','definition':{'identity_key':'method'}}])
    assert plan._unresolved_atomics=={'atomic':reason} and plan._normalizations==[]
