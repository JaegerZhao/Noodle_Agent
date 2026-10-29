"""Versioned system-edge corrections; author relations remain under author control."""

import json

from .contracts import Link, canonical, sha
from .repository import rows


def key(owner, link):
    return sha(canonical([owner, link.target_note_id, link.relation, link.navigation_role,
                          link.from_section_id, link.target_section_id, link.semantic_direction]))


def apply_overrides(store, owner, links, *, connection=None):
    if connection is None:
        with store.db.engine.connect() as c:
            records = rows(c, 'SELECT * FROM relation_overrides WHERE owner_note_id=:id', id=owner)
    else:
        records = rows(connection, 'SELECT * FROM relation_overrides WHERE owner_note_id=:id', id=owner)
    operations = {r['relation_key']: json.loads(r['operation_json']) for r in records}
    result = []
    for link in links:
        operation = operations.get(key(owner, link)) if link.origin != 'AUTHOR' else None
        if operation:
            if operation.get('replacement'):
                replacement = Link.model_validate(operation['replacement'])
                if replacement not in result:
                    result.append(replacement)
        elif link not in result:
            result.append(link)
    return result
