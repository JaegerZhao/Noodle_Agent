"""Real failed responses, per-Atomic isolation, single review, and severe stops."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_next_source_sections import Agent, Parser, prepared  # noqa: F401
from test_next_structured_review import PatchCalls, specs

from noodle_agent.next.assembly_generation import (
    SourceSectionPipeline,
    checked_source_draft,
    merge_publication_receipts,
)
from noodle_agent.next.concepts.entity_patch_v2 import write_group
from noodle_agent.next.generation import Generation
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.resilience import SevereQualityFailure, readable
from noodle_agent.next.section_contracts import SourceDraft
from noodle_agent.next.verification import verify_current_generation, verify_generation

REPO = Path(__file__).resolve().parents[3]
BASELINE = REPO / ".local-only/pdf-pipeline-20261023T195228Z"


def test_original_failed_responses_clean_omissions_without_changing_body_or_source():
    if not BASELINE.is_dir():
        pytest.skip("Optional local immutable response replay")
    plan = json.loads((BASELINE / "source-plan.json").read_text(encoding="utf-8"))
    selected = plan["plans"][0]["unit_ids"]
    library = BASELINE / "library"
    with sqlite3.connect((library / "noodle.db").as_uri() + "?mode=ro", uri=True) as c:
        rows = c.execute("SELECT artifact_ref FROM job_checkpoints WHERE stage LIKE 'source.write.source_2d0179b00096dbc8349f%' ORDER BY created_at").fetchall()
    assert len(rows) == 2
    for row, count in zip(rows, [13, 2], strict=True):
        path = library / row[0]
        before = path.read_bytes()
        old = SourceDraft.model_validate_json(before)
        diagnostics = []
        accepted = checked_source_draft(old, selected, {"../assets/" + p.name for p in (library / "assets").iterdir()}, diagnostics=diagnostics)
        assert len(diagnostics[0]["omissions"]) == count
        assert accepted.assignments == old.assignments
        # Existing renderer canonicalizes local image paths, without rewriting prose.
        assert [s.markdown for s in accepted.sections] == [s.markdown.replace("](assets/", "](../assets/") for s in old.sections]
        assert set(accepted.assignments) | set(accepted.omissions) == set(selected)
        assert path.read_bytes() == before


@pytest.mark.parametrize("bad", ["unknown", "missing"])
def test_normalization_does_not_forge_missing_sources_or_remove_unknown_assignments(bad):
    value = SourceDraft(title="Method", sections=[{"key":"s","title":"Definition","markdown":"A supported definition."}], assignments={"u":"s"})
    value = value.model_copy(update={"assignments":{"fake":"s"} if bad == "unknown" else {}})
    with pytest.raises(QualityFailure, match="extra_assignments|missing"):
        checked_source_draft(value, ["u"], [], diagnostics=[])


@pytest.mark.parametrize("text", ["", " \n ", "\ufffd" * 40, "锟斤拷" * 8])
def test_empty_and_corrupt_content_remains_severe(text):
    with pytest.raises(SevereQualityFailure):
        readable(text)


class WholeAgent(Agent):
    fast_model = "fixture-fast"
    pro_model = "fixture-pro"

    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure

    def structured_json(self, *, operation, user, **kwargs):
        data = user["content"]
        if operation in {"next.structure", "next.structure_exclusion_review"}:
            return {"topics":[{"start_unit":"u00000","title":"Term frequency","topic_key":"tf"},
                {"start_unit":"u00002","title":"Inverse document frequency","topic_key":"idf"}]}
        if operation == "next.assembly_review":
            self.operations.append(operation)
            return {"checked_plans":[a["plan_key"] for a in data["assembly"]],"checked_omissions":[],"findings":[]}
        result = super().structured_json(operation=operation, user=user, **kwargs)
        if operation == "next.section_write":
            result.update(summary="Explains the supplied definition and conditions.",entity_hints={"intent":"EXPLAIN","subject":"TF" if "Inverse" not in result["title"] else "IDF"})
            if self.failure and result["title"] == "Term frequency":
                if self.failure == "empty":
                    result["sections"] = []
                else:
                    result["assignments"] = {"UNKNOWN":"explain"}
        return result


def run_generation(context, failure=None, agent=None):
    store, lease, _ = context
    agent = agent or WholeAgent(failure)
    profile = {"parser":Parser.name,"generation_layout":"source-sections-v1","concept_layout":"entity-v2",
        "execution_policy":"pipeline-throughput-v1","checking_policy":"single-check-v1",
        "planning_mode":"learning-objects-v1","quality_policy":"structured-quality-v2","review_policy":"essential-v3"}
    result = Generation(store, Parser(), agent, lease, profile=profile, concurrency=2).run()
    store.jobs.finish(lease, result)
    return store, lease, result, agent


def test_one_protocol_failure_does_not_cancel_other_atomic_or_publish_false_completion(prepared):  # noqa: F811
    store, lease, result, agent = run_generation(prepared, "bad-id")
    assert len(result["notes"]) == 1 and result["notes"][0]["title"] == "Inverse document frequency"
    assert result["coverage"]["complete"] is False and len(result["coverage"]["failed_plans"]) == 1
    assert result["coverage"]["missing_unit_ids"] == ["u00000", "u00001"]
    checked = verify_current_generation(store, lease.job_id)
    assert not checked["passed"] and checked["version_integrity_passed"]
    assert agent.operations.count("next.assembly_review") == 1
    assert verify_generation(store, lease.job_id)['status'] == 'ENGINEERING_READBACK_PARTIAL'


def test_truncated_review_is_recorded_once_and_does_not_stop_readable_notes(prepared):  # noqa: F811
    from noodle_agent.adapters.knowledge.common import TruncatedStructuredResponse

    class TruncatedReviewer(WholeAgent):
        def structured_json(self, *, operation, **kwargs):
            if operation == 'next.assembly_review':
                self.operations.append(operation)
                raise TruncatedStructuredResponse('output limit')
            return super().structured_json(operation=operation, **kwargs)

    _, _, result, agent = run_generation(prepared, agent=TruncatedReviewer())
    assert result['coverage']['complete'] and len(result['notes']) == 2
    assert agent.operations.count('next.assembly_review') == 2


def test_prior_truncation_matches_exact_request_without_another_provider_call(prepared):  # noqa: F811
    from noodle_agent.adapters.knowledge.common import TruncatedStructuredResponse
    from noodle_agent.next.audit import ProviderAudit, prior_truncated_response

    store, lease, _ = prepared
    request = {'system':'review', 'user':{'content':'original'}, 'operation':'next.assembly_review'}
    ProviderAudit(store.repository, lease).record_llm(operation=request['operation'],
        input_text=request['system']+'\n'+json.dumps(request['user'],ensure_ascii=False),
        output_text='{"checked_plans":[', status='FAILED', finish_reason='length')
    with pytest.raises(TruncatedStructuredResponse, match='Known truncated'):
        prior_truncated_response(store.repository, lease, **request)
    prior_truncated_response(store.repository, lease, **{**request,'user':{'content':'changed'}})


def test_explicit_partial_retry_preserves_published_notes_and_recovers_only_failed_atomic(prepared):  # noqa: F811
    from noodle_agent.next.workspace_worker import record_acceptance

    store, old_lease, first, _ = run_generation(prepared, "bad-id")
    first_report = record_acceptance(store,{'job_id':old_lease.job_id})
    frozen = store.path(first_report['report_ref']).read_bytes()
    original = first['notes'][0]
    store.jobs.retry_review(old_lease.job_id)
    lease = store.jobs.claim('recovery-test',kind='GENERATE_ATOMICS')
    assert lease.job_id == old_lease.job_id
    _, _, result, agent = run_generation((store,lease,prepared[2]))
    assert result['coverage']['complete'] and len(result['notes']) == 2
    assert original in result['notes']
    assert store.get_note(original['note_id'])['revision']['id'] == original['revision_id']
    assert agent.operations.count('next.section_write') == 1
    assert agent.operations.count('next.assembly_review') == 1
    assert verify_generation(store,lease.job_id)['status'] == 'ENGINEERING_READBACK_PASSED'
    second_report = record_acceptance(store,{'job_id':lease.job_id})
    assert first_report['report_ref'] != second_report['report_ref']
    assert store.path(first_report['report_ref']).read_bytes() == frozen
    assert record_acceptance(store,{'job_id':lease.job_id})['report_ref'] == second_report['report_ref']


def test_readback_exception_is_a_versioned_diagnostic_not_a_workspace_stop(prepared, monkeypatch):  # noqa: F811
    from noodle_agent.next import verification
    from noodle_agent.next.workspace_worker import record_acceptance

    def failure(*args):
        raise RuntimeError('Injected read-back failure')
    monkeypatch.setattr(verification,'verify_generation',failure)
    store,lease,_ = prepared
    result=record_acceptance(store,{'job_id':lease.job_id})
    assert result['status']=='ENGINEERING_READBACK_FAILED' and result['error_code']=='RuntimeError'
    assert store.path(result['report_ref']).is_file()


def test_paused_chapter_with_deferred_atomic_can_resume_checked_recovery(prepared):  # noqa: F811
    from noodle_agent.next.pipeline_state import readiness
    from noodle_agent.next.task_control import PauseRequested, park, request_pause

    store, lease, source = prepared
    class PausingAgent(WholeAgent):
        def structured_json(self, *, operation, **kwargs):
            value = super().structured_json(operation=operation,**kwargs)
            if operation == 'next.assembly_review':
                request_pause(store,lease.job_id)
            return value
    with pytest.raises(PauseRequested):
        run_generation(prepared,agent=PausingAgent('bad-id'))
    park(store,lease)
    assert readiness(store,source['revision_id'])['coverage']['deferred'] == 1
    with store.db.engine.connect() as c:
        before = c.exec_driver_sql("SELECT id,current_revision_id FROM notes WHERE kind='ATOMIC' AND current_revision_id IS NOT NULL").one()
    store.jobs.retry_review(lease.job_id)
    next_lease = store.jobs.claim('paused-recovery',kind='GENERATE_ATOMICS')
    _, _, result, _ = run_generation((store,next_lease,source))
    assert result['coverage']['complete'] and len(result['notes']) == 2
    assert store.get_note(before[0])['revision']['id'] == before[1]
    assert readiness(store,source['revision_id'])['coverage']['deferred'] == 0
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM job_stage_events WHERE stage='control.resumed'").scalar_one() == 1


def test_assigned_source_wins_conflicting_omission_without_body_edits():
    draft = SourceDraft(title='Definition', sections=[{'key':'s','title':'Count','markdown':'A supported counting definition.'}],assignments={'u':'s'}, omissions={'u':'Navigation'})
    diagnostics = []
    fixed = checked_source_draft(draft,['u'],[],diagnostics=diagnostics)
    assert fixed.sections == draft.sections and fixed.assignments == draft.assignments
    assert fixed.omissions == {} and draft.omissions == {'u':'Navigation'}
    assert diagnostics == [{'code':'ASSIGNED_OMISSIONS_REMOVED','unit_ids':['u']}]


def test_out_of_range_metadata_is_removed_without_inventing_support():
    draft = SourceDraft(title='Definition',sections=[{'key':'s','title':'Count','markdown':'A supported counting definition.'}],assignments={'u':'s','neighbor':'s'})
    diagnostics = []
    fixed = checked_source_draft(draft,['u'],[],diagnostics=diagnostics)
    assert fixed.sections == draft.sections and fixed.assignments == {'u':'s'}
    assert diagnostics == [{'code':'OUT_OF_RANGE_ASSIGNMENTS_REMOVED','unit_ids':['neighbor']}]
    with pytest.raises(QualityFailure,match='missing'):
        checked_source_draft(draft,['u','missing'],[],diagnostics=[])
    unsupported = draft.model_copy(update={'sections':draft.sections+[draft.sections[0].model_copy(update={'key':'other'})],'assignments':{'u':'s','neighbor':'other'}})
    compiled = checked_source_draft(unsupported,['u'],[],diagnostics=[])
    assert len(compiled.sections) == 1 and compiled.sections[0].markdown.count(draft.sections[0].markdown)==2
    with pytest.raises(QualityFailure,match='unknown_sections'):
        checked_source_draft(draft.model_copy(update={'assignments':{'u':'not-a-section'}}),['u'],[],diagnostics=[])


def test_disjoint_reviews_overlap_and_keep_source_order_without_extra_calls():
    from threading import Barrier

    barrier = Barrier(2,timeout=3)
    pipeline = SourceSectionPipeline(SimpleNamespace(store=None,concurrency=2,profile={'review_policy':'essential-v3','execution_policy':'pipeline-throughput-v1'}))
    seen = []
    def review(records,key):
        seen.append(key)
        barrier.wait()
        return records,key,key
    pipeline.review = review
    partitions = [[{'key':'first'}],[{'key':'second'}]]
    result = pipeline.review_partitions(partitions,'chapter')
    assert [index for index,_ in result] == [0,1]
    assert [value[0] for _,value in result] == partitions
    assert sorted(seen) == ['chapter.0','chapter.1']


def test_review_packet_drops_internal_block_ids_but_preserves_all_source_content():
    unit = {'unit_id':'u','block_id':'immutable-parser-block','page':1,'kind':'TEXT','text':'Full source definition','asset_ref':'../assets/source.png'}
    record = {'key':'p','draft':{'title':'Definition','sections':[],'assignments':{},'omissions':{'u':'Administrative'}},'inputs':{'units':[unit]},'contexts':[]}
    pipeline = SourceSectionPipeline(SimpleNamespace(store=None,profile={'checking_policy':'single-check-v1'}))
    projected = pipeline.make_packet([record])['assembly'][0]['source_units'][0]
    assert projected == {k:v for k,v in unit.items() if k!='block_id'}
    assert unit['block_id'] == 'immutable-parser-block'


def test_shared_source_sections_compile_losslessly_before_the_single_review():
    draft=SourceDraft(title='Layer complexity',sections=[{'key':'criteria','title':'Criteria','markdown':'Compare computation and dependency paths.'},{'key':'table','title':'Complexity table','markdown':'| Layer | Cost |\n| --- | --- |\n| Attention | $n^2d$ |'}],assignments={'u':'criteria'})
    diagnostics=[]
    compiled=checked_source_draft(draft,['u'],[],diagnostics=diagnostics)
    assert compiled.assignments == {'u':'criteria'} and len(compiled.sections)==1
    assert all(s.title in compiled.sections[0].markdown and s.markdown in compiled.sections[0].markdown for s in draft.sections)
    assert diagnostics[0]['code']=='SECTIONS_COALESCED_SHARED_SOURCE'
    assert len(draft.sections)==2


def test_repeated_partial_recovery_keeps_earlier_success_and_only_remaining_failure():
    first = {'notes':[{'note_id':'a','revision_id':'ra'}],'resolved_plan_keys':['p1'],'omissions':{'u1':'Administrative'},'deferred':[{'plan_key':'p2'},{'plan_key':'p3'}]}
    second = {'notes':[{'note_id':'b','revision_id':'rb'}],'resolved_plan_keys':['p2'],'omissions':{},'deferred':[{'plan_key':'p3'}]}
    partial = merge_publication_receipts([first,second])
    assert partial['notes'] == first['notes']+second['notes'] and partial['deferred'] == [{'plan_key':'p3'}]
    last = {'notes':[{'note_id':'c','revision_id':'rc'}],'resolved_plan_keys':['p3'],'omissions':{}}
    complete = merge_publication_receipts([first,second,last])
    assert len(complete['notes']) == 3 and complete['deferred'] == [] and complete['omissions']==first['omissions']
    from noodle_agent.next.repository import Conflict
    with pytest.raises(Conflict,match='already accepted'):
        merge_publication_receipts([first,{'notes':[{'note_id':'a','revision_id':'changed'}]}])


def test_severe_empty_writer_stops_without_acceptance_or_extra_review(prepared):  # noqa: F811
    with pytest.raises(SevereQualityFailure, match="empty"):
        run_generation(prepared, "empty")


@pytest.mark.parametrize("restore", [False, True])
def test_all_omission_draft_reaches_single_review_and_can_restore_content(prepared, restore):  # noqa: F811
    class OmissionAgent(WholeAgent):
        def structured_json(self, *, operation, user, **kwargs):
            data = user["content"]
            if operation == "next.section_write" and data["plan"]["title"] == "Term frequency":
                self.operations.append(operation)
                return {"title":"Term frequency", "sections":[], "assignments":{},
                    "omissions":{u["unit_id"]:"Administrative material" for u in data["units"]}}
            if operation == "next.assembly_review":
                self.operations.append(operation)
                omitted = next((r for r in data["assembly"] if r["proposed_omissions"]), None)
                if omitted is None:
                    return {"checked_plans":[r["plan_key"] for r in data["assembly"]],
                            "checked_omissions":[], "findings":[]}
                assert omitted["section_index"] == []
                return {"checked_plans":[r["plan_key"] for r in data["assembly"]],
                    "checked_omissions":list(omitted["proposed_omissions"]),
                    "findings":[{"plan_key":omitted["plan_key"],"category":"CANDIDATE_ERROR","kind":"OMISSION",
                        "source_ids":list(omitted["proposed_omissions"]),"issue":"Restore teaching content"}] if restore else []}
            if operation == "next.section_repair":
                self.operations.append(operation)
                return {"sections":[{"key":"restored","title":"Definition","markdown":"Term frequency counts a term."}],
                    "restored_assignments":{u:"restored" for u in data["draft"]["omissions"]},
                    "summary":"Explains term frequency and its counting definition.",
                    "entity_hints":{"intent":"EXPLAIN","subject":"Term frequency"}}
            return super().structured_json(operation=operation,user=user,**kwargs)
    store, _, result, agent = run_generation(prepared, agent=OmissionAgent())
    assert result["coverage"]["complete"]
    assert len(result["notes"]) == (2 if restore else 1)
    assert agent.operations.count("next.assembly_review") == 2
    assert agent.operations.count("next.section_repair") == int(restore)
    from noodle_agent.next.source_structure import source_structure
    structure = source_structure(store,result['source_revision_id'])
    first = next(p for p in structure['planned_notes'] if p['title']=='Term frequency')
    assert first['status'] == ('PUBLISHED' if restore else 'OMITTED')


def test_good_generation_is_complete_and_keeps_one_review_per_section(prepared):  # noqa: F811
    store, lease, result, agent = run_generation(prepared)
    assert len(result["notes"]) == 2 and result["coverage"]["complete"]
    assert verify_current_generation(store, lease.job_id)["passed"]
    assert agent.operations.count("next.assembly_review") == 2


def test_atomic_review_repairs_once_without_adjudication_or_second_semantic_review():
    record = {"key":"p","draft":{"title":"Method","summary":"Explanation","sections":[{"key":"s","title":"Formula","markdown":"$x=1$"}],"assignments":{"u":"s"},"omissions":{}},
        "inputs":{"units":[{"unit_id":"u","text":"$x=2$","page":1}],"image_paths":[]},"contexts":[],"evidence":{"u":"e"}}
    operations = []
    def call(stage, operation, instruction, payload, contract, **kwargs):
        operations.append(operation)
        assert kwargs["max_attempts"] == 1
        if operation == 'next.section_repair':
            return contract(sections=[{'key':'s','markdown':'$x=2$'}])
        return contract(checked_plans=["p"],checked_omissions=[],findings=[{"plan_key":"p","section_key":"s","category":"CANDIDATE_ERROR","issue":"Fix local value","candidate_quote":"$x=1$","source_ids":["u"]}])
    pipeline = SourceSectionPipeline(SimpleNamespace(store=None,profile={"review_policy":"essential-v3"},call=call))
    _, final, _ = pipeline.typed_review([record], "batch")
    assert final.passed and operations == ["next.assembly_review","next.section_repair"]
    assert record['draft']['sections'][0]['markdown'] == '$x=2$'
    assert record["post_repair_semantic_review"] is False


def test_concept_repair_has_one_review_and_no_guard_or_upstream_cycle():
    class Calls(PatchCalls):
        review_policy = "essential-v3"
        def __init__(self):
            super().__init__()
            self.operations = []
        def run(self, operation, *args, **kwargs):
            self.operations.append(operation)
            return super().run(operation, *args, **kwargs)
    calls = Calls()
    result = write_group(None,calls,specs())
    assert calls.operations == ["next.entity_write","next.entity_review","next.entity_repair"]
    assert result["b"][1]["review"]["post_repair_semantic_review"] is False


@pytest.mark.parametrize("severe", [False, True])
def test_nonempty_candidate_gets_one_repair_even_when_reviewer_calls_it_severe(severe):
    record = {"key":"p","draft":{"title":"Method","summary":"Explanation","sections":[{"key":"s","title":"Formula","markdown":"Wrong statement"}],"assignments":{"u":"s"},"omissions":{}},
        "inputs":{"units":[{"unit_id":"u","text":"Correct statement","page":1}],"image_paths":[]},"contexts":[],"evidence":{"u":"e"}}
    operations = []
    def call(stage, operation, instruction, payload, contract, **kwargs):
        operations.append(operation)
        if operation == 'next.section_repair':
            return contract(sections=[{'key':'s','markdown':'Correct statement'}])
        return contract(checked_plans=["p"],checked_omissions=[],findings=[{"plan_key":"p","section_key":"s","category":"CANDIDATE_ERROR","severity":"SEVERE" if severe else "REPAIRABLE",
            "issue":"Contradicts actual source","candidate_quote":"Wrong statement","source_ids":["u"]}])
    pipeline = SourceSectionPipeline(SimpleNamespace(store=None,profile={"review_policy":"essential-v3"},call=call))
    accepted, _, _ = pipeline.typed_review([record],"batch")
    assert accepted[0]["draft"]["sections"][0]["markdown"] == "Correct statement"
    assert operations == ["next.assembly_review", 'next.section_repair']


def test_malformed_review_is_recorded_without_semantic_retry():
    record = {"key":"p","draft":{"title":"Method","summary":"Explanation","sections":[{"key":"s","title":"Definition","markdown":"Supported explanation"}],"assignments":{"u":"s"},"omissions":{}},
        "inputs":{"units":[{"unit_id":"u","text":"Supported explanation","page":1}],"image_paths":[]},"contexts":[],"evidence":{"u":"e"}}
    operations = []
    def call(*args, **kwargs):
        operations.append(args[1])
        raise QualityFailure("Reviewer returned an invalid response")
    pipeline = SourceSectionPipeline(SimpleNamespace(store=None,profile={"review_policy":"essential-v3"},call=call))
    accepted, _, _ = pipeline.typed_review([record],"batch")
    assert operations == ["next.assembly_review"]
    assert accepted[0]["warnings"][0]["code"] == "SECTION_REVIEW_UNAVAILABLE"


def test_answer_uses_one_review_and_one_correction_with_citation_validation(tmp_path):
    from test_next_rag_effectiveness import note

    from noodle_agent.next.model_calls import Calls
    from noodle_agent.next.retrieval.answering import Answerer
    from noodle_agent.next.retrieval.indexing import Indexer
    from noodle_agent.next.retrieval.search import Searcher
    from noodle_agent.next.store import Store
    store = Store(tmp_path / "answer")
    try:
        note(store,"Gradient","The chain rule composes derivatives.")
        indexer = Indexer(store)
        indexer.build("KEYWORD_ONLY")
        class AnswerAgent:
            name = "single-review-answer-fixture"
            def __init__(self):
                self.operations = []
            def structured_json(self, operation, **kwargs):
                self.operations.append(operation)
                if operation == "next.answer":
                    return {"claims":[{"text":"Correct composition" if self.operations.count(operation)==2 else "Wrong operation","citation_ids":["K1"]}]}
                return {"passed":False,"issues":["Fix local operation"],"supported_claim_indexes":[]}
        agent = AnswerAgent()
        result = Answerer(Searcher(indexer),Calls(store,agent,review_policy="essential-v3")).answer({"query":"Gradient","retrieval_mode":"KEYWORD_ONLY","rerank":"OFF"})
        assert result["status"] == "READY" and result["answer_markdown"].startswith("Correct composition")
        assert agent.operations == ["next.answer","next.answer_review","next.answer"]
        assert result["diagnostics"]["post_repair_semantic_review"] is False
    finally:
        store.close()


def test_reusable_runner_rejects_wrong_pdf_and_verifies_existing_parse_and_plan():
    if not BASELINE.is_dir():
        pytest.skip("Optional local immutable parse replay")
    spec = importlib.util.spec_from_file_location("pdf_validation_replay", REPO / "scripts/pdf_validation_replay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    info = json.loads((BASELINE / "run-input.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="PDF hash"):
        module.verified_parse(BASELINE,"wrong")
    artifact, assets, proof = module.verified_parse(BASELINE,info["source_sha256"])
    from noodle_agent.next.source_sections import units_from_blocks
    # Replay is valid against its own frozen implementation. New routing code
    # must not silently accept a plan produced by a different implementation.
    frozen_repo = BASELINE / "frozen-code"
    plan, _ = module.verified_plan(BASELINE,frozen_repo,units_from_blocks(artifact),info["source_sha256"])
    assert artifact["page_count"] == 52 and assets and proof and len(plan["plans"]) == 5


def test_frozen_cases_check_uses_original_frozen_bytes_not_source_json_layout(tmp_path):
    import hashlib
    spec = importlib.util.spec_from_file_location("pdf_validation_replay", REPO / "scripts/pdf_validation_replay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = {"cases":[{"id":"Q1","query":"Original question"}]}
    raw_input = json.dumps(cases).encode()
    frozen = json.dumps(cases,indent=2).encode()
    assert raw_input != frozen
    (tmp_path / "acceptance-cases.json").write_bytes(frozen)
    (tmp_path / "planning-input-manifest.json").write_text(json.dumps({"acceptance_sha256":hashlib.sha256(frozen).hexdigest()}))
    assert module.frozen_cases_unchanged(tmp_path)
    (tmp_path / "acceptance-cases.json").write_text(json.dumps({"cases":[{"id":"Q1","query":"Changed"}]}))
    assert not module.frozen_cases_unchanged(tmp_path)
