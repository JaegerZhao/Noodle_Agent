"""Next-only workspace API: navigation, explicit author edits and portable packages."""

from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .contracts import Candidate, Id, Link, MapEntry, Review, Scope, Section
from .editor import edit_section
from .navigation import current_notes, frozen_candidate, graph, refresh_maps
from .repository import Conflict, InvalidKnowledge, one, rows
from .vault import import_vault, reading_zip, write_vault


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Id
    section_id: Id
    markdown: str = Field(min_length=1, max_length=100000)
    title: str | None = Field(default=None, min_length=1, max_length=500)


class SourceState(BaseModel):
    expected_revision: Id
    deleted: bool


class FolderEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=500)
    expected_version: int | None = None


class SourceMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_metadata_version: int = Field(ge=0)
    display_title: str | None = Field(default=None, min_length=1, max_length=500)
    folder_id: Id | None = None


class BatchSourceItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: Id
    expected_revision: Id | None = None
    expected_metadata_version: int = Field(default=0, ge=0)


class SourceBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: Id
    action: Literal['move', 'delete', 'restore']
    items: list[BatchSourceItem] = Field(min_length=1, max_length=500)
    folder_id: Id | None = None


class PublicState(BaseModel):
    expected_revision: Id
    public: bool


class NoteState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: Id
    active: bool


class Relations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Id
    links: list[Link] = Field(max_length=200)
    editor_contract: str | None = None


class MapEdit(BaseModel):
    note_id: Id | None = None
    expected_revision: Id | None = None
    title: str = Field(min_length=1, max_length=500)
    entries: list[MapEntry] = Field(max_length=5000)
    knowledge_root: bool = False


def install(app, store):
    def current_profile():
        profile = app.state.application.indexer.profile
        return profile['id'] if profile else 'unconfigured'

    @app.get('/internal/next/source-revisions/{revision}/readiness')
    def source_readiness(revision: str):
        from .pipeline_state import readiness
        return readiness(store, revision, profile_id=current_profile())

    @app.get('/internal/next/source-revisions/{revision}/reading')
    def source_reading(revision: str, view: Literal['full','manifest','version'] = 'full'):
        if view != 'full':
            from .reading_manifest import manifest
            result = manifest(store, revision, version_only=view=='version')
        else:
            from .pipeline_state import reading
            result = reading(store, revision, profile_id=current_profile())
        with store.db.engine.connect() as c:
            meta = one(c, '''SELECT m.display_title,m.metadata_version FROM source_library_metadata m
                JOIN source_revisions r ON r.source_id=m.source_id WHERE r.id=:r''', r=revision)
        if meta:
            from .contracts import sha
            result['version'] = sha((result.get('version', '') + str(meta['metadata_version'])).encode())
            if meta['display_title']:
                result['display_title'] = meta['display_title']
        return result

    @app.get('/internal/next/knowledge/roots')
    def knowledge_roots():
        from .knowledge_roots import roots
        return roots(store)

    @app.get('/internal/next/workspace/summary')
    def workspace_summary():
        from .knowledge_roots import roots
        notes = current_notes(store, limit=100000)['items']
        items = roots(store, _notes=notes)['items']
        with store.db.engine.connect() as c:
            sources = c.exec_driver_sql("SELECT count(*) FROM sources WHERE kind='PDF' AND deleted_at IS NULL").scalar_one()
        return {'sources':sources, 'concepts':sum(n['kind']=='CONCEPT' for n in notes),
            'maps':len(items)}

    @app.get('/internal/next/knowledge/roots/{identity}/tree')
    def knowledge_tree(identity: str, limit: int = Query(300, ge=1, le=3000), offset: int = Query(0, ge=0),
                       snapshot: str | None = None, parent_entry_id: str | None = None):
        from .knowledge_roots import tree
        return tree(store, identity, limit=limit, offset=offset, snapshot=snapshot, parent_entry_id=parent_entry_id)

    @app.get('/internal/next/knowledge/roots/{identity}/members')
    def knowledge_members(identity: str, limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0), q: str = ''):
        from .knowledge_roots import member_cards
        return member_cards(store, identity, limit=limit, offset=offset, q=q)

    @app.get("/internal/next/jobs/{identity}/timeline")
    def job_timeline(identity: str):
        from .metrics import project_readiness, timeline

        try:
            result = timeline(store, identity)
            import json

            from .pipeline_state import readiness
            revision = json.loads(result['job']['input_json']).get('revision_id')
            if revision:
                with store.db.engine.connect() as c:
                    available = store.sources.source_valid(c,revision)
                result['readiness'] = readiness(store, revision, profile_id=current_profile()) if available else None
                result['historical_source'] = not available
            return project_readiness(result)
        except KeyError as exc:
            raise HTTPException(404, "Unknown task") from exc

    @app.get('/internal/next/generation-tasks')
    def generation_tasks(limit: int = Query(20, ge=1, le=100), cursor: str | None = None):
        import base64
        import json
        after = {}
        if cursor:
            try:
                stamp, identity = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if not isinstance(stamp, str) or not isinstance(identity, str):
                    raise TypeError('Invalid cursor')
                after = {'stamp': stamp, 'identity': identity}
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, 'Invalid task cursor') from exc
        where = "AND (j.created_at<:stamp OR (j.created_at=:stamp AND j.id<:identity))" if after else ''
        with store.db.engine.connect() as c:
            items = [dict(r) for r in rows(c, "SELECT j.id,j.status,j.created_at,j.attempts,j.error_json,p.stage,p.details_json,json_extract(j.input_json,'$.revision_id') source_revision_id,"
                "(SELECT coalesce(m.display_title,s.title) FROM sources s LEFT JOIN source_library_metadata m ON m.source_id=s.id WHERE s.id=json_extract(j.input_json,'$.entity_id')) source_title,"
                "(SELECT s.deleted_at FROM sources s WHERE s.id=json_extract(j.input_json,'$.entity_id')) source_deleted_at,"
                "(SELECT s.current_revision_id FROM sources s WHERE s.id=json_extract(j.input_json,'$.entity_id')) source_current_revision_id "
                "FROM jobs j LEFT JOIN job_progress p ON p.job_id=j.id WHERE j.kind='GENERATE_ATOMICS' "
                + where + ' ORDER BY j.created_at DESC,j.id DESC LIMIT :limit', **after, limit=limit+1)]
        more = len(items)>limit
        items = items[:limit]
        next_cursor = base64.urlsafe_b64encode(json.dumps([items[-1]['created_at'],items[-1]['id']]).encode()).decode() if more else None
        return {'items':items, 'next_cursor':next_cursor}

    @app.post("/internal/next/jobs/{identity}/pause")
    def pause_job(identity: str):
        from .task_control import request_pause

        return request_pause(store, identity)

    @app.post("/internal/next/jobs/{identity}/resume")
    def resume_job(identity: str):
        from .task_control import resume

        return resume(store, identity)

    @app.get("/internal/next/config")
    def configuration():
        import os
        from urllib.parse import urlsplit

        url = os.getenv("NOODLE_HARNESS_WEB_URL", "http://127.0.0.1:3080")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            url = "http://127.0.0.1:3080"
        return {"contract": "next-v1", "harness_url": url, "vault_auto_import": "explicit opt-in"}

    @app.get("/internal/next/library")
    def library(
        kind: Literal["ATOMIC", "CONCEPT", "MOC"] | None = None,
        q: str = "",
        source_revision: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        from .learning_titles import present_titles
        result = current_notes(
            store, kind=kind, q=q, offset=offset, limit=limit, source_revision=source_revision
        )
        result['items'] = present_titles(store, result['items'])
        return result

    @app.get("/internal/next/source-revisions/{revision}/outline")
    def source_outline(revision: str):
        from .source_structure import source_structure

        return source_structure(store, revision)

    @app.get("/internal/next/sources")
    def sources(folder: str = 'all', q: str = '', sort: str = 'created_desc',
                offset: int = Query(0, ge=0), limit: int | None = Query(None, ge=1, le=500),
                include_deleted: bool = True):
        from .source_library import list_sources
        result = list_sources(store, folder=folder, q=q, sort=sort, offset=offset, limit=limit,
                              include_deleted=include_deleted)
        return result if limit is not None else result['items']

    @app.get('/internal/next/source-folders')
    def source_folders():
        from .source_library import folders
        return folders(store)

    @app.post('/internal/next/source-folders')
    def create_folder(value: FolderEdit):
        from .source_library import folder_write
        return folder_write(store, value.name)

    @app.patch('/internal/next/source-folders/{identity}')
    def update_folder(identity: str, value: FolderEdit):
        from .source_library import folder_write
        return folder_write(store, value.name, identity, value.expected_version)

    @app.delete('/internal/next/source-folders/{identity}')
    def remove_folder(identity: str, expected_version: int):
        from .source_library import folder_delete
        return folder_delete(store, identity, expected_version)

    @app.patch('/internal/next/sources/{identity}/metadata')
    def source_metadata(identity: str, value: SourceMetadata):
        from .source_library import update_metadata
        changes = value.model_dump(exclude_unset=True, exclude={'expected_metadata_version'})
        if 'display_title' in changes and changes['display_title'] is None:
            raise HTTPException(422, 'Display title cannot be null')
        return update_metadata(store, identity, changes, value.expected_metadata_version)

    @app.post('/internal/next/source-library/batch')
    def source_batch(value: SourceBatch):
        from .source_library import batch
        if value.action != 'move' and any(not i.expected_revision for i in value.items):
            raise HTTPException(422, 'Lifecycle operations require current source revisions')
        return batch(store, value.operation_id, value.action,
                     [i.model_dump() for i in value.items], value.folder_id)

    @app.get('/internal/next/source-statuses')
    def source_statuses():
        from .organization import coverage
        from .pipeline_state import readiness
        from .source_library import list_sources
        structure = coverage(store)
        return {'items': [readiness(store, s['current_revision_id'], profile_id=current_profile(), structure=structure)
                          for s in list_sources(store, include_deleted=False)['items'] if s['kind'] == 'PDF']}

    @app.put("/internal/next/sources/{identity}/state")
    def source_state(identity: str, value: SourceState):
        try:
            return store.sources.set_source_deleted(
                identity, value.deleted, expected_revision=value.expected_revision
            )
        except KeyError as exc:
            raise HTTPException(404, "Unknown source") from exc

    @app.get("/internal/next/resolve-link")
    def resolve_link(owner: str, target: str):
        from .markdown_links import resolve_reference

        note = store.get_note(owner)
        found = resolve_reference(target, note, current_notes(store, limit=100000)["items"])
        return {"note_id": found["id"]}

    @app.get("/internal/next/concept-graph")
    def entity_graph(center: str | None = None, root: str | None = None, hops: int = Query(1, ge=1, le=4),
                     limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                     snapshot: str | None = None):
        from .knowledge_graph import concept_graph
        allowed = None
        if root and root != 'all':
            from .knowledge_roots import membership
            _, allowed = membership(store, root)
        return concept_graph(store, center=center, hops=hops, limit=limit, offset=offset, snapshot=snapshot, allowed=allowed)

    @app.get("/internal/next/concept-outline/{identity}")
    def entity_outline(identity: str, max_depth: int = Query(8, ge=0, le=30),
                       limit: int = Query(300, ge=1, le=3000), snapshot: str | None = None):
        from .knowledge_graph import concept_outline
        return concept_outline(store, identity, max_depth=max_depth, limit=limit, snapshot=snapshot)

    @app.get("/internal/next/graph")
    def relations(
        center: str | None = None, suggestions: bool = False, limit: int = Query(300, ge=1, le=1000),
        offset: int = Query(0, ge=0), snapshot: str | None = None, root: str | None = None,
        layers: str | None = None, node_kinds: str | None = None, hops: int = Query(1, ge=1, le=4), connected_only: bool = False,
        mode: Literal['hierarchy', 'semantic', 'document', 'provenance'] | None = None, relations: str | None = None,
        q: str = '', include_external: bool = False
    ):
        allowed = None
        if root and root != 'all':
            from .knowledge_roots import membership
            _, members = membership(store, root)
            from .graph_projection import projection
            _, projected_edges, _ = projection(store)
            allowed = members | {e['target'] for e in projected_edges if e['source'] in members and e['layer'] == 'SUPPORT'}
            if include_external:
                focus = members | ({center} if center else set())
                allowed |= focus | {n for e in projected_edges if e['source'] in focus or e['target'] in focus for n in (e['source'], e['target'])}
        return graph(store, center, include_suggestions=suggestions, limit=limit, offset=offset,
                     snapshot=snapshot, allowed=allowed, layers=layers.split(',') if layers else None,
                     node_kinds=node_kinds.split(',') if node_kinds else None, hops=hops, connected_only=connected_only,
                     mode=mode, relations=relations.split(',') if isinstance(relations, str) and relations else None, q=q)

    @app.get('/internal/next/knowledge/maintenance')
    def knowledge_maintenance():
        from .organization import coverage
        with store.db.engine.connect() as c:
            issues = [dict(r) for r in rows(c, "SELECT id,status,error_json,created_at FROM jobs WHERE kind='REVIEW_ORGANIZATION' AND status='REVIEW' ORDER BY created_at DESC LIMIT 20")]
        return {'coverage': coverage(store), 'historical_issues': issues}

    @app.get('/internal/next/notes/{identity}/sources')
    def concept_source_groups(identity: str, revision: str | None = None):
        from .concept_sources import sources
        return sources(store,identity,revision)

    @app.get("/internal/next/notes/{identity}/history")
    def history(identity: str, limit: int | None = Query(None, ge=1, le=100), offset: int = Query(0, ge=0)):
        with store.db.engine.connect() as c:
            items = [
                dict(r)
                for r in rows(
                    c,
                    "SELECT id,parent_revision_id,ordinal,title,author,status,created_at,markdown_sha256 FROM note_revisions WHERE note_id=:id ORDER BY ordinal DESC",
                    id=identity,
                )
            ]
        if limit is None:
            return items
        from .note_history import change_kind
        page=[{**item,'change_kind':change_kind(store,item)} for item in items[offset:offset+limit]]
        return {'items': page, 'total': len(items),
                'next_offset': offset+limit if offset+limit<len(items) else None}

    @app.get("/internal/next/notes/{identity}/revisions/{revision}")
    def historical(identity: str, revision: str):
        return store.get_note(identity, revision_id=revision, historical=True)

    @app.put("/internal/next/notes/{identity}/sections")
    def edit(identity: str, value: Edit):
        return edit_section(store, identity, **value.model_dump())

    @app.put("/internal/next/notes/{identity}/public")
    def visibility(identity: str, value: PublicState):
        with store.repository.transaction() as c:
            n = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=identity)
            if not n or n["current_revision_id"] != value.expected_revision:
                raise Conflict("Note revision changed")
            c.execute(
                text(
                    "INSERT INTO publication_visibility(note_id,is_public) VALUES(:n,:p) ON CONFLICT(note_id) DO UPDATE SET is_public=excluded.is_public"
                ),
                {"n": identity, "p": int(value.public)},
            )
        return {"note_id": identity, "public": value.public}

    @app.get("/internal/next/notes/{identity}/links")
    def read_owned_links(identity: str):
        from .relations import owned_links
        return owned_links(store, identity)

    @app.get('/internal/next/notes/{identity}/connections')
    def note_connections(identity: str):
        data = graph(store, center=identity, limit=1000)
        return {**data, 'note_id': identity}

    @app.put('/internal/next/notes/{identity}/state')
    def note_state(identity: str, value: NoteState):
        operation = store.restore_note if value.active else store.withdraw_note
        return operation(identity, expected_revision=value.expected_revision)

    @app.put("/internal/next/notes/{identity}/links")
    def links(identity: str, value: Relations):
        from .relations import update_author_links
        return update_author_links(store, identity, value.expected_revision, value.links,
                                   editor_contract=value.editor_contract)

    @app.post("/internal/next/maps")
    def save_map(value: MapEdit):
        from .repository import identifier
        identity = value.note_id or store.content.create_note('MOC', value.title, creation_key='knowledge-root:' + identifier() if value.knowledge_root else None)
        if value.note_id:
            n = store.get_note(identity)
            if n["kind"] != "MOC":
                raise InvalidKnowledge("Expected a map")
        scope = Scope(includes=["阅读导航"])
        if value.note_id:
            old = frozen_candidate(store, n["revision"]["id"])
            if old.map_policy != "MANUAL":
                raise InvalidKnowledge("Copy a system map to a new personal map before editing")
            candidate = old.model_copy(update={"title": value.title, "map_entries": value.entries})
        else:
            candidate = Candidate(
                note_id=identity,
                title=value.title,
                scope=scope,
                sections=[
                    Section(
                        section_id="overview",
                        title="关于这张地图",
                        question="如何阅读？",
                        scope=scope,
                        markdown="沿下面的目录浏览相关知识点。",
                    )
                ],
                map_entries=value.entries,
                policy_version=store.repository.policy_version,
            )
        return store.publish(
            candidate,
            expected_revision=value.expected_revision,
            review=Review(
                candidate_sha256=candidate.fingerprint,
                policy_version=candidate.policy_version,
                reviewer="author.map",
                passed=True,
            ),
        )

    @app.post("/internal/next/maps/refresh")
    def maps():
        return refresh_maps(store)

    @app.post("/internal/next/vault/export")
    def vault_export():
        return write_vault(store)

    @app.post("/internal/next/vault/import")
    def vault_import(apply: bool = False):
        import json

        try:
            return import_vault(store, apply=apply)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise Conflict("Keep Vault identity metadata as exported JSON-compatible YAML") from exc

    @app.get("/internal/next/notes/{identity}/export")
    def export_article(identity: str):
        from .exports import article_zip

        return Response(
            article_zip(store, identity),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=noodle-article.zip"},
        )

    @app.get("/internal/next/export")
    def export(public: bool = False, obsidian: bool = True):
        return Response(
            reading_zip(store, public=public, obsidian=obsidian),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=noodle-knowledge.zip"},
        )

    @app.get("/internal/next/recovery")
    def backup():
        from .backup import recovery_zip

        return Response(
            recovery_zip(store),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=noodle-recovery.zip"},
        )

    @app.post("/internal/next/jobs/{identity}/retry")
    def retry(identity: str):
        store.jobs.retry_review(identity)
        return {"job_id": identity, "status": "PENDING"}

    @app.get("/internal/next/assets/{filename}")
    def asset(filename: str):
        import re

        if not re.fullmatch(r"[a-f0-9]{64}\.(png|jpg|jpeg|webp)", filename):
            raise HTTPException(404, "Unknown image")
        path = store.path("assets/" + filename)
        if not path.is_file():
            raise HTTPException(404, "Unknown image")
        from .contracts import sha

        if sha(path.read_bytes()) != path.stem:
            raise InvalidKnowledge("Image hash differs")
        return FileResponse(path)

    web = Path(__file__).parent.parent / "web"

    @app.get("/")
    @app.get("/app/")
    def workbench():
        return FileResponse(web / "next/index.html")

    @app.get("/app/static/vendor/pdfjs/{filename}")
    def pdfjs_module(filename: str):
        # Windows MIME registry can label .mjs as text/plain, which browsers reject.
        if filename not in {"pdf.min.mjs", "pdf.worker.min.mjs"}:
            raise HTTPException(404, "Unknown PDF module")
        return FileResponse(web / "vendor/pdfjs" / filename, media_type="text/javascript")

    app.mount("/app/static", StaticFiles(directory=web), name="next-static")


def require_current(store, connection, revision):
    if not store.content.valid_revision(connection, revision):
        raise Conflict("Knowledge support changed")
