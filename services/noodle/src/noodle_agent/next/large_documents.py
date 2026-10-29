"""Bounded PDF transport and chapter planning, retaining one original source identity."""

import re
from datetime import UTC, datetime

import fitz

from .contracts import canonical, sha
from .planning import QualityFailure, windows


def chapter_ranges(content):
    with fitz.open(stream=content, filetype="pdf") as pdf:
        starts = [
            (str(title), int(page))
            for level, title, page in pdf.get_toc()
            if re.match(r"^\d+\s+\S", str(title)) and 1 <= page <= len(pdf)
        ]
        starts = sorted(dict.fromkeys(starts), key=lambda item: item[1])
        if not starts or starts[0][1] != 1:
            starts.insert(0, ("前置内容", 1))
        return [
            {
                "title": title,
                "start": start,
                "end": starts[i + 1][1] - 1 if i + 1 < len(starts) else len(pdf),
            }
            for i, (title, start) in enumerate(starts)
        ]


def parse_parts(parsing, source, original, page_limit=180):
    # Each submit has its own INTENT and receipt. A lost receipt never causes re-upload.
    from .parsing import ParsePending

    store, journal, parser = parsing.store, parsing.journal, parsing.parser
    parts, pending = [], []
    base = f"artifacts/parser/{journal.lease.job_id}"
    with fitz.open(stream=original, filetype="pdf") as pdf:
        count = len(pdf)
        for index, start in enumerate(range(0, count, page_limit)):
            end = min(count, start + page_limit)
            inputs = {
                "original_sha256": source["sha256"],
                "start": start,
                "end": end,
                "profile": parsing.profile,
                "policy": "pdf-parts-v1",
            }
            prefix = f"parse.part.{index}"
            raw = journal.load(prefix + ".download", inputs)
            if raw is None:
                submitted = journal.load(prefix + ".submit", inputs)
                if submitted is None:
                    with fitz.open() as part:
                        part.insert_pdf(pdf, from_page=start, to_page=end - 1)
                        content = part.tobytes(garbage=4, deflate=True)
                    ref = f"{base}/input-{index}-{sha(content)[:16]}.pdf"
                    store.put(ref, content)
                    journal.intent(prefix + ".submit", inputs)
                    submitted = parser.submit(store.path(ref), f"{source['id']}-part-{index}")
                    submitted["submitted_at"] = datetime.now(UTC).isoformat()
                    journal.save(prefix + ".submit", inputs, submitted)
                elapsed = (
                    datetime.now(UTC) - datetime.fromisoformat(submitted["submitted_at"])
                ).total_seconds()
                if elapsed > 3600:
                    raise QualityFailure(
                        f"PDF part {index + 1} exceeded one hour; inspect existing task"
                    )
                result = (
                    submitted
                    if submitted.get("state") == "done"
                    else parser.poll(submitted["external_id"])
                )
                state = str(result.get("state", "pending")).lower()
                if state in {"failed", "error"}:
                    raise QualityFailure(f"PDF part {index + 1} failed; inspect provider receipt")
                if state != "done":
                    pending.append(index + 1)
                    continue
                document = parser.download_and_normalize(
                    submitted["external_id"],
                    result.get("result") or result,
                    store.path(f"{base}/part-{index}"),
                    source["title"],
                )
                raw = document.model_dump(mode="json")
                if any(not 0 <= b["page_index"] < end - start for b in raw["blocks"]):
                    raise QualityFailure("Partition parser page outside its submitted range")
                journal.save(prefix + ".download", inputs, raw)
            parts.append((index, start, raw))
            parsing.progress(
                "parse-part",
                {"completed": len(parts), "total": (count + page_limit - 1) // page_limit},
            )
    if pending:
        raise ParsePending(f"parts awaiting provider: {pending}")
    blocks = []
    for index, offset, raw in parts:
        for block in raw["blocks"]:
            blocks.append(
                {
                    **block,
                    "block_id": f"part{index}:{block['block_id']}",
                    "page_index": block["page_index"] + offset,
                    "metadata": {**block.get("metadata", {}), "part": index, "page_offset": offset},
                }
            )
    return {
        "blocks": blocks,
        "parser_name": parser.name,
        "parser_version": canonical(sorted({p[2]["parser_version"] for p in parts})),
    }


def planning_windows(units, artifact):
    if artifact.get("page_count", 0) <= 180:
        return list(windows(units))
    chapters = artifact.get("chapters") or [{"start": 1, "end": artifact["page_count"]}]
    result = []
    for chapter in chapters:
        selected = [u for u in units if chapter["start"] <= u.page <= chapter["end"]]
        result.extend(windows(selected))
    if [u.unit_id for window in result for u in window] != [u.unit_id for u in units]:
        raise QualityFailure("Chapter windows did not preserve the complete ordered unit manifest")
    return result


def topic_batches(topics, by_unit, artifact, budget=55000):
    # Cross-chapter deduplication belongs to Concept maintenance, not a monolithic note.
    chapters = artifact.get("chapters") or [{"start": 1, "end": artifact["page_count"]}]
    batches, batch, size, previous = [], [], 2, None
    for topic in topics:
        page = min(by_unit[k].page for k in topic.units)
        chapter = next((i for i, ch in enumerate(chapters) if ch["start"] <= page <= ch["end"]), -1)
        value = {
            **topic.model_dump(),
            "source_chars": sum(len(by_unit[k].text) for k in topic.units),
        }
        cost = len(canonical(value)) + 1
        if cost + 2 > budget:
            raise QualityFailure(
                "One topic exceeds bounded merge budget; planning must narrow its scope"
            )
        if batch and (size + cost > budget or chapter != previous):
            batches.append(batch)
            batch, size = [], 2
        batch.append(topic)
        size += cost
        previous = chapter
    if batch:
        batches.append(batch)
    return batches
