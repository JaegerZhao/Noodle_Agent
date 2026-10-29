"""Deterministic formatting revisions reuse checked facts and immutable evidence."""

import json

from sqlalchemy import text

from ..contracts import Candidate, Review
from ..eligibility import eligible
from ..repository import Conflict, one
from .core_state import compact_markdown
from .maintenance_run import write

POLICY = 'concept-wrapper-format-v1'


def apply(store, run):
    with store.db.engine.connect() as c:
        notes = [n for n in eligible(c) if n['kind'] == 'CONCEPT']
    changed, kept, skipped = [], [], []
    for note in notes:
        base = note['revision_id']
        ref = f'artifacts/builds/{base}.json'
        with store.db.engine.connect() as c:
            proof = one(c, 'SELECT sha256 FROM build_artifacts WHERE storage_ref=:r', r=ref)
        raw = json.loads(store.read(ref, proof['sha256']))
        original = Candidate.model_validate(raw['candidate'])
        prior_review = Review.model_validate(raw['review'])
        if not prior_review.passed or prior_review.candidate_sha256 != original.fingerprint:
            raise Conflict('Formatting requires a matching checked publication contract')
        sections = [s.model_copy(update={'markdown': compact_markdown(s.markdown, original.title)}) for s in original.sections]
        if any(not s.markdown.strip() for s in sections):
            skipped.append({'note_id': note['id'], 'reason': 'Formatting would leave an empty section'})
            continue
        if any(s.protected and s.markdown != old.markdown for s, old in zip(sections, original.sections, strict=True)):
            skipped.append({'note_id': note['id'], 'reason': 'Protected content retained'})
            continue
        candidate = original.model_copy(update={'sections': sections})
        if candidate.fingerprint == original.fingerprint:
            kept.append(note['id'])
            continue
        report = {**prior_review.report, 'format_normalization': {'policy': POLICY,
            'prior_revision': base, 'prior_candidate_sha256': original.fingerprint,
            'changed_sections': [s.section_id for s, old in zip(sections, original.sections, strict=True) if s.markdown != old.markdown]}}
        # This changes presentation, so it must not inherit a metadata-only proof.
        report.pop('metadata_only', None)
        with store.repository.transaction() as c:
            def accepted(connection, revision, identity=note['id'], base=base):
                connection.execute(text("UPDATE contribution_decisions SET applied_revision_id=:r WHERE concept_id=:id AND applied_revision_id=:old AND status='ACCEPTED'"),
                    {'r': revision, 'id': identity, 'old': base})
            published = store.publisher.publish(candidate, expected_revision=base,
                review=Review(candidate_sha256=candidate.fingerprint, policy_version=candidate.policy_version,
                    reviewer='program.concept_format_acceptance', passed=True, report=report),
                accepted=accepted, _connection=c)
        changed.append({'note_id': note['id'], 'prior_revision': base, **published})
    result = {'policy': POLICY, 'changed': changed, 'kept': len(kept), 'skipped': skipped, 'model_calls': 0}
    write(run / 'format-acceptance.json', result)
    return result
