"""Bounded semantic neighborhoods cover every requested root without a full-library prompt."""

from .contracts import canonical
from .segmentation import tokens


def neighborhoods(context, *, budget=15000):
    from .concepts.catalogue import terms
    nodes = {n['id']: n for n in context['concepts']}
    vocabulary = {k: terms(n['title'] + ' ' + n.get('description', '')) for k, n in nodes.items()}
    relations = context['relations']

    def packet(seeds):
        selected = set(seeds)
        for seed in seeds:
            selected.update(e['target'] if e['source'] == seed else e['source'] for e in relations if seed in {e['source'], e['target']})
            candidates = sorted((k for k in nodes if k != seed), key=lambda k: (-len(vocabulary[seed] & vocabulary[k]), k))
            selected.update(k for k in candidates[:3] if vocabulary[seed] & vocabulary[k])
        supports = {r for k in selected for r in context['inputs'].get(k, [])}
        return {'concepts': [n for n in context['concepts'] if n['id'] in selected],
            'relations': [e for e in relations if e['source'] in selected and e['target'] in selected],
            'roots': seeds, 'inputs': {k: context['inputs'][k] for k in selected},
            'atomic_cards': [a for a in context['atomic_cards'] if a['revision'] in supports],
            'scope': 'These requested roots and their bounded candidate neighbors; omitted nodes are not asserted absent.'}

    def split(seeds):
        value = packet(seeds)
        if tokens(canonical(value)) <= budget:
            return [value]
        if len(seeds) > 1:
            mid = len(seeds) // 2
            return split(seeds[:mid]) + split(seeds[mid:])
        # A single heavily supported entity keeps all its identity and support
        # references; descriptions are selected by bounded neighboring input groups.
        cards, result = value['atomic_cards'], []
        for start in range(0, len(cards), 12):
            result.append({**value, 'atomic_cards': cards[start:start + 12], 'scope': value['scope'] + ' Atomic descriptions are partitioned; inputs retains the complete support identity set.'})
        return result or [value]

    return [p for start in range(0, len(context['roots']), 12) for p in split(context['roots'][start:start + 12])]


def review_partitioned(store, calls, context, nodes, supported, roots, existing):
    from .organization import OrganizationCheck, OrganizationPlan, RootReason
    from .organization_v2 import review
    links, reasons, edges = {}, {}, set(existing)
    packets = neighborhoods(context)
    for packet in packets:
        plan, _ = review(store, calls, packet, nodes, supported, packet['roots'], edges)
        for link in plan.links:
            parent, child = (link.owner_note_id, link.target_note_id) if link.navigation_role == 'TOPIC_CHILD' else (link.target_note_id, link.owner_note_id)
            edges.add((parent, child))
            links[(parent, child)] = link
        reasons.update({r.note_id: r.reason for r in plan.independent_roots})
    children = {child for _, child in links}
    result = OrganizationPlan(links=list(links.values()), independent_roots=[RootReason(note_id=r, reason=reasons.get(r, '未在本轮有界邻域发现来源支持的上级，保留独立入口')) for r in roots if r not in children])
    return result, OrganizationCheck(passed=True, checked_root_ids=roots, checked_link_indexes=list(range(len(result.links))), review_mode='BOUNDED_NEIGHBORHOODS_' + str(len(packets)))
