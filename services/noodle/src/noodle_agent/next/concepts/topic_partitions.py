"""Bound growing internal topics without splitting facts or inventing more entities."""

from ..contracts import sha
from ..segmentation import tokens


def partition_topics(grouped, *, budget=6500, protected=()):
    result = {}
    for key, entries in grouped.items():
        if not key.startswith('entity_topic_') or key in protected:
            result[key] = entries
            continue
        parts, current, cost, seen = [], [], 0, set()
        for entry in entries:
            fragment = entry[1]
            extra = 0 if fragment['sha256'] in seen else tokens(fragment['text'])
            if current and cost + extra > budget:
                parts.append(current)
                current, cost, seen = [], 0, set()
                extra = tokens(fragment['text'])
            current.append(entry)
            cost += extra
            seen.add(fragment['sha256'])
        if current:
            parts.append(current)
        for index, part in enumerate(parts):
            part_key = key if index == 0 else key + '_' + sha(part[0][1]['fragment_id'])[:10]
            if index:
                part = [(item, f, {**decision, 'section_title': (decision.get('section_title') or f['title']) + f'（{index + 1}）'}) for item, f, decision in part]
            result[part_key] = part
    return result
