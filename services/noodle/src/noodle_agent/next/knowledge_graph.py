"""Typed Concept-only graph and deterministic root-directory projections (no UI)."""

from pydantic import Field

from .contracts import Contract, Id, canonical, sha
from .repository import Conflict, InvalidKnowledge


class ConceptNode(Contract):
    id: Id
    title: str
    description: str
    revision: Id


class ConceptEdge(Contract):
    edge_id: str | None = None
    layer: str = 'SEMANTIC'
    direction: str = 'DIRECTED'
    from_section_id: str | None = None
    target_section_id: str | None = None
    target_anchor: str | None = None
    source: Id
    target: Id
    relation: str
    navigation_role: str | None = None
    position: int = 0
    source_revision: Id
    origin_note_id: Id
    support_revision_ids: list[Id] = Field(default_factory=list)
    basis: str


class ConceptGraph(Contract):
    schema_version: str = "concept-graph-v1"
    snapshot: str
    nodes: list[ConceptNode]
    edges: list[ConceptEdge]
    boundary_edges: list[ConceptEdge] = Field(default_factory=list)
    total: int
    truncated: bool
    next_offset: int | None


class OutlineEntry(Contract):
    entry_id: Id
    parent_entry_id: Id | None
    concept_id: Id
    title: str
    depth: int = Field(ge=0)
    reference: bool
    cycle: bool
    has_more: bool


class ConceptOutline(Contract):
    schema_version: str = "concept-outline-v1"
    snapshot: str
    root_concept_id: Id
    entries: list[OutlineEntry]
    expanded_concepts: int
    truncated: bool
    unclassified: list[Id]
    unclassified_total: int
    scope: str = "reachable confirmed organization links; cross-links are not children"


def snapshot_data(store):
    from .graph_projection import projection
    records, relations, _ = projection(store)
    concepts = {key: row for key, row in records.items() if row['kind'] == 'CONCEPT'}
    edges = [ConceptEdge(
        source=e['source'], target=e['target'], relation=e['relation'],
        navigation_role=e['navigation_role'], position=e['position'],
        source_revision=e['source_revision'], origin_note_id=e['origin_note_id'],
        support_revision_ids=e['support_revision_ids'],
        basis=e['basis'].get('basis', '') if isinstance(e['basis'], dict) else e['basis'],
        edge_id=e['edge_id'], layer=e['layer'], direction=e['direction'],
        from_section_id=e['from_section_id'], target_section_id=e['target_section_id'],
        target_anchor=e['target_anchor'])
        for e in relations if e['source'] in concepts and e['target'] in concepts and e['status'] == 'CONFIRMED']
    edges.sort(key=lambda e: (e.source, e.position, e.target, e.relation, e.edge_id))
    nodes = {key: ConceptNode(id=key, title=r['title'], description=r['summary'], revision=r['current_revision_id'])
             for key, r in concepts.items()}
    version = sha(canonical([sorted((n.id, n.title) for n in nodes.values()),
                              [(e.edge_id, e.source, e.target) for e in edges]]))
    return version, nodes, edges


def concept_graph(store, *, center=None, hops=1, limit=100, offset=0, snapshot=None, allowed=None):
    if not 1 <= hops <= 4 or not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("Invalid graph bounds")
    version, nodes, edges = snapshot_data(store)
    if snapshot and snapshot != version:
        raise Conflict("Graph snapshot changed; restart pagination")
    selected = set(nodes)
    if allowed is not None:
        selected &= allowed
    if center:
        if center not in nodes:
            raise InvalidKnowledge("Root Concept is not currently eligible")
        selected = {center}
        frontier = {center}
        for _ in range(hops):
            reached = {e.target for e in edges if e.source in frontier} | {
                e.source for e in edges if e.target in frontier
            }
            frontier = reached - selected
            selected |= frontier
        if allowed is not None:
            selected &= allowed
    ordered = sorted(selected, key=lambda k: (k != center, nodes[k].title, k))
    kept = set(ordered[offset : offset + limit])
    end = offset + limit
    return ConceptGraph(
        snapshot=version,
        nodes=[nodes[k] for k in ordered if k in kept],
        edges=[e for e in edges if e.source in kept and e.target in kept],
        boundary_edges=[
            e
            for e in edges
            if e.source in selected
            and e.target in selected
            and ((e.source in kept) != (e.target in kept))
        ],
        total=len(ordered),
        truncated=end < len(ordered),
        next_offset=end if end < len(ordered) else None,
    ).model_dump()


def concept_outline(store, root, *, max_depth=8, limit=300, snapshot=None, _data=None):
    if not 0 <= max_depth <= 30 or not 1 <= limit <= 3000:
        raise ValueError("Invalid outline bounds")
    version, nodes, edges = _data if _data is not None else snapshot_data(store)
    if snapshot and snapshot != version:
        raise Conflict("Graph snapshot changed; restart outline")
    if root not in nodes:
        raise InvalidKnowledge("Root Concept is not currently eligible")
    children = {}
    for edge in edges:
        if edge.navigation_role == "TOPIC_CHILD":
            children.setdefault(edge.source, []).append(edge.target)
    expanded = set()
    entries = []
    truncated = False
    stack = [(root, None, 0, ())]
    while stack:
        identity, parent, depth, path = stack.pop()
        if len(entries) >= limit:
            truncated = True
            break
        occurrence = "entry_" + sha(canonical([*path, identity]))[:20]
        repeated = identity in expanded
        cycle = identity in path
        values = children.get(identity, [])
        expandable = bool(values) and (depth >= max_depth or repeated or cycle)
        entries.append(
            {
                "entry_id": occurrence,
                "parent_entry_id": parent,
                "concept_id": identity,
                "title": nodes[identity].title,
                "depth": depth,
                "reference": repeated,
                "cycle": cycle,
                "has_more": expandable,
            }
        )
        if repeated or cycle:
            continue
        expanded.add(identity)
        if depth >= max_depth:
            truncated = truncated or bool(values)
            continue
        stack.extend(
            (child, occurrence, depth + 1, (*path, identity)) for child in reversed(values)
        )
    incoming = {e.target for e in edges if e.navigation_role == "TOPIC_CHILD"}
    unclassified = sorted(key for key in nodes if key not in incoming)
    return ConceptOutline(
        snapshot=version,
        root_concept_id=root,
        entries=entries,
        expanded_concepts=len(expanded),
        truncated=truncated,
        unclassified=unclassified[:100],
        unclassified_total=len(unclassified),
    ).model_dump()
