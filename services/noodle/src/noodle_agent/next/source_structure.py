"""Read-only source hierarchy, plan and publication projection; no model calls."""

import json
import re
from functools import lru_cache

from .contracts import canonical, sha
from .document_outline import build_outline, placement, placement_for_units, unit_node_map
from .repository import Conflict, one, rows


@lru_cache(maxsize=4)
def cached_outline(root, blob, digest):
    from pathlib import Path

    content = (Path(root) / blob).read_bytes()
    if sha(content) != digest:
        raise Conflict("Original PDF hash mismatch")
    return build_outline(content)


def source_structure(store, revision):
    with store.db.engine.connect() as c:
        source = one(
            c,
            "SELECT r.*,s.current_revision_id,s.deleted_at FROM source_revisions r JOIN sources s ON s.id=r.source_id WHERE r.id=:r",
            r=revision,
        )
        if not source or not store.sources.source_valid(c, revision):
            raise Conflict("Source revision is not current and available")
        records = rows(
            c,
            "SELECT n.* FROM notes n WHERE n.kind='ATOMIC' AND substr(n.creation_key,1,length(:key))=:key AND n.visibility='ACTIVE' AND n.current_revision_id IS NOT NULL",
            key=f"atomic:{revision}:",
        )
        records = [r for r in records if store.content.valid_revision(c, r["current_revision_id"])]
        job = one(
            c,
            "SELECT * FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC LIMIT 1",
            r=revision,
        )
        checkpoints = (
            rows(
                c,
                "SELECT * FROM job_checkpoints WHERE job_id=:id AND state='DONE' ORDER BY created_at DESC",
                id=job["id"],
            )
            if job
            else []
        )
        pages = {
            r["id"]: sorted(
                {
                    x["page"]
                    for x in rows(
                        c,
                        "SELECT a.page_number AS page FROM section_evidence e JOIN evidence_anchors a ON a.id=e.evidence_id WHERE e.revision_id=:r AND a.source_revision_id=:s AND a.page_number IS NOT NULL",
                        r=r["current_revision_id"],
                        s=revision,
                    )
                }
            )
            for r in records
        }
        parse = one(
            c,
            "SELECT * FROM parse_runs WHERE source_revision_id=:r ORDER BY created_at DESC LIMIT 1",
            r=revision,
        )
    normalized = next((r for r in checkpoints if r["stage"] == "parse.normalized"), None)
    parsed_artifact = None
    if normalized:
        parsed_artifact = json.loads(
            store.read(normalized["artifact_ref"], normalized["artifact_sha256"])
        )["artifact"]
    source_plan_row = next((r for r in checkpoints if r["stage"].startswith("source.sections.plan")), None)
    source_plan = (
        json.loads(store.read(source_plan_row["artifact_ref"], source_plan_row["artifact_sha256"]))
        if source_plan_row
        else None
    )
    if source_plan:
        from .document_outline import DocumentOutline

        outline = DocumentOutline.model_validate(source_plan.get("source_outline", source_plan["outline"])
            if source_plan.get("document_profile", {}).get("kind") == "BOOK" else source_plan["outline"])
    else:
        outline = cached_outline(str(store.root.resolve()), source["blob_ref"], source["sha256"])
    nodes = [n.model_dump() for n in outline.nodes]
    if source_plan:
        for node in nodes:
            title = source_plan.get('display_titles', {}).get(node['node_id'])
            if title:
                node['source_title'], node['title'] = node['title'], title
    from .generation_contracts import Unit

    manifest = next((r for r in checkpoints if r["stage"] == "units"), None)
    units = (
        [
            Unit.model_validate(u)
            for u in json.loads(store.read(manifest["artifact_ref"], manifest["artifact_sha256"]))[
                "units"
            ]
        ]
        if manifest
        else []
    )
    owners = (
        {u: p.get("outline_node_id") or p["key"] for p in source_plan["plans"] for u in p["unit_ids"]}
        if source_plan
        else unit_node_map(outline, units)
    )
    covered_units = set()
    published = []
    from .reading_projection import anchor_context, section_anchors

    reading_context = anchor_context(outline, units) if records else None
    for r in records:
        with store.db.engine.connect() as c:
            proof = one(
                c,
                "SELECT * FROM build_artifacts WHERE storage_ref=:ref LIMIT 1",
                ref=f"artifacts/builds/{r['current_revision_id']}.json",
            )
        ids = []
        if proof:
            receipt = json.loads(store.read(proof["storage_ref"], proof["sha256"]))
            ids = receipt["review"]["report"].get("units", [])
        covered_units.update(ids)
        primary, contexts = placement_for_units(outline, ids, owners, pages[r["id"]])
        published.append(
            {
                "note_id": r["id"],
                "revision_id": r["current_revision_id"],
                "creation_key": r["creation_key"],
                "title": r["title"],
                "pages": pages[r["id"]],
                "primary_node_id": primary,
                "context_node_ids": contexts,
                "basis": "SOURCE_UNIT_HEADING" if ids else "PDF_PAGE_RANGE",
                "unit_ids": ids,
            }
        )
        published[-1]['section_anchors'] = section_anchors(store, r['current_revision_id'], outline, units, context=reading_context)
    full = next((r for r in checkpoints if r["stage"] == "book.plan.v2"), None)
    if full is None:
        full = max(
            (r for r in checkpoints if r["stage"].startswith("book.plan.partial.")),
            key=lambda r: int(r["stage"].rsplit(".", 1)[1]),
            default=None,
        )
    planned = []
    mode = "NOT_PLANNED"
    excluded_units = set()
    resolved_plans = set()
    deferred_plans = set()
    if source_plan:
        from .learning_titles import reading_title

        planned = [
            {
                "key": p["key"],
                "creation_key": f"atomic:{revision}:source-section:{p['key']}",
                "title": p["title"],
                "display_title": reading_title(p),
                "primary_node_id": (p.get("outline_node_id") or p["key"]) if source_plan.get("document_profile", {}).get("kind") == "BOOK" and source_plan.get("source_outline") else p["key"],
                "context_node_ids": [],
                "unit_ids": p["unit_ids"],
                "pages": p["pages"],
                "basis": source_plan["basis"],
            }
            for p in source_plan["plans"]
        ]
        mode = "COMPLETE_SOURCE_SECTIONS"
        excluded_units.update(e["unit_id"] for e in source_plan["layout_exclusions"])
        for receipt in checkpoints:
            if receipt["stage"] == "coverage" or receipt['stage'].startswith('coverage.'):
                excluded_units.update(
                    e["unit_id"]
                    for e in json.loads(
                        store.read(receipt["artifact_ref"], receipt["artifact_sha256"])
                    )["exclusions"]
                )
            elif receipt['stage'].startswith('source.publish.'):
                value = json.loads(store.read(receipt['artifact_ref'],receipt['artifact_sha256']))
                excluded_units.update(value.get('omissions', {}))
                resolved_plans.update(value.get('resolved_plan_keys', []))
        by_key = {p["creation_key"]: p for p in planned}
        for output in published:
            mapped = by_key.get(output["creation_key"])
            if mapped:
                output['display_title'] = mapped['display_title'] if output['title'] == mapped['title'] else output['title']
                output["primary_node_id"] = mapped["primary_node_id"]
                output["context_node_ids"] = []
    elif full:
        from .book_planning import BookPlan

        plan = BookPlan.model_validate(
            json.loads(store.read(full["artifact_ref"], full["artifact_sha256"]))
        )
        if plan.source_revision_id != revision or plan.outline.source_sha256 != source["sha256"]:
            raise Conflict("Book plan source binding mismatch")
        planned = [n.model_dump() for n in plan.notes]
        excluded_units.update(
            e["unit_id"] for ch in plan.chapters for e in ch.plan["coverage"]["exclusions"]
        )
        mode = "COMPLETE" if plan.coverage["complete"] else "PARTIAL_ROLLING"
    elif parsed_artifact is not None or parse:
        # Existing jobs can expose reviewed chapter plans without pretending the whole book is planned.
        artifact = parsed_artifact or json.loads(
            store.read(parse["artifact_ref"], parse["artifact_sha256"])
        )
        seen = set()
        has_chapters = any(
            re.fullmatch(r"chapter\.(\d+)\.(?:content-selection-v1\.)?coverage", r["stage"])
            for r in checkpoints
        )
        for receipt in checkpoints:
            match = re.fullmatch(
                r"chapter\.(\d+)\.(?:content-selection-v1\.)?coverage", receipt["stage"]
            )
            root_plan = receipt["stage"] == "coverage" and not has_chapters
            if not root_plan and not match:
                continue
            index = -1 if root_plan else int(match[1])
            if index in seen:
                continue
            seen.add(index)
            if not root_plan and index >= len(artifact.get("chapters", [])):
                continue
            chapter = (
                {"title": "全部内容", "start": 1} if root_plan else artifact["chapters"][index]
            )
            target = next((n.node_id for n in outline.nodes if n.title == chapter["title"]), None)
            if target is None:
                target = placement(outline, [chapter["start"]])[0]
            coverage = json.loads(store.read(receipt["artifact_ref"], receipt["artifact_sha256"]))
            excluded_units.update(e["unit_id"] for e in coverage["exclusions"])
            merged = coverage["groups"]
            for g in merged["groups"]:
                planned.append(
                    {
                        "key": g["key"],
                        "creation_key": f"atomic:{revision}:{sha(canonical(merged))}:{g['key']}",
                        "title": g["title"],
                        "question": g["question"],
                        "scope": g["scope"],
                        "chapter_key": str(index),
                        "primary_node_id": target,
                        "context_node_ids": [],
                        "unit_ids": [],
                        "pages": [],
                        "basis": "FROZEN_CHAPTER_PLAN",
                    }
                )
        if planned:
            mode = "PARTIAL_LEGACY" if has_chapters else "COMPLETE_LEGACY"
    by_creation = {n["creation_key"]: n for n in published}
    for row in checkpoints:
        if row['stage'].startswith('source.publish.'):
            value = json.loads(store.read(row['artifact_ref'], row['artifact_sha256']))
            deferred_plans.update(v['plan_key'] for v in value.get('deferred', []))
    deferred_plans.difference_update(resolved_plans)
    by_node = {n["node_id"]: n for n in nodes}
    for note in planned:
        output = by_creation.get(note["creation_key"])
        if output and mode in {"PARTIAL_LEGACY", "COMPLETE_LEGACY"}:
            note["primary_node_id"] = output["primary_node_id"]
            note["pages"] = output["pages"]
            note["unit_ids"] = output["unit_ids"]
        note["note_id"] = output["note_id"] if output else None
        note["status"] = "PUBLISHED" if output else "OMITTED" if note.get('key') in resolved_plans and set(note['unit_ids']) <= excluded_units else "PLANNED"
        if not output and note.get('key') in deferred_plans:
            note['status'] = 'DEFERRED'

    def within(owner, parent):
        while owner:
            if owner == parent:
                return True
            owner = by_node.get(owner, {}).get("parent_id")
        return False

    node_units = {n["node_id"]: set() for n in nodes}
    for unit, owner in (reading_context[0] if reading_context else owners).items():
        while owner:
            node_units[owner].add(unit)
            owner = by_node[owner]["parent_id"]
    for n in nodes:
        expected = {
            p["creation_key"] for p in planned if within(p["primary_node_id"], n["node_id"])
        }
        actual = {
            p["creation_key"] for p in published if within(p["primary_node_id"], n["node_id"])
        }
        n["planned_count"] = len(expected)
        n["published_count"] = len(actual)
        owned = node_units[n["node_id"]]
        if owned and owned <= excluded_units:
            n["status"] = "NO_NOTES"
            continue
        if owned and owned <= covered_units | excluded_units:
            n["status"] = "COMPLETE"
            continue
        n["status"] = (
            "COMPLETE"
            if expected and expected <= set(by_creation)
            else "PARTIAL"
            if actual
            else "PLANNED"
            if expected
            else "NO_NOTES"
            if mode.startswith("COMPLETE")
            else "NOT_PLANNED"
        )
    return {
        "source_id": source["source_id"],
        'display_title': source_plan.get('display_title', '') if source_plan else '',
        "source_revision_id": revision,
        "outline": {**outline.model_dump(), "nodes": nodes},
        "planning_status": mode,
        "notes": published,
        "planned_notes": planned,
        "generation_status": job["status"] if job else None,
        'document_profile': source_plan.get('document_profile', {}) if source_plan else {},
        'chapter_tasks': source_plan.get('chapter_tasks', []) if source_plan else [],
        "generation_error": json.loads(job["error_json"] or "{}") if job else None,
        "counts": {"published": len(published), "planned": len(planned)},
    }
