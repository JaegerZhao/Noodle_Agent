"""Freeze structure and chapter plans; retain existing whole-book v2 identities."""

import copy
import re
from types import SimpleNamespace

from .contracts import Contract, canonical, sha
from .document_outline import (
    DocumentOutline,
    build_outline,
    check_book_coverage,
    placement_for_units,
    unit_node_map,
)
from .planning import QualityFailure
from .repository import one, rows

POLICY = "book-plan-v2"


class PlannedNote(Contract):
    key: str
    creation_key: str
    title: str
    question: str
    scope: dict
    chapter_key: str
    primary_node_id: str | None
    context_node_ids: list[str]
    unit_ids: list[str]
    pages: list[int]


class PlannedChapter(Contract):
    key: str
    title: str
    start: int
    end: int
    namespace: str
    plan: dict


class BookPlan(Contract):
    policy: str = POLICY
    source_revision_id: str
    parse_id: str
    profile_sha256: str
    outline: DocumentOutline
    layout_mode: str
    chapters: list[PlannedChapter]
    notes: list[PlannedNote]
    coverage: dict


def prepare_book(generation, parsed, units, *, chapter_limit=None):
    from .chapter_generation import ScopedJournal

    revision = generation.lease.input["revision_id"]
    with generation.store.db.engine.connect() as c:
        source = one(c, "SELECT * FROM source_revisions WHERE id=:id", id=revision)
    content = generation.store.read(source["blob_ref"], source["sha256"])
    outline_data = generation.journal.compute(
        "outline.v2",
        {"source": source["sha256"], "policy": "document-outline-v2"},
        lambda: build_outline(content, parsed["artifact"]["blocks"]).model_dump(),
    )
    outline = DocumentOutline.model_validate(outline_data)
    if any(i["severity"] == "ERROR" for i in outline.issues):
        raise QualityFailure(
            "Original outline needs review before book planning: " + canonical(outline.issues)
        )
    binding = {
        "source": revision,
        "parse_id": parsed["parse_id"],
        "profile": sha(canonical(generation.profile)),
    }
    cached = generation.journal.load("book.plan.v2", binding)
    if cached is not None:
        plan = BookPlan.model_validate(cached)
        check_book_coverage(units, [ch.plan for ch in plan.chapters])
        return plan

    def choose_layout():
        with generation.store.db.engine.connect() as c:
            receipts = rows(
                c,
                "SELECT stage FROM job_checkpoints WHERE job_id=:id AND state='DONE'",
                id=generation.lease.job_id,
            )
        frozen_indices = set()
        for receipt in receipts:
            match = re.fullmatch(
                r"chapter\.(\d+)\.(?:content-selection-v1\.)?units", receipt["stage"]
            )
            if match:
                frozen_indices.add(int(match[1]))
        old_chapters = parsed["artifact"].get("chapters", [])
        frozen = []
        for index in sorted(frozen_indices):
            if index >= len(old_chapters):
                raise QualityFailure("Frozen chapter no longer has a matching original range")
            ch = old_chapters[index]
            key, namespace = f"chapter{index}", f"chapter.{index}."
            if index == 0 and ch["title"] == "前置内容":
                key += ".content-selection-v1"
                namespace += "content-selection-v1."
            frozen.append({**ch, "planning_key": key, "namespace": namespace})
        if not frozen:
            return {"mode": "OUTLINE_SEGMENTS", "chapters": outline.segments}
        # Reuse only already frozen ranges. All remaining pages follow the corrected
        # hierarchy, so appendices and references never remain in the final chapter.
        remaining = []
        for segment in outline.segments:
            intervals = [(segment["start"], segment["end"])]
            for old in frozen:
                next_intervals = []
                for start, end in intervals:
                    if old["end"] < start or old["start"] > end:
                        next_intervals.append((start, end))
                    else:
                        if start < old["start"]:
                            next_intervals.append((start, old["start"] - 1))
                        if end > old["end"]:
                            next_intervals.append((old["end"] + 1, end))
                intervals = next_intervals
            for start, end in intervals:
                key = f"outline.{segment['node_id']}.{start}.{end}"
                remaining.append(
                    {
                        **segment,
                        "start": start,
                        "end": end,
                        "planning_key": key,
                        "namespace": key + ".",
                    }
                )
        return {
            "mode": "MIXED_FROZEN_AND_OUTLINE",
            "chapters": sorted(frozen + remaining, key=lambda ch: ch["start"]),
        }

    layout = generation.journal.compute("book.layout.v2", binding, choose_layout)
    chapters, notes = [], []
    by_unit = {u.unit_id: u for u in units}
    unit_order = {u.unit_id: i for i, u in enumerate(units)}
    owners = unit_node_map(outline, units)
    for index, chapter in enumerate(layout["chapters"][:chapter_limit]):
        selected = [u for u in units if chapter["start"] <= u.page <= chapter["end"]]
        if not selected:
            continue
        key, namespace = f"chapter{index}", f"chapter.{index}."
        if "planning_key" in chapter:
            key, namespace = chapter["planning_key"], chapter["namespace"]
        elif index == 0 and chapter["title"] == "前置内容" and len(layout["chapters"]) > 1:
            key += ".content-selection-v1"
            namespace += "content-selection-v1."
        child = copy.copy(generation)
        child.chapter_units, child.chapter_key, child.plan_only = selected, key, True
        child.prepared_plan = None
        child.profile = {
            **generation.profile,
            "chapter": {k: chapter[k] for k in ("title", "start", "end")},
        }
        child.journal = ScopedJournal(generation.journal, namespace)
        artifact = {**parsed["artifact"], "chapters": [chapter]}
        child.parsing = SimpleNamespace(
            vision=generation.parsing.vision,
            run=lambda revision, artifact=artifact: {
                "parse_id": parsed["parse_id"],
                "artifact": artifact,
            },
        )
        child.progress = lambda stage, info, chapter=chapter, index=index: generation.progress(
            "plan-book." + stage,
            {
                **info,
                "chapter": chapter["title"],
                "chapter_position": index + 1,
                "chapter_total": len(layout["chapters"]),
            },
        )
        plan = child.run()
        chapters.append(
            PlannedChapter(
                key=key,
                namespace=namespace,
                title=chapter["title"],
                start=chapter["start"],
                end=chapter["end"],
                plan=plan,
            )
        )
        topics = {t["key"]: t for t in plan["topics"]}
        for group in (plan["merged"] or {}).get("groups", []):
            ids = sorted(
                {u for t in group["topic_keys"] for u in topics[t]["units"]},
                key=unit_order.__getitem__,
            )
            pages = sorted({by_unit[u].page for u in ids})
            primary, contexts = placement_for_units(outline, ids, owners, pages)
            notes.append(
                PlannedNote(
                    key=group["key"],
                    creation_key=f"atomic:{revision}:{sha(canonical(plan['merged']))}:{group['key']}",
                    title=group["title"],
                    question=group["question"],
                    scope=group["scope"],
                    chapter_key=key,
                    primary_node_id=primary,
                    context_node_ids=contexts,
                    unit_ids=ids,
                    pages=pages,
                )
            )
        generation.progress(
            "plan-book.chapter-ready",
            {
                "completed": len(chapters),
                "total": len(layout["chapters"]),
                "notes": len(notes),
                "chapter": chapter["title"],
            },
        )
    complete = chapter_limit is None or chapter_limit >= len(layout["chapters"])
    selected_units = (
        units
        if complete
        else [u for u in units if any(ch.start <= u.page <= ch.end for ch in chapters)]
    )
    check_book_coverage(selected_units, [ch.plan for ch in chapters])
    if not notes and complete:
        raise QualityFailure("Book has no substantive planned notes")
    if len({n.creation_key for n in notes}) != len(notes):
        raise QualityFailure("Duplicate planned atomic identity")
    plan = BookPlan(
        source_revision_id=revision,
        parse_id=parsed["parse_id"],
        profile_sha256=binding["profile"],
        outline=outline,
        layout_mode=layout["mode"],
        chapters=chapters,
        notes=notes,
        coverage={
            "total_units": len(units),
            "included_units": sum(ch.plan["coverage"]["included_units"] for ch in chapters),
            "excluded_units": sum(len(ch.plan["coverage"]["exclusions"]) for ch in chapters),
            "complete": complete,
            "total_chapters": len(layout["chapters"]),
        },
    )
    generation.journal.save(
        "book.plan.v2" if complete else f"book.plan.partial.{chapter_limit}",
        binding,
        plan.model_dump(),
    )
    generation.progress(
        "book-plan-ready" if complete else "chapter-plan-ready",
        {"chapters": len(chapters), "notes": len(notes), "source_units": len(units)},
    )
    return plan


def published_planned_notes(store, plan):
    keys = {n.creation_key: n for n in plan.notes}
    found = {}
    with store.db.engine.connect() as c:
        for n in rows(
            c,
            "SELECT * FROM notes WHERE kind='ATOMIC' AND visibility='ACTIVE' AND current_revision_id IS NOT NULL",
        ):
            if n["creation_key"] in keys and store.content.valid_revision(
                c, n["current_revision_id"]
            ):
                target = keys[n["creation_key"]]
                found[target.key] = {
                    "note_id": n["id"],
                    "revision_id": n["current_revision_id"],
                    "title": n["title"],
                    "question": target.question,
                }
    return found
