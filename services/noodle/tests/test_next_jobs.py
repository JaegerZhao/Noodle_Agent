import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from noodle_agent.next.contracts import Candidate, Review, Scope, Section
from noodle_agent.next.repository import Conflict, one
from noodle_agent.next.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "next")
    instance.add_source("作者提供的测试事实".encode(), "测试", kind="AUTHOR")
    yield instance
    instance.close()


def event_id(store):
    with store.db.engine.connect() as connection:
        return one(connection, "SELECT id FROM events WHERE event_type='SOURCE_ADDED'")["id"]


def test_dispatch_fanout_is_idempotent_and_unknown_types_do_not_starve(store):
    assert store.jobs.dispatch({"SOURCE_ADDED": ["INGEST", "INDEX", "INDEX"]}) == 1
    assert store.jobs.dispatch({"SOURCE_ADDED": ["INGEST", "INDEX"]}) == 0
    with store.db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 2
    with store.repository.transaction() as connection:
        for i in range(120):
            store.repository.event(
                connection, "UNHANDLED", "entity", None, {}, key=f"unhandled:{i}"
            )
    store.add_source("新事实".encode(), "B", kind="AUTHOR")
    assert store.jobs.dispatch({"SOURCE_ADDED": ["INGEST"]}, limit=1) == 1


def test_concurrent_workers_do_not_claim_same_job(store):
    store.jobs.enqueue(event_id(store), "INGEST", {"revision": "fixed-snapshot"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(lambda w: store.jobs.claim(w, kind="INGEST"), ["a", "b"]))
    assert sum(lease is not None for lease in result) == 1


def test_expired_worker_cannot_heartbeat_finish_or_publish(store):
    source_event = event_id(store)
    job = store.jobs.enqueue(source_event, "INGEST", {})
    first = store.jobs.claim("same-worker", kind="INGEST")
    with store.repository.transaction() as connection:
        connection.exec_driver_sql("UPDATE jobs SET lease_until='2000-01-01' WHERE id=?", (job,))
    second = store.jobs.claim("same-worker", kind="INGEST")
    assert first.token != second.token
    for action in [lambda: store.jobs.heartbeat(first), lambda: store.jobs.finish(first, {})]:
        with pytest.raises(Conflict, match="lease"):
            action()
    with store.db.engine.connect() as connection:
        source = one(connection, "SELECT current_revision_id FROM sources")
    anchor = store.add_evidence(source["current_revision_id"], "测试事实")
    note = store.create_note("ATOMIC", "事实")
    scope = Scope(includes=["测试事实"])
    candidate = Candidate(
        note_id=note,
        title="事实",
        scope=scope,
        sections=[
            Section(
                section_id="fact",
                title="事实",
                question="内容？",
                scope=scope,
                markdown="测试事实",
                evidence_ids=[anchor],
            )
        ],
    )
    review = Review(
        candidate_sha256=candidate.fingerprint,
        policy_version=candidate.policy_version,
        reviewer="fixture",
        passed=True,
    )
    with pytest.raises(Conflict, match="lease"):
        store.publish(candidate, expected_revision=None, review=review, lease=first)
    published = store.publish(candidate, expected_revision=None, review=review, lease=second)
    assert published["revision_id"]
    store.jobs.finish(second, published)


def test_event_completes_only_after_all_children_succeed(store):
    source_event = event_id(store)
    store.jobs.dispatch({"SOURCE_ADDED": ["A", "B"]})
    a = store.jobs.claim("worker", kind="A")
    b = store.jobs.claim("worker", kind="B")
    store.jobs.finish(a, {})
    with store.db.engine.connect() as connection:
        assert (
            one(connection, "SELECT status FROM events WHERE id=:id", id=source_event)["status"]
            == "DISPATCHED"
        )
    store.jobs.fail(b, {"code": "RETRYABLE"}, retry=True)
    b2 = store.jobs.claim("worker", kind="B")
    store.jobs.finish(b2, {})
    with store.db.engine.connect() as connection:
        assert (
            one(connection, "SELECT status FROM events WHERE id=:id", id=source_event)["status"]
            == "COMPLETED"
        )
    with pytest.raises(ValueError, match="Completed event"):
        store.jobs.enqueue(source_event, "NEW", {})


def test_retries_are_bounded_and_payload_is_frozen(store):
    event = event_id(store)
    job = store.jobs.enqueue(event, "A", {"revision_id": "immutable"})
    assert store.jobs.enqueue(event, "A", {"revision_id": "changed"}) == job
    for _ in range(3):
        lease = store.jobs.claim("worker", kind="A")
        assert lease.input["revision_id"] == "immutable"
        store.jobs.fail(lease, {"code": "NETWORK"}, retry=True)
    assert store.jobs.claim("worker", kind="A") is None
    with store.db.engine.connect() as connection:
        assert one(connection, "SELECT status FROM jobs WHERE id=:id", id=job)["status"] == "REVIEW"


def test_repeated_worker_crashes_are_bounded(store):
    job = store.jobs.enqueue(event_id(store), "CRASH", {})
    for _ in range(3):
        lease = store.jobs.claim("worker", kind="CRASH")
        assert lease is not None
        with store.repository.transaction() as connection:
            connection.exec_driver_sql(
                "UPDATE jobs SET lease_until='2000-01-01' WHERE id=?", (job,)
            )
    assert store.jobs.claim("worker", kind="CRASH") is None
    with store.db.engine.connect() as connection:
        assert one(connection, "SELECT status FROM jobs WHERE id=:id", id=job)["status"] == "REVIEW"


@pytest.mark.parametrize('body', [None, {}])
def test_retry_api_needs_no_reason_and_rejects_duplicate_requests(store, body):
    from fastapi.testclient import TestClient

    from noodle_agent.next.api import create_app
    from noodle_agent.next.runtime import Application

    job_id = store.jobs.enqueue(event_id(store), 'INGEST', {'revision': 'unchanged'})
    lease = store.jobs.claim('test', kind='INGEST')
    store.jobs.fail(lease, {'code': 'INSPECTED_FAILURE'}, retry=False)
    with TestClient(create_app(application=Application(store, index_mode='KEYWORD_ONLY'))) as client:
        url = f'/internal/next/jobs/{job_id}/retry'
        kwargs = {} if body is None else {'json': body}
        response = client.post(url, **kwargs)
        assert response.status_code == 200
        assert response.json() == {'job_id': job_id, 'status': 'PENDING'}
        assert client.post(url).status_code == 409
        assert client.post('/internal/next/jobs/missing/retry').status_code == 409
        schema = client.get('/openapi.json').json()['paths']['/internal/next/jobs/{identity}/retry']
        assert 'requestBody' not in schema['post']
    with store.db.engine.connect() as connection:
        job = one(connection, 'SELECT * FROM jobs WHERE id=:id', id=job_id)
        assert job['attempts'] == 0
        assert json.loads(job['input_json']) == {'revision': 'unchanged'}
        events = connection.exec_driver_sql(
            "SELECT payload_json FROM events WHERE event_type='JOB_RETRY_REQUESTED'"
        ).fetchall()
        assert len(events) == 1
        audit = json.loads(events[0][0])
        assert audit['previous_error']['code'] == 'INSPECTED_FAILURE'
        assert audit['previous_attempts'] == 1
        assert 'reason' not in audit


def test_recovery_uses_append_order_when_event_times_tie(store):
    from noodle_agent.next.checkpoints import Checkpoints
    from noodle_agent.next.contracts import canonical
    from noodle_agent.next.repository import insert, now

    store.jobs.enqueue(event_id(store), 'INGEST', {})
    lease = store.jobs.claim('test', kind='INGEST')
    journal = Checkpoints(store.repository, lease)
    journal.save('failed', {}, {'error': 'checked failure'})
    with store.repository.transaction() as c:
        watermark = c.exec_driver_sql('SELECT max(rowid) FROM job_checkpoints').scalar_one()
        stamp = now()
        for identity, limit in [('zz-older', 0), ('aa-newer', watermark)]:
            insert(c, 'events', id=identity, dedupe_key=identity, event_type='JOB_RETRY_REQUESTED',
                   entity_id=lease.job_id, revision_id=None,
                   payload_json=canonical({'checkpoint_watermark': limit}), status='QUEUED', created_at=stamp)
    assert journal.operator_recovery(['failed']) == {'event_id': 'aa-newer'}
