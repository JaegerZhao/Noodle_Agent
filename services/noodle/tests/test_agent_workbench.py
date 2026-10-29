"""DSH integration contracts without paid models or production mutations."""

import json

import pytest
from fastapi.testclient import TestClient
from test_next_rag_effectiveness import note

from noodle_agent.config import Settings
from noodle_agent.next.agent_api import context_path, scoped_query
from noodle_agent.next.api import create_app
from noodle_agent.next.retrieval.contracts import Query
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path/'library')
    identity, source, revision = note(store, '梯度', '😀梯度说明与来源。')
    application = Application(store, index_mode='KEYWORD_ONLY')
    application.indexer.build('KEYWORD_ONLY')
    with TestClient(create_app(application=application)) as client:
        yield store, client, identity, source, revision
    store.close()


def test_frozen_cwd_uses_explicit_configuration_directory(tmp_path, monkeypatch):
    config = tmp_path/'project'
    config.mkdir()
    (config/'.env').write_text('NOODLE_LLM=deepseek\nDASHSCOPE_API_KEY=fixture-not-a-real-key\n', encoding='utf-8')
    frozen = tmp_path/'frozen'
    frozen.mkdir()
    monkeypatch.chdir(frozen)
    for name in ('NOODLE_LLM', 'DASHSCOPE_API_KEY', 'EMBEDDING_MODEL', 'EMBEDDING_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NOODLE_CONFIG_DIR', str(config))
    value = Settings.from_env()
    assert value.llm == 'deepseek' and value.embedding_api_key
    assert value.embedding_model == 'text-embedding-v4'


def test_readiness_distinguishes_live_profile_from_stored_index(workspace):
    _, client, *_ = workspace
    value = client.get('/internal/next/agent/readiness').json()
    assert value['modes']['KEYWORD_ONLY']['status'] == 'READY'
    assert value['modes']['HYBRID']['status'] == 'PROFILE_MISMATCH'
    assert not value['embedding']['configured']
    assert 'api_key' not in json.dumps(value).lower()


def test_readiness_and_keyword_search_share_the_same_hybrid_head(workspace):
    from test_next_rag_effectiveness import Embedding

    from noodle_agent.next.retrieval.indexing import Indexer
    from noodle_agent.next.retrieval.readiness import readiness
    store, _, *_ = workspace
    from sqlalchemy import text
    with store.repository.transaction() as c:
        c.execute(text("DELETE FROM index_heads WHERE mode_key LIKE 'KEYWORD_ONLY:%'"))
    index = Indexer(store, Embedding(), allow_test_embeddings=True)
    index.build('HYBRID')
    app = Application(store, embedding=Embedding(), allow_test_embeddings=True)
    reported = readiness(app)['modes']['KEYWORD_ONLY']
    assert reported['status'] == 'READY'
    assert reported['index_generation'] == app.indexer.snapshot('KEYWORD_ONLY')[0]['id']
    assert reported['lexical_projection_of'] == 'HYBRID'


def test_request_usage_keeps_layers_separate_and_missing_counts_unknown(workspace):
    from noodle_agent.next.retrieval.usage import request_usage
    store, *_ = workspace
    store.put('artifacts/api-calls/usage-test/a.json',json.dumps({'kind':'LLM_CALL','model':'flash','usage':{'prompt_tokens':12,'completion_tokens':3,'total_tokens':15,'prompt_cache_hit_tokens':8}}).encode())
    store.put('artifacts/api-calls/usage-test/b.json',json.dumps({'kind':'EMBEDDING_CALL','model':'embed','usage':None}).encode())
    value = request_usage(store, {'request_id':'usage-test','diagnostics':{'rerank':{'provider':'dashscope','status':'READY','usage':{'total_tokens':25}}}})
    assert value['llm']['total_tokens'] == 15 and value['llm']['cached_input_tokens'] == 8
    assert value['embedding']['reported_calls'] == 0 and 'total_tokens' not in value['embedding']
    assert value['rerank']['total_tokens'] == 25
    assert 'agent' not in value


def test_selected_source_context_supplies_current_revision_without_job_enumeration(workspace):
    store, client, _, source, _ = workspace
    save_context(store, 'context-source', {'mode':'SOURCE','source_ids':[source['source_id']]})
    context = client.get('/internal/next/agent/context',headers={'X-Harness-Session-Id':'context-source'}).json()['context']
    assert context['selected_source']['source_revision_id'] == source['revision_id']
    assert context['selected_source']['source_id'] == source['source_id']
    assert 'api_key' not in json.dumps(context).lower()


def test_compact_read_requires_section_and_keeps_real_provenance(workspace):
    _, client, identity, source, _ = workspace
    value = client.get('/internal/next/agent/notes/'+identity).json()
    assert value['status'] == 'READY' and value['note_id'] == identity
    assert 'markdown' not in value['sections'][0]
    section = client.get('/internal/next/agent/notes/'+identity+'?section_id=main').json()['sections'][0]
    assert '😀梯度' in section['markdown']
    assert section['provenance'][0]['source_revision_id'] == source['revision_id']
    assert client.get('/internal/next/agent/notes/'+source['revision_id']).status_code == 404
    assert client.get('/internal/next/agent/notes/'+identity+'?section_id=missing').status_code == 404


def test_discovery_returns_identities_not_full_text(workspace):
    _, client, identity, source, _ = workspace
    value = client.get('/internal/next/agent/library').json()
    assert value['items'][0]['id'] == identity
    assert 'markdown' not in value['items'][0]
    source_value = client.get('/internal/next/agent/sources').json()['items'][0]
    assert source_value['source_id'] == source['source_id']
    assert source_value['source_revision_id'] == source['revision_id']
    assert client.get('/internal/next/agent/tasks').json()['status'] == 'READY'


def save_context(store, session, scope):
    path = context_path(store, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'context': {'scope': scope, 'retrieval_mode': 'KEYWORD_ONLY'}}), encoding='utf-8')


def test_session_scope_cannot_be_broadened_by_tool_arguments(workspace):
    store, client, identity, _, _ = workspace
    save_context(store, 's1', {'mode': 'NOTES', 'note_ids': [identity]})
    value = scoped_query(store, Query(query='梯度', scope={'mode': 'GLOBAL'}), 's1')
    assert value.scope.mode == 'NOTES' and value.scope.note_ids == [identity]
    assert value.retrieval_mode == 'KEYWORD_ONLY'
    outside, _, _ = note(store, '其他', '其他内容')
    assert client.get('/internal/next/agent/notes/'+outside, headers={'X-Harness-Session-Id': 's1'}).status_code == 409


def test_original_question_is_bound_to_its_user_rpc_not_later_turns(workspace):
    store, *_ = workspace
    path = context_path(store, 's1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'context': {'scope': {'mode': 'GLOBAL'}}, 'rpc_id': 'user-1', 'user_question': '只解释 C，不要推导。'}), encoding='utf-8')
    expanded = Query(query='解释 C，并展开 KKT 和对偶推导')
    current = scoped_query(store, expanded, 's1', user_rpc_id='user-1', preserve_question=True)
    assert current.query == '只解释 C，不要推导。'
    later = scoped_query(store, expanded, 's1', user_rpc_id='user-2', preserve_question=True)
    assert later.query == expanded.query


def test_bridge_does_not_expose_host_files_or_credentials(workspace):
    _, client, *_ = workspace
    for method in ('credentials.describe', 'settings.update', 'host.openPath', 'host.createDirectory'):
        assert client.post('/internal/next/agent/rpc/'+method, json={'payload': {}}).status_code == 403


@pytest.mark.parametrize('body', [[], {'payload': 'wrong'}, None])
def test_malformed_rpc_is_a_client_error_not_an_internal_failure(workspace, body):
    _, client, *_ = workspace
    assert client.post('/internal/next/agent/rpc/session.prompt', json=body).status_code == 422


def test_prompt_retry_is_deduplicated_and_context_persisted(workspace, monkeypatch):
    store, client, identity, _, _ = workspace
    calls = []
    async def upstream(method, payload, rpc_id=None):
        calls.append((method, payload))
        value = {'items': [{'sessionId': 'session-test', 'running': False}]} if method == 'session.list' else {'accepted': True}
        return {'type': 'server-response', 'rpcId': rpc_id, 'result': {'ok': True, 'value': value}}
    monkeypatch.setattr('noodle_agent.next.agent_api.upstream', upstream)
    body = {'rpcId': 'once', 'payload': {'sessionId': 'session-test', 'mode': 'queue', 'content': [{'type': 'text', 'text': '梯度是什么'}]},
            'context': {'scope': {'mode': 'NOTES', 'note_ids': [identity]}, 'retrieval_mode': 'KEYWORD_ONLY'}}
    path = '/internal/next/agent/rpc/session.prompt'
    assert client.post(path, json=body).status_code == 200
    assert client.post(path, json=body).status_code == 200
    assert len([c for c in calls if c[0] == 'session.prompt']) == 1
    assert context_path(store, 'session-test').is_file()
    changed = {**body, 'context': {'scope': {'mode': 'GLOBAL'}}}
    assert client.post(path, json=changed).status_code == 409


def test_running_turn_rejects_scope_change(workspace, monkeypatch):
    store, client, identity, _, _ = workspace
    save_context(store, 'session-busy', {'mode': 'NOTES', 'note_ids': [identity]})
    before = context_path(store, 'session-busy').read_bytes()
    async def upstream(*_):
        return {'result': {'ok': True, 'value': {'items': [{'sessionId': 'session-busy', 'running': True}]}}}
    monkeypatch.setattr('noodle_agent.next.agent_api.upstream', upstream)
    r = client.post('/internal/next/agent/rpc/session.prompt', json={'payload': {'sessionId': 'session-busy'}, 'context': {'scope': {'mode': 'GLOBAL'}}})
    assert r.status_code == 409
    assert context_path(store, 'session-busy').read_bytes() == before


def test_events_use_native_dsh_websocket_carrier(workspace, monkeypatch):
    _, client, *_ = workspace
    observed = []
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def __aiter__(self):
            async def frames():
                yield '{"type":"server-request","rpcId":"x","payload":{"type":"session/subscribed"}}'
            return frames()
    def connect(url, **options):
        observed.append((url, options))
        return Socket()
    monkeypatch.setattr('noodle_agent.next.agent_api.connect_events', connect)
    response = client.get('/internal/next/agent/events/mux')
    assert response.status_code == 200
    assert response.text.startswith('event: bridge\ndata: {"status":"connected"}')
    assert 'data: {"type":"server-request"' in response.text
    assert observed[0][0].endswith('/api/events.mux') and observed[0][0].startswith('ws:')
    assert observed[0][1]['proxy'] is None


def test_combined_downlink_forwards_mux_and_host_without_two_browser_connections(workspace, monkeypatch):
    _, client, *_ = workspace
    opened, closed = [], []

    class Socket:
        def __init__(self, channel):
            self.channel = channel

        async def __aenter__(self):
            opened.append(self.channel)
            return self

        async def __aexit__(self, *_):
            closed.append(self.channel)

        def __aiter__(self):
            async def frames():
                yield json.dumps({'payload': {'type': self.channel}})
            return frames()

    monkeypatch.setattr('noodle_agent.next.agent_api.connect_events', lambda url, **_: Socket(url.rsplit('.', 1)[-1]))
    response = client.get('/internal/next/agent/events/all')
    assert response.status_code == 200
    assert opened == ['mux', 'host']
    assert response.text.count('event: bridge') == 1
    assert '"type": "mux"' in response.text and '"type": "host"' in response.text
    assert set(closed) == {'mux', 'host'}
