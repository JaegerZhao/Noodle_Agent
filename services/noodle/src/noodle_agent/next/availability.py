"""Shared current/pinned navigation availability; history is never RAG evidence."""

from .repository import one


def target_state(store, connection, note_id, revision_id=None):
    if not note_id:
        return {'available': False, 'availability': 'GROUP', 'target_kind': None, 'target_mode': 'CURRENT'}
    note = one(connection, 'SELECT * FROM notes WHERE id=:id', id=note_id)
    if not note:
        return {'available': False, 'availability': 'MISSING', 'target_kind': None, 'target_mode': 'PINNED' if revision_id else 'CURRENT'}
    pinned = revision_id and revision_id != note['current_revision_id']
    if pinned:
        found = one(connection, 'SELECT id FROM note_revisions WHERE id=:r AND note_id=:id', r=revision_id, id=note_id)
        return {'available': bool(found), 'availability': 'HISTORICAL' if found else 'MISSING',
                'target_kind': note['kind'], 'target_mode': 'PINNED', 'resolved_revision_id': revision_id}
    valid = bool(note['current_revision_id'] and store.content.valid_revision(connection, note['current_revision_id']))
    head = one(connection, 'SELECT status FROM note_revisions WHERE id=:r', r=note['current_revision_id']) if note['current_revision_id'] else None
    return {'available': valid,
            'availability': 'AVAILABLE' if valid else 'WITHDRAWN' if note['visibility'] == 'WITHDRAWN' or head and head['status'] == 'WITHDRAWN' else 'UPDATING',
            'target_kind': note['kind'], 'target_mode': 'PINNED' if revision_id else 'CURRENT',
            'resolved_revision_id': note['current_revision_id']}
