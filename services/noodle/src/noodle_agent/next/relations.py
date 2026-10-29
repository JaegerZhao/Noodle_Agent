"""Lossless author relation editing; display edges are never write contracts."""

import json

from .contracts import Review, canonical
from .navigation import frozen_candidate
from .repository import Conflict, InvalidKnowledge, one, rows

CONTRACT = 'owned-links-v1'


def managed(link):
    return bool(link.navigation_role or link.support_revision_ids or link.origin == 'SYSTEM')


def owned_links(store, identity):
    note = store.get_note(identity)
    revision = note['revision']['id']
    candidate = frozen_candidate(store, revision)
    with store.db.engine.connect() as c:
        records = rows(c, 'SELECT l.id,n.title target_title FROM links l JOIN notes n ON n.id=l.target_note_id WHERE l.from_revision_id=:r ORDER BY l.rowid', r=revision)
    if len(records) != len(candidate.links):
        raise InvalidKnowledge('Relation publication proof differs from stored links')
    return {'editor_contract': CONTRACT, 'note_id': identity, 'expected_revision': revision,
            'links': [{'edge_id': record['id'], 'owner_note_id': identity, 'target_title': record['target_title'],
                       'editable': not managed(link), 'value': link.model_dump()}
                      for record, link in zip(records, candidate.links, strict=True)]}


def update_author_links(store, identity, expected_revision, links, *, editor_contract):
    if editor_contract != CONTRACT:
        raise Conflict('Reload the relation editor: display graphs cannot be saved as owned links')
    note = store.get_note(identity)
    if note['revision']['id'] != expected_revision:
        raise Conflict('Note revision changed')
    candidate = frozen_candidate(store, expected_revision)
    if any(managed(link) for link in links):
        raise InvalidKnowledge('System relations and evidence are read-only in this editor')
    # Preserve unknown legacy author fields and ordering; do not republish a no-op.
    previous = [link for link in candidate.links if not managed(link)]
    if [l.model_dump(exclude={'origin'}) for l in previous] == [l.model_dump(exclude={'origin'}) for l in links]:
        return {'note_id': identity, 'revision_id': expected_revision, 'unchanged': True}
    authored = [link.model_copy(update={'origin': 'AUTHOR'}) for link in links]
    encoded = [canonical(l.model_dump()) for l in authored]
    if len(encoded) != len(set(encoded)):
        raise InvalidKnowledge('Duplicate author relation')
    updated = candidate.model_copy(update={'links': [l for l in candidate.links if managed(l)] + authored})
    with store.db.engine.connect() as c:
        proof = one(c, 'SELECT storage_ref,sha256 FROM build_artifacts WHERE storage_ref=:r',
                    r=f'artifacts/builds/{expected_revision}.json')
    old = json.loads(store.read(proof['storage_ref'], proof['sha256']))

    def guard(c):
        if not store.content.valid_revision(c, expected_revision):
            raise Conflict('Relation source support changed')

    def accepted(c, revision):
        c.exec_driver_sql("UPDATE contribution_decisions SET applied_revision_id=? WHERE concept_id=? AND applied_revision_id=? AND status='ACCEPTED'",
                          (revision, identity, expected_revision))

    return store.publish(updated, expected_revision=expected_revision, guard=guard, accepted=accepted,
        review=Review(candidate_sha256=updated.fingerprint, policy_version=updated.policy_version,
            reviewer='author.relations', passed=True,
            report={**old['review']['report'], 'metadata_only': True,
                    'prior_revision': expected_revision, 'relation_editor_contract': CONTRACT}))
