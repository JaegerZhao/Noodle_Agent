"""Resolve exact identifiers and ambiguous aliases without another model decision."""

import re

from .entity_contracts import Entity
from .identity_keys import reference_keys


def name(value):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', value.casefold())


def normalize(plan, catalog):
    replacements, entities = {}, {}
    refs = reference_keys(catalog)
    for entity in plan.entities:
        exact = [c for c in catalog if c['domain'] == entity.domain and (
            c.get('definition', {}).get('identity_key') == (entity.storage_identity_key or entity.identity_key)
            or name(c['title']) == name(entity.title))]
        if not entity.reuse_id and len(exact) == 1:
            target = exact[0]
            stored = target.get('definition', {}).get('identity_key', target['id'])
            key = refs[f'C{catalog.index(target)}']
            replacements[entity.identity_key] = key
            entity = Entity(**{**entity.model_dump(), 'identity_key': key, 'storage_identity_key': stored if key != stored else None, 'reuse_id': target['id']})
            plan._normalizations.append({'code': 'EXACT_CANONICAL_OBJECT_REUSED', 'concept_id': target['id'], 'identity_key': key})
        # A model-proposed alias equal to another canonical object's name is
        # ambiguous, not proof that the two objects should merge.
        others = {name(c['title']) for c in catalog if c['domain'] == entity.domain and c['id'] != entity.reuse_id}
        others.update(name(e.title) for e in plan.entities if e.domain == entity.domain and e.identity_key != entity.identity_key)
        aliases = [a for a in entity.aliases if name(a) not in others]
        if aliases != entity.aliases:
            plan._normalizations.append({'code': 'AMBIGUOUS_ALIAS_OMITTED', 'identity_key': entity.identity_key,
                'aliases': [a for a in entity.aliases if a not in aliases], 'network_call': False})
            entity = entity.model_copy(update={'aliases': aliases})
            for update in plan.identity_updates:
                if update['concept_id'] == entity.reuse_id:
                    update['aliases'] = aliases
        entities.setdefault(entity.identity_key, entity)
    for identity, route in plan.routes.items():
        plan.routes[identity] = route.model_copy(update={
            'entity_key': replacements.get(route.entity_key, route.entity_key),
            'selected_contributions': [s.model_copy(update={'entity_key': replacements.get(s.entity_key, s.entity_key)})
                for s in route.selected_contributions]})
    plan.relations[:] = [r.model_copy(update={'source_key': replacements.get(r.source_key, r.source_key),
        'target_key': replacements.get(r.target_key, r.target_key)}) for r in plan.relations]
    plan.entities[:] = list(entities.values())
