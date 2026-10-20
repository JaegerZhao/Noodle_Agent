import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from noodle_agent.next.contracts import Candidate, Input, MapEntry, Review, Scope, Section, sha
from noodle_agent.next.store import Conflict, InvalidKnowledge, Store, one
from noodle_agent.storage.database import Database

SCOPE = Scope(includes=["梯度计算"], excludes=["优化器细节"])


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "next")
    yield result
    result.close()


def evidence(store, title="A", quote="链式法则用于计算梯度"):
    source = store.add_source(b"%PDF-fixture-" + title.encode(), title)
    parse = store.record_parse(
        source["revision_id"],
        "fixture-v1",
        {},
        {"blocks": [{"id": "b1", "page": 1, "text": quote}]},
    )
    anchor = store.add_evidence(source["revision_id"], quote, page=1, block_id="b1", parse_id=parse)
    return source, anchor


def candidate(note_id, anchor, *, content="依据来源解释链式法则。", protected=False):
    return Candidate(
        note_id=note_id,
        title="反向传播",
        scope=SCOPE,
        sections=[
            Section(
                section_id="definition",
                title="基本原理",
                question="如何计算梯度？",
                scope=SCOPE,
                markdown=content,
                evidence_ids=[anchor],
                protected=protected,
            )
        ],
    )


def approve(value):
    return Review(
        candidate_sha256=value.fingerprint,
        policy_version=value.policy_version,
        reviewer="fixture-review-not-real-quality",
        passed=True,
    )


def publish(store, value, expected=None):
    return store.publish(value, expected_revision=expected, review=approve(value))


def atomic(store, title="A"):
    source, anchor = evidence(store, title)
    note = store.create_note("ATOMIC", "链式法则")
    result = publish(store, candidate(note, anchor))
    return source, anchor, note, result


def concept(store, atomic_revision, anchor):
    note = store.create_note("CONCEPT", "反向传播")
    value = candidate(note, anchor)
    section = value.sections[0].model_copy(
        update={
            "inputs": [
                Input(
                    atomic_revision_id=atomic_revision,
                    section_ids=["definition"],
                    evidence_ids=[anchor],
                    action="CREATE_CONCEPT",
                )
            ]
        }
    )
    value = value.model_copy(update={"sections": [section]})
    return note, value, publish(store, value)


def test_next_fresh_migration_is_serialized_and_has_no_legacy_tables(tmp_path):
    def initialize(_):
        instance = Store(tmp_path / "shared")
        instance.close()

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(initialize, range(3)))
    with sqlite3.connect(tmp_path / "shared/noodle.db") as connection:
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "notes",
            "note_revisions",
            "sections",
            "concept_inputs",
            "events",
            "chunks",
        } <= tables
        assert not {"atomic_notes", "concept_modules", "syntheses", "note_versions"} & tables
        assert connection.execute("SELECT name FROM storage_contract").fetchone() == ("next-v1",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "next_0003",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_next_refuses_legacy_database_without_ddl(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    with sqlite3.connect(root / "noodle.db") as connection:
        connection.execute("CREATE TABLE old_notes(id TEXT)")
        connection.execute("INSERT INTO old_notes VALUES ('preserve')")
    with pytest.raises(ValueError, match="Legacy or unknown"):
        Store(root)
    with sqlite3.connect(root / "noodle.db") as connection:
        assert connection.execute("SELECT * FROM old_notes").fetchall() == [("preserve",)]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("old_notes",)]
    assert not (root / "backups").exists()


def test_legacy_runner_refuses_next_database(store):
    old = Database(f"sqlite:///{store.root / 'noodle.db'}")
    try:
        with pytest.raises(ValueError, match="contract mismatch"):
            old.init()
    finally:
        old.close()


def test_identical_pdf_is_not_an_independent_source(store):
    first = store.add_source(b"%PDF-identical", "Original")
    second = store.add_source(b"%PDF-identical", "Renamed")
    assert second["duplicate"]
    assert first["source_id"] == second["source_id"]
    with store.db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM sources").scalar_one() == 1


def test_pdf_evidence_is_bound_to_its_parse_and_page(store):
    source, _ = evidence(store)
    with pytest.raises(InvalidKnowledge, match="parse and page"):
        store.add_evidence(source["revision_id"], "invented", page=1)
    parse = store.record_parse(
        source["revision_id"], "fixture", {}, {"blocks": [{"id": "b", "page": 2, "text": "known"}]}
    )
    with pytest.raises(InvalidKnowledge, match="parsed block"):
        store.add_evidence(source["revision_id"], "invented", page=2, block_id="b", parse_id=parse)


def test_author_content_has_explicit_source_and_cannot_fake_pdf_location(store):
    source = store.add_source("我的推导记录".encode(), "作者记录", kind="AUTHOR")
    anchor = store.add_evidence(source["revision_id"], "推导记录")
    assert anchor
    with pytest.raises(InvalidKnowledge, match="Author evidence"):
        store.add_evidence(source["revision_id"], "推导记录", page=1)


def test_candidate_rejects_unknown_fields_and_duplicate_sections(store):
    _, anchor = evidence(store)
    note = store.create_note("ATOMIC", "测试")
    value = candidate(note, anchor)
    with pytest.raises(ValidationError):
        Candidate.model_validate({**value.model_dump(), "publication_status": "PUBLISHED"})
    with pytest.raises(ValidationError):
        Candidate.model_validate(
            {**value.model_dump(), "sections": value.model_dump()["sections"] * 2}
        )


def test_semantic_review_is_required_and_bound_to_candidate(store):
    _, anchor = evidence(store)
    note = store.create_note("ATOMIC", "测试")
    value = candidate(note, anchor)
    receipt = approve(value)
    changed = value.model_copy(update={"title": "different"})
    with pytest.raises(InvalidKnowledge, match="semantic review"):
        store.publish(changed, expected_revision=None, review=receipt)
    with pytest.raises(InvalidKnowledge, match="semantic review"):
        store.publish(
            value, expected_revision=None, review=receipt.model_copy(update={"passed": False})
        )


def test_markdown_is_immutable_with_frozen_provenance_and_event(store):
    _, anchor, note, result = atomic(store)
    saved = store.get_note(note)
    assert saved["revision"]["markdown_sha256"] == sha(saved["markdown"])
    section = saved["sections"][0]
    assert (
        sha(saved["markdown"][section["start_offset"] : section["end_offset"]]) == section["sha256"]
    )
    with store.db.engine.connect() as connection:
        assert one(connection, "SELECT * FROM section_evidence WHERE evidence_id=:id", id=anchor)
        assert one(
            connection, "SELECT * FROM events WHERE revision_id=:id", id=result["revision_id"]
        )
        artifact = one(connection, "SELECT * FROM build_artifacts WHERE kind='PUBLICATION'")
        frozen = json.loads(store.read(artifact["storage_ref"], artifact["sha256"]))
        assert frozen["candidate"]["note_id"] == note
    with pytest.raises(InvalidKnowledge, match="Immutable file"):
        store.put(saved["revision"]["markdown_ref"], b"overwrite")


def test_structurally_valid_missing_evidence_cannot_publish(store):
    note = store.create_note("ATOMIC", "测试")
    with pytest.raises(InvalidKnowledge, match="Unknown or invalid evidence"):
        publish(store, candidate(note, "missing-evidence"))
    with store.db.engine.connect() as connection:
        assert (
            one(connection, "SELECT current_revision_id FROM notes WHERE id=:id", id=note)[
                "current_revision_id"
            ]
            is None
        )
        assert connection.exec_driver_sql("SELECT count(*) FROM note_revisions").scalar_one() == 0


def test_concept_requires_valid_atomic_selection(store):
    _, anchor, _, source_revision = atomic(store)
    note, value, result = concept(store, source_revision["revision_id"], anchor)
    assert store.get_note(note)["eligible"]
    wrong_input = value.sections[0].inputs[0].model_copy(update={"section_ids": ["unknown"]})
    changed = value.model_copy(
        update={"sections": [value.sections[0].model_copy(update={"inputs": [wrong_input]})]}
    )
    with pytest.raises(InvalidKnowledge, match="Unknown selected section"):
        publish(store, changed, result["revision_id"])
    recursive = wrong_input.model_copy(
        update={"atomic_revision_id": result["revision_id"], "section_ids": ["definition"]}
    )
    changed = value.model_copy(
        update={"sections": [value.sections[0].model_copy(update={"inputs": [recursive]})]}
    )
    with pytest.raises(InvalidKnowledge, match="ATOMIC revision"):
        publish(store, changed, result["revision_id"])


def test_source_deletion_immediately_blocks_atomic_and_concept(store):
    source, anchor, atomic_id, revision = atomic(store)
    concept_id, _, result = concept(store, revision["revision_id"], anchor)
    store.set_source_deleted(source["source_id"], True)
    for note in (atomic_id, concept_id):
        with pytest.raises(InvalidKnowledge, match="no longer current"):
            store.get_note(note)
    historical = store.get_note(concept_id, revision_id=result["revision_id"], historical=True)
    assert not historical["eligible"]
    store.set_source_deleted(source["source_id"], False)
    assert store.get_note(atomic_id)["eligible"]


def test_atomic_edit_invalidates_old_concept_input(store):
    _, anchor, note, revision = atomic(store)
    concept_id, _, _ = concept(store, revision["revision_id"], anchor)
    publish(store, candidate(note, anchor, content="新的有效解释。"), revision["revision_id"])
    with pytest.raises(InvalidKnowledge):
        store.get_note(concept_id)


def test_concurrent_publication_uses_expected_head(store):
    _, anchor, note, revision = atomic(store)

    def edit(i):
        try:
            publish(store, candidate(note, anchor, content=f"解释 {i}"), revision["revision_id"])
            return "published"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(edit, [1, 2])) == ["conflict", "published"]


def test_event_failure_rolls_back_head_and_revision(store, monkeypatch):
    _, anchor = evidence(store)
    note = store.create_note("ATOMIC", "测试")

    def fail(*args, **kwargs):
        raise RuntimeError("simulated outbox failure")

    monkeypatch.setattr(store.repository, "event", fail)
    with pytest.raises(RuntimeError, match="outbox"):
        publish(store, candidate(note, anchor))
    with store.db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM note_revisions").scalar_one() == 0
        assert (
            one(connection, "SELECT current_revision_id FROM notes WHERE id=:id", id=note)[
                "current_revision_id"
            ]
            is None
        )


def test_protected_sections_cannot_be_changed_removed_or_unprotected(store):
    _, anchor = evidence(store)
    note = store.create_note("ATOMIC", "测试")
    value = candidate(note, anchor, protected=True)
    revision = publish(store, value)
    for changed in [
        candidate(note, anchor, content="覆盖", protected=True),
        candidate(note, anchor, protected=False),
    ]:
        with pytest.raises(Conflict, match="Protected section"):
            publish(store, changed, revision["revision_id"])


def test_moc_reuses_identity_and_rejects_cyclic_structure(store):
    _, _, target, _ = atomic(store)
    note = store.create_note("MOC", "学习地图")
    value = Candidate(
        note_id=note,
        title="学习地图",
        scope=SCOPE,
        sections=[
            Section(
                section_id="intro",
                title="阅读说明",
                question="如何学习？",
                scope=SCOPE,
                markdown="按顺序阅读。",
            )
        ],
        map_entries=[
            MapEntry(entry_id="group", title="基础"),
            MapEntry(
                entry_id="item", parent_entry_id="group", title="链式法则", target_note_id=target
            ),
        ],
    )
    revision = publish(store, value)
    assert store.get_note(note)["map_entries"][1]["target_note_id"] == target
    assert "## 阅读路线" in store.get_note(note)["markdown"]
    assert "../Notes/" in store.get_note(note)["markdown"]
    cycle = value.model_copy(
        update={
            "map_entries": [
                MapEntry(entry_id="a", parent_entry_id="b", title="A"),
                MapEntry(entry_id="b", parent_entry_id="a", title="B"),
            ]
        }
    )
    with pytest.raises(InvalidKnowledge, match="cyclic map"):
        publish(store, cycle, revision["revision_id"])


def test_corrupt_markdown_is_not_served(store):
    _, _, note, _ = atomic(store)
    value = store.get_note(note)
    store.path(value["revision"]["markdown_ref"]).write_text("corrupt", encoding="utf-8")
    with pytest.raises(InvalidKnowledge, match="hash differs"):
        store.get_note(note)


def test_source_replacement_invalidates_old_support_and_keeps_history(store):
    source, anchor, note, revision = atomic(store)
    concept_id, _, _ = concept(store, revision["revision_id"], anchor)
    replaced = store.replace_source(
        source["source_id"], b"%PDF-revised", expected_revision=source["revision_id"]
    )
    assert replaced["revision_id"] != source["revision_id"]
    for note_id in (note, concept_id):
        with pytest.raises(InvalidKnowledge):
            store.get_note(note_id)
    with pytest.raises(Conflict, match="Source head"):
        store.replace_source(
            source["source_id"], b"%PDF-third", expected_revision=source["revision_id"]
        )
    repeated = store.add_source(b"%PDF-fixture-A", "old upload renamed")
    assert repeated["duplicate"]
    assert repeated["source_id"] == source["source_id"]
    assert repeated["matched_revision_id"] == source["revision_id"]


def test_next_cli_does_not_load_legacy_service(tmp_path, monkeypatch, capsys):
    from noodle_agent import cli

    def fail():
        raise AssertionError("Legacy service must not be initialized")

    monkeypatch.setattr(cli, "_service", fail)
    cli.main(["next-init", "--data-dir", str(tmp_path / "new-cli")])
    assert json.loads(capsys.readouterr().out)["current"] == "next_0003"
    cli.main(["next-schema"])
    schema = json.loads(capsys.readouterr().out)
    assert schema["contract"] == "next-v1"
    assert schema["candidate"]["additionalProperties"] is False


def test_policy_upgrade_does_not_reuse_unreviewed_history(store):
    _, _, note, _ = atomic(store)
    store.repository.activate_policy("writing-next-v2", expected_policy="writing-next-v1")
    stricter = Store(store.root, policy_version="writing-next-v2")
    try:
        with pytest.raises(InvalidKnowledge, match="no longer current"):
            stricter.get_note(note)
    finally:
        stricter.close()


def test_database_rejects_moc_retrieval_chunks(store):
    from sqlalchemy.exc import IntegrityError

    note = store.create_note("MOC", "导航")
    value = Candidate(
        note_id=note,
        title="导航",
        scope=SCOPE,
        sections=[
            Section(
                section_id="intro",
                title="说明",
                question="如何阅读？",
                scope=SCOPE,
                markdown="导航内容不是知识证据。",
            )
        ],
    )
    result = publish(store, value)
    with (
        pytest.raises(IntegrityError, match="ATOMIC/CONCEPT"),
        store.repository.transaction() as connection,
    ):
        connection.exec_driver_sql(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
            ("invalid", result["revision_id"], "intro", 0, 1, "test"),
        )


def test_map_markdown_orders_parents_before_children_and_escapes_labels(store):
    _, _, target, _ = atomic(store)
    note = store.create_note("MOC", "地图")
    value = Candidate(
        note_id=note,
        title="地图",
        scope=SCOPE,
        sections=[
            Section(
                section_id="intro",
                title="说明",
                question="如何读？",
                scope=SCOPE,
                markdown="依次阅读。",
            )
        ],
        map_entries=[
            MapEntry(
                entry_id="child", parent_entry_id="parent", title="[子项]", target_note_id=target
            ),
            MapEntry(entry_id="parent", title="基础分组"),
        ],
    )
    publish(store, value)
    markdown = store.get_note(note)["markdown"]
    assert markdown.index("基础分组") < markdown.index(r"\[子项\]")


def test_next_incremental_migration_preserves_rows_and_creates_backup(tmp_path):
    from alembic import command

    from noodle_agent.storage.runner import config_for

    path = tmp_path / "prior.db"
    database = Database(f"sqlite:///{path}", contract="next-v1")
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            command.upgrade(config_for(connection, contract="next-v1"), "next_0001")
            connection.exec_driver_sql(
                "INSERT INTO sources(id,kind,title,created_at) VALUES('preserve','AUTHOR','作者记录','2026-10-21')"
            )
            connection.commit()
        database.init()
        assert database.last_migration["current"] == "next_0003"
        assert database.last_migration["backup"]
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM sources").scalar_one() == "preserve"
            assert any(
                row[1] == "ux_next_event_job_kind"
                for row in connection.exec_driver_sql("PRAGMA index_list(jobs)")
            )
    finally:
        database.close()


def test_policy_change_fences_existing_other_process_publisher(store):
    _, anchor, note, revision = atomic(store)
    stale = Store(store.root)
    value = candidate(note, anchor)
    receipt = approve(value)
    try:
        store.repository.activate_policy("writing-next-v2", expected_policy="writing-next-v1")
        with pytest.raises(Conflict, match="policy changed"):
            stale.publish(value, expected_revision=revision["revision_id"], review=receipt)
        with pytest.raises(InvalidKnowledge):
            stale.get_note(note)
        with pytest.raises(Conflict, match="policy mismatch"):
            Store(store.root)
    finally:
        stale.close()


def test_reupload_after_source_revert_selects_current_revision(store):
    first = store.add_source(b"%PDF-original", "A")
    second = store.replace_source(
        first["source_id"], b"%PDF-new", expected_revision=first["revision_id"]
    )
    reverted = store.replace_source(
        first["source_id"], b"%PDF-original", expected_revision=second["revision_id"]
    )
    repeated = store.add_source(b"%PDF-original", "renamed")
    assert repeated["duplicate"]
    assert repeated["matched_revision_id"] == reverted["revision_id"]
    assert repeated["source_id"] == first["source_id"]
