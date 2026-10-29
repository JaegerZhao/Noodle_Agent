"""Current-only map and relation projections. Navigation never becomes RAG evidence."""

import json
from collections import defaultdict

from .contracts import Candidate, MapEntry, Review, Scope, Section, sha
from .eligibility import VALID
from .repository import one, rows


def current_notes(store, *, kind=None, q="", offset=0, limit=100, source_revision=None):
    with store.db.engine.connect() as c:
        records = rows(
            c,
            VALID + " SELECT n.*,r.summary,r.markdown_ref,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.visibility='ACTIVE' AND r.status='PUBLISHED' AND r.policy_version=:policy AND (SELECT active_policy FROM storage_contract WHERE id=1)=:policy AND (n.kind='MOC' OR r.id IN (SELECT id FROM valid)) AND (COALESCE(n.creation_key,'') NOT LIKE 'source-map:%' OR EXISTS(SELECT 1 FROM source_revisions sr JOIN sources s ON s.id=sr.source_id WHERE s.deleted_at IS NULL AND s.current_revision_id=sr.id AND n.creation_key LIKE 'source-map:'||sr.id||':%')) AND (:kind IS NULL OR n.kind=:kind) AND (n.title LIKE :q OR n.aliases_json LIKE :q) AND (:source_key IS NULL OR substr(n.creation_key,1,length(:source_key))=:source_key) ORDER BY n.created_at,n.id",
            kind=kind,
            q="%" + q + "%",
            source_key=f"atomic:{source_revision}:" if source_revision else None,
            policy=store.repository.policy_version,
        )
        valid = [{**dict(n), "eligible": True} for n in records]
    return {"items": valid[offset : offset + limit], "total": len(valid)}


def frozen_candidate(store, revision):
    ref = f"artifacts/builds/{revision}.json"
    with store.db.engine.connect() as c:
        build = one(c, "SELECT sha256 FROM build_artifacts WHERE storage_ref=:r", r=ref)
    if not build:
        raise ValueError("Missing publication contract")
    return Candidate.model_validate(json.loads(store.read(ref, build["sha256"]))["candidate"])


def graph(store, center=None, **kwargs):
    from .graph_projection import graph as project
    return project(store, center, **kwargs)


def refresh_maps(store):
    concepts = current_notes(store, kind="CONCEPT", limit=100000)["items"]
    domains = defaultdict(list)
    for n in concepts:
        domains[n["domain"]].append(n)
    identity = store.content.create_note("MOC", "知识地图", creation_key="auto-map:concepts:v1")
    with store.db.engine.connect() as c:
        head = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=identity)[
            "current_revision_id"
        ]
    entries = []
    for index, (domain, notes) in enumerate(sorted(domains.items())):
        parent = "domain-" + sha(domain)[:20]
        entries.append(MapEntry(entry_id=parent, title=domain))
        for n in sorted(notes, key=lambda n: (n["title"], n["id"])):
            entries.append(
                MapEntry(
                    entry_id=f"note-{n['id']}",
                    parent_entry_id=parent,
                    title=n["title"],
                    target_note_id=n["id"],
                )
            )
    scope = Scope(includes=["当前可用的知识点导航"])
    candidate = Candidate(
        note_id=identity,
        title="知识地图",
        summary="按领域浏览当前知识点。",
        scope=scope,
        map_policy="AUTO",
        sections=[
            Section(
                section_id="overview",
                title="阅读导航",
                question="有哪些知识点？",
                scope=scope,
                markdown="知识点随资料变化持续更新。尚未形成知识点的内容可从原子笔记和来源目录阅读。",
            )
        ],
        map_entries=entries,
        policy_version=store.repository.policy_version,
    )
    if head and frozen_candidate(store, head).fingerprint == candidate.fingerprint:
        return {"note_id": identity, "revision_id": head, "unchanged": True}
    return store.publish(
        candidate,
        expected_revision=head,
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="navigation.auto-map-v1",
            passed=True,
            report={"rule": "valid Concepts grouped by domain; no inferred semantic relation"},
        ),
    )
