"""Foundation state and format projections share the checked publication boundary."""

import json
import re

from ..contracts import canonical, sha

POLICY = 'foundation-core-v1'


def foundation_state(items, definition, previous_report):
    foundation, supplemental = [], []
    prior = {i for ids in previous_report.get('section_contributions', {}).values() for i in ids}
    previous_state = previous_report.get('foundation', {})
    prior_roles = {r['decision_id']: role for group, role in [('foundation', 'FOUNDATION'), ('supplemental', 'SUPPLEMENT')]
                   for r in previous_state.get(group, [])}
    prior_sources = {r.get('source_revision_id') for r in previous_state.get('foundation', [])}
    needs_rebase = False
    for item in items:
        if item['action'] == 'LINK_ONLY':
            continue
        decision = json.loads(item['decision_json'])
        role = decision.get('source_role', 'UNKNOWN')
        if role == 'UNKNOWN':
            role = prior_roles.get(item['id'], definition.get('_source_role', 'SUPPLEMENT'))
        record = {'decision_id': item['id'], 'atomic_revision_id': item['atomic_revision_id'],
                  'fragment_id': item['fragment_id'], 'source_revision_id': decision.get('source_revision_id')}
        if role == 'FOUNDATION':
            foundation.append(record)
            needs_rebase |= (item['id'] not in prior and item['action'] != 'ADD_EVIDENCE'
                             and decision.get('source_revision_id') not in prior_sources)
        else:
            supplemental.append(record)
    return {'policy': POLICY, 'state': 'FOUNDATION_READY' if foundation else 'PROVISIONAL',
            'foundation': foundation, 'supplemental': supplemental,
            'input_hash': sha(canonical([foundation, supplemental])),
            'rebase': bool(previous_report and needs_rebase)}


def compact_markdown(markdown, title):
    """Remove only wrapper duplicates; do not delete formulas or similar prose."""
    lines, result, fence, math_end = markdown.splitlines(), [], None, None
    owned = {title.strip(), '知识框架'}
    for line in lines:
        stripped = line.strip()
        if fence is None and math_end:
            result.append(line)
            if stripped == math_end:
                math_end = None
            continue
        if fence is None and stripped in {'$$', r'\['}:
            math_end = '$$' if stripped == '$$' else r'\]'
            result.append(line)
            continue
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            result.append(line)
            continue
        heading = re.fullmatch(r'\s*#{1,6}\s+(.+?)\s*#*\s*', line)
        label = re.fullmatch(r'\s*\*\*([^*\n]{1,32})\*\*\s*', line)
        if fence is None and heading and heading.group(1).strip() in owned:
            continue
        if fence is None and label and not re.search(r'[$=\\]', label.group(1)):
            line = '### ' + label.group(1).rstrip('：:')
        result.append(line)
    return '\n'.join(result).strip()


def core_summary(sections, title):
    from ..card_preview import text_excerpt
    for section in sections:
        text=text_excerpt(section.markdown,'')
        if text:
            return text
    return title
