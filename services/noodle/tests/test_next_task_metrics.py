"""Task display projections never change provider receipts or pause semantics."""

from noodle_agent.next.contracts import canonical
from noodle_agent.next.metrics import (
    model_usage,
    paused_within,
    project_readiness,
    stage_for,
    task_clock,
)


def event(stage, stamp):
    return {'stage': 'control.' + stage, 'created_at': '2026-10-25T' + stamp + '+00:00'}


def test_usage_groups_known_failed_tokens_but_preserves_unknown_usage():
    calls = [{'kind': kind, 'model': model, 'total_tokens': tokens, 'status': status}
             for kind, model, tokens, status in [('LLM_CALL', 'flash', 100, 'COMPLETED'),
                 ('LLM_CALL', 'pro', 50, 'FAILED'), ('VISION_CALL', 'vision', None, 'FAILED'),
                 ('VISION_CALL', 'vision', 40, 'COMPLETED'), ('EMBEDDING_CALL', 'embed', 10, 'COMPLETED')]]
    groups = model_usage(calls)
    assert [g['total_tokens'] for g in groups] == [150, 40, 10]
    assert groups[1]['missing_usage'] == 1
    assert calls[2]['total_tokens'] is None


def test_first_claim_does_not_switch_queue_wait_into_and_out_of_execution_time():
    job={'id':'new','status':'PENDING','attempts':0,'created_at':'2026-10-25T00:00:00+00:00'}
    waiting=task_clock(job,[],None,[],{'state':'RUN'},'2026-10-25T00:01:02+00:00')
    assert waiting['active_ms']==0 and waiting['waiting_ms']==62000
    assert not waiting['ticking'] and waiting['waiting_ticking']
    events=[{'stage':'execution.attempt-start','created_at':'2026-10-25T00:01:02+00:00'}]
    running=task_clock({**job,'status':'RUNNING','attempts':1},events,None,[],{'state':'RUN'},'2026-10-25T00:01:07+00:00')
    assert running['active_ms']==5000 and running['waiting_ms']==62000
    assert running['ticking'] and not running.get('waiting_ticking')


def test_task_clock_subtracts_paused_time_and_resumes_from_frozen_elapsed():
    job = {'status': 'PAUSED', 'created_at': '2026-10-25T00:00:00+00:00'}
    events = [event('paused', '01:00:00')]
    paused = task_clock(job, events, None, [], {'state': 'PAUSED'}, '2026-10-25T04:00:00+00:00')
    assert paused['active_ms'] == 3600000 and paused['pause_ms'] == 10800000
    assert not paused['ticking']
    job['status'] = 'RUNNING'
    resumed = task_clock(job, [*events, event('resumed', '04:00:00')], None, [], {'state': 'RUN'}, '2026-10-25T04:00:05+00:00')
    assert resumed['active_ms'] == 3605000 and resumed['ticking']


def test_pause_request_does_not_stop_clock_before_the_chapter_finishes():
    job = {'status': 'RUNNING', 'created_at': '2026-10-25T00:00:00+00:00'}
    result = task_clock(job, [event('pause-requested', '00:10:00')], None, [], {'state': 'PAUSE_REQUESTED'}, '2026-10-25T00:11:00+00:00')
    assert result['active_ms'] == 660000 and result['ticking']


def test_completed_clock_stops_at_last_recorded_work():
    job = {'status': 'SUCCEEDED', 'created_at': '2026-10-25T00:00:00+00:00'}
    progress = {'updated_at': '2026-10-25T00:20:00+00:00'}
    result = task_clock(job, [], progress, [], {'state': 'RUN'}, '2026-10-25T04:00:00+00:00')
    assert result['active_ms'] == 1200000 and not result['ticking']


def test_imported_generation_without_original_trace_never_charges_later_concept_calls():
    job={'id':'original','status':'SUCCEEDED','created_at':'2026-10-25T00:00:00+00:00'}
    calls=[{'job_id':'concept','created_at':'2026-10-28T00:00:00+00:00'}]
    clock=task_clock(job,[],None,calls,{'state':'RUN'},'2026-10-28T01:00:00+00:00')
    assert clock['active_ms'] is None and clock['generation_timing_available'] is False
    stages=[{'stage':key,'status':'WAITING','calls':0} for key in ['parse','plan','write','concept','organization']]
    result=project_readiness({'summary':clock,'stages':stages,'readiness':{'coverage':{'published':8},
        'concept_maintenance':{'status':'COMPLETE'},'knowledge_structure':{'status':'COMPLETE'}}})
    assert all(s['status']=='AVAILABLE' for s in stages[:3])
    assert stages[3]['status']==stages[4]['status']=='COMPLETED'
    assert '当前库未记录' in result['summary']['execution_note']


def test_completed_legacy_generation_span_stops_before_descendant_maintenance():
    job={'id':'original','status':'SUCCEEDED','created_at':'2026-10-25T00:00:00+00:00'}
    calls=[{'job_id':'concept','created_at':'2026-10-28T00:00:00+00:00'}]
    clock=task_clock(job,[],{'updated_at':'2026-10-25T00:10:00+00:00'},calls,{'state':'RUN'},'2026-10-28T01:00:00+00:00')
    assert clock['active_ms']==600000


def test_recovery_and_structure_operations_have_their_own_correct_stages():
    assert stage_for('next.section_format_recovery')=='write'
    assert stage_for('next.local_finding_review')=='write'
    assert stage_for('next.organization_plan')=='organization'


def test_resume_does_not_charge_provider_or_review_wait_as_execution():
    job = {'status':'RUNNING','created_at':'2026-10-25T00:00:00+00:00'}
    events = [{'stage':'execution.blocked','created_at':'2026-10-25T01:00:00+00:00'},
        {'stage':'execution.resumed','created_at':'2026-10-25T03:00:00+00:00'}]
    clock = task_clock(job,events,None,[],{'state':'RUN'},'2026-10-25T03:00:05+00:00')
    assert clock['blocked_ms']==7200000 and clock['active_ms']==3605000
    assert clock['pause_ms']==0 and clock['ticking']


def test_stage_pause_overlap_is_clipped_to_its_own_time_span():
    events = [event('paused', '01:00:00'), event('resumed', '04:00:00')]
    assert paused_within(events, '2026-10-25T00:00:00+00:00', '2026-10-25T00:30:00+00:00') == 0
    assert paused_within(events, '2026-10-25T01:30:00+00:00', '2026-10-25T04:30:00+00:00') == 9000000


def test_remote_parse_wait_keeps_clock_running_between_polls_without_counting_review_wait():
    def at(stage, seconds, **detail):
        return {'stage': stage, 'created_at': f'2026-10-25T00:00:{seconds:02d}+00:00',
                'detail_json': canonical(detail)}
    events = [at('execution.attempt-start', 0), at('parse-started', 1),
              at('execution.attempt-end', 2, status='DEFERRED', deferred_stage='PARSE_PENDING')]
    job = {'status': 'PENDING', 'created_at': events[0]['created_at']}
    clock = task_clock(job, events, None, [], {'state': 'RUN'}, '2026-10-25T00:00:30+00:00')
    assert clock['ticking'] and clock['active_ms'] == 30000
    paused = task_clock({**job, 'status': 'PAUSED'}, [*events, at('control.paused', 10)],
                        None, [], {'state': 'PAUSED'}, '2026-10-25T00:00:30+00:00')
    assert not paused['ticking'] and paused['active_ms'] == 10000
    events += [at('execution.attempt-start', 40), at('parse-started', 41),
               at('parse-completed', 45), at('execution.attempt-end', 50, status='REVIEW')]
    stopped = task_clock({**job, 'status': 'REVIEW'}, events, None, [], {'state': 'RUN'},
                         '2026-10-25T01:00:00+00:00')
    assert not stopped['ticking'] and stopped['active_ms'] == 50000


def test_ordinary_defer_does_not_count_queue_wait_as_remote_parsing():
    events = [{'stage': 'execution.attempt-start', 'created_at': '2026-10-25T00:00:00+00:00'},
              {'stage': 'source-framework', 'created_at': '2026-10-25T00:00:01+00:00'},
              {'stage': 'execution.attempt-end', 'created_at': '2026-10-25T00:00:02+00:00',
               'detail_json': canonical({'status': 'DEFERRED'})}]
    clock = task_clock({'status': 'PENDING', 'created_at': events[0]['created_at']}, events,
                       None, [], {'state': 'RUN'}, '2026-10-25T00:00:30+00:00')
    assert not clock['ticking'] and clock['active_ms'] == 2000
