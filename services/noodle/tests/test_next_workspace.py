"""P5 acceptance: author provenance, CAS, maps, Vault, visibility and recovery."""

import io
import json
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from noodle_agent.next.api import create_app
from noodle_agent.next.backup import recovery_zip, restore_package
from noodle_agent.next.contracts import Candidate, Input, Link, Review, Scope, Section
from noodle_agent.next.editor import edit_section
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.navigation import frozen_candidate, graph, refresh_maps
from noodle_agent.next.repository import Conflict, InvalidKnowledge
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store
from noodle_agent.next.vault import import_vault, portable_files, reading_zip, write_vault

SCOPE = Scope(includes=["梯度"])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "knowledge")
    yield s
    s.close()


def publish(store, candidate, expected=None):
    return store.publish(
        candidate,
        expected_revision=expected,
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="test",
            passed=True,
        ),
    )


def atomic(store, title="梯度", parts=("梯度是方向导数的线性表示。", "学习率控制更新步长。")):
    source = store.add_source("\n".join(parts).encode(), title, kind="AUTHOR")
    identity = store.create_note("ATOMIC", title)
    candidate = Candidate(
        note_id=identity,
        title=title,
        scope=SCOPE,
        sections=[
            Section(
                section_id=f"s{i}",
                title=f"章节{i}",
                question="如何理解？",
                scope=SCOPE,
                markdown=p,
                evidence_ids=[store.add_evidence(source["revision_id"], p)],
            )
            for i, p in enumerate(parts)
        ],
    )
    return publish(store, candidate), source


def concept(store, source_revision, title="梯度下降"):
    base = frozen_candidate(store, source_revision["revision_id"])
    identity = store.create_note("CONCEPT", title, domain="机器学习")
    sections = [
        s.model_copy(
            update={
                "inputs": [
                    Input(
                        atomic_revision_id=source_revision["revision_id"],
                        section_ids=[s.section_id],
                        evidence_ids=s.evidence_ids,
                        action="CREATE_CONCEPT",
                    )
                ]
            }
        )
        for s in base.sections
    ]
    return publish(store, Candidate(note_id=identity, title=title, scope=SCOPE, sections=sections))


def test_independent_sections_merge_and_same_section_conflicts(store):
    original, _ = atomic(store)
    first = edit_section(
        store,
        original["note_id"],
        expected_revision=original["revision_id"],
        section_id="s0",
        markdown="作者补充：方向需要归一化。",
    )
    second = edit_section(
        store,
        original["note_id"],
        expected_revision=original["revision_id"],
        section_id="s1",
        markdown="作者补充：步长还需结合目标函数。",
    )
    result = store.get_note(original["note_id"])
    assert first["revision_id"] != second["revision_id"]
    assert "方向需要归一化" in result["markdown"] and "步长还需" in result["markdown"]
    assert all(s["protected"] for s in result["sections"])
    with pytest.raises(Conflict):
        edit_section(
            store,
            original["note_id"],
            expected_revision=original["revision_id"],
            section_id="s0",
            markdown="冲突内容",
        )
    repeated = edit_section(
        store,
        original["note_id"],
        expected_revision=first["revision_id"],
        section_id="s1",
        markdown="作者补充：步长还需结合目标函数。",
    )
    assert repeated["unchanged"]


def test_concept_human_edit_has_explicit_support_and_invalidates_with_old_source(store):
    a, source = atomic(store)
    c = concept(store, a)
    edited = edit_section(
        store,
        c["note_id"],
        expected_revision=c["revision_id"],
        section_id="s0",
        markdown="作者补充：该方法的表现与条件有关。",
    )
    note = store.get_note(c["note_id"])
    assert note["eligible"]
    with store.db.engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM concept_inputs WHERE concept_revision_id=:id"),
                {"id": edited["revision_id"]},
            ).scalar_one()
            == 3
        )
    store.set_source_deleted(source["source_id"], True)
    with pytest.raises(InvalidKnowledge):
        store.get_note(c["note_id"])


def test_author_pdf_origins_remain_eligibility_dependencies(store):
    a, _source = atomic(store)
    # Model an inherited origin using a separate immutable AUTHOR snapshot; the validity
    # barrier is identical for PDF origins and does not pretend authored text has PDF pages.
    origin = store.add_source(b"original source", "original", kind="AUTHOR")
    with store.repository.transaction() as c:
        c.execute(
            text("INSERT INTO author_origin_sources VALUES(:r,:s)"),
            {"r": a["revision_id"], "s": origin["revision_id"]},
        )
    assert store.get_note(a["note_id"])["eligible"]
    store.set_source_deleted(origin["source_id"], True)
    with store.db.engine.connect() as c:
        assert a["revision_id"] not in {n["revision_id"] for n in eligible(c)}
    with pytest.raises(InvalidKnowledge):
        store.get_note(a["note_id"])


def test_auto_maps_are_idempotent_and_omit_invalid_targets(store):
    a, source = atomic(store)
    c = concept(store, a)
    first = refresh_maps(store)
    second = refresh_maps(store)
    assert second["unchanged"] and second["revision_id"] == first["revision_id"]
    g = graph(store, c["note_id"])
    assert any(e["relation"] == "DERIVED_FROM" for e in g["edges"])
    assert any(e["relation"] == "MAP_ENTRY" for e in g["edges"])
    store.set_source_deleted(source["source_id"], True)
    refresh_maps(store)
    assert not store.get_note(first["note_id"])["map_entries"]
    assert c["note_id"] not in {n["id"] for n in graph(store)["nodes"]}


def test_confirmed_prerequisite_cycle_is_rejected(store):
    a, _ = atomic(store, "A")
    b, _ = atomic(store, "B")
    first = frozen_candidate(store, a["revision_id"])
    first = first.model_copy(
        update={
            "links": [
                Link(
                    target_note_id=b["note_id"],
                    relation="PREREQUISITE_OF",
                    status="CONFIRMED",
                    basis="作者确认",
                )
            ]
        }
    )
    publish(store, first, a["revision_id"])
    second = frozen_candidate(store, b["revision_id"])
    second = second.model_copy(
        update={
            "links": [
                Link(
                    target_note_id=a["note_id"],
                    relation="PREREQUISITE_OF",
                    status="CONFIRMED",
                    basis="作者确认",
                )
            ]
        }
    )
    with pytest.raises(InvalidKnowledge, match="cycle"):
        publish(store, second, b["revision_id"])


def test_vault_import_and_retry_do_not_loop_or_duplicate(store):
    a, _ = atomic(store)
    write_vault(store)
    n = store.get_note(a["note_id"])
    p = store.path("Vault/" + n["stable_path"])
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            "梯度是方向导数的线性表示。", "梯度需要结合方向理解。"
        ),
        encoding="utf-8",
    )
    assert len(import_vault(store)["edits"]) == 1
    report = import_vault(store, apply=True)
    assert len(report["published"]) == 1
    assert import_vault(store, apply=True)["published"] == []
    assert "梯度需要结合方向理解" in store.get_note(a["note_id"])["markdown"]


def test_vault_duplicate_id_and_concurrent_edits_are_not_overwritten(store):
    a, _ = atomic(store)
    write_vault(store)
    n = store.get_note(a["note_id"])
    p = store.path("Vault/" + n["stable_path"])
    copy = p.with_name("copy.md")
    shutil.copyfile(p, copy)
    with pytest.raises(Conflict, match="Duplicate"):
        import_vault(store, apply=True)
    copy.unlink()
    p.write_text(
        p.read_text(encoding="utf-8").replace("梯度是方向导数的线性表示。", "本地改动。"),
        encoding="utf-8",
    )
    edit_section(
        store,
        a["note_id"],
        expected_revision=a["revision_id"],
        section_id="s0",
        markdown="服务端改动。",
    )
    with pytest.raises(Conflict):
        write_vault(store)
    with pytest.raises(Conflict):
        import_vault(store, apply=True)
    assert "本地改动" in p.read_text(encoding="utf-8")


def test_vault_rename_and_deletion_are_not_source_deletion(store):
    a, _source = atomic(store)
    write_vault(store)
    n = store.get_note(a["note_id"])
    p = store.path("Vault/" + n["stable_path"])
    moved = p.with_name("renamed.md")
    p.rename(moved)
    report = import_vault(store, apply=True)
    assert len(report["renamed"]) == 1 and p.exists()
    assert not moved.exists()
    p.unlink()
    report = import_vault(store, apply=True)
    assert report["missing"] == [a["note_id"]] and p.exists()
    assert store.get_note(a["note_id"])["eligible"]


def test_public_reading_package_excludes_private_notes_relations_and_pdfs(store):
    a, _ = atomic(store, "public-title")
    b, _ = atomic(store, "private-title")
    ca = concept(store, a)
    cb = concept(store, b, "private-concept")
    moc = refresh_maps(store)
    with store.repository.transaction() as c:
        for identity in (ca["note_id"], moc["note_id"]):
            c.execute(text("INSERT INTO publication_visibility VALUES(:id,1)"), {"id": identity})
    files = portable_files(store, public=True, obsidian=False)
    assert not any(path.startswith("blobs/") or path.endswith(".pdf") for path in files)
    assert not any(
        b"private-title" in data or b"private-concept" in data for data in files.values()
    )
    assert cb["note_id"] not in json.dumps(json.loads(files[".noodle-manifest.json"]))
    with zipfile.ZipFile(io.BytesIO(reading_zip(store))) as archive:
        assert ".noodle-manifest.json" in archive.namelist()


def test_recovery_roundtrip_and_archive_escape_rejected(store, tmp_path):
    a, _ = atomic(store)
    data = recovery_zip(store)
    report = restore_package(data, tmp_path / "restored")
    assert report["integrity"] == "ok"
    other = Store(tmp_path / "restored")
    assert other.get_note(a["note_id"])["markdown"] == store.get_note(a["note_id"])["markdown"]
    other.close()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr(
            "recovery-manifest.json",
            json.dumps(
                {
                    "contract": "next-recovery-v1",
                    "files": [{"path": "../outside.md", "sha256": "x", "bytes": 1}],
                }
            ),
        )
        z.writestr("../outside.md", "x")
    with pytest.raises(InvalidKnowledge):
        restore_package(stream.getvalue(), tmp_path / "unsafe")
    assert not (tmp_path / "outside.md").exists()


def test_workspace_api_is_next_only_and_edit_has_cas(store):
    a, source = atomic(store)
    app = Application(store, index_mode="KEYWORD_ONLY")
    with TestClient(create_app(application=app)) as client:
        assert client.get("/app/").status_code == 200
        assert client.get("/internal/next/library").json()["total"] == 1
        assert client.get("/internal/next/library?kind=ATOMIC").status_code == 200
        assert client.get("/internal/next/sources").status_code == 200
        assert client.get("/internal/next/graph").json()["nodes"][0]["id"] == a["note_id"]
        bad = client.put(
            "/internal/next/notes/" + a["note_id"] + "/sections",
            json={"expected_revision": "unknown", "section_id": "s0", "markdown": "新内容"},
        )
        assert bad.status_code == 409
        maps = client.post("/internal/next/maps/refresh").json()
        assert client.get("/internal/next/notes/" + maps["note_id"]).status_code == 200
        assert client.get("/internal/v1/notes").status_code == 404
        assert client.get("/internal/next/assets/not-an-image.png").status_code == 404
        wrong = client.put(
            "/internal/next/sources/" + source["source_id"] + "/state",
            json={"expected_revision": "unknown", "deleted": True},
        )
        assert wrong.status_code == 409


def test_checked_parallel_stops_queuing_after_failure():
    from noodle_agent.next.parallel import checked_parallel

    seen = []

    def fail(item):
        seen.append(item)
        raise ValueError("provider failed")

    with pytest.raises(ValueError, match="provider failed"):
        list(checked_parallel(fail, range(1000), 1))
    assert seen == [0]


def test_compact_candidates_and_retrieved_identity_audit_are_bounded():
    from noodle_agent.next.concepts.catalogue import audit_identity, candidates
    from noodle_agent.next.concepts.contracts import Identity
    from noodle_agent.next.contracts import canonical

    catalogue = [
        {
            "id": f"concept-{i}",
            "title": f"Topic {i}",
            "domain": "ML",
            "aliases": [],
            "sections": [{"section_id": "main", "markdown": "detail " * 2000}],
            "scope": {"includes": [f"topic {i}"]},
        }
        for i in range(20)
    ]
    selected, complete = candidates(catalogue, {"text": "Topic 15", "title": "Topic 15"})
    assert not complete and len(canonical(selected)) <= 60000
    assert len(selected) < len(catalogue)

    class Calls:
        def __init__(self):
            self.checked = []

        def run(self, operation, system, content, contract, check=None):
            self.checked.extend(item["id"] for item in content["identities"])
            result = contract(concept_id=None, reason="different scope")
            check(result)
            return result

    calls = Calls()
    match, report = audit_identity(
        calls,
        Identity(title="New", domain="ML", identity_key="new-topic", question="Why?", scope=SCOPE),
        catalogue,
        budget=1000,
    )
    assert match is None and not report["all_pages_checked"]
    assert set(calls.checked) < {item["id"] for item in catalogue}
    assert len(canonical(selected)) < 14000
    assert all("markdown" not in section for item in selected for section in item["sections"])


def test_edited_pdf_atomic_retains_source_scope_and_single_family(store):
    from test_next_content import atomic as pdf_atomic

    from noodle_agent.next.retrieval.contracts import Query
    from noodle_agent.next.retrieval.indexing import Indexer
    from noodle_agent.next.retrieval.search import Searcher

    source, _anchor, identity, old = pdf_atomic(store)
    updated = edit_section(
        store,
        identity,
        expected_revision=old["revision_id"],
        section_id="definition",
        markdown="作者说明：链式法则用于计算复合函数的梯度。",
    )
    index = Indexer(store)
    index.build("KEYWORD_ONLY")
    found = Searcher(index).search(
        Query(
            query="链式法则",
            retrieval_mode="KEYWORD_ONLY",
            rerank="OFF",
            allowed_kinds=["ATOMIC"],
            scope={"mode": "SOURCE", "source_ids": [source["source_id"]]},
        )
    )
    assert found["status"] == "READY" and found["hits"][0]["revision_id"] == updated["revision_id"]
    assert found["counts"]["independent_source_families"] == 1
    assert found["hits"][0]["provenance"][0]["page"] is None
    store.set_source_deleted(source["source_id"], True)
    assert (
        Searcher(index).search(Query(query="链式法则", retrieval_mode="KEYWORD_ONLY"))["hits"] == []
    )


def test_vault_projection_resumes_a_crash_and_keeps_manifest_last(store, monkeypatch):
    from noodle_agent.next import vault_projection as projection

    a, _ = atomic(store)
    write_vault(store)
    original = store.get_note(a["note_id"])
    path = store.path("Vault/" + original["stable_path"])
    edit_section(
        store,
        a["note_id"],
        expected_revision=a["revision_id"],
        section_id="s0",
        markdown="服务器更新后的正文。",
    )
    real = projection.atomic_write

    def fail_manifest(path, data):
        if path.name == ".noodle-manifest.json" and path.parent == store.path("Vault"):
            raise OSError("simulated power loss")
        return real(path, data)

    monkeypatch.setattr(projection, "atomic_write", fail_manifest)
    with pytest.raises(OSError, match="power loss"):
        write_vault(store)
    assert store.path("Vault/.noodle-pending.json").exists()
    monkeypatch.setattr(projection, "atomic_write", real)
    write_vault(store)
    assert not store.path("Vault/.noodle-pending.json").exists()
    assert "服务器更新后的正文" in path.read_text(encoding="utf-8")
    assert import_vault(store)["edits"] == []


def test_markdown_links_derive_backlinks_and_standard_export(store):
    a, _ = atomic(store, "Gradient")
    b, _ = atomic(store, "Optimizer")
    edit_section(
        store,
        a["note_id"],
        expected_revision=a["revision_id"],
        section_id="s0",
        markdown="阅读 [[Optimizer|优化器]]。代码中的 `[[NotALink]]` 只是文本。",
    )
    relation = graph(store, b["note_id"])
    assert any(
        e["source"] == a["note_id"]
        and e["target"] == b["note_id"]
        and "markdown_range" in e["basis"]
        for e in relation["edges"]
    )
    assert not relation["unresolved"]
    files = portable_files(store, obsidian=False)
    body = files[store.get_note(a["note_id"])["stable_path"]].decode()
    assert "[[Optimizer|优化器]]" not in body and "[[NotALink]]" in body
    assert "[优化器](" in body


def test_two_human_edits_do_not_add_independent_pdf_families(store):
    from test_next_content import atomic as pdf_atomic

    source, _anchor, identity, old = pdf_atomic(store)
    first = edit_section(
        store,
        identity,
        expected_revision=old["revision_id"],
        section_id="definition",
        markdown="第一次作者修订。",
    )
    second = edit_section(
        store,
        identity,
        expected_revision=first["revision_id"],
        section_id="definition",
        markdown="第二次作者修订。",
    )
    candidate = frozen_candidate(store, second["revision_id"])
    assert candidate.origin_source_revisions == [source["revision_id"]]


def test_vault_generated_footer_and_identity_edits_are_preserved_as_conflicts(store):
    a, _ = atomic(store)
    write_vault(store)
    note = store.get_note(a["note_id"])
    path = store.path("Vault/" + note["stable_path"])
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\nLocal footer text\n", encoding="utf-8")
    with pytest.raises(Conflict, match="navigation"):
        import_vault(store, apply=True)
    assert "Local footer text" in path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(json.dumps(a["note_id"]), json.dumps("changed-id"), 1), encoding="utf-8"
    )
    with pytest.raises(Conflict, match="identity"):
        import_vault(store, apply=True)


def test_recovery_rejects_missing_body_even_if_remaining_hashes_match(store, tmp_path):
    atomic(store)
    content = recovery_zip(store)
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as old, zipfile.ZipFile(result, "w") as new:
        manifest = json.loads(old.read("recovery-manifest.json"))
        manifest["files"] = [f for f in manifest["files"] if not f["path"].startswith("revisions/")]
        for f in manifest["files"]:
            new.writestr(f["path"], old.read(f["path"]))
        new.writestr("recovery-manifest.json", json.dumps(manifest))
    with pytest.raises(InvalidKnowledge, match="file reference"):
        restore_package(result.getvalue(), tmp_path / "incomplete")
    assert not (tmp_path / "incomplete").exists()


def test_failed_concept_edit_withdraws_provisional_author_support_and_can_resume(
    store, monkeypatch
):
    a, _ = atomic(store)
    c = concept(store, a)
    actual = store.publish

    def fail_target(candidate, **kwargs):
        if candidate.note_id == c["note_id"]:
            raise Conflict("simulated concurrent target change")
        return actual(candidate, **kwargs)

    monkeypatch.setattr(store, "publish", fail_target)
    with pytest.raises(Conflict):
        edit_section(
            store,
            c["note_id"],
            expected_revision=c["revision_id"],
            section_id="s0",
            markdown="作者补充的解释。",
        )
    with store.db.engine.connect() as conn:
        support = (
            conn.execute(
                text(
                    "SELECT id,current_revision_id,visibility FROM notes WHERE creation_key LIKE 'author:%'"
                )
            )
            .mappings()
            .one()
        )
        assert support["visibility"] == "WITHDRAWN"
        assert support["current_revision_id"] not in {n["revision_id"] for n in eligible(conn)}
    monkeypatch.setattr(store, "publish", actual)
    result = edit_section(
        store,
        c["note_id"],
        expected_revision=c["revision_id"],
        section_id="s0",
        markdown="作者补充的解释。",
    )
    assert store.get_note(c["note_id"])["revision"]["id"] == result["revision_id"]


def test_generation_task_stays_visible_when_maintenance_fills_page(store):
    from noodle_agent.next.contracts import canonical
    from noodle_agent.next.repository import identifier, insert, now

    with store.repository.transaction() as c:
        event = store.repository.event(c, "TEST_ROOT", "source-a", None, {}, key="test-root")
        root = identifier()
        insert(
            c,
            "jobs",
            id=root,
            event_id=event,
            kind="GENERATE_ATOMICS",
            status="PENDING",
            input_json=canonical({"entity_id": "source-a"}),
            created_at=now(),
        )
        for i in range(205):
            event = store.repository.event(
                c, "TEST_MAINTENANCE", "source-a", None, {}, key=f"test-maintenance-{i}"
            )
            insert(
                c,
                "jobs",
                id=identifier(),
                event_id=event,
                kind="INDEX_NOTE",
                status="PENDING",
                input_json="{}",
                created_at=now(),
            )
    with TestClient(
        create_app(application=Application(store, index_mode="KEYWORD_ONLY"))
    ) as client:
        result = client.get("/internal/next/jobs")
        assert result.status_code == 200
        jobs = result.json()
        assert len(jobs) == 200 and jobs[0]["id"] == root
        assert jobs[0]["entity_id"] == "source-a" and jobs[0]["created_at"]


def test_pdf_reader_rejects_an_outdated_expected_source_revision(store):
    import fitz

    with fitz.open() as pdf:
        pdf.new_page()
        source = store.add_source(pdf.tobytes(), "PDF reader fixture")
    with TestClient(
        create_app(application=Application(store, index_mode="KEYWORD_ONLY"))
    ) as client:
        url = f"/internal/next/sources/{source['source_id']}/file"
        assert client.get(url, params={"revision": source["revision_id"]}).status_code == 200
        assert client.get(url, params={"revision": "outdated"}).status_code == 409


def test_pdfjs_module_mime_does_not_depend_on_windows_registry(store):
    with TestClient(
        create_app(application=Application(store, index_mode="KEYWORD_ONLY"))
    ) as client:
        for filename in ["pdf.min.mjs", "pdf.worker.min.mjs"]:
            response = client.get("/app/static/vendor/pdfjs/" + filename)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/javascript")
        assert client.get("/app/static/vendor/pdfjs/not-allowed.mjs").status_code == 404


def test_source_reading_includes_active_notes_before_chapter_map_publication(store):
    first, source = atomic(store)
    other, _ = atomic(store, title="其他资料", parts=("另一份资料的独立内容。",))
    prefix = f"atomic:{source['revision_id']}:"
    with store.repository.transaction() as c:
        c.execute(
            text("UPDATE notes SET creation_key=:key WHERE id=:id"),
            {"key": prefix + "plan:chapter2.topic", "id": first["note_id"]},
        )
    with TestClient(
        create_app(application=Application(store, index_mode="KEYWORD_ONLY"))
    ) as client:
        url = "/internal/next/library"
        params = {"kind": "ATOMIC", "source_revision": source["revision_id"]}
        result = client.get(url, params=params).json()
        assert result["total"] == 1
        assert [n["id"] for n in result["items"]] == [first["note_id"]]
        # Author edits retain the generated identity and its original-source fence.
        edit_section(
            store,
            first["note_id"],
            expected_revision=first["revision_id"],
            section_id="s0",
            markdown="作者补充当前章节。",
        )
        assert client.get(url, params=params).json()["total"] == 1
        store.sources.set_source_deleted(
            source["source_id"], True, expected_revision=source["revision_id"]
        )
        assert client.get(url, params=params).json()["total"] == 0
        assert (
            client.get(url, params={"kind": "ATOMIC"}).json()["items"][0]["id"] == other["note_id"]
        )
