"""Persist explained object bindings without rewriting the frozen source outline."""

from ..contracts import canonical, sha
from .entity_contracts import Entity
from .identity_keys import reference_keys

POLICY = 'resolved-object-binding-v1'


def revised(value):
    # Explicit actions take precedence over explanatory prose. Older records
    # retain their conservative interpretation for compatibility.
    return value.get('outline_action') == 'REVISE' if value.get('outline_action') else bool(value.get('outline_revision_reason'))


def resolve_bindings(plan, cards, context, catalog):
    entities = {e.identity_key: e for e in plan.entities}
    refs = reference_keys(catalog)
    by_key = {refs[f'C{i}']: c for i, c in enumerate(catalog)}
    known = {}
    for item in context.get('same_source_bindings', []):
        binding = item.get('resolved_binding', {})
        key = binding.get('source_hint_key')
        if key and not revised(binding):
            known.setdefault(key, set()).add(binding['entity_key'])
    outlines = {p['plan_key']: p.get('concept_outline') for p in context.get('topics', [])}
    for card in cards:
        route = plan.routes.get(card['id'])
        if route and route.entity_key is None and route.selected_contributions:
            binding = {'policy': POLICY, 'boundary_policy': context.get('policy'),
                'atomic_revision_id': card.get('revision'), 'outline_sha256': context.get('outline_sha256'),
                'entity_keys': sorted({s.entity_key for s in route.selected_contributions
                    if s.purpose not in {'CONTEXT_ONLY', 'CONFLICT'}}), 'action': 'DISTRIBUTE', 'reason': route.reason}
            plan.routes[card['id']] = route.model_copy(update={'binding': {**binding, 'version': sha(canonical(binding))}})
            continue
        if not route or route.object_match is None and not route.selected_contributions or route.entity_key not in entities:
            continue
        hint = card.get('concept_outline') or outlines.get(card.get('plan_key')) or {}
        key = hint.get('concept_key')
        prior = known.get(key, set())
        # A confirmed shared object survives batch arrival order. A deliberate
        # outline revision stays local rather than poisoning the shared binding.
        if key and route.object_match in {'SAME_OBJECT', 'INTERNAL_VARIANT'} and not revised(route.model_dump()) and len(prior) == 1:
            target = next(iter(prior))
            if target != route.entity_key and (target in by_key or target in entities):
                existing = by_key.get(target)
                if existing is None or existing.get('visibility', 'ACTIVE') == 'ACTIVE':
                    definition = existing.get('definition', {}) if existing else {}
                    if existing:
                        entities[target] = Entity(title=existing['title'], domain=existing['domain'],
                            identity_key=target, description=definition.get('description') or existing['title'],
                            storage_identity_key=definition.get('identity_key') if target != definition.get('identity_key') else None,
                            question=definition.get('question') or existing['title'],
                            scope=definition.get('scope') or {'includes': [existing['title']]},
                            aliases=existing.get('aliases', []), reuse_id=existing['id'])
                    old = route.entity_key
                    route = route.model_copy(update={'entity_key': target,
                        'selected_contributions': [selection.model_copy(update={'entity_key': target})
                            if selection.entity_key == old else selection for selection in route.selected_contributions]})
                    plan._normalizations.append({'code': 'RESOLVED_OBJECT_BINDING',
                        'atomic_id': card['id'], 'from': old, 'to': target})
        binding = {'policy': POLICY, 'entity_key': route.entity_key,
            'boundary_policy': context.get('policy'),
            'source_hint_key': key, 'atomic_revision_id': card.get('revision'),
            'outline_sha256': context.get('outline_sha256'), 'object_match': route.object_match,
            'outline_action': route.outline_action,
            'outline_revision_reason': route.outline_revision_reason, 'reason': route.reason}
        plan.routes[card['id']] = route.model_copy(update={'binding': {**binding, 'version': sha(canonical(binding))}})
        if key and not revised(route.model_dump()):
            known.setdefault(key, set()).add(route.entity_key)
    plan.entities[:] = list(entities.values())
