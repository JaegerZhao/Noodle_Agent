"""Entity maintenance, images and graph structures use explicit fixtures only."""

import json

import pytest
from fastapi.testclient import TestClient

from noodle_agent.next.api import create_app
from noodle_agent.next.concepts.entity_contracts import EntityPlan
from noodle_agent.next.concepts.entity_routing import validate_plan
from noodle_agent.next.concepts.entity_writing import asset_packet, checked_images
from noodle_agent.next.contracts import Candidate, EntityHints, Link, Review, Scope, Section, sha
from noodle_agent.next.knowledge_graph import concept_graph, concept_outline
from noodle_agent.next.map_projection import ordered_entries
from noodle_agent.next.navigation import current_notes, frozen_candidate
from noodle_agent.next.repository import Conflict, InvalidKnowledge, one
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store
from noodle_agent.next.vault import portable_files


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "entity")
    yield value
    value.close()


def publish_atomic(store, source, title, subject, body, *, intent="EXPLAIN", section_title="原理"):
    anchor = store.add_evidence(source["revision_id"], body)
    identity = store.content.create_note(
        "ATOMIC",
        title,
        creation_key=f"atomic:{source['revision_id']}:source-section:{sha(title)[:20]}",
    )
    value = Candidate(
        note_id=identity,
        title=title,
        summary=f"{title}说明{subject}的具体原理和条件。",
        entity_hints=EntityHints(
            subject=subject, subjects=[subject], context="降维", intent=intent
        ),
        scope=Scope(includes=[subject]),
        sections=[
            Section(
                section_id="main",
                title=section_title,
                question=section_title,
                scope=Scope(includes=[subject]),
                markdown=body,
                evidence_ids=[anchor],
            )
        ],
    )
    store.publish(
        value,
        expected_revision=None,
        review=Review(
            candidate_sha256=value.fingerprint,
            policy_version=value.policy_version,
            reviewer="fixture",
            passed=True,
        ),
    )
    return identity


class Agent:
    name = "explicit-entity-fixture"
    fast_model = "test-fast"
    pro_model = "test-pro"

    def __init__(self):
        self.calls = []

    def structured_json(self, operation, user, **kwargs):
        self.calls.append((operation, kwargs))
        data = user["content"]
        if operation == "next.entity_plan":
            entities = {}
            assignments = []
            relations = []
            targets = {}
            for card in data["atomics"]:
                subject = card["hints"]["subject"]
                key = {"降维": "dimensionality", "PCA": "pca", "SVD": "svd"}.get(
                    subject, subject.casefold()
                )
                proposal = {
                    "ref": "N" + str(len(entities)),
                    "title": subject,
                    "domain": "ml",
                    "identity_key": key,
                    "description": subject + "的原理与条件。",
                    "aliases": [],
                }
                existing = next(
                    (i for i in data["candidates"] if i["name"] == subject), None
                )
                if existing:
                    targets[key] = existing["ref"]
                else:
                    if key not in entities:
                        entities[key] = proposal
                    targets[key] = entities[key]["ref"]
                assignments.append({
                    "atomic": card["ref"],
                    "target": targets[key],
                    "action": "REUSE" if existing else "CREATE",
                    "object_match": "SAME_OBJECT",
                    "sections": {
                        s["ref"]: "comparison"
                        if card["hints"]["intent"] == "COMPARE"
                        else "overview"
                        if key == "dimensionality"
                        else "principles"
                        for s in card["sections"]
                    },
                    "reason": "fixture checked primary entity",
                })
            if "dimensionality" in targets:
                support = [r["atomic"] for r in assignments if r["target"] == targets["dimensionality"]]
                for key in ["pca", "svd"]:
                    if key in targets:
                        relations.append(
                            {
                                "source_key": targets["dimensionality"],
                                "target_key": targets[key],
                                "navigation_role": "TOPIC_CHILD",
                                "support_atomic_ids": support,
                                "reason": "来源以降维问题组织方法",
                            }
                        )
            return {"new_entities": list(entities.values()), "assignments": assignments, "relations": relations}
        if operation == "next.organization_plan":
            return {
                "links": [],
                "independent_roots": [
                    {"note_id": i, "reason": "Explicit fixture independent root"}
                    for i in data["roots"]
                ],
            }
        if operation == "next.organization_review":
            return {
                "passed": True,
                "issues": [],
                "checked_root_ids": data["roots"],
                "checked_link_indexes": list(range(len(data["plan"]["links"]))),
            }
        if operation == "next.entity_plan_review":
            return {
                "passed": True,
                "issues": [],
                "checked_atomic_ids": [c["ref"] for c in data["atomics"]],
            }
        if operation == "next.entity_write":
            return {
                "markdown": "\n\n".join(
                    dict.fromkeys(f["text"].strip() for f in data["fragments"])
                ),
                "images": {k: {"action": "USE", "reason": "原理示例"} for k in data["images"]},
            }
        if operation == "next.entity_review":
            return {
                "passed": True,
                "supported_fragment_ids": [f["fragment_id"] for f in data["fragments"]],
                "checked_image_ids": list(data["images"]),
            }
        raise AssertionError(operation)


def drain(app):
    for _ in range(250):
        outcome = app.run_once("entity-test")
        if outcome is None:
            return
        assert outcome["status"] != "REVIEW", outcome
    pytest.fail("Maintenance did not become idle")


def course(store, image=False):
    pic = ""
    if image:
        data = b"fixture-image"
        ref = f"assets/{sha(data)}.png"
        store.put(ref, data)
        pic = f"\n\n![PCA 方向示例](../{ref})"
    snapshot = "\n".join(
        [
            "降维可使用 PCA 与截断 SVD，比较时应注意中心化与输入含义。",
            "PCA 在中心化数据上寻找正交的最大方差方向。" + pic,
            "PCA 的线性投影有特定适用条件。",
            "SVD 对矩阵进行分解；截断时一般为近似。",
            "降维语境中比较 PCA 与 SVD 需要先明确中心化及矩阵约定。",
        ]
    )
    source = store.add_source(snapshot.encode(), "course", kind="AUTHOR")
    a = publish_atomic(
        store,
        source,
        "降维方法概述",
        "降维",
        "降维可使用 PCA 与截断 SVD，比较时应注意中心化与输入含义。",
        intent="OVERVIEW",
    )
    b = publish_atomic(
        store, source, "PCA 原理", "PCA", "PCA 在中心化数据上寻找正交的最大方差方向。" + pic
    )
    c = publish_atomic(store, source, "PCA 的优缺点", "PCA", "PCA 的线性投影有特定适用条件。")
    d = publish_atomic(store, source, "SVD 原理", "SVD", "SVD 对矩阵进行分解；截断时一般为近似。")
    e = publish_atomic(
        store,
        source,
        "PCA 与 SVD 的比较",
        "降维",
        "降维语境中比较 PCA 与 SVD 需要先明确中心化及矩阵约定。",
        intent="COMPARE",
        section_title="方法比较",
    )
    return source, [a, b, c, d, e]


def test_entity_facets_share_identity_and_plan_once(store):
    course(store)
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    drain(app)
    concepts = current_notes(store, kind="CONCEPT")["items"]
    assert {c["title"] for c in concepts} == {"降维", "PCA", "SVD"}
    assert sum(op == "next.entity_plan" for op, _ in agent.calls) == 1
    assert (
        next(args for op, args in agent.calls if op == "next.entity_plan").get("use_pro_model") is True
    )
    assert not any("concept_decid" in op or "batch_review" in op for op, _ in agent.calls)
    pca = next(c for c in concepts if c["title"] == "PCA")
    assert "线性投影" in store.get_note(pca["id"])["markdown"]
    assert pca["summary"] != "PCA"
    before = len(agent.calls)
    drain(app)
    assert len(agent.calls) == before


def test_comparison_routes_to_parent_with_links_and_outline(store):
    course(store)
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    drain(app)
    by = {c["title"]: c["id"] for c in current_notes(store, kind="CONCEPT")["items"]}
    assert "比较 PCA 与 SVD" in store.get_note(by["降维"])["markdown"]
    tree = concept_outline(store, by["降维"])
    assert {e["concept_id"] for e in tree["entries"]} == set(by.values())
    assert all(e["depth"] == 1 for e in tree["entries"] if e["concept_id"] != by["降维"])
    g = concept_graph(store, center=by["降维"])
    assert len(g["edges"]) == 2
    assert all(
        e["navigation_role"] == "TOPIC_CHILD" and e["support_revision_ids"] for e in g["edges"]
    )
    files = portable_files(store)
    pca_file = next(v.decode() for k, v in files.items() if k.startswith("Concepts/PCA-"))
    assert "反向链接" in pca_file and "降维" in pca_file


def test_images_receive_explicit_dispositions_and_export(store):
    course(store, image=True)
    drain(Application(store, agent=Agent(), index_mode="KEYWORD_ONLY"))
    pca = next(c for c in current_notes(store, kind="CONCEPT")["items"] if c["title"] == "PCA")
    note = store.get_note(pca["id"])
    assert "![PCA 方向示例]" in note["markdown"]
    proof = json.loads(
        store.path(f"artifacts/builds/{note['revision']['id']}.json").read_text(encoding="utf-8")
    )
    assert any(c.get("image_choices") for c in proof["review"]["report"]["checks"].values())
    files = portable_files(store)
    assert sum(k.startswith("assets/") for k in files) == 1


def test_exact_support_addition_does_not_rewrite(store):
    source, _ids = course(store)
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    drain(app)
    count = sum(op == "next.entity_write" for op, _ in agent.calls)
    publish_atomic(
        store, source, "PCA 同文补充来源", "PCA", "PCA 在中心化数据上寻找正交的最大方差方向。"
    )
    drain(app)
    assert sum(op == "next.entity_write" for op, _ in agent.calls) == count
    pca = next(c for c in current_notes(store, kind="CONCEPT")["items"] if c["title"] == "PCA")
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "SELECT count(DISTINCT atomic_revision_id) FROM concept_inputs WHERE concept_revision_id=?",
                (pca["current_revision_id"],),
            ).scalar_one()
            == 3
        )


def test_withdrawal_removes_unsupported_facts_and_graph_support(store):
    _, ids = course(store)
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    drain(app)
    before = store.get_note(ids[1])
    store.withdraw_note(ids[1], expected_revision=before["revision"]["id"])
    drain(app)
    pca = next(c for c in current_notes(store, kind="CONCEPT")["items"] if c["title"] == "PCA")
    assert "最大方差方向" not in store.get_note(pca["id"])["markdown"]
    assert "线性投影" in store.get_note(pca["id"])["markdown"]


def test_outline_bounds_snapshot_and_graph_api(store):
    course(store)
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    drain(app)
    parent = next(
        c["id"] for c in current_notes(store, kind="CONCEPT")["items"] if c["title"] == "降维"
    )
    assert concept_outline(store, parent, max_depth=0)["truncated"]
    assert concept_outline(store, parent, limit=1)["truncated"]
    with pytest.raises(Conflict):
        concept_graph(store, snapshot="outdated")
    with TestClient(create_app(application=app)) as client:
        g = client.get("/internal/next/concept-graph?limit=1").json()
        assert g["next_offset"] == 1
        assert client.get(f"/internal/next/concept-outline/{parent}").status_code == 200
        assert client.get("/internal/next/concept-graph?limit=0").status_code == 422


def test_map_preorder_collapses_old_repeated_heading_and_prunes_empty():
    from noodle_agent.next.contracts import MapEntry

    values = [
        MapEntry(entry_id="a", title="课程"),
        MapEntry(entry_id="b", parent_entry_id="a", title="PCA"),
        MapEntry(entry_id="copyright", title="版权"),
        MapEntry(entry_id="link", parent_entry_id="b", title="PCA", target_note_id="pca"),
    ]
    result = ordered_entries(values, prune_empty=True, available={"pca"})
    assert [(e.title, d) for e, d in result] == [("课程", 0), ("PCA", 1)]
    assert result[-1][0].target_note_id == "pca"


def test_omitting_image_without_disposition_fails():
    from noodle_agent.next.concepts.entity_contracts import Chapter

    with pytest.raises(ValueError):
        checked_images(Chapter(markdown="正文"), {"image_a": {"asset_ref": "../assets/a.png"}})


def test_missing_image_fails_instead_of_silently_dropping(store):
    with pytest.raises(ValueError):
        asset_packet(
            store,
            [
                {
                    "text": f"![图](../assets/{'a' * 64}.png)",
                    "fragment_id": "f",
                    "evidence_ids": ["e"],
                }
            ],
        )


def test_unsupported_identity_or_missing_sections_rejected():
    value = EntityPlan(
        entities=[
            {
                "title": "PCA",
                "description": "中心化数据的投影",
                "domain": "ml",
                "identity_key": "pca",
                "question": "PCA是什么",
                "scope": {"includes": ["PCA"]},
            }
        ],
        routes={"a": {"entity_key": "pca", "sections": {}, "reason": "test"}},
    )
    with pytest.raises(ValueError):
        validate_plan(value, [{"id": "a", "sections": [{"id": "s"}]}], [])


def test_entity_facet_title_rejected():
    value = EntityPlan(
        entities=[
            {
                "title": "SVD 的优点与局限",
                "description": "局限",
                "domain": "ml",
                "identity_key": "svd_limits",
                "question": "局限",
                "scope": {"includes": ["SVD"]},
            }
        ],
        routes={},
    )
    with pytest.raises(ValueError):
        validate_plan(value, [], [])


def test_structural_link_requires_owned_support(store):
    course(store)
    drain(Application(store, agent=Agent(), index_mode="KEYWORD_ONLY"))
    values = current_notes(store, kind="CONCEPT")["items"]
    parent = values[0]
    target = values[1]
    old = frozen_candidate(store, parent["current_revision_id"])
    candidate = old.model_copy(
        update={
            "links": [
                Link(
                    target_note_id=target["id"],
                    relation="LINKS_TO",
                    status="CONFIRMED",
                    basis="test",
                    navigation_role="TOPIC_CHILD",
                )
            ]
        }
    )
    with pytest.raises(InvalidKnowledge):
        store.publish(
            candidate,
            expected_revision=parent["current_revision_id"],
            review=Review(
                candidate_sha256=candidate.fingerprint,
                policy_version=candidate.policy_version,
                reviewer="fixture",
                passed=True,
            ),
        )


def test_confirmed_context_route_avoids_second_identity_model(store):
    source, _ = course(store)
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    drain(app)
    before = sum(op == "next.entity_plan" for op, _ in agent.calls)
    publish_atomic(store, source, "PCA 新笔记", "PCA", "PCA 在中心化数据上寻找正交的最大方差方向。")
    drain(app)
    assert sum(op == "next.entity_plan" for op, _ in agent.calls) == before


def test_comparison_does_not_use_explain_only_fast_route(store):
    from noodle_agent.next.concepts.entity_routing import hint_signature

    assert hint_signature(
        {"intent": "COMPARE", "subject": "降维", "context": "线性代数"}
    ) != hint_signature({"intent": "EXPLAIN", "subject": "降维", "context": "线性代数"})
    assert hint_signature(
        {"intent": "EXPLAIN", "subject": "LDA", "context": "主题建模"}
    ) != hint_signature({"intent": "EXPLAIN", "subject": "LDA", "context": "分类判别"})


def test_metadata_defaults_keep_legacy_fingerprint():
    from noodle_agent.next.contracts import canonical

    value = Candidate(
        note_id="a",
        title="A",
        scope=Scope(includes=["A"]),
        sections=[
            Section(
                section_id="s",
                title="S",
                question="Q",
                scope=Scope(includes=["A"]),
                markdown="body",
            )
        ],
        links=[Link(target_note_id="b", relation="LINKS_TO", status="CONFIRMED", basis="fixture")],
    )
    legacy = value.model_dump()
    legacy.pop("entity_hints")
    legacy.pop("origin_source_revisions")
    for field in ["navigation_role", "position", "support_revision_ids", "origin", 'semantic_direction']:
        legacy["links"][0].pop(field)
    assert value.fingerprint == sha(canonical(legacy))
    changed = value.model_copy(update={"entity_hints": EntityHints(subject="A")})
    assert changed.fingerprint != value.fingerprint


def test_navigation_position_zero_is_part_of_fingerprint():
    value = Candidate(
        note_id="a",
        title="A",
        scope=Scope(includes=["A"]),
        sections=[
            Section(
                section_id="s",
                title="S",
                question="Q",
                scope=Scope(includes=["A"]),
                markdown="body",
            )
        ],
        links=[Link(target_note_id="b", relation="LINKS_TO", status="CONFIRMED", basis="fixture")],
    )
    changed = value.model_copy(
        update={"links": [value.links[0].model_copy(update={"position": 0})]}
    )
    assert changed.fingerprint != value.fingerprint


def test_organization_cycle_rejected_at_publication(store):
    course(store)
    drain(Application(store, agent=Agent(), index_mode="KEYWORD_ONLY"))
    records = {r["title"]: r for r in current_notes(store, kind="CONCEPT")["items"]}
    pca = records["PCA"]
    old = frozen_candidate(store, pca["current_revision_id"])
    supports = sorted({i.atomic_revision_id for s in old.sections for i in s.inputs})
    updated = old.model_copy(
        update={
            "links": [
                Link(
                    target_note_id=records["降维"]["id"],
                    relation="LINKS_TO",
                    status="CONFIRMED",
                    basis="invalid cycle",
                    navigation_role="TOPIC_CHILD",
                    support_revision_ids=supports,
                )
            ]
        }
    )
    with pytest.raises(InvalidKnowledge, match="cycle"):
        store.publish(
            updated,
            expected_revision=pca["current_revision_id"],
            review=Review(
                candidate_sha256=updated.fingerprint,
                policy_version=updated.policy_version,
                reviewer="fixture",
                passed=True,
            ),
        )


def test_multiple_parent_outline_references_one_entity(store):
    course(store)
    drain(Application(store, agent=Agent(), index_mode="KEYWORD_ONLY"))
    records = {r["title"]: r for r in current_notes(store, kind="CONCEPT")["items"]}
    pca = records["PCA"]
    old = frozen_candidate(store, pca["current_revision_id"])
    supports = sorted({i.atomic_revision_id for s in old.sections for i in s.inputs})
    updated = old.model_copy(
        update={
            "links": [
                Link(
                    target_note_id=records["SVD"]["id"],
                    relation="LINKS_TO",
                    status="CONFIRMED",
                    basis="fixture organization",
                    navigation_role="TOPIC_CHILD",
                    support_revision_ids=supports,
                )
            ]
        }
    )
    store.publish(
        updated,
        expected_revision=pca["current_revision_id"],
        review=Review(
            candidate_sha256=updated.fingerprint,
            policy_version=updated.policy_version,
            reviewer="fixture",
            passed=True,
        ),
    )
    tree = concept_outline(store, records["降维"]["id"])
    assert len([e for e in tree["entries"] if e["concept_id"] == records["SVD"]["id"]]) == 2
    assert sum(e["reference"] for e in tree["entries"]) == 1


def test_upgrade_from_previous_schema_preserves_data_and_makes_backup(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine

    from noodle_agent.storage.runner import config_for, upgrade

    path = tmp_path / "upgrade.db"
    engine = create_engine("sqlite:///" + str(path))
    with engine.begin() as c:
        command.upgrade(config_for(c, contract="next-v1"), "next_0007")
    with engine.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO sources(id,kind,title,current_revision_id,deleted_at,created_at) VALUES('preserve','AUTHOR','snapshot',NULL,NULL,'2026-10-23')"
        )
    result = upgrade(engine, contract="next-v1")
    assert result["current"] == "next_0011" and result["backup"]
    with engine.connect() as c:
        assert (
            c.exec_driver_sql("SELECT title FROM sources WHERE id='preserve'").scalar_one()
            == "snapshot"
        )
        assert c.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def test_child_supported_existing_parent_link_normalizes_directory(store):
    course(store)
    drain(Application(store, agent=Agent(), index_mode="KEYWORD_ONLY"))
    records = {r["title"]: r for r in current_notes(store, kind="CONCEPT")["items"]}
    parent = records["降维"]
    old_parent = frozen_candidate(store, parent["current_revision_id"])
    # Clear its forward links so the test depends only on the child's new evidence.
    cleared = old_parent.model_copy(update={"links": []})
    store.publish(
        cleared,
        expected_revision=parent["current_revision_id"],
        review=Review(
            candidate_sha256=cleared.fingerprint,
            policy_version=cleared.policy_version,
            reviewer="fixture",
            passed=True,
        ),
    )
    child = records["PCA"]
    old = frozen_candidate(store, child["current_revision_id"])
    supports = sorted({i.atomic_revision_id for s in old.sections for i in s.inputs})
    linked = old.model_copy(
        update={
            "links": [
                Link(
                    target_note_id=parent["id"],
                    relation="LINKS_TO",
                    status="CONFIRMED",
                    basis="来源明确讨论降维",
                    navigation_role="TOPIC_PARENT",
                    support_revision_ids=supports,
                )
            ]
        }
    )
    store.publish(
        linked,
        expected_revision=child["current_revision_id"],
        review=Review(
            candidate_sha256=linked.fingerprint,
            policy_version=linked.policy_version,
            reviewer="fixture",
            passed=True,
        ),
    )
    graph = concept_graph(store)
    edge = next(
        e for e in graph["edges"] if e["source"] == parent["id"] and e["target"] == child["id"]
    )
    assert edge["origin_note_id"] == child["id"] and edge["support_revision_ids"] == supports
    tree = concept_outline(store, parent["id"])
    assert {e["concept_id"] for e in tree["entries"]} == {parent["id"], child["id"]}


def test_confirmed_scientific_errors_trigger_targeted_guard():
    from noodle_agent.next.critical_quality import critical_problems

    assert critical_problems("PCA 原理", "PCA 要构造与原始特征正交的单位向量。")
    assert critical_problems("LDA 实现", "simple_preprocess: `deacc=True` 移除标点。")
    assert critical_problems("SVD 实现", "正文。", "修复了代码说明。")


def test_critical_guard_does_not_reject_explicit_corrections_or_code_comments():
    from noodle_agent.next.critical_quality import critical_problems

    assert not critical_problems("PCA", "PCA 不是与原始特征正交，而是主成分方向彼此正交。")
    assert not critical_problems(
        "LDA",
        "```python\n# deacc=True removes punctuations\nsimple_preprocess(x, deacc=True)\n```\n正文保留设置，不解释其作用。",
    )


def test_graph_pagination_keeps_cross_page_edges(store):
    course(store)
    drain(Application(store, agent=Agent(), index_mode="KEYWORD_ONLY"))
    page = concept_graph(store, limit=1)
    assert page["boundary_edges"]
    assert page["snapshot"]
    other = concept_graph(store, limit=2, offset=1, snapshot=page["snapshot"])
    keys = {
        (e["source"], e["target"])
        for result in [page, other]
        for e in result["edges"] + result["boundary_edges"]
    }
    assert len(keys) == 2


def test_comparison_with_known_context_uses_parent_without_new_identity_call(store):
    source, _ = course(store)
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    drain(app)
    prior = sum(op == "next.entity_plan" for op, _ in agent.calls)
    identity = publish_atomic(
        store,
        source,
        "PCA 与 SVD 的另一次比较",
        "PCA 与 SVD",
        "降维语境中比较 PCA 与 SVD 需要先明确中心化及矩阵约定。",
        intent="COMPARE",
        section_title="比较",
    )
    drain(app)
    assert sum(op == "next.entity_plan" for op, _ in agent.calls) == prior
    note = store.get_note(identity)
    with store.db.engine.connect() as c:
        route = one(
            c,
            "SELECT concept_id FROM atomic_entity_routes WHERE atomic_revision_id=:r",
            r=note["revision"]["id"],
        )
    assert store.get_note(route["concept_id"])["title"] == "降维"


def test_revision_changed_target_rebuilds_both_old_and_new_concepts(store):
    from noodle_agent.next.convergence import convergence_report

    _source, identities = course(store)
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    drain(app)
    atomic = store.get_note(identities[2])
    previous = frozen_candidate(store, atomic["revision"]["id"])
    revised = previous.model_copy(
        update={"entity_hints": previous.entity_hints.model_copy(update={"subject": "SVD"})}
    )
    store.publish(
        revised,
        expected_revision=atomic["revision"]["id"],
        review=Review(
            candidate_sha256=revised.fingerprint,
            policy_version=revised.policy_version,
            reviewer="fixture",
            passed=True,
        ),
    )
    assert not convergence_report(store, require_idle=False)["passed"]
    drain(app)
    result = convergence_report(store)
    assert result["passed"], result
    with store.db.engine.connect() as c:
        stale = one(
            c,
            "SELECT 1 FROM concept_inputs ci JOIN notes n ON n.current_revision_id=ci.concept_revision_id WHERE ci.atomic_revision_id=:r",
            r=atomic["revision"]["id"],
        )
    assert stale is None
    assert {n["title"] for n in current_notes(store, kind="CONCEPT")["items"]} == {
        "降维",
        "PCA",
        "SVD",
    }


def test_revision_becoming_source_only_removes_old_contribution(store):
    from noodle_agent.next.convergence import convergence_report

    _source, identities = course(store)

    class SummaryAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            if operation == "next.entity_plan" and all(
                c["hints"]["intent"] == "SUMMARY" for c in user["content"]["atomics"]
            ):
                self.calls.append((operation, kwargs))
                return {
                    "new_entities": [],
                    "relations": [],
                    "assignments": [
                        {
                            "atomic": c["ref"],
                            "target": None,
                            "action": "SOURCE_ONLY",
                            "reason": "Source-only summary fixture",
                        }
                        for c in user["content"]["atomics"]
                    ],
                }
            return super().structured_json(operation, user, **kwargs)

    app = Application(store, agent=SummaryAgent(), index_mode="KEYWORD_ONLY")
    drain(app)
    atomic = store.get_note(identities[2])
    prior = frozen_candidate(store, atomic["revision"]["id"])
    candidate = prior.model_copy(
        update={"entity_hints": prior.entity_hints.model_copy(update={"intent": "SUMMARY"})}
    )
    store.publish(
        candidate,
        expected_revision=atomic["revision"]["id"],
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="fixture",
            passed=True,
        ),
    )
    drain(app)
    assert convergence_report(store)["passed"]
    with store.db.engine.connect() as c:
        assert (
            one(
                c,
                "SELECT 1 FROM concept_inputs ci JOIN notes n ON n.current_revision_id=ci.concept_revision_id WHERE ci.atomic_revision_id=:r",
                r=atomic["revision"]["id"],
            )
            is None
        )


def test_downstream_findings_trigger_checked_atomic_revision_and_rebuild(store):
    from noodle_agent.next.convergence import convergence_report

    _source, ids = course(store)
    old = store.get_note(ids[2])["revision"]["id"]
    target = "PCA 的线性投影有特定适用条件。"

    class CorrectingAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            data = user["content"]
            if operation == "next.entity_review" and any(
                f["text"].strip() == target for f in data["fragments"]
            ):
                return {
                    "passed": False,
                    "issues": ["Fixture: the source condition needs clarification"],
                    "supported_fragment_ids": [f["fragment_id"] for f in data["fragments"]],
                    "checked_image_ids": list(data["images"]),
                }
            if operation == "next.quality_triage":
                f = next(f for f in data["fragments"] if f["text"].strip() == target)
                return {
                    "problems": [
                        {
                            "fragment_id": f["fragment_id"],
                            "issue": "Fixture: clarify the stated applicability condition",
                        }
                    ]
                }
            if operation == "next.atomic_quality_repair":
                return {
                    "sections": [
                        {
                            "key": s["section_id"],
                            "markdown": s["markdown"]
                            + "\n\n来源说明：应结合具体场景核对适用条件。",
                        }
                        for s in data["sections"]
                    ]
                }
            if operation == "next.atomic_quality_review":
                return {
                    "passed": True,
                    "issues": [],
                    "checked_section_ids": [s["section_id"] for s in data["after"]["sections"]],
                }
            return super().structured_json(operation, user, **kwargs)

    app = Application(store, agent=CorrectingAgent(), index_mode="KEYWORD_ONLY")
    drain(app)
    new = store.get_note(ids[2])
    assert new["revision"]["id"] != old
    assert "来源说明" in new["markdown"]
    with store.db.engine.connect() as c:
        assert one(c, "SELECT 1 FROM jobs WHERE kind='REPAIR_ATOMIC' AND status='SUCCEEDED'")
        assert (
            one(
                c,
                "SELECT parent_revision_id FROM note_revisions WHERE id=:id",
                id=new["revision"]["id"],
            )["parent_revision_id"]
            == old
        )
    assert convergence_report(store)["passed"]


def test_organization_adds_supported_metadata_without_rewriting_body(store):
    from noodle_agent.next.convergence import convergence_report
    from noodle_agent.next.organization import request_organization_review

    course(store)

    class OrganizationAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            data = user["content"]
            if operation == "next.entity_plan":
                result = super().structured_json(operation, user, **kwargs)
                svd_ref = next((e["ref"] for e in result["new_entities"] if e["identity_key"] == "svd"), None)
                result["relations"] = [r for r in result["relations"] if r["target_key"] != svd_ref]
                return result
            if operation == "next.organization_plan":
                self.calls.append((operation, kwargs))
                nodes = {n["title"]: n["id"] for n in data["concepts"]}
                return {
                    "links": [
                        {
                            "owner_note_id": nodes["SVD"],
                            "target_note_id": nodes["降维"],
                            "navigation_role": "TOPIC_PARENT",
                            "support_revision_ids": data["inputs"][nodes["SVD"]],
                            "reason": "The source describes SVD as a dimensionality reduction method",
                        }
                    ],
                    "independent_roots": [
                        {"note_id": nodes["降维"], "reason": "Root problem domain"}
                    ],
                }
            return super().structured_json(operation, user, **kwargs)

    agent = OrganizationAgent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    drain(app)
    svd = next(n for n in current_notes(store, kind="CONCEPT")["items"] if n["title"] == "SVD")
    current = store.get_note(svd["id"])
    with store.db.engine.connect() as c:
        parent = one(
            c,
            "SELECT parent_revision_id FROM note_revisions WHERE id=:r",
            r=current["revision"]["id"],
        )["parent_revision_id"]
    assert parent
    old = frozen_candidate(store, parent)
    new = frozen_candidate(store, current["revision"]["id"])
    assert [s.markdown for s in old.sections] == [s.markdown for s in new.sections]
    graph = concept_graph(store)
    children = {e["target"] for e in graph["edges"] if e.get("navigation_role") == "TOPIC_CHILD"}
    assert [n["title"] for n in graph["nodes"] if n["id"] not in children] == ["降维"]
    calls = len(agent.calls)
    request_organization_review(store, cause="repeat checked epoch")
    drain(app)
    assert len(agent.calls) == calls
    assert convergence_report(store)["passed"]
