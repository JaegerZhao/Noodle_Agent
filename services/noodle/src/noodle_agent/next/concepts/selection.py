"""Compile sparse section selections over frozen fragments, without resplitting."""


def contributions(value, manifest, primary):
    route = value['route']
    selections = route.get('selected_contributions', [])
    default_purpose = 'SUPPORT_ONLY' if route.get('contribution_mode') == 'EVIDENCE' else 'CORE_DELTA'
    if route.get('text_contribution', 'AUTO') != 'AUTO':
        default_purpose = route['text_contribution']
    for fragment in manifest:
        matches = [s for s in selections if fragment['section_id'] in s.get('section_ids', []) or fragment.get('fragment_id') in s.get('fragment_ids', [])]
        if not matches:
            yield primary, default_purpose if primary else 'CONTEXT_ONLY', fragment, route['reason']
        for selection in matches:
            yield value['mapping'][selection['entity_key']], selection['purpose'], fragment, selection['reason']
