"""DSH session transport and compact domain projections; never a second Agent loop."""

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import connect as connect_events
from websockets.exceptions import WebSocketException

from .contracts import canonical, sha
from .repository import one, rows
from .retrieval.contracts import Query, Scope

# The bridge intentionally excludes credentials, host filesystem and settings APIs.
RPC_METHODS = {'session.list', 'session.create', 'session.history', 'session.prompt',
               'session.cancel', 'session.rename', 'session.fork', 'session.search',
               'workspace.list', 'workspace.archiveSession'}


class ConversationContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Scope = Field(default_factory=Scope)
    retrieval_mode: str = Field(default='HYBRID', pattern='^(HYBRID|KEYWORD_ONLY)$')
    entity_note_ids: list[str] = Field(default_factory=list, max_length=10)


def context_path(store, session_id):
    return store.path('agent-runtime/context/' + sha(session_id) + '.json')


def read_context(store, session_id):
    path = context_path(store, session_id)
    if not session_id or not path.is_file():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def scoped_query(store, query, session_id, *, user_rpc_id=None, preserve_question=False):
    value = read_context(store, session_id)
    if not value:
        return query
    context = ConversationContext.model_validate(value['context'])
    current_request = bool(user_rpc_id and value.get('rpc_id') == user_rpc_id)
    return Query.model_validate({**query.model_dump(), 'scope': context.scope.model_dump(),
        'query': value['user_question'] if preserve_question and current_request and value.get('user_question') else query.query,
        'allowed_kinds': ['ATOMIC'] if context.scope.mode == 'SOURCE' else query.allowed_kinds,
        'retrieval_mode': context.retrieval_mode,
        'entity_note_ids': query.entity_note_ids or (context.entity_note_ids if current_request else [])})


def validate_context(store, context):
    with store.db.engine.connect() as c:
        for identity in context.scope.source_ids:
            if not one(c, 'SELECT id FROM sources WHERE id=:id AND deleted_at IS NULL', id=identity):
                raise HTTPException(409, '当前资料已失效或不存在。')
    for identity in context.scope.note_ids:
        note = store.get_note(identity)
        if not note.get('eligible') or note['kind'] == 'MOC':
            raise HTTPException(409, '当前笔记不可作为问答范围。')


def require_note_scope(store, note, session_id):
    saved = read_context(store, session_id)
    if not saved:
        return
    scope = saved['context']['scope']
    if scope['mode'] == 'NOTES' and note['id'] not in scope['note_ids']:
        raise HTTPException(409, '该笔记不在当前问答范围内。')
    if scope['mode'] == 'SOURCE':
        with store.db.engine.connect() as c:
            sources = {r['source_id'] for r in rows(c, '''SELECT DISTINCT sr.source_id FROM section_evidence se
                JOIN evidence_anchors e ON e.id=se.evidence_id JOIN source_revisions sr ON sr.id=e.source_revision_id
                WHERE se.revision_id=:r''', r=note['revision']['id'])}
        if note['kind'] != 'ATOMIC' or not sources or not sources <= set(scope['source_ids']):
            raise HTTPException(409, '该笔记不属于当前资料。')


def harness_url():
    url = os.getenv('NOODLE_HARNESS_WEB_URL', 'http://127.0.0.1:3080').rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password or parsed.path:
        raise HTTPException(503, 'DSH 必须配置为本机服务地址。')
    return url


async def upstream(method, payload, rpc_id=None):
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        try:
            response = await client.post(harness_url() + '/api/' + method, json={
                'type': 'client-request', 'rpcId': rpc_id or str(uuid4()), 'method': method, 'payload': payload})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, 'DSH 服务暂不可用，请检查服务是否启动。') from exc


def install(app, store):
    session_locks = {}

    @app.get('/internal/next/agent/context')
    def active_context(request: Request):
        value = read_context(store, request.headers.get('x-harness-session-id', ''))
        if not value:
            return {'context': None}
        context = dict(value['context'])
        with store.db.engine.connect() as c:
            epoch = c.exec_driver_sql('SELECT validity_epoch FROM storage_contract WHERE id=1').scalar_one()
            version = c.exec_driver_sql('SELECT count(*),max(created_at) FROM note_revisions').one()
            heads = [dict(r) for r in rows(c, 'SELECT mode_key,backend,generation FROM index_heads ORDER BY mode_key,backend')]
        context['knowledge_snapshot'] = {'validity_epoch': epoch, 'revisions': version[0],
                                         'latest_revision_at': version[1], 'index_heads': heads}
        context['refresh_requested'] = bool(re.search(r'重新(?:检索|核验|查询)|最新|刚(?:修改|删除|上传)|refresh|recheck',
                                                     value.get('user_question', ''), re.IGNORECASE))
        if context['scope']['mode'] == 'SOURCE':
            with store.db.engine.connect() as c:
                source = one(c, 'SELECT id AS source_id,current_revision_id AS source_revision_id,title FROM sources WHERE id=:id AND deleted_at IS NULL', id=context['scope']['source_ids'][0])
            if source:
                context['selected_source'] = dict(source)
        return {'context': context}

    @app.get('/internal/next/agent/notes/{identity}')
    def compact_note(identity: str, request: Request, section_id: str | None = None):
        try:
            note = store.get_note(identity)
        except (ValueError, KeyError) as exc:
            raise HTTPException(404, '需要 note_id；source_revision_id 不能用于读取笔记。') from exc
        if not note.get('eligible') or note['kind'] == 'MOC':
            raise HTTPException(409, '笔记不是当前可检索版本。')
        require_note_scope(store, note, request.headers.get('x-harness-session-id', ''))
        with store.db.engine.connect() as c:
            provenance = [dict(r) for r in rows(c, '''SELECT se.section_id,e.source_revision_id,e.page_number AS page
                FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id WHERE se.revision_id=:r''', r=note['revision']['id'])]
        sections = []
        for section in note['sections']:
            item = {k: section[k] for k in ('section_id', 'title', 'start_offset', 'end_offset', 'provenance') if k in section}
            item['provenance'] = [{k: r[k] for k in ('source_revision_id', 'page')} for r in provenance if r['section_id'] == section['section_id']]
            if section_id == section['section_id']:
                item['markdown'] = note['markdown'][section['start_offset']:section['end_offset']]
            sections.append(item)
        if section_id and not any(s['section_id'] == section_id for s in sections):
            raise HTTPException(404, '指定小节不存在。')
        with store.db.engine.connect() as c:
            supports = [dict(r) for r in rows(c, '''SELECT DISTINCT n.id AS note_id,n.title,n.current_revision_id AS revision_id
                FROM concept_inputs i JOIN notes n ON n.current_revision_id=i.atomic_revision_id
                WHERE i.concept_revision_id=:id LIMIT 30''', id=note['revision']['id'])] if note['kind'] == 'CONCEPT' else []
        return {'status': 'READY', 'note_id': identity, 'kind': note['kind'], 'title': note['title'],
                'revision_id': note['revision']['id'], 'summary': note['revision'].get('summary', ''),
                'sections': sections, 'supporting_notes': supports}

    @app.get('/internal/next/agent/library')
    def compact_library(q: str = '', kind: str | None = None, source_revision: str | None = None, offset: int = 0):
        from .navigation import current_notes
        if kind not in {None, 'ATOMIC', 'CONCEPT', 'MOC'} or offset < 0:
            raise HTTPException(422, '无效列表参数。')
        value = current_notes(store, kind=kind, q=q, source_revision=source_revision, offset=offset, limit=20)
        return {'status': 'READY', 'total': value['total'], 'offset': offset,
                'items': [{k: n[k] for k in ('id', 'kind', 'title', 'current_revision_id')} for n in value['items']]}

    @app.get('/internal/next/agent/sources')
    def compact_sources(q: str = '', offset: int = 0):
        if offset < 0:
            raise HTTPException(422, 'offset must be non-negative')
        with store.db.engine.connect() as c:
            value = [dict(r) for r in rows(c, '''SELECT s.id AS source_id,s.title,s.current_revision_id AS source_revision_id,r.original_name
                FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id WHERE s.deleted_at IS NULL AND (s.title LIKE :q OR r.original_name LIKE :q)
                ORDER BY s.created_at DESC LIMIT 21 OFFSET :o''', q='%'+q+'%', o=offset)]
        return {'status': 'READY', 'items': value[:20], 'has_more': len(value) > 20}

    @app.get('/internal/next/agent/tasks')
    def compact_tasks(source_id: str = '', status: str = ''):
        with store.db.engine.connect() as c:
            value = [dict(r) for r in rows(c, '''SELECT id AS job_id,kind,status,created_at,json_extract(input_json,'$.entity_id') AS source_id
                FROM jobs WHERE (:s='' OR json_extract(input_json,'$.entity_id')=:s) AND (:t='' OR status=:t)
                ORDER BY created_at DESC LIMIT 20''', s=source_id, t=status)]
        return {'status': 'READY', 'items': value}

    @app.get('/internal/next/agent/tasks/{identity}')
    def compact_task(identity: str):
        from .metrics import timeline
        try:
            value = timeline(store, identity)
        except KeyError as exc:
            raise HTTPException(404, '生成任务不存在。') from exc
        return {'status': 'READY', 'job': {k: value['job'][k] for k in ('id', 'kind', 'status', 'attempts', 'created_at')},
                'control': value['control'], 'summary': value['summary'],
                'stages': [{k: s[k] for k in ('stage', 'status', 'elapsed_ms', 'total_tokens') if k in s} for s in value['stages']]}

    @app.get('/internal/next/agent/source-revisions/{revision}/status')
    def compact_source_status(revision: str):
        from .pipeline_state import readiness
        try:
            value = readiness(store, revision, profile_id=(app.state.application.indexer.profile or {}).get('id'))
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, '来源版本不存在。') from exc
        return {'status': 'READY', **{k: value[k] for k in ('source_id', 'source_revision_id', 'job_id',
            'generation_status', 'notes_status', 'readable', 'coverage', 'atomic_index', 'concept_maintenance')},
            'chapters_total': len(value['chapters']),
            'chapters_completed': sum(c['status'] == 'READY' for c in value['chapters'])}

    async def dispatch_rpc(method, body):
        if method not in RPC_METHODS:
            raise HTTPException(403, '此 DSH 接口未向知识工作台开放。')
        if not isinstance(body, dict) or not isinstance(body.get('payload', {}), dict):
            raise HTTPException(422, 'Invalid RPC envelope')
        payload = body.get('payload', {})
        rpc_id = str(body.get('rpcId') or uuid4())
        session_id = str(payload.get('sessionId', ''))
        # Deduplicate ambiguous prompt delivery without resending paid model work.
        receipt = store.path('artifacts/agent-prompts/' + sha(rpc_id) + '.json')
        if method == 'session.prompt':
            fingerprint = sha(canonical(body))
            if receipt.is_file():
                saved = json.loads(receipt.read_text(encoding='utf-8'))
                if saved['fingerprint'] != fingerprint:
                    raise HTTPException(409, '相同请求 ID 不能用于不同问题。')
                if saved.get('response'):
                    return saved['response']
                raise HTTPException(409, '发送状态待确认，请刷新会话，不要重复提交。')
            try:
                context = ConversationContext.model_validate(body.get('context', {}))
            except ValueError as exc:
                raise HTTPException(422, '问答范围格式无效。') from exc
            validate_context(store, context)
            listing = await upstream('session.list', {})
            sessions = listing.get('result', {}).get('value', {}).get('items', [])
            selected = next((s for s in sessions if s['sessionId'] == session_id), None)
            if not selected:
                raise HTTPException(404, 'DSH 会话不存在。')
            if selected.get('running'):
                raise HTTPException(409, '请等待或停止当前回答后再发送。')
            if selected.get('cwd') and Path(selected['cwd']).resolve() != Path(os.getenv('NOODLE_CONFIG_DIR') or Path.cwd()).resolve():
                raise HTTPException(409, '该会话属于其他工作区。')
            content = payload.get('content', [])
            if not isinstance(content, list) or any(not isinstance(p, dict) or (p.get('type') == 'text' and not isinstance(p.get('text'), str)) for p in content):
                raise HTTPException(422, '消息内容格式无效。')
            user_question = '\n'.join(p['text'] for p in content if p.get('type') == 'text').strip()
            if not user_question or len(user_question) > 4000:
                raise HTTPException(422, '问题请控制在 1–4000 字之间。')
            saved_context = {'context': context.model_dump(), 'rpc_id': rpc_id, 'user_question': user_question}
            path = context_path(store, session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.'+str(uuid4())+'.tmp')
            temporary.write_text(canonical(saved_context), encoding='utf-8')
            temporary.replace(path)
            # Exclusive creation fences simultaneous duplicate submissions.
            receipt.parent.mkdir(parents=True, exist_ok=True)
            try:
                with receipt.open('x', encoding='utf-8') as stream:
                    json.dump({'fingerprint': fingerprint}, stream)
            except FileExistsError as exc:
                raise HTTPException(409, '问题正在发送，请刷新会话确认。') from exc
        result = await upstream(method, payload, rpc_id)
        if method == 'session.prompt':
            receipt.write_text(canonical({'fingerprint': fingerprint, 'response': result}), encoding='utf-8')
        return JSONResponse(result)

    @app.post('/internal/next/agent/rpc/{method}')
    async def rpc(method: str, request: Request):
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(422, '请求不是有效 JSON。') from exc
        if not isinstance(body, dict) or not isinstance(body.get('payload', {}), dict):
            raise HTTPException(422, 'Invalid RPC envelope')
        if method == 'session.prompt' and isinstance(body, dict):
            identity = str((body.get('payload') or {}).get('sessionId', ''))
            lock = session_locks.setdefault(identity, asyncio.Lock())
            async with lock:
                return await dispatch_rpc(method, body)
        return await dispatch_rpc(method, body)

    @app.get('/internal/next/agent/citations/{request_id}/{citation_id}')
    def citation(request_id: str, citation_id: str):
        if not all(re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', v) for v in (request_id, citation_id)):
            raise HTTPException(422, '无效引用身份。')
        path = store.path(f'artifacts/answers/{request_id}.json')
        if not path.is_file():
            raise HTTPException(404, '回答引用记录不存在。')
        response = json.loads(path.read_text(encoding='utf-8'))['response']
        value = next((c for c in response['citations'] if c['citation_id'] == citation_id), None)
        if not value:
            raise HTTPException(404, '指定引用不存在。')
        return value

    @app.get('/internal/next/agent/events/{channel}')
    async def events(channel: str, request: Request):
        if channel not in {'mux', 'host', 'all'}:
            raise HTTPException(404)
        async def stream():
            pending = {}
            try:
                # The installed DSH web carrier serves event downlinks over
                # WebSocket (plain GET is 426), while RPC remains HTTP.
                origin = harness_url()
                channels = ['mux', 'host'] if channel == 'all' else [channel]
                async with AsyncExitStack() as stack:
                    for name in channels:
                        url = origin.replace('http:', 'ws:', 1).replace('https:', 'wss:', 1) + '/api/events.' + name
                        socket = await stack.enter_async_context(connect_events(url, origin=origin, proxy=None, open_timeout=15, max_size=8*1024*1024))
                        iterator = socket.__aiter__()
                        pending[asyncio.create_task(anext(iterator))] = iterator
                    # The combined downlink is ready only after BOTH upstreams
                    # connect. One browser SSE avoids exhausting HTTP/1 slots.
                    yield 'event: bridge\ndata: {"status":"connected"}\n\n'
                    while True:
                        if await request.is_disconnected():
                            break
                        done, _ = await asyncio.wait(pending, timeout=10, return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            # Heartbeats confirm the upstream is still connected,
                            # including an idle host channel with no business events.
                            yield 'event: bridge\ndata: {"status":"connected"}\n\n'
                            continue
                        for completed in done:
                            iterator = pending.pop(completed)
                            try:
                                frame = completed.result()
                            except StopAsyncIteration:
                                return
                            text = frame.decode() if isinstance(frame, bytes) else frame
                            yield 'data: ' + text + '\n\n'
                            pending[asyncio.create_task(anext(iterator))] = iterator
            except (OSError, WebSocketException, asyncio.CancelledError):
                return
            finally:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/internal/next/agent/respond')
    async def respond(request: Request):
        body = await request.json()
        if body.get('type') != 'client-response' or not isinstance(body.get('rpcId'), str):
            raise HTTPException(422, 'Invalid approval response')
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(harness_url()+'/api/respond', json=body)
            return JSONResponse(response.json(), status_code=response.status_code)
