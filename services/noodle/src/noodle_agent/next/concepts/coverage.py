"""Revision-bound coverage from actual prose, never from planned scope claims."""

from ..contracts import canonical, sha

POLICY = 'core-coverage-v1'


def core_coverage(item, limit=6000):
    sections = [s for s in item.get('sections', []) if not s.get('planned')]
    body = '\n\n'.join(s.get('markdown', '') for s in sections)
    valid = item.get('valid', bool(body)) and item.get('visibility', 'ACTIVE') == 'ACTIVE'
    excerpt = body[:limit] if valid else ''
    return {
        'policy': POLICY,
        'revision_id': item.get('revision_id'),
        'body_sha256': item.get('markdown_sha256') or sha(body),
        'coverage_unknown': not valid or not body or len(body) > limit,
        'complete': bool(valid and body and len(body) <= limit),
        'sections': [{'section_id': s.get('section_id'), 'title': s['title']} for s in sections],
        'text': excerpt,
        'projection_sha256': sha(canonical([item.get('revision_id'), excerpt])),
    }


def omitted_coverage(value):
    return {**value, 'text': '', 'complete': False, 'coverage_unknown': True,
            'omission_reason': 'ROUTING_PACKET_BUDGET'}
