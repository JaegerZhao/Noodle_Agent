"""One SQL eligibility policy for indexing, retrieval and dependency maintenance."""

from sqlalchemy import bindparam, text

# Materialize current heads and support sets once. Otherwise SQLite can inline
# them inside every Concept dependency check and repeatedly scan old revisions.
VALID = """
WITH current_revisions AS MATERIALIZED (
 SELECT r.id,r.note_id,n.kind FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id
 WHERE n.visibility='ACTIVE' AND r.status='PUBLISHED'
 AND r.policy_version=(SELECT active_policy FROM storage_contract WHERE id=1)
), invalid_sources AS MATERIALIZED (
 SELECT sr.id FROM source_revisions sr JOIN sources s ON s.id=sr.source_id
 WHERE s.deleted_at IS NOT NULL OR s.current_revision_id!=sr.id
), unsupported AS MATERIALIZED (
 SELECT se.revision_id FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id
 WHERE e.source_revision_id IN (SELECT id FROM invalid_sources)
 UNION SELECT revision_id FROM author_origin_sources WHERE source_revision_id IN (SELECT id FROM invalid_sources)
), atomic_valid AS MATERIALIZED (
 SELECT r.id FROM current_revisions r WHERE r.kind='ATOMIC'
 AND EXISTS(SELECT 1 FROM section_evidence se WHERE se.revision_id=r.id)
 AND r.id NOT IN (SELECT revision_id FROM unsupported)
), concept_support AS MATERIALIZED (
 SELECT ci.concept_revision_id FROM concept_inputs ci LEFT JOIN atomic_valid a ON a.id=ci.atomic_revision_id
 GROUP BY ci.concept_revision_id HAVING min(a.id IS NOT NULL)=1
), blocked AS MATERIALIZED (
 SELECT DISTINCT d.concept_id FROM contribution_decisions d JOIN atomic_valid a ON a.id=d.atomic_revision_id
 JOIN current_revisions r ON r.note_id=d.concept_id
 WHERE (d.status='REVIEW' AND d.action='CONFLICT' AND COALESCE(json_extract(d.decision_json,'$._local_conflict'),0)!=1) OR
 (d.status='ACCEPTED' AND json_extract(d.decision_json,'$._requires_publication')=1 AND (d.applied_revision_id IS NULL OR d.applied_revision_id!=r.id))
), valid AS (
 SELECT id FROM atomic_valid UNION ALL
 SELECT r.id FROM current_revisions r WHERE r.kind='CONCEPT'
 AND r.note_id NOT IN (SELECT concept_id FROM blocked)
 AND r.id IN (SELECT concept_revision_id FROM concept_support)
 AND EXISTS(SELECT 1 FROM section_evidence se WHERE se.revision_id=r.id)
 AND r.id NOT IN (SELECT revision_id FROM unsupported)
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
