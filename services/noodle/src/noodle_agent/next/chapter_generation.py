"""Incremental textbook generation; one original source and one resumable root job."""

import copy
from types import SimpleNamespace

from .contracts import MapEntry, sha
from .generation_contracts import MergePlan
from .planning import QualityFailure


class ScopedJournal:
    def __init__(self, journal, prefix):
        self.journal, self.prefix, self.lease = journal, prefix, journal.lease

    def exists(self, stage):
        return self.journal.exists(self.prefix + stage)

    def load(self, stage, inputs):
        return self.journal.load(self.prefix + stage, inputs)

    def save(self, stage, inputs, value):
        return self.journal.save(self.prefix + stage, inputs, value)

    def compute(self, stage, inputs, function):
        return self.journal.compute(self.prefix + stage, inputs, function)

    def operator_recovery(self, stages):
        return self.journal.operator_recovery([self.prefix + stage for stage in stages])

    def intent(self, stage, inputs):
        return self.journal.intent(self.prefix + stage, inputs)


def generate_chapters(generation, parsed, units, *, book_plan=None, only_chapter=None):
    from .book_planning import prepare_book, published_planned_notes
    from .contracts import canonical

    revision = generation.lease.input["revision_id"]
    # Hard barrier: no writing or publication until the entire book plan passes.
    if (
        book_plan is None
        and not generation.plan_only
        and not generation.journal.exists("book.plan.v2")
    ):
        return generate_rolling(generation, parsed, units)
    plan = book_plan or prepare_book(generation, parsed, units)
    if generation.plan_only:
        return {
            "source_revision_id": revision,
            "parse_id": parsed["parse_id"],
            "book_plan": plan.model_dump(),
            "notes": [],
            "source_map": None,
            "coverage": plan.coverage,
            "status": "PLANNED_NOT_GENERATED",
        }
    published = published_planned_notes(generation.store, plan)
    groups = [g for ch in plan.chapters for g in (ch.plan["merged"] or {}).get("groups", [])]
    if not groups:
        from .task_control import boundary

        boundary(generation.store, generation.lease)
        return {"notes": [], "source_map": None, "coverage": plan.coverage, "status": "PARTIAL"}
    merged = MergePlan.model_validate(
        {"summary": "按已核对的章节规划逐篇生成笔记。", "groups": groups}
    )
    reports, failures = [], []

    def publish_map(complete=False):
        complete = plan.coverage["complete"] and len(published) == len(plan.notes)
        entries = [
            MapEntry(
                entry_id=n.node_id,
                parent_entry_id=n.parent_id,
                title=n.title,
                description=f"原资料第 {n.start}–{n.end} 页",
            )
            for n in plan.outline.nodes
        ]
        for note in plan.notes:
            output = published.get(note.key)
            entries.append(
                MapEntry(
                    entry_id="planned-" + sha(canonical(note.creation_key))[:20],
                    parent_entry_id=note.primary_node_id,
                    title=output["title"] if output else note.title,
                    target_note_id=output["note_id"] if output else None,
                    description=note.question + ("" if output else "（待生成）"),
                )
            )
        coverage = {
            "total_units": len(units),
            "included_units": plan.coverage["included_units"],
            "exclusions": [e for ch in plan.chapters for e in ch.plan["coverage"]["exclusions"]],
            "groups": merged.model_dump(),
            "chapters": [
                {
                    "chapter": {"title": ch.title, "start": ch.start, "end": ch.end},
                    **ch.plan["coverage"],
                }
                for ch in plan.chapters
            ],
            "parse_id": parsed["parse_id"],
            "complete": complete,
            "book_plan_sha256": sha(canonical(plan.model_dump())),
        }
        summary = (
            "全书笔记已完成。"
            if complete
            else (
                "全书规划已完成，尚未完成全书写作。"
                if plan.coverage["complete"]
                else "当前部分章节已规划，后续章节待规划。"
            )
        ) + f" 已发布 {len(published)} / {len(plan.notes)} 篇。"
        return generation.finish_source_map(
            revision,
            parsed,
            merged.model_copy(update={"summary": summary}),
            coverage,
            list(published.values()),
            stable_key=f"source-map:{revision}:chaptered-v1",
            entries=entries,
        )

    result = publish_map()
    for index, chapter in enumerate(plan.chapters):
        if only_chapter is not None and chapter.key != only_chapter:
            continue
        from .repository import one
        from .task_control import boundary, enter_chapter

        boundary(generation.store, generation.lease)
        expected = [n for n in plan.notes if n.chapter_key == chapter.key]
        with generation.store.db.engine.connect() as c:
            reusable = bool(expected) and all(
                n.key in published
                and one(
                    c,
                    "SELECT id FROM build_artifacts WHERE job_id=:j AND storage_ref=:ref",
                    j=generation.lease.job_id,
                    ref=f"artifacts/builds/{published[n.key]['revision_id']}.json",
                )
                for n in expected
            )
        if reusable:
            reports.append(
                {
                    "chapter": {"title": chapter.title, "start": chapter.start, "end": chapter.end},
                    **chapter.plan["coverage"],
                }
            )
            generation.progress(
                "chapter-reused",
                {"chapter": chapter.title, "count": len(expected), "notes": len(published)},
            )
            continue
        enter_chapter(generation.store, generation.lease, chapter.key, chapter.title)
        child = copy.copy(generation)
        child.chapter_units = [u for u in units if chapter.start <= u.page <= chapter.end]
        child.chapter_key, child.plan_only, child.prepared_plan = chapter.key, False, chapter.plan
        child.profile = {
            **generation.profile,
            "chapter": {"title": chapter.title, "start": chapter.start, "end": chapter.end},
        }
        child.journal = ScopedJournal(generation.journal, chapter.namespace)
        artifact = {
            **parsed["artifact"],
            "chapters": [{"title": chapter.title, "start": chapter.start, "end": chapter.end}],
        }
        child.parsing = SimpleNamespace(
            vision=generation.parsing.vision,
            run=lambda revision, artifact=artifact: {
                "parse_id": parsed["parse_id"],
                "artifact": artifact,
            },
        )
        child.progress = lambda stage, info, chapter=chapter, index=index: generation.progress(
            stage,
            {
                **info,
                "chapter": chapter.title,
                "chapter_position": index + 1,
                "chapter_total": len(plan.chapters),
            },
        )
        current = child.run()
        failures.extend(current.get("failures", []))
        for output in current["notes"]:
            target = next(
                n
                for n in plan.notes
                if n.creation_key == generation.store.get_note(output["note_id"])["creation_key"]
            )
            published[target.key] = output
        reports.append(
            {
                "chapter": {"title": chapter.title, "start": chapter.start, "end": chapter.end},
                **current["coverage"],
            }
        )
        # Read APIs project every individual publication; chapter snapshots are durable summaries.
        result = publish_map()
        generation.progress(
            "chapter-completed",
            {
                "completed": index + 1,
                "total": len(plan.chapters),
                "notes": len(published),
                "title": chapter.title,
                "failed": len(current.get("failures", [])),
                "chapter_notes": len(current["notes"]),
                "chapter_planned": len((chapter.plan.get("merged") or {}).get("groups", [])),
            },
        )
        from .task_control import boundary

        boundary(generation.store, generation.lease)
    if not plan.coverage["complete"]:
        return {**result, "status": "PARTIAL", "failures": failures}
    if failures or len(published) != len(plan.notes):
        raise QualityFailure(
            "Book has unpublished planned notes; checked failures: " + canonical(failures)
        )
    result = publish_map(True)
    generation.journal.compute("coverage", {"chapters": reports}, lambda: result["coverage"])
    return result


def generate_rolling(generation, parsed, units):
    """New jobs plan one chapter at a time; v2 jobs retain their frozen plan."""
    from .book_planning import prepare_book
    from .task_control import boundary

    limit = 1
    while True:
        boundary(generation.store, generation.lease)
        plan = prepare_book(generation, parsed, units, chapter_limit=limit)
        result = generate_chapters(
            generation,
            parsed,
            units,
            book_plan=plan,
            only_chapter=plan.chapters[-1].key if plan.chapters else None,
        )
        if plan.coverage["complete"]:
            return result
        boundary(generation.store, generation.lease)
        limit += 1
