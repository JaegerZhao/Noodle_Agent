"""One SQL eligibility policy for indexing, retrieval and dependency maintenance."""

from sqlalchemy import bindparam, text

VALID = """
WITH atomic_valid AS (
 SELECT r.id FROM note_revisions r JOIN notes n ON n.id=r.note_id
 WHERE n.kind='ATOMIC' AND n.visibility='ACTIVE' AND n.current_revision_id=r.id
 AND r.status='PUBLISHED' AND r.policy_version=(SELECT active_policy FROM storage_contract WHERE id=1)
 AND EXISTS(SELECT 1 FROM section_evidence se WHERE se.revision_id=r.id)
 AND NOT EXISTS(SELECT 1 FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id
 JOIN source_revisions sr ON sr.id=e.source_revision_id JOIN sources s ON s.id=sr.source_id
 WHERE se.revision_id=r.id AND (s.deleted_at IS NOT NULL OR s.current_revision_id!=sr.id))
), valid AS (
 SELECT id FROM atomic_valid UNION ALL
 SELECT r.id FROM note_revisions r JOIN notes n ON n.id=r.note_id
 WHERE n.kind='CONCEPT' AND n.visibility='ACTIVE' AND n.current_revision_id=r.id
 AND r.status='PUBLISHED' AND r.policy_version=(SELECT active_policy FROM storage_contract WHERE id=1)
 AND NOT EXISTS(SELECT 1 FROM contribution_decisions d WHERE d.concept_id=n.id AND ((d.status='REVIEW' AND d.action='CONFLICT') OR (d.status='ACCEPTED' AND json_extract(d.decision_json,'$._requires_publication')=1 AND (d.applied_revision_id IS NULL OR d.applied_revision_id!=r.id))) AND d.atomic_revision_id IN (SELECT id FROM atomic_valid))
 AND EXISTS(SELECT 1 FROM concept_inputs ci WHERE ci.concept_revision_id=r.id)
 AND NOT EXISTS(SELECT 1 FROM concept_inputs ci WHERE ci.concept_revision_id=r.id AND ci.atomic_revision_id NOT IN (SELECT id FROM atomic_valid))
 AND EXISTS(SELECT 1 FROM section_evidence se WHERE se.revision_id=r.id)
 AND NOT EXISTS(SELECT 1 FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id
 JOIN source_revisions sr ON sr.id=e.source_revision_id JOIN sources s ON s.id=sr.source_id
 WHERE se.revision_id=r.id AND (s.deleted_at IS NOT NULL OR s.current_revision_id!=sr.id))
)
"""


def eligible(connection, revision_ids=None):
    query = (
        VALID
        + " SELECT n.*,r.id revision_id,r.markdown_ref,r.markdown_sha256,r.summary,r.scope_json FROM valid v JOIN note_revisions r ON r.id=v.id JOIN notes n ON n.id=r.note_id"
    )
    if revision_ids is not None:
        if not revision_ids:
            return []
        statement = text(query + " WHERE r.id IN :ids").bindparams(bindparam("ids", expanding=True))
        return [
            dict(r) for r in connection.execute(statement, {"ids": list(revision_ids)}).mappings()
        ]
    return [dict(r) for r in connection.execute(text(query)).mappings()]
