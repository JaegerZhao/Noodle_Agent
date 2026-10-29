"""Token-bounded identity cards; never shorten Atomic descriptions or evidence."""

import copy

from ..contracts import canonical, sha
from ..segmentation import tokens
from .catalogue import ranked
from .coverage import omitted_coverage
from .entity_contracts import ROLE_TITLES
from .routing_protocol import packet

POLICY = 'routing-packet-budget-v1'


def bounded_packet(cards, catalog, context, budget=24000, maximum=None):
    maximum = max(budget, maximum if maximum is not None else budget)
    original = {**packet(cards, catalog), 'allowed_roles': ROLE_TITLES, 'source_context': context}
    original_tokens = tokens(canonical(original))
    if original_tokens <= budget:
        return catalog, original, {'policy': POLICY, 'original_tokens': original_tokens,
            'tokens': original_tokens, 'target_tokens':budget, 'maximum_tokens':maximum,
            'expanded_for_relevant_evidence':False,
            'catalog_total': len(catalog), 'catalog_selected': len(catalog), 'compact': False}
    outlined = {(c.get('concept_outline') or {}).get('concept_key') for c in cards} - {None, ''}
    names = {name.casefold() for c in cards for name in [c['title'],
        (c.get('concept_outline') or {}).get('concept_title', ''),
        c.get('hints', {}).get('subject', '')] if name}
    bound_refs = {b['concept_ref'] for b in context.get('same_source_bindings', [])
        if b.get('resolved_binding', {}).get('source_hint_key') in outlined}
    mandatory = [c for i, c in enumerate(catalog) if c.get('definition', {}).get('identity_key') in outlined
        or f'C{i}' in bound_refs
        or names.intersection(name.casefold() for name in [c['title'], *c.get('aliases', [])])]
    priority = {c['id']: c for c in mandatory}
    # No extra embedding or pre-routing model call.
    rankings = [ranked(catalog, {'title': c['title'], 'text': c['description']}) for c in cards]
    for index in range(len(catalog)):
        for ranking in rankings:
            item = ranking[index]
            priority.setdefault(item['id'], item)
    old_refs = {c['id']: f'C{i}' for i, c in enumerate(catalog)}

    def compile(selected, details=()):
        value = packet(cards, selected)
        for item, candidate in zip(selected, value['candidates'], strict=True):
            candidate.pop('checked_core_excerpt', None)
            if item['id'] not in details:
                candidate['core_coverage'] = omitted_coverage(candidate['core_coverage'])
            candidate.pop('sections', None)
            candidate['details_omitted'] = ['sections'] if item['id'] in details else ['core_excerpt', 'sections']
        projected = copy.deepcopy(context)
        refs = {old_refs[c['id']]: f'C{i}' for i, c in enumerate(selected)}
        projected['same_source_bindings'] = [dict(b, concept_ref=refs[b['concept_ref']])
            for b in context.get('same_source_bindings', []) if b['concept_ref'] in refs]
        projected['candidate_catalog'] = {'total': len(catalog), 'selected': len(selected),
            'compact_identity_cards': True, 'omitted_candidates_do_not_prove_absence': True}
        projected['checked_foundation_candidates'] = [
            {'ref':candidate['ref'], 'name':candidate['name'],
             'purpose':'Compare the existing foundation object before reusing a narrower supplemental variant'}
            for candidate in value['candidates'] if candidate.get('source_role')=='FOUNDATION'
            and candidate['core_coverage'].get('complete')]
        return {**value, 'allowed_roles': ROLE_TITLES, 'source_context': projected}

    selected = list(mandatory)
    payload = compile(selected)
    # Business cost targets are advisory. Required identities and relevant
    # complete cores must survive even when their legitimate size exceeds them.
    # Reserve actual coverage for plausible targets *before* filling the packet
    # with unrelated identities. Otherwise a nearly full catalogue silently
    # starves the very evidence needed to distinguish duplicate vs net-new.
    likely = dict.fromkeys([c['id'] for c in mandatory]
        + [c['id'] for ranking in rankings for c in ranking[:2]])
    details = set()
    by_id = {c['id']:c for c in catalog}
    for identity in likely:
        trial_selected = selected if identity in {c['id'] for c in selected} else [*selected, by_id[identity]]
        trial = compile(trial_selected, details | {identity})
        selected, payload = trial_selected, trial
        if packet([], [by_id[identity]])['candidates'][0]['core_coverage']['text']:
            details.add(identity)
    for item in priority.values():
        if item['id'] in {c['id'] for c in selected}:
            continue
        trial = compile([*selected, item], details)
        if tokens(canonical(trial)) <= budget:
            selected.append(item)
            payload = trial
    receipt = {'policy': POLICY, 'original_tokens': original_tokens, 'tokens': tokens(canonical(payload)),
        'budget_policy':'OBSERVE_CONTINUE', 'diagnosis':{'required_evidence_retained':True, 'unrelated_fill_limited':True,
            'exceeded_previous_maximum':tokens(canonical(payload))>maximum},
        'target_tokens':budget, 'maximum_tokens':maximum,
        'expanded_for_relevant_evidence':tokens(canonical(payload))>budget,
        'catalog_total': len(catalog), 'catalog_selected': len(selected), 'compact': True,
        'catalog_sha256': sha(canonical(catalog)), 'selected_ids': [c['id'] for c in selected],
        'core_excerpts':len(details), 'coverage_priority':True, 'coverage_preserved_ids':sorted(details)}
    return selected, payload, receipt
