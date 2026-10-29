"""Ephemeral short references; persistent evidence and identities stay program-owned."""

from ..planning import QualityFailure
from .coverage import core_coverage
from .entity_contracts import Entity, EntityPlan, Relation, Route, SelectedContribution
from .identity_keys import reference_keys


def coverage(item):
    definition = item.get("definition", {}).get("scope", {})
    published = item.get("scope", {})
    return {"includes": list(dict.fromkeys(definition.get("includes", []) + published.get("includes", [])))}


def boundary(item):
    definition = item.get('definition', {})
    resolved = definition.get('_identity_boundary')
    if resolved:
        return {**resolved, 'status': 'RESOLVED_OBJECT'}
    return {'title': item['title'], 'domain': item['domain'], 'aliases': item.get('aliases', []),
            'status': 'LEGACY_UNRESOLVED', 'scope': {'includes': [item['title']]}}


def routing_coverage(item):
    value = core_coverage(item)
    if boundary(item)['status'] == 'LEGACY_UNRESOLVED':
        # Checked prose is useful evidence of what the reader already has.
        # Legacy *ownership* stays unknown: prose about another object must not
        # redefine the title or turn a uses relationship into identity.
        return {**value, 'identity_ownership':'UNRESOLVED',
            'identity_use':'DO_NOT_INFER_IDENTITY_FROM_INCLUDED_CONTENT'}
    return value


def packet(cards, catalog):
    return {
        "atomics": [
            {
                "ref": f"A{i}",
                "title": c["title"],
                "description": c["description"],
                "hints": c["hints"],
                'concept_outline': c.get('concept_outline'),
                'figures': c.get('figures', []),
                "sections": [
                    {"ref": f"S{j}", "title": s["title"], **({'excerpt': s['excerpt']} if s.get('excerpt') else {})}
                    for j, s in enumerate(c["sections"])
                ],
                'fragments': [{'ref': f'F{j}', 'section_ref': next((f'S{k}' for k, s in enumerate(c['sections']) if s['id'] == f['section_id']), None),
                               'heading': f.get('heading', ''), 'excerpt': f['excerpt']} for j, f in enumerate(c.get('fragments', []))],
            }
            for i, c in enumerate(cards)
        ],
        "candidates": [
            {
                "ref": f"C{i}",
                "name": boundary(c)['title'],
                "domain": c["domain"],
                "description": boundary(c).get('description', c['title']),
                "aliases": boundary(c).get('aliases', []),
                "scope": boundary(c)['scope'],
                'identity_boundary': boundary(c),
                'scope_is_coverage': False,
                'core_coverage': routing_coverage(c),
                "sections": [s["title"] for s in c.get("sections", [])] if boundary(c)['status'] == 'RESOLVED_OBJECT' else [],
                'internal_topics': c.get('internal_topics', []) if boundary(c)['status'] == 'RESOLVED_OBJECT' else [],
                'checked_core_excerpt': routing_coverage(c)['text'][:900],
                'source_role': c.get('definition', {}).get('_source_role', 'SUPPLEMENT'),
            }
            for i, c in enumerate(catalog)
        ],
    }


def expand(decision, cards, catalog):
    atomics = {f"A{i}": c for i, c in enumerate(cards)}
    candidates = {f"C{i}": c for i, c in enumerate(catalog)}
    entities, issues = {}, []
    targets = reference_keys(catalog, decision.new_entities)
    for e in decision.new_entities:
        if e.ref in entities:
            raise QualityFailure("Duplicate new entity reference: " + e.ref)
        entities[e.ref] = Entity(
            title=e.title,
            domain=e.domain,
            identity_key=targets[e.ref],
            storage_identity_key=e.identity_key if targets[e.ref] != e.identity_key else None,
            description=e.description,
            aliases=e.aliases,
            question=e.title,
            scope={"includes": e.scope_includes or [e.title]},
        )
    routes, unresolved, normalizations = {}, {}, []
    seen = set()
    for assignment in decision.assignments:
        card = atomics.get(assignment.atomic)
        if not card:
            issues.append(
                {
                    "reason": "UNKNOWN_ATOMIC",
                    "assignment": assignment.model_dump(),
                    "status": "REVIEW",
                }
            )
            continue
        identity = card["id"]
        # A complete, explicit evidence distribution does not claim that the
        # source object and the receiving object have the same identity.
        if (assignment.action == 'REUSE' and assignment.object_match == 'USES'
                and assignment.target in candidates and assignment.selections
                and all(s.purpose in {'SUPPORT_ONLY','FURTHER_READING','CONTEXT_ONLY','CONFLICT'}
                        and not s.fragments for s in assignment.selections)
                and {ref for s in assignment.selections for ref in s.sections}
                    == {f'S{i}' for i in range(len(card['sections']))}):
            normalizations.append({'atomic':assignment.atomic,'code':'EXPLICIT_EVIDENCE_DISTRIBUTION',
                'original_action':'REUSE','action':'DISTRIBUTE','target':assignment.target})
            assignment = assignment.model_copy(update={'action':'DISTRIBUTE','target':None})
        target = assignment.target
        error = None
        if identity in seen:
            error = "DUPLICATE_ASSIGNMENT"
        elif assignment.action == "REVIEW":
            error = assignment.reason
        elif assignment.action == "SOURCE_ONLY":
            if target or assignment.sections or assignment.selections:
                error = "SOURCE_ONLY_HAS_TARGET"
        elif assignment.action == 'DISTRIBUTE':
            if target or not assignment.selections:
                error = 'DISTRIBUTION_REQUIRES_SELECTED_TARGETS'
        elif target not in targets:
            error = "UNKNOWN_TARGET"
        elif target in candidates and assignment.object_match in {'USES', 'DIFFERENT_OBJECT', 'UNCERTAIN'}:
            error = 'OBJECT_MATCH_DOES_NOT_ALLOW_REUSE'
        elif target in candidates and candidates[target].get('visibility') == 'WITHDRAWN':
            error = 'TARGET_MANUALLY_WITHDRAWN'
        elif (
            assignment.action == "REUSE"
            and target not in candidates
            or assignment.action == "CREATE"
            and target in candidates
        ):
            normalizations.append({'atomic':assignment.atomic,'target':target,'code':'ACTION_FROM_DECLARED_TARGET',
                'original_action':assignment.action,'action':'REUSE' if target in candidates else 'CREATE'})
        section_refs = {f"S{i}": s["id"] for i, s in enumerate(card["sections"])}
        fragment_refs = {f'F{i}': f['fragment_id'] for i, f in enumerate(card.get('fragments', []))}
        if not set(assignment.sections) <= set(section_refs):
            error = "UNKNOWN_SECTION"
        if any(s.target not in targets or not set(s.sections) <= set(section_refs) or not set(s.fragments) <= set(fragment_refs) for s in assignment.selections):
            error = 'UNKNOWN_CONTRIBUTION_SELECTION'
        if any(s.target in candidates and candidates[s.target].get('visibility') == 'WITHDRAWN' for s in assignment.selections):
            error = 'TARGET_MANUALLY_WITHDRAWN'
        selected_pairs = [(s.target, ref) for s in assignment.selections for ref in [*s.sections, *s.fragments]]
        if len(selected_pairs) != len(set(selected_pairs)):
            error = 'DUPLICATE_CONTRIBUTION_SELECTION'
        seen.add(identity)
        if error:
            unresolved[identity] = error
            routes[identity] = Route(reason=error)
            continue
        for selected_target in dict.fromkeys([target, *(s.target for s in assignment.selections)]):
            if selected_target not in candidates:
                continue
            c = candidates[selected_target]
            d = c.get("definition", {})
            entities[selected_target] = Entity(
                title=c["title"],
                domain=c["domain"],
                identity_key=targets[selected_target],
                storage_identity_key=d.get('identity_key', c['id']) if targets[selected_target] != d.get('identity_key', c['id']) else None,
                question=d.get("question") or c["title"],
                scope=d.get("scope") or {"includes": [c["title"]]},
                description=c.get("summary") or d.get("description") or c["title"],
                aliases=c.get("aliases", []),
                reuse_id=c["id"],
            )
        routes[identity] = Route(
            entity_key=targets.get(target),
            topic=assignment.topic,
            topic_key=assignment.topic_key if target in candidates and any(t['key'] == assignment.topic_key for t in candidates[target].get('internal_topics', [])) else '',
            contribution_mode=assignment.contribution_mode,
            text_contribution=assignment.text_contribution,
            media_contribution=assignment.media_contribution,
            object_match=assignment.object_match or ('SAME_OBJECT' if target in targets and target not in candidates else None),
            outline_action=assignment.outline_action,
            outline_revision_reason=assignment.outline_revision_reason,
            selected_contributions=[SelectedContribution(entity_key=targets[s.target],
                section_ids=[section_refs[ref] for ref in s.sections],
                fragment_ids=[fragment_refs[ref] for ref in s.fragments], purpose=s.purpose, reason=s.reason)
                for s in assignment.selections],
            reason=assignment.reason,
            sections={
                section_id: assignment.sections.get(ref, assignment.role)
                for ref, section_id in section_refs.items()
            }
            if target
            or assignment.selections
            else {},
        )
    for identity in (c["id"] for c in cards):
        if identity not in routes:
            unresolved[identity] = "MISSING_ASSIGNMENT"
            routes[identity] = Route(reason="MISSING_ASSIGNMENT")
    used = {r.entity_key for r in routes.values() if r.entity_key}
    used.update(s.entity_key for r in routes.values() for s in r.selected_contributions)
    normalizations.extend(
        {"code": "UNUSED_DECLARATION_OMITTED", "ref": ref, 'network_call': False}
        for ref, e in entities.items()
        if not e.reuse_id and e.identity_key not in used
    )
    relations = [
        Relation(
            **{
                **r.model_dump(),
                "source_key": targets.get(r.source_key, r.source_key),
                "target_key": targets.get(r.target_key, r.target_key),
                "support_atomic_ids": [
                    atomics[a]["id"] if a in atomics else a for a in r.support_atomic_ids
                ],
            }
        )
        for r in decision.relations
    ]
    result = EntityPlan(
        entities=[e for e in entities.values() if e.identity_key in used],
        routes=routes,
        relations=relations,
    )
    result._unresolved_atomics = unresolved
    result._protocol_issues = issues
    result._normalizations = normalizations
    for update in decision.identity_updates:
        if update.ref not in candidates:
            result._protocol_issues.append({'reason': 'UNKNOWN_IDENTITY_UPDATE', 'ref': update.ref})
            continue
        item = candidates[update.ref]
        data = {**update.model_dump(exclude={'ref'}), 'concept_id': item['id'],
                'identity_key': targets[update.ref], 'domain': item['domain']}
        result.identity_updates.append(data)
        result.entities[:] = [e.model_copy(update={'title': update.title, 'description': update.description,
                              'aliases': update.aliases, 'scope': update.scope})
                              if e.reuse_id == item['id'] else e for e in result.entities]
    return result
