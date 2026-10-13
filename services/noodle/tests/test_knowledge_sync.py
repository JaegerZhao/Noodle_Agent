from pathlib import Path

from sqlalchemy import select
from test_mvp import create_fixture, isolated_settings
from test_synthesis import _SemanticFixture

from noodle_agent.db import (
    BackboneCorpus,
    Concept,
    ConceptContribution,
    KnowledgeSyncEvent,
    Synthesis,
    SynthesisNode,
    SynthesisVersion,
)
from noodle_agent.services import NoodleService
from noodle_agent.synthesis.knowledge_sync import blocked_concepts


class SyncFixture(_SemanticFixture):
    def structured_json(self, **kwargs):
        if kwargs['operation'] != 'knowledge_sync.classify_v1':
            return super().structured_json(**kwargs)
        user = kwargs['user']
        note = user['note']
        candidates = user['candidates']
        existing = next((c for c in candidates if c['title'] == note['title']), None)
        return {'updates': [{'decision': 'ADD_DETAIL' if existing else 'NEW_CONCEPT',
            'concept_id': existing['id'] if existing else '', 'title': note['title'],
            'module_key': 'overview', 'domain_key': 'machine-learning',
            'scope': {'includes': [note['title']], 'excludes': [], 'aliases': []},
            'reason': 'fixture source-backed update', 'new_knowledge': [note['thesis']],
            'selected_block_ids': [b['id'] for b in note['blocks'] if b['supported']],
            'placements': [{'synthesis_id': g['synthesis_id'], 'group_key': g['group_key']} for g in user['groups']]}]}


def setup_system(service):
    with service.db.session() as session:
        corpus = BackboneCorpus(stable_key='sync-system', domain_key='machine-learning', title='System',
                                manifest_json='{"origin":"SYSTEM"}', local_path='', content_sha256='x', page_count=0)
        session.add(corpus); session.flush()
        synthesis = Synthesis(stable_key='sync-system', domain_key='machine-learning', title='System')
        session.add(synthesis); session.flush()
        release = SynthesisVersion(synthesis_id=synthesis.id, backbone_corpus_id=corpus.id, version_no=1,
                                   title='System', content_sha256='x', status='PUBLISHED')
        session.add(release); session.flush()
        session.add(SynthesisNode(synthesis_version_id=release.id, stable_key='models', title='Models',
                                  original_title='Models', level=1, sort_order=0, node_kind='GROUP'))
        synthesis.active_version_id=release.id
        return synthesis.id


def sync(service):
    event_id = service.knowledge_sync.claim('test-worker')
    assert event_id
    result = service.knowledge_sync.run(event_id, 'test-worker')
    assert not result.get('error'), result
    assert not result.get('errors'), result
    return event_id, result


def make_service(root):
    pdf = create_fixture(root)
    return pdf


def test_upload_creates_shared_concepts_and_directory_then_delete_withdraws(tmp_path: Path):
    pdf = make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service = NoodleService(settings); service.init()
        system_id = setup_system(service)
        upload=service.ingest_pdf(pdf)
        service.run_job_until_terminal(upload['job_id'],timeout_seconds=15)
        service.agent=SyncFixture();service.semantic_synthesis.agent=service.agent
        _event_id,result=sync(service)
        assert result['releases']
        with service.db.session() as session:
            concepts=session.scalars(select(Concept).where(Concept.visibility=='ACTIVE')).all()
            assert concepts
            current=session.get(Synthesis,system_id).active_version_id
            nodes=session.scalars(select(SynthesisNode).where(SynthesisNode.synthesis_version_id==current)).all()
            refs=[n for n in nodes if n.concept_id]
            assert len(refs)==len(concepts)
            assert all(n.parent_id for n in refs)
        query = service.list_notes()[0]['title']
        assert service.search(query)
        service.delete_source_local_data(upload['source_id'])
        assert service.search(query) == []
        with service.db.session() as session:
            assert blocked_concepts(session)
        _,deleted=sync(service)
        assert all(r['status']=='WITHDRAWN' for r in deleted['releases'])
        assert service.knowledge_curation.search_concepts('CNN')==[]
        repeated=service.ingest_pdf(pdf)
        assert repeated['deduplicated']=='false'
        assert repeated['source_id']!=upload['source_id']
        service.close()


def test_delete_one_of_multiple_sources_keeps_remaining_source_and_new_version(tmp_path: Path):
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        service.agent=SyncFixture();service.semantic_synthesis.agent=service.agent
        uploads=[]
        for _ in range(2):
            item=service.ingest_pdf(pdf,force_new_version=True)
            service.run_job_until_terminal(item['job_id'],timeout_seconds=15)
            uploads.append(item);sync(service)
        with service.db.session() as session:
            before={c.id:c.active_version_id for c in session.scalars(select(Concept).where(Concept.visibility=='ACTIVE'))}
        service.delete_source_local_data(uploads[1]['source_id'])
        _,result=sync(service)
        assert result['releases']
        assert all(r['status']=='PUBLISHED' for r in result['releases'])
        with service.db.session() as session:
            assert all(session.get(Concept,c).active_version_id!=v for c,v in before.items())
            assert not blocked_concepts(session)
        service.close()


def test_lease_and_outbox_idempotency(tmp_path: Path):
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init()
        with service.db.session() as session:
            event_id=service.knowledge_sync.enqueue(session,'source','SOURCE_DELETED',{},key='same')
            assert service.knowledge_sync.enqueue(session,'source','SOURCE_DELETED',{},key='same')==event_id
        assert service.knowledge_sync.claim('worker-a')==event_id
        assert service.knowledge_sync.claim('worker-b') is None
        with service.db.session() as session:
            event=session.get(KnowledgeSyncEvent,event_id)
            event.lease_owner='worker-b'
        result=service.knowledge_sync.run(event_id,'worker-a')
        assert 'lease lost' in result['error']
        service.close()


def test_restore_reactivates_pinned_sources_and_directory(tmp_path: Path):
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();system_id=setup_system(service)
        service.agent=SyncFixture();service.semantic_synthesis.agent=service.agent
        upload=service.ingest_pdf(pdf)
        service.run_job_until_terminal(upload['job_id'],timeout_seconds=15);sync(service)
        service.delete_source_local_data(upload['source_id']);sync(service)
        restored=service.knowledge_sync.restore_source(upload['source_id'])
        assert restored['sync_event_id']
        _,result=sync(service)
        assert result['releases'] and all(r['status']=='PUBLISHED' for r in result['releases'])
        assert service.search(service.list_notes()[0]['title'])
        with service.db.session() as session:
            current=session.get(Synthesis,system_id).active_version_id
            nodes=session.scalars(select(SynthesisNode).where(SynthesisNode.synthesis_version_id==current)).all()
            assert any(n.concept_id and n.node_kind=='CONCEPT' for n in nodes)
        service.close()


def test_note_delete_updates_source_release_and_withdraws_only_its_contribution(tmp_path: Path):
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        service.agent=SyncFixture();service.semantic_synthesis.agent=service.agent
        upload=service.ingest_pdf(pdf)
        service.run_job_until_terminal(upload['job_id'],timeout_seconds=15);sync(service)
        notes=service.list_notes();removed=notes[0]
        service.knowledge_sync.delete_note(removed['note_id'])
        assert all(n['note_id']!=removed['note_id'] for n in service.list_notes())
        _,result=sync(service)
        assert any(r['status']=='WITHDRAWN' for r in result['releases'])
        assert len(service.list_notes())==len(notes)-1
        service.close()


def test_edit_supersedes_old_version_and_never_copies_its_removed_evidence(tmp_path: Path):
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        service.agent=SyncFixture();service.semantic_synthesis.agent=service.agent
        upload=service.ingest_pdf(pdf)
        service.run_job_until_terminal(upload['job_id'],timeout_seconds=15);sync(service)
        note=service.get_note(service.list_notes()[0]['note_id'])
        old_id=note['version_id']
        service.edit_note_block(note['note_id'],expected_version_id=old_id,
                                block_key=note['blocks'][0]['block_key'],new_text='Edited assertion without PDF support.')
        event_id=service.knowledge_sync.claim('edit-worker')
        result=service.knowledge_sync.run(event_id,'edit-worker')
        assert not result.get('error') and not result.get('errors'),result
        with service.db.session() as session:
            assert not session.scalar(select(ConceptContribution.id).where(
                ConceptContribution.atomic_note_version_id==old_id,ConceptContribution.decision_status=='ACCEPTED'))
        service.close()


class DeleteDuringWrite(SyncFixture):
    def __init__(self,service,source_id):
        self.service=service;self.source_id=source_id;self.deleted=False

    def structured_json(self,**kwargs):
        if kwargs['operation']=='semantic_synthesis.write_concept_v3' and not self.deleted:
            self.deleted=True
            self.service.delete_source_local_data(self.source_id)
        return super().structured_json(**kwargs)


def test_source_deleted_during_model_call_cannot_publish_late_candidate(tmp_path: Path):
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        upload=service.ingest_pdf(pdf)
        service.run_job_until_terminal(upload['job_id'],timeout_seconds=15)
        service.agent=DeleteDuringWrite(service,upload['source_id']);service.semantic_synthesis.agent=service.agent
        event_id=service.knowledge_sync.claim('late-worker')
        result=service.knowledge_sync.run(event_id,'late-worker')
        assert result.get('errors')
        assert any('manifest changed' in e['error'] for e in result['errors'])
        with service.db.session() as session:
            assert not session.scalar(select(Concept.id).where(Concept.visibility=='ACTIVE'))
        assert service.knowledge_curation.search_concepts('CNN')==[]
        service.close()


def test_parse_cache_recreates_source_identity_and_evidence_without_provider_poll(tmp_path: Path):
    import json
    import shutil
    from dataclasses import replace

    from noodle_agent.db import Job, ParseRun, SourcePage
    from noodle_agent.parse_cache import reuse_parse

    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(replace(settings,mineru_mode='precision',mineru_language='ch'))
        service.init()
        first=service.ingest_pdf(pdf)
        service.run_job_until_terminal(first['job_id'],timeout_seconds=15)
        with service.db.session() as session:
            job=session.get(Job,first['job_id'])
            path=Path(json.loads(job.state_json)['normalized_path'])
            data=json.loads(path.read_text(encoding='utf-8'))
            data['parser_name']='mineru';data['parser_version']=service.settings.mineru_model_version
            for block in data['blocks']:
                if block.get('asset_path'):
                    source=Path(block['asset_path']);target=path.parent/source.name
                    shutil.copyfile(source,target);block['asset_path']=str(target)
            path.write_text(json.dumps(data),encoding='utf-8')
            parse=session.get(ParseRun,job.parse_run_id)
            parse.provider='mineru';parse.parser_version=service.settings.mineru_model_version
            old_pages=set(session.scalars(select(SourcePage.id).where(SourcePage.parse_run_id==parse.id)))
        service.delete_source_local_data(first['source_id'])
        second=service.ingest_pdf(pdf)
        class Parser:
            name='mineru'
            def submit(self,*args):
                raise AssertionError('Cached PDF must not submit remote parser')
            def poll(self,*_args):
                raise AssertionError('Cached PDF must not poll remote parser')
        service.parser=Parser()
        service._submit_parse(second['job_id'])
        assert reuse_parse(service,second['job_id'])
        with service.db.session() as session:
            job=session.get(Job,second['job_id'])
            assert json.loads(job.state_json)['parse_cache_hit']
            new_pages=set(session.scalars(select(SourcePage.id).where(SourcePage.parse_run_id==job.parse_run_id)))
            assert new_pages and not old_pages & new_pages
        service.close()


def test_reference_aliases_roundtrip_without_decoding_invented_ids():
    from noodle_agent.adapters.llm import compact_reference_ids
    identifier = '12345678-1234-1234-1234-123456789012'
    encoded, decode = compact_reference_ids({'evidence_refs': [identifier], 'title': identifier})
    assert encoded['evidence_refs'] == ['REF_C1']
    assert encoded['title'] == identifier
    assert decode({'evidence_refs': ['REF_C1', 'REF_C999']})['evidence_refs'] == [identifier, 'REF_C999']


def test_rejected_increment_preserves_valid_base_but_never_deleted_sources(tmp_path):
    from noodle_agent.db import ConceptModule, ConceptReleaseModule, KnowledgeModuleState
    from noodle_agent.synthesis.knowledge_sync import digest
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        service.agent=SyncFixture();service.semantic_synthesis.agent=service.agent
        upload=service.ingest_pdf(pdf)
        service.run_job_until_terminal(upload['job_id'],timeout_seconds=15);sync(service)
        with service.db.session() as session:
            concept=session.scalar(select(Concept).where(Concept.visibility=='ACTIVE'))
            base=concept.active_version_id
            pin=session.scalar(select(ConceptReleaseModule).where(ConceptReleaseModule.concept_version_id==base))
            mid=pin.module_id;cid=concept.id
            event=service.knowledge_sync.enqueue(session,upload['source_id'],'TEST',{},key=digest('review'))
            service.knowledge_sync._dirty(session,mid,event)
        owner='review-test';assert service.knowledge_sync.claim(owner)==event
        result=service.knowledge_sync._quarantine_review(mid,event,owner,'unsupported new assertion')
        assert result['previous_release_available']
        with service.db.session() as session:
            assert session.get(Concept,cid).active_version_id==base
            assert cid not in blocked_concepts(session)
            # A rejected new, unpinned module cannot mask the published base.
            module=ConceptModule(concept_id=cid,module_key='new-review-module',semantic_role='OVERVIEW')
            session.add(module);session.flush()
            session.add(KnowledgeModuleState(module_id=module.id,revision=1,dirty=True,event_id=event))
        with service.db.session() as session:
            assert cid not in blocked_concepts(session)
        service.delete_source_local_data(upload['source_id'])
        pinned_current=service.get_concept(cid,version_id=base)
        assert not pinned_current['blocks'] and pinned_current['sync_status']=='REBUILDING'
        result=service.knowledge_sync._quarantine_review(mid,event,owner,'source deleted')
        assert not result['previous_release_available']
        with service.db.session() as session:
            assert cid in blocked_concepts(session)
        service.close()




class SpanJudgeFixture(SyncFixture):
    def structured_json(self, **kwargs):
        if kwargs['operation'] == 'semantic_synthesis.judge_concept_module_v1':
            user=kwargs['user']
            return {'decision':'PASS','summary':'Minor readability suggestion only.',
                'issues':[{'issue_id':'span-ref','severity':'MINOR','category':'READABILITY',
                    'block_key':user['module']['payload']['blocks'][0]['block_key'],
                    'message':'Readable source-backed content.', 'evidence_refs':[user['allowed_evidence_span_ids'][0]]}],
                'coverage':{}}
        return super().structured_json(**kwargs)


def test_judge_legal_span_reference_is_normalized_to_its_contribution(tmp_path):
    import json

    from noodle_agent.db import ConceptModuleJudgeRecord
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        upload=service.ingest_pdf(pdf);service.run_job_until_terminal(upload['job_id'],timeout_seconds=15)
        service.agent=SpanJudgeFixture();service.semantic_synthesis.agent=service.agent
        _,result=sync(service)
        assert result['releases']
        with service.db.session() as session:
            record=session.scalar(select(ConceptModuleJudgeRecord))
            issue=json.loads(record.issues_json)[0]
            assert issue['evidence_span_ids']
            assert session.get(ConceptContribution,issue['evidence_refs'][0]) is not None
        service.close()


class InventedJudgeFixture(SyncFixture):
    def structured_json(self, **kwargs):
        if kwargs['operation'] in {'semantic_synthesis.judge_concept_module_v1','semantic_synthesis.judge_concept_module_schema_repair'}:
            return {'decision':'PASS','summary':'Invented reference must fail closed.',
                'issues':[{'issue_id':'invented','severity':'MINOR','category':'READABILITY',
                    'message':'Bad ID', 'evidence_refs':['00000000-0000-0000-0000-000000000000']}], 'coverage':{}}
        return super().structured_json(**kwargs)


def test_unknown_judge_reference_cannot_publish_and_is_retryable_error(tmp_path):
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        upload=service.ingest_pdf(pdf);service.run_job_until_terminal(upload['job_id'],timeout_seconds=15)
        service.agent=InventedJudgeFixture();service.semantic_synthesis.agent=service.agent
        owner='invalid-judge';eid=service.knowledge_sync.claim(owner)
        result=service.knowledge_sync.run(eid,owner)
        assert result['errors'] and all('schema invalid' in item['error'] for item in result['errors'])
        with service.db.session() as session:
            assert not session.scalar(select(Concept.id).where(Concept.visibility=='ACTIVE'))
            assert session.get(KnowledgeSyncEvent,eid).status=='COMPLETED_WITH_ERRORS'
        assert service.knowledge_sync.retry(eid)['status']=='QUEUED'
        service.close()


class RejectJudgeFixture(SyncFixture):
    def structured_json(self, **kwargs):
        if kwargs['operation'] == 'semantic_synthesis.judge_concept_module_v1':
            return {'decision':'REJECT','summary':'Unsupported scientific condition.',
                'issues':[{'issue_id':'scope','severity':'BLOCKER','category':'EVIDENCE_GAP',
                    'message':'Requires source review.',
                    'evidence_refs':[kwargs['user']['allowed_contribution_ids'][0]]}], 'coverage':{}}
        return super().structured_json(**kwargs)


def test_quality_review_is_not_technical_failure_and_can_be_rejudged(tmp_path):
    pdf=make_service(tmp_path)
    with isolated_settings(tmp_path) as settings:
        service=NoodleService(settings);service.init();setup_system(service)
        upload=service.ingest_pdf(pdf);service.run_job_until_terminal(upload['job_id'],timeout_seconds=15)
        service.agent=RejectJudgeFixture();service.semantic_synthesis.agent=service.agent
        eid,result=sync(service)
        assert not result['releases']
        with service.db.session() as session:
            assert session.get(KnowledgeSyncEvent,eid).status=='COMPLETED_WITH_REVIEW'
            assert session.scalar(select(ConceptContribution.id).where(ConceptContribution.decision_status=='REVIEW_REQUIRED'))
            assert not session.scalar(select(Concept.id).where(Concept.visibility=='ACTIVE'))
        import pytest
        with pytest.raises(ValueError, match="outside this sync event"):
            service.knowledge_sync.retry(eid, module_ids=['foreign-module'])
        assert service.knowledge_sync.retry(eid)['status']=='QUEUED'
        service.agent=SyncFixture();service.semantic_synthesis.agent=service.agent
        retried,published=sync(service)
        assert retried==eid and published['releases']
        with service.db.session() as session:
            assert session.get(KnowledgeSyncEvent,eid).status=='COMPLETED'
            assert not blocked_concepts(session)
        service.close()
