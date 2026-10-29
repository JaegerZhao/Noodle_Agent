"""One current graph with explicit layers, anchors, provenance and bounded pages."""

import json
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from .contracts import canonical, sha
from .eligibility import eligible
from .markdown_links import references, resolve_reference
from .repository import Conflict, InvalidKnowledge, rows

_projections = OrderedDict()


@lru_cache(maxsize=2048)
def markdown_adjacency(root, ref, digest):
    base = Path(root).resolve()
    path = (base / ref).resolve()
    if not path.is_relative_to(base):
        raise InvalidKnowledge('Markdown adjacency escaped library')
    data = path.read_bytes()
    if sha(data) != digest:
        raise InvalidKnowledge('Markdown adjacency hash differs')
    return references(data.decode().split('<!-- noodle:derived -->', 1)[0])


def projection(store, *, include_suggestions=False):
    with store.db.engine.connect() as c:
        epoch = c.exec_driver_sql('SELECT validity_epoch FROM storage_contract WHERE id=1').scalar_one()
    cache_key = (str(store.root), epoch, include_suggestions)
    if cache_key in _projections:
        return _projections[cache_key]
    from .navigation import current_notes
    notes = current_notes(store, limit=100000)['items']
    by_id = {n['id']: n for n in notes}
    with store.db.engine.connect() as c:
        valid = {r['revision_id'] for r in eligible(c)}
        links = rows(c, 'SELECT l.*,n.id source_id FROM links l JOIN notes n ON n.current_revision_id=l.from_revision_id')
        entries = rows(c, 'SELECT m.*,n.id source_id FROM map_entries m JOIN notes n ON n.current_revision_id=m.map_revision_id WHERE m.target_note_id IS NOT NULL')
        inputs = rows(c, 'SELECT i.*,n.id source_id,a.note_id target_note_id FROM concept_inputs i JOIN notes n ON n.current_revision_id=i.concept_revision_id JOIN note_revisions a ON a.id=i.atomic_revision_id')
        sections = {(s['note_id'], s['section_id']) for s in rows(c, 'SELECT n.id note_id,s.section_id FROM notes n JOIN sections s ON s.revision_id=n.current_revision_id')}
    edges, unresolved = [], []

    def append(source, target, relation, layer, basis, revision, *, owner=None, navigation_role=None,
               from_section_id=None, target_section_id=None, anchor=None, supports=(), position=0, status='CONFIRMED'):
        if source not in by_id or target not in by_id:
            return
        key = [source, target, relation, layer, owner or source, navigation_role, from_section_id, target_section_id, anchor]
        edges.append({'edge_id': 'edge_' + sha(canonical(key))[:24], 'source': source, 'target': target,
            'relation': relation, 'layer': layer, 'direction': 'SYMMETRIC' if relation in {'RELATED_TO', 'COMPARES_WITH', 'CONTRADICTS'} else 'DIRECTED',
            'status': status, 'basis': basis, 'revision': revision, 'source_revision': revision,
            'origin_note_id': owner or source, 'navigation_role': navigation_role,
            'from_section_id': from_section_id, 'target_section_id': target_section_id, 'target_anchor': anchor,
            'support_revision_ids': list(supports), 'position': position})

    for note in notes:
        if note['kind'] == 'MOC':
            continue
        for ref in markdown_adjacency(str(store.root), note['markdown_ref'], note['markdown_sha256']):
            try:
                target = resolve_reference(ref['target'], note, notes)
            except Conflict:
                unresolved.append({'source': note['id'], 'target_text': ref['target'], 'revision': note['current_revision_id']})
                continue
            anchor = unquote(ref['target'].partition('#')[2]) or None
            append(note['id'], target['id'], 'LINKS_TO', 'DOCUMENT_LINK',
                   {'markdown_range': [ref['start'], ref['end']]}, note['current_revision_id'], anchor=anchor)
    for link in links:
        if link['status'] == 'SUGGESTED' and not include_suggestions:
            continue
        basis = json.loads(link['basis_json'])
        if any(r not in valid for r in basis.get('support_revision_ids', [])):
            continue
        if link['target_section_id'] and (link['target_note_id'], link['target_section_id']) not in sections:
            continue
        reverse = basis.get('navigation_role') == 'TOPIC_PARENT' or basis.get('semantic_direction') == 'INCOMING'
        source, target = (link['target_note_id'], link['source_id']) if reverse else (link['source_id'], link['target_note_id'])
        reading = basis.get('origin') == 'SYSTEM' and link['relation'] == 'LINKS_TO' and not basis.get('navigation_role') and by_id.get(target, {}).get('kind') == 'ATOMIC'
        append(source, target, link['relation'], 'SUPPORT' if reading else 'SEMANTIC',
               {**basis, **({'purpose': 'FURTHER_READING'} if reading else {})}, link['from_revision_id'],
               owner=link['source_id'], navigation_role='TOPIC_CHILD' if basis.get('navigation_role') else None,
               from_section_id=link['from_section_id'], target_section_id=link['target_section_id'],
               supports=basis.get('support_revision_ids', []), position=basis.get('position') or 0, status=link['status'])
    for entry in entries:
        target = by_id.get(entry['target_note_id'])
        if target and (not entry['target_revision_id'] or entry['target_revision_id'] == target['current_revision_id']):
            append(entry['source_id'], target['id'], 'MAP_ENTRY', 'MEMBERSHIP', {'entry': entry['entry_id']},
                   entry['map_revision_id'], anchor=entry['entry_id'], position=entry['position'])
    for item in inputs:
        if item['atomic_revision_id'] in valid:
            selected_sections = json.loads(item['selection_json'])
            append(item['source_id'], item['target_note_id'], 'DERIVED_FROM', 'SUPPORT',
                   {'type': 'concept_input', 'atomic_revision_id': item['atomic_revision_id'], 'source_section_ids': selected_sections}, item['concept_revision_id'],
                   from_section_id=item.get('section_id'), target_section_id=selected_sections[0] if len(selected_sections) == 1 else None,
                   supports=[item['atomic_revision_id']])
    unique = {}
    for edge in edges:
        key = edge['edge_id']
        if key in unique:
            prior = unique[key]
            prior['support_revision_ids'] = sorted(set(prior['support_revision_ids'] + edge['support_revision_ids']))
            prior.setdefault('basis_details', [prior['basis']])
            if edge['basis'] not in prior['basis_details']:
                prior['basis_details'].append(edge['basis'])
        else:
            unique[key] = edge
    value = by_id, sorted(unique.values(), key=lambda e: e['edge_id']), unresolved
    _projections[cache_key] = value
    while len(_projections) > 8:
        _projections.popitem(last=False)
    return value


def source_projection(store, original, original_edges):
    nodes, edges = dict(original), list(original_edges)
    with store.db.engine.connect() as c:
        sources = {r['id']: dict(r) for r in rows(c, '''SELECT s.*,coalesce(m.display_title,s.title) display_title
            FROM sources s LEFT JOIN source_library_metadata m ON m.source_id=s.id WHERE s.deleted_at IS NULL''')}
        evidence = rows(c, '''SELECT n.id atomic_id,r.id atomic_revision,e.source_revision_id,s.id source_id,
            se.section_id,e.page_number FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id
            JOIN note_revisions r ON r.id=se.revision_id JOIN notes n ON n.current_revision_id=r.id
            JOIN sources s ON s.current_revision_id=e.source_revision_id WHERE n.kind='ATOMIC' AND s.deleted_at IS NULL''')
    grouped = {}
    for item in evidence:
        if item['atomic_id'] not in nodes or item['source_id'] not in sources:
            continue
        group = grouped.setdefault((item['atomic_id'], item['source_id']), {'pages': set(), 'sections': set(), 'revision': item['atomic_revision']})
        if item['page_number']:
            group['pages'].add(item['page_number'])
        group['sections'].add(item['section_id'])
    for (atomic_id, source_id), value in grouped.items():
        source = sources[source_id]
        identity = 'source:' + source_id
        nodes[identity] = {'id': identity, 'title': source['display_title'], 'kind': 'SOURCE',
                           'current_revision_id': source['current_revision_id'], 'source_revision_id': source['current_revision_id']}
        edges.append({'edge_id': 'edge_' + sha(canonical([atomic_id, identity, 'source-evidence']))[:24],
            'source': atomic_id, 'target': identity, 'relation': 'DERIVED_FROM', 'layer': 'SUPPORT',
            'direction': 'DIRECTED', 'status': 'CONFIRMED', 'navigation_role': None,
            'source_revision': value['revision'], 'origin_note_id': atomic_id, 'support_revision_ids': [value['revision']],
            'basis': {'type': 'source_evidence', 'pages': sorted(value['pages']), 'section_ids': sorted(value['sections'])}})
    return nodes, edges


def graph(store, center=None, *, include_suggestions=False, limit=500, offset=0,
          snapshot=None, allowed=None, layers=None, node_kinds=None, hops=1, connected_only=False,
          mode=None, relations=None, q=''):
    if not 1 <= limit <= 100000 or offset < 0 or not 1 <= hops <= 4:
        raise ValueError('Invalid graph bounds')
    nodes, edges, unresolved = projection(store, include_suggestions=include_suggestions)
    if mode == 'provenance':
        nodes, edges = source_projection(store, nodes, edges)
        layers = ['SUPPORT']
        if allowed is not None:
            allowed = set(allowed) | {e['target'] for e in edges if e['source'] in allowed and nodes[e['target']]['kind']=='SOURCE'}
    elif mode in {'hierarchy', 'semantic'}:
        layers = ['SEMANTIC']
        if node_kinds is None:
            node_kinds = ['CONCEPT']
        if mode == 'hierarchy':
            edges = [e for e in edges if e.get('navigation_role')]
    elif mode == 'document':
        layers = ['DOCUMENT_LINK']
    selected = set(nodes) if allowed is None else set(nodes) & set(allowed)
    if node_kinds:
        selected = {n for n in selected if nodes[n]['kind'] in node_kinds or n == center}
    if layers:
        edges = [e for e in edges if e['layer'] in layers]
    if relations:
        edges = [e for e in edges if e['relation'] in relations]
    edges = [e for e in edges if e['source'] in selected and e['target'] in selected]
    if connected_only:
        selected &= {n for e in edges for n in (e['source'], e['target'])} | ({center} if center else set())
    hierarchy = [e for e in edges if e.get('navigation_role')]
    parents = {e['target'] for e in hierarchy}
    children = {e['source'] for e in hierarchy}
    roles = {n: 'SOURCE' if nodes[n]['kind']=='SOURCE' else 'ROOT' if n in children and n not in parents else
             'MIDDLE' if n in children and n in parents else 'LEAF' if n in parents else 'ISOLATED' for n in selected}
    if q:
        selected = {n for n in selected if q.casefold() in nodes[n]['title'].casefold()} | ({center} if center in selected else set())
    if center:
        if center not in selected:
            raise InvalidKnowledge('Center is outside the active graph scope')
        reached, frontier = {center}, {center}
        for _ in range(hops):
            adjacent = {e['source'] for e in edges if e['target'] in frontier} | {e['target'] for e in edges if e['source'] in frontier}
            frontier = adjacent - reached
            reached |= frontier
        selected &= reached
        edges = [e for e in edges if e['source'] in selected and e['target'] in selected]
    edges = [e for e in edges if e['source'] in selected and e['target'] in selected]
    version = sha(canonical([sorted((n, nodes[n]['title'], nodes[n]['kind']) for n in selected),
                             [[e['edge_id'], e['status']] for e in edges]]))
    if snapshot and snapshot != version:
        raise Conflict('Graph snapshot changed; restart pagination')
    ordered = sorted(selected, key=lambda n: (n != center, nodes[n]['title'], n))
    kept = set(ordered[offset:offset + limit])
    boundary = [e for e in edges if (e['source'] in kept) != (e['target'] in kept)]
    boundary_ids = {n for e in boundary for n in [e['source'], e['target']]} - kept
    def node(n):
        return {'id': n, 'title': nodes[n]['title'], 'kind': nodes[n]['kind'], 'revision': nodes[n]['current_revision_id'],
                'role': roles.get(n, 'ISOLATED'), **({'source_revision_id': nodes[n]['source_revision_id']} if nodes[n]['kind']=='SOURCE' else {})}
    return {'schema_version': 'knowledge-graph-v2', 'snapshot': version,
            'topology_snapshot': version, 'content_snapshot': sha(canonical([
                sorted((n, nodes[n]['current_revision_id'], nodes[n]['title']) for n in selected),
                [(e['edge_id'], e.get('source_revision'), e.get('basis')) for e in edges]])),
            'nodes': [node(n) for n in ordered if n in kept], 'edges': [e for e in edges if e['source'] in kept and e['target'] in kept],
            'boundary_edges': boundary, 'boundary_nodes': [node(n) for n in sorted(boundary_ids)],
            'unresolved': [u for u in unresolved if u['source'] in kept], 'total': len(ordered),
            'truncated': offset + limit < len(ordered), 'next_offset': offset + limit if offset + limit < len(ordered) else None}
