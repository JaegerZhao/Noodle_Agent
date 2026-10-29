"""Current-version readiness, typed navigation and verified reuse; no providers."""

import json
from types import SimpleNamespace

import fitz
import pytest
from fastapi.testclient import TestClient
from test_next_rag_effectiveness import Embedding

from noodle_agent.next.api import create_app
from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.contracts import Candidate, Review, Scope, Section
from noodle_agent.next.editor import edit_section
from noodle_agent.next.events import dispatch
from noodle_agent.next.model_packets import EvidencePacket
from noodle_agent.next.pipeline_state import readiness
from noodle_agent.next.repository import one
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.runtime import Application
from noodle_agent.next.source_cache import reuse_parse
from noodle_agent.next.store import Store


@pytest.fixture
def corpus(tmp_path):
    store = Store(tmp_path / 'formal')
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 40), 'Gradient definitions and examples.')
        content = pdf.tobytes()
    source = store.add_source(content, 'Learning fixture')
    dispatch(store)
    lease = store.jobs.claim('offline', kind='GENERATE_ATOMICS')
    journal = Checkpoints(store.repository, lease)
    profile = {'parser': 'mineru', 'parse_mode': 'precision', 'parse_model': 'vlm', 'language': 'ch'}
    raw = {'title': 'Learning fixture', 'page_count': 1, 'profile': profile, 'parser_version': 'fixture', 'blocks': [{'id': 'b', 'page': 1, 'kind': 'TEXT', 'text': 'Gradient definitions and examples.'}]}
    parse_id = store.sources.record_parse(source['revision_id'], 'fixture', profile, raw)
    journal.save('parse.normalized', {}, {'parse_id': parse_id, 'artifact': raw})
    journal.save('source.sections.plan.fixture', {}, {'planning_policy': 'fixture', 'plans': [{'key': 'a'}, {'key': 'b'}], 'chapter_tasks': [{'chapter_key': 'chapter', 'title': 'Foundations', 'plan_keys': ['a', 'b']}]})

    def publish(key):
        identity = store.content.create_note('ATOMIC', key, creation_key=f"atomic:{source['revision_id']}:source-section:{key}")
        anchor = store.add_evidence(source['revision_id'], raw['blocks'][0]['text'], page=1, block_id='b', parse_id=parse_id)
        value = Candidate(note_id=identity, title=key, scope=Scope(includes=['Gradient']), sections=[Section(section_id='body', title='Definition', question='What is a gradient?', scope=Scope(includes=['Gradient']), markdown='Gradient definitions and examples.', evidence_ids=[anchor])])
        return store.publish(value, expected_revision=None, review=Review(candidate_sha256=value.fingerprint, policy_version=value.policy_version, reviewer='offline-fixture', passed=True))
    yield store, source, lease, journal, profile, publish
    store.close()


def test_job_success_does_not_claim_indexing_and_concept_pending_does_not_block(corpus):
    store, source, lease, _, _, publish = corpus
    first, second = publish('a'), publish('b')
    store.jobs.finish(lease, {'notes': [first, second]})
    assert readiness(store, source['revision_id'])['notes_status'] == 'INDEX_PENDING'
    indexer = Indexer(store, Embedding(), allow_test_embeddings=True)
    indexer.build()
    state = readiness(store, source['revision_id'], profile_id=indexer.profile['id'])
    assert state['notes_status'] == 'READY'
    assert state['chapters'][0]['status'] == 'READY'
    assert state['concept_maintenance']['routing_pending'] == 2
    assert state['concept_maintenance']['status'] == 'RUNNING'
    assert state['atomic_index']['ready_at']
    assert readiness(store, source['revision_id'], profile_id='new-profile')['notes_status'] == 'INDEX_PENDING'
    edited = edit_section(store, first['note_id'], expected_revision=first['revision_id'], section_id='body', markdown='The gradient contains all partial derivatives.')
    assert edited['revision_id'] != first['revision_id']
    assert readiness(store, source['revision_id'], profile_id=indexer.profile['id'])['notes_status'] == 'INDEX_PENDING'


def test_partial_coverage_is_readable_but_never_complete(corpus):
    store, source, lease, journal, _, publish = corpus
    first = publish('a')
    journal.save('source.publish.batch', {}, {'notes': [first], 'resolved_plan_keys': ['a'], 'deferred': [{'plan_key': 'b'}]})
    store.jobs.finish(lease, {'notes': [first]})
    Indexer(store, Embedding(), allow_test_embeddings=True).build()
    state = readiness(store, source['revision_id'])
    assert state['readable'] and state['notes_status'] == 'PARTIAL'
    assert state['coverage']['deferred'] == 1


def test_failed_jobs_without_route_or_work_status_are_visible(corpus):
    from noodle_agent.next.contracts import canonical
    from noodle_agent.next.repository import identifier, insert, now
    store, source, lease, _, _, publish = corpus
    first = publish('a')
    with store.repository.transaction() as c:
        for kind in ('ANALYZE_ATOMIC', 'BUILD_CONCEPT'):
            insert(c, 'jobs', id=identifier(), event_id=lease.input['event_id'], kind=kind,
                status='REVIEW', created_at=now(), input_json=canonical({'entity_id':first['note_id'],
                    'revision_id':first['revision_id'], 'payload':{'cause':lease.job_id}}),
                error_json=canonical({'code':'QualityFailure','detail':'Packet exceeds budget'}))
    state = readiness(store, source['revision_id'])
    maintenance = state['concept_maintenance']
    assert maintenance['status'] == 'PARTIAL'
    assert maintenance['failed_routing_jobs'] == maintenance['failed_build_jobs'] == 1
    assert state['knowledge_structure']['upstream_pending'] == 1


def test_ark_billing_stop_is_a_recoverable_provider_issue(corpus):
    store, source, lease, _, _, publish = corpus
    publish('a')
    assert store.jobs.fail(lease, {'code':'PermissionDeniedError','detail':'Error code: 403 AccountOverdueError'}, retry=True) == 'REVIEW'
    state = readiness(store, source['revision_id'])
    assert state['generation_issue']['category'] == 'PROVIDER_BILLING'
    assert state['generation_issue']['retry_after_recovery']
    assert state['readable']


def test_http_readiness_uses_the_active_embedding_profile(corpus):
    store, _source, lease, _, _, publish = corpus
    publish('a'); publish('b')
    store.jobs.finish(lease, {})
    app = Application(store, embedding=Embedding(), allow_test_embeddings=True)
    app.indexer.build()
    with TestClient(create_app(application=app)) as client:
        value = client.get('/internal/next/source-statuses')
        assert value.status_code == 200
        assert value.json()['items'][0]['notes_status'] == 'READY'
        assert client.get('/internal/next/jobs/' + lease.job_id + '/timeline').json()['readiness']['readable']
        root = client.post('/internal/next/maps', json={'title': '机器学习', 'entries': [], 'knowledge_root': True})
        assert root.status_code == 200
        listed = client.get('/internal/next/knowledge/roots').json()['items']
        assert any(v['title'] == '机器学习' and v['kind'] == 'PERSONAL' for v in listed)
        assert client.get('/internal/next/knowledge/roots/' + root.json()['note_id'] + '/tree').json()['entries'] == []


def test_normal_upload_reuses_verified_parse_without_copying_notes(corpus, tmp_path):
    origin, source, lease, _, profile, _ = corpus
    origin.jobs.finish(lease, {})
    with origin.db.engine.connect() as c:
        row = one(c, 'SELECT * FROM source_revisions WHERE id=:r', r=source['revision_id'])
    fresh = Store(tmp_path / 'second')
    try:
        s = fresh.add_source(origin.read(row['blob_ref'], row['sha256']), 'Other filename')
        dispatch(fresh)
        current = fresh.jobs.claim('normal-worker', kind='GENERATE_ATOMICS')
        with fresh.db.engine.connect() as c:
            requested = one(c, 'SELECT * FROM source_revisions WHERE id=:r', r=s['revision_id'])
        result = reuse_parse(fresh, Checkpoints(fresh.repository, current), requested, profile, {'new': True}, roots=[origin.root])
        assert result['artifact']['blocks'][0]['text'] == 'Gradient definitions and examples.'
        assert result['parse_id'] != source['revision_id']
        with fresh.db.engine.connect() as c:
            assert c.exec_driver_sql('SELECT count(*) FROM notes').scalar_one() == 0
            assert c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one() == 0
    finally:
        fresh.close()


def test_deleted_source_reimport_reuses_same_library_parse_without_any_provider(corpus, monkeypatch):
    store, source, lease, _, profile, _ = corpus
    monkeypatch.setenv('NOODLE_NEXT_PARSE_CACHE_LIBRARIES', '[]')
    store.jobs.finish(lease, {})
    with store.db.engine.connect() as c:
        old = one(c, 'SELECT * FROM source_revisions WHERE id=:r', r=source['revision_id'])
    store.set_source_deleted(source['source_id'], True)
    fresh = store.add_source(store.read(old['blob_ref'], old['sha256']), 'Reimported.pdf', regenerate_removed=True)
    assert fresh['regenerated'] and fresh['revision_id'] != source['revision_id']
    dispatch(store)
    current = store.jobs.claim('local-reuse', kind='GENERATE_ATOMICS')
    with store.db.engine.connect() as c:
        requested = one(c, 'SELECT * FROM source_revisions WHERE id=:r', r=fresh['revision_id'])
    journal = Checkpoints(store.repository, current)
    assert reuse_parse(store, journal, requested, {**profile, 'parse_model': 'different'}, {}, roots=[]) is None
    result = reuse_parse(store, journal, requested, profile, {'new': True}, roots=[])
    assert result['artifact']['blocks'][0]['text'] == 'Gradient definitions and examples.'
    assert journal.exists('parse.reuse')
    assert not journal.exists('source.sections.plan.fixture')
    with store.db.engine.connect() as c:
        stages = [r[0] for r in c.exec_driver_sql('SELECT stage FROM job_checkpoints WHERE job_id=?', (current.job_id,))]
    assert set(stages) == {'parse.reuse', 'parse.normalized'}
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one() == 0
        assert c.exec_driver_sql('SELECT count(*) FROM notes').scalar_one() == 0


def test_evidence_packet_keeps_text_and_restores_reviewer_locations():
    original = [{'key': 'topic', 'fragments': [{'fragment_id': 'fragment_' + 'a'*24, 'text': '$x^2$ with the original condition.', 'title': 'Method', 'section_title': 'Proof', 'atomic_revision_id': 'private-long-id', 'evidence_ids': ['private-proof'], 'sha256': 'z'*64}], 'images': {}}]
    packet = EvidencePacket(original)
    projected = packet.project(original)
    assert projected[0]['fragments'][0]['text'] == original[0]['fragments'][0]['text']
    assert projected[0]['fragments'][0]['fragment_id'] == 'F0'
    assert 'private-long-id' not in json.dumps(projected)
    report = SimpleNamespace(chapters={'topic': SimpleNamespace(supported_fragment_ids=['F0'], findings=[SimpleNamespace(source_ids=['F0'])], data_checks=[])})
    packet.restore_review(report)
    assert report.chapters['topic'].findings[0].source_ids == ['fragment_' + 'a'*24]
    assert packet.receipt(original)['projected_estimated_tokens'] < packet.receipt(original)['original_estimated_tokens']


def test_existing_concept_topic_is_reused_without_new_entity_identity():
    from noodle_agent.next.concepts.entity_contracts import RoutingDecision
    from noodle_agent.next.concepts.routing_protocol import expand
    cards = [{'id': 'atomic', 'sections': [{'id': 'section'}]}]
    catalog = [{'id': 'concept', 'title': 'SVM', 'domain': 'ml', 'definition': {'identity_key': 'svm'},
        'internal_topics': [{'key': 'entity_topic_margin', 'title': 'Margin'}]}]
    raw = RoutingDecision(assignments=[{'atomic': 'A0', 'target': 'C0', 'action': 'REUSE', 'topic': 'Margin', 'topic_key': 'entity_topic_margin', 'reason': 'Same internal topic'}])
    plan = expand(raw, cards, catalog)
    assert plan.routes['atomic'].topic_key == 'entity_topic_margin'
    assert plan.entities[0].reuse_id == 'concept'


def test_topic_partition_preserves_every_contribution_and_stable_prefix():
    from noodle_agent.next.concepts.topic_partitions import partition_topics
    entries = [({'id': str(i)}, {'fragment_id': 'f' + str(i), 'sha256': str(i), 'text': 'Definition with assumptions. '*20, 'title': 'Method'}, {'section_title': 'Theory'}) for i in range(5)]
    grouped = partition_topics({'entity_topic_theory': entries}, budget=200)
    assert len(grouped) > 1
    assert [v[0]['id'] for values in grouped.values() for v in values] == [str(i) for i in range(5)]
    assert next(iter(grouped)) == 'entity_topic_theory'
