"""Membership, stable forest occurrences and version-bound pagination."""

from .availability import target_state
from .contracts import canonical, sha
from .knowledge_graph import snapshot_data
from .knowledge_roots import roots
from .navigation import current_notes
from .repository import Conflict, InvalidKnowledge


def membership(store, identity, *, _notes=None):
    notes = _notes if _notes is not None else current_notes(store, limit=100000)['items']
    root = next((r for r in roots(store, _notes=notes)['items'] if r['id'] == identity), None)
    if root is None:
        raise InvalidKnowledge('Unknown knowledge root')
    by_id = {n['id']: n for n in notes}
    if root['kind'] == 'PERSONAL':
        entries = store.get_note(identity)['map_entries']
        selected = {e['target_note_id'] for e in entries if e['target_note_id'] in by_id and by_id[e['target_note_id']]['kind']=='CONCEPT'
                    and (not e['target_revision_id'] or e['target_revision_id'] == by_id[e['target_note_id']]['current_revision_id'])}
    else:
        selected = {n['id'] for n in notes if n['kind'] == 'CONCEPT' and
                    (root['kind'] != 'DOMAIN' or n['domain'] == root['title'] or n['domain'].startswith(root['title'] + '/'))}
    return root, selected


def forest(root_id, nodes, edges, selected):
    """Prefer the nearest supported parent; preserve unrelated alternatives in Graph."""
    parents = {n: set() for n in selected}
    for edge in edges:
        if edge.navigation_role == 'TOPIC_CHILD' and edge.source in selected and edge.target in selected:
            parents[edge.target].add(edge.source)
    def ancestors(node):
        found, pending = set(), list(parents[node])
        while pending:
            current = pending.pop()
            if current not in found:
                found.add(current)
                pending.extend(parents[current] - found)
        return found

    chosen = {}
    for node, candidates in parents.items():
        if len(candidates) == 1:
            chosen[node] = next(iter(candidates))
        elif candidates:
            # A->B->C plus A->C is one clear reading path, not two
            # competing classifications. Genuine multi-domain parents remain roots.
            nearest = [p for p in candidates if candidates - {p} <= ancestors(p)]
            if len(nearest) == 1:
                chosen[node] = nearest[0]
    cycles, done = set(), set()
    for node in sorted(selected):
        path, seen, current = [], {}, node
        while current in chosen and current not in done:
            if current in seen:
                cycles.update(path[seen[current]:])
                break
            seen[current] = len(path)
            path.append(current)
            current = chosen[current]
        done.update(path)
    for node in cycles:
        chosen.pop(node, None)
    children = {}
    for node in selected:
        children.setdefault(chosen.get(node), []).append(node)
    for values in children.values():
        values.sort(key=lambda n: (nodes[n].title, n))
    def entry_id(node):
        return 'entry_' + sha(canonical([root_id, node]))[:24]
    result, stack = [], [(n, 0) for n in reversed(children.get(None, []))]
    while stack:
        node, depth = stack.pop()
        parent = chosen.get(node)
        result.append({'entry_id': entry_id(node), 'parent_entry_id': entry_id(parent) if parent else None,
            'entry_kind': 'NOTE', 'concept_id': node, 'target_note_id': node, 'target_kind': 'CONCEPT',
            'target_mode': 'CURRENT', 'target_revision_id': None, 'title': nodes[node].title,
            'depth': depth, 'position': len(result), 'available': True, 'availability': 'AVAILABLE',
            'reference': False, 'cycle': node in cycles, 'has_more': False,
            'child_count': len(children.get(node, [])),
            'alternative_parent_ids': sorted(parents[node] - ({parent} if parent else set()))})
        stack.extend((child, depth + 1) for child in reversed(children.get(node, [])))
    by_node = {entry['concept_id']: entry for entry in result}
    for node in sorted(selected):
        primary = chosen.get(node)
        for parent in sorted(parents[node] - ({primary} if primary else set())):
            # Ancestor shortcuts are already represented by the nearest path.
            if node in cycles or parent in cycles or primary and parent in ancestors(primary):
                continue
            result.append({**by_node[node], 'entry_id': 'ref_' + sha(canonical([root_id, parent, node]))[:24],
                'parent_entry_id': entry_id(parent), 'depth': by_node[parent]['depth']+1,
                'position': len(result), 'reference': True, 'primary_entry_id': entry_id(node), 'child_count': 0})
            by_node[parent]['child_count'] += 1
    return result


def tree(store, identity, *, limit=300, offset=0, snapshot=None, parent_entry_id=None):
    if not 1 <= limit <= 3000 or offset < 0:
        raise ValueError('Invalid tree bounds')
    _, nodes, edges = snapshot_data(store)
    notes = current_notes(store, limit=100000)['items']
    root, selected = membership(store, identity, _notes=notes)
    if root['kind'] == 'PERSONAL':
        note = store.get_note(identity)
        with store.db.engine.connect() as c:
            entries = [{**e, **target_state(store, c, e['target_note_id'], e['target_revision_id'])}
                       for e in note['map_entries']]
        counts = {}
        for entry in entries:
            counts[entry['parent_entry_id']] = counts.get(entry['parent_entry_id'], 0) + 1
        entries = [{**e, 'concept_id': e['target_note_id'] if e['target_kind'] == 'CONCEPT' else None,
                    'entry_kind': 'NOTE' if e['target_note_id'] else 'GROUP',
                    'child_count': counts.get(e['entry_id'], 0), 'reference': False, 'cycle': False, 'has_more': False}
                   for e in entries]
    else:
        entries = forest(identity, nodes, edges, selected & set(nodes))
    structural = [{k: v for k, v in e.items() if k not in {'map_revision_id', 'resolved_revision_id'}} for e in entries]
    version = sha(canonical([identity, root.get('revision_id'), structural]))
    if snapshot and snapshot != version:
        raise Conflict('Tree snapshot changed; restart pagination')
    if parent_entry_id is not None:
        entries = [e for e in entries if (e['parent_entry_id'] or '') == parent_entry_id]
    total = len(entries)
    page = entries[offset:offset + limit]
    loaded = {e['entry_id'] for e in page}
    for entry in page:
        entry['has_more'] = any(e['parent_entry_id'] == entry['entry_id'] and e['entry_id'] not in loaded for e in entries)
    return {'schema_version': 'knowledge-tree-v2', 'snapshot': version, 'root': root,
            'entries': page, 'total': total, 'member_total': len(selected),
            'truncated': offset + limit < total, 'next_offset': offset + limit if offset + limit < total else None}
