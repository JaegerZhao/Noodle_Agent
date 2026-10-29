"""Semantic endpoints and evidence ownership are separate model decisions."""

from typing import Literal

from pydantic import Field, model_validator

from ..contracts import Contract
from .entity_contracts import Relation, RoutingDecision
from .identity_keys import reference_keys
from .routing_protocol import expand


class EdgeSupport(Contract):
    atomic_ref: str
    endpoint: Literal["PARENT", "CHILD", "SOURCE", "TARGET"]


class SemanticRelation(Contract):
    kind: Literal["TOPIC", "RELATED_TO", "COMPARES_WITH", 'USES', 'VARIANT_OF']
    parent_ref: str | None = None
    child_ref: str | None = None
    source_ref: str | None = None
    target_ref: str | None = None
    supports: list[EdgeSupport] = Field(min_length=1)
    reason: str = Field(min_length=1)
    position: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def endpoints(self):
        if self.kind == "TOPIC":
            if not self.parent_ref or not self.child_ref or self.source_ref or self.target_ref or any(s.endpoint not in {"PARENT", "CHILD"} for s in self.supports):
                raise ValueError("TOPIC requires parent_ref/child_ref and endpoint PARENT/CHILD support")
        elif not self.source_ref or not self.target_ref or self.parent_ref or self.child_ref or any(s.endpoint not in {"SOURCE", "TARGET"} for s in self.supports):
            raise ValueError("General relations require source_ref/target_ref and SOURCE/TARGET support")
        return self


class CanonicalDecision(RoutingDecision):
    relations: list[SemanticRelation] = Field(default_factory=list)


def expand_canonical(decision, cards, catalog):
    basic = RoutingDecision(assignments=decision.assignments, new_entities=decision.new_entities, identity_updates=decision.identity_updates)
    plan = expand(basic, cards, catalog)
    targets = reference_keys(catalog, decision.new_entities)
    atomics = {f"A{i}": c["id"] for i, c in enumerate(cards)}
    available = {e.identity_key for e in plan.entities} | {targets[f"C{i}"] for i in range(len(catalog))}
    for relation in decision.relations:
        left, right = (relation.parent_ref, relation.child_ref) if relation.kind == "TOPIC" else (relation.source_ref, relation.target_ref)
        left_key, right_key = targets.get(left), targets.get(right)
        reason, supports = None, {"left": [], "right": []}
        if left_key not in available or right_key not in available or left_key == right_key:
            reason = "UNKNOWN_ENDPOINT"
        else:
            for proof in relation.supports:
                identity = atomics.get(proof.atomic_ref)
                side = "left" if proof.endpoint in {"PARENT", "SOURCE"} else "right"
                expected = left_key if side == "left" else right_key
                if identity is None or expected not in {plan.routes[identity].entity_key, *(s.entity_key for s in plan.routes[identity].selected_contributions if s.purpose not in {'CONTEXT_ONLY', 'CONFLICT'})}:
                    reason = "SUPPORT_OWNER_MISMATCH"
                    break
                supports[side].append(identity)
        if reason:
            plan._unresolved_relations.append({"relation": relation.model_dump(), "reason": reason, "status": "SUGGESTED"})
            continue
        # Prefer storing a hierarchy relation on child evidence; direction is
        # derived by the program rather than guessed alongside storage ownership.
        use_right = bool(supports["right"]) if relation.kind == "TOPIC" else not supports["left"]
        owner, target = (right_key, left_key) if use_right else (left_key, right_key)
        plan.relations.append(Relation(source_key=owner, target_key=target,
            navigation_role=("TOPIC_PARENT" if use_right else "TOPIC_CHILD") if relation.kind == "TOPIC" else None,
            relation="LINKS_TO" if relation.kind == "TOPIC" else relation.kind,
            semantic_direction='INCOMING' if use_right and relation.kind not in {'TOPIC', 'RELATED_TO', 'COMPARES_WITH'} else None,
            support_atomic_ids=list(dict.fromkeys(supports["right" if use_right else "left"])),
            target_support_atomic_ids=list(dict.fromkeys(supports["left" if use_right else "right"])),
            reason=relation.reason, position=relation.position))
    return plan
