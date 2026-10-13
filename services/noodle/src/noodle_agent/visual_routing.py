from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import NoteSetPlan, ParsedBlock, ParsedDocument


@dataclass(frozen=True)
class LocalVisualRoute:
    block_id: str
    route: str
    reason: str
    image_sha256: str
    source_caption: str = ""
    fragment_group: str = ""
    affinity_group: str = ""
    group_type: str = "SINGLE"
    related_block_ids: tuple[str, ...] = ()


def _source_caption(block: ParsedBlock) -> str:
    mineru = block.metadata.get("mineru") if isinstance(block.metadata, dict) else None
    if not isinstance(mineru, dict):
        return block.text.strip()
    raw_caption = mineru.get("image_caption") or mineru.get("caption")
    if isinstance(raw_caption, list):
        caption = " ".join(str(item).strip() for item in raw_caption if str(item).strip())
    else:
        caption = str(raw_caption or "").strip()
    if caption:
        return caption

    footnotes = mineru.get("image_footnote") or []
    if not isinstance(footnotes, list):
        footnotes = [footnotes]
    attribution = re.compile(
        r"copyright|courtesy|reproduced|permission|public domain|usage permitted|"
        r"photos? by|image source|figure source|^\s*\[[^]]*et al\.?[^]]*]\s*$",
        re.IGNORECASE,
    )
    semantic_footnotes = [
        str(item).strip()
        for item in footnotes
        if str(item).strip() and not attribution.search(str(item))
    ]
    if semantic_footnotes:
        return " | ".join(semantic_footnotes)
    # MinerU commonly copies copyright/credit footnotes into block.text. When
    # every footnote is attribution-only, require visual analysis instead of
    # treating the credit line as a teaching caption.
    if footnotes:
        return ""
    return block.text.strip()


def _page_extents(document: ParsedDocument) -> dict[int, tuple[float, float]]:
    extents: dict[int, tuple[float, float]] = {}
    for block in document.blocks:
        if not block.bbox or len(block.bbox) != 4:
            continue
        _x0, _y0, x1, y1 = block.bbox
        width, height = extents.get(block.page_index, (1.0, 1.0))
        extents[block.page_index] = (max(width, float(x1)), max(height, float(y1)))
    return extents


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overlap_ratio(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    overlap = max(0.0, min(end_a, end_b) - max(start_a, start_b))
    return overlap / max(1.0, min(end_a - start_a, end_b - start_b))


def _visual_affinity_groups(
    document: ParsedDocument,
    blocks: list[ParsedBlock],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Find conservative same-figure candidates without calling them fragments.

    MinerU often emits a teaching figure as several aligned panels. Same-page
    membership alone is insufficient: two unrelated figures may share a slide.
    We therefore connect only panels whose bounding boxes are both close and
    aligned. Vision may later refine a candidate to MULTI_PANEL or FRAGMENT.
    """

    extents = _page_extents(document)
    by_page: dict[int, list[ParsedBlock]] = {}
    for block in blocks:
        if block.bbox and len(block.bbox) == 4:
            by_page.setdefault(block.page_index, []).append(block)

    result: dict[str, tuple[str, tuple[str, ...]]] = {}
    for page_index, page_blocks in by_page.items():
        if len(page_blocks) < 2:
            continue
        page_width, page_height = extents.get(page_index, (1.0, 1.0))
        neighbors: dict[str, set[str]] = {block.block_id: set() for block in page_blocks}
        for index, left in enumerate(page_blocks):
            lx0, ly0, lx1, ly1 = (float(value) for value in left.bbox or [])
            for right in page_blocks[index + 1 :]:
                rx0, ry0, rx1, ry1 = (float(value) for value in right.bbox or [])
                horizontal_gap = max(0.0, max(lx0, rx0) - min(lx1, rx1))
                vertical_gap = max(0.0, max(ly0, ry0) - min(ly1, ry1))
                vertical_alignment = _overlap_ratio(ly0, ly1, ry0, ry1)
                horizontal_alignment = _overlap_ratio(lx0, lx1, rx0, rx1)
                aligned_row = (
                    vertical_alignment >= 0.40 and horizontal_gap / max(1.0, page_width) <= 0.16
                )
                aligned_column = (
                    horizontal_alignment >= 0.50 and vertical_gap / max(1.0, page_height) <= 0.10
                )
                if aligned_row or aligned_column:
                    neighbors[left.block_id].add(right.block_id)
                    neighbors[right.block_id].add(left.block_id)

        visited: set[str] = set()
        for block in page_blocks:
            if block.block_id in visited or not neighbors[block.block_id]:
                continue
            stack = [block.block_id]
            component: list[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                component.append(current)
                stack.extend(neighbors[current] - visited)
            if len(component) < 2:
                continue
            ordered = tuple(
                item.block_id
                for item in sorted(
                    (item for item in page_blocks if item.block_id in component),
                    key=lambda item: (
                        float((item.bbox or [0, 0])[1]),
                        float((item.bbox or [0])[0]),
                    ),
                )
            )
            digest = hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()[:8]
            group_id = f"page-{page_index + 1}-{digest}"
            for block_id in ordered:
                result[block_id] = (
                    group_id,
                    tuple(item for item in ordered if item != block_id),
                )
    return result


def route_planned_visuals(
    document: ParsedDocument,
    blocks: list[ParsedBlock],
) -> dict[str, LocalVisualRoute]:
    """Use only high-confidence local rules before spending vision tokens.

    A planner-selected image is retained by default. Local routing may avoid an
    API call when MinerU already supplies a caption, or reject only tiny/line-like
    uncaptained fragments. Ambiguous visuals always go to the API.
    """

    extents = _page_extents(document)
    affinity_groups = _visual_affinity_groups(document, blocks)

    result: dict[str, LocalVisualRoute] = {}
    for block in blocks:
        path = Path(str(block.asset_path))
        image_sha256 = _sha256(path)
        caption = _source_caption(block)
        affinity_group, related_block_ids = affinity_groups.get(block.block_id, ("", ()))
        if caption:
            result[block.block_id] = LocalVisualRoute(
                block_id=block.block_id,
                route="LOCAL_METADATA",
                reason="MinerU supplied a usable image caption",
                image_sha256=image_sha256,
                source_caption=caption[:500],
                affinity_group=affinity_group,
                group_type="MULTI_PANEL" if affinity_group else "SINGLE",
                related_block_ids=related_block_ids,
            )
            continue

        route = "API_FAST"
        reason = "planner-selected visual requires semantic description"
        if block.bbox and len(block.bbox) == 4:
            x0, y0, x1, y1 = (float(value) for value in block.bbox)
            width = max(0.0, x1 - x0)
            height = max(0.0, y1 - y0)
            page_width, page_height = extents.get(block.page_index, (1.0, 1.0))
            area_ratio = width * height / max(1.0, page_width * page_height)
            aspect = max(width, height) / max(1.0, min(width, height))
            byte_size = path.stat().st_size
            if (area_ratio <= 0.0015 and byte_size <= 8_192) or (
                aspect >= 18 and area_ratio <= 0.01
            ):
                route = "LOCAL_DECORATIVE"
                reason = (
                    "uncaptioned tiny or line-like MinerU fragment "
                    f"(area_ratio={area_ratio:.4f}, bytes={byte_size})"
                )
        result[block.block_id] = LocalVisualRoute(
            block_id=block.block_id,
            route=route,
            reason=reason,
            image_sha256=image_sha256,
            affinity_group=affinity_group,
            group_type="MULTI_PANEL" if affinity_group else "SINGLE",
            related_block_ids=related_block_ids,
        )
    return result


def batch_by_page_affinity(
    items: list[dict[str, Any]], batch_size: int
) -> list[list[dict[str, Any]]]:
    """Keep same-page MinerU fragments adjacent, then fill bounded batches."""

    ordered = sorted(
        items,
        key=lambda item: (
            int(item["block"].page_index),
            float((item["block"].bbox or [0])[0]),
            float((item["block"].bbox or [0, 0])[1]),
        ),
    )
    return [ordered[index : index + batch_size] for index in range(0, len(ordered), batch_size)]


def enrich_planned_visual_sequences(
    document: ParsedDocument,
    note_set: NoteSetPlan,
) -> tuple[NoteSetPlan, dict[str, list[str]]]:
    """Restore MinerU panels located between planner-selected endpoints.

    A planner may select the input and output panels of one slide while missing
    the kernel, highlighted intermediate state, or arrow that MinerU extracted
    as separate image blocks. When at least two selected assets on the same page
    bound other image blocks, those in-between blocks belong to the candidate
    sequence and are restored before visual routing. The later local/vision
    router may still mark a tiny arrow as decorative, but it can no longer hide
    a meaningful intermediate panel from the writer.
    """

    enriched = note_set.model_copy(deep=True)
    block_order = {block.block_id: index for index, block in enumerate(document.blocks)}
    asset_blocks = [
        block
        for block in document.blocks
        if block.asset_path and block.bbox and len(block.bbox) == 4
    ]
    by_id = {block.block_id: block for block in asset_blocks}
    by_page: dict[int, list[ParsedBlock]] = {}
    for block in asset_blocks:
        by_page.setdefault(block.page_index, []).append(block)

    additions: dict[str, list[str]] = {}
    for spec in enriched.notes:
        selected = {block_id for block_id in spec.asset_block_ids if block_id in by_id}
        selected_by_page: dict[int, list[ParsedBlock]] = {}
        for block_id in selected:
            block = by_id[block_id]
            selected_by_page.setdefault(block.page_index, []).append(block)
        added: list[str] = []
        for page_index, endpoints in selected_by_page.items():
            if len(endpoints) < 2:
                continue
            x0 = min(float(block.bbox[0]) for block in endpoints if block.bbox)
            y0 = min(float(block.bbox[1]) for block in endpoints if block.bbox)
            x1 = max(float(block.bbox[2]) for block in endpoints if block.bbox)
            y1 = max(float(block.bbox[3]) for block in endpoints if block.bbox)
            height = max(1.0, y1 - y0)
            vertical_padding = height * 0.18
            for candidate in by_page.get(page_index, []):
                if candidate.block_id in selected or not candidate.bbox:
                    continue
                cx = (float(candidate.bbox[0]) + float(candidate.bbox[2])) / 2
                cy = (float(candidate.bbox[1]) + float(candidate.bbox[3])) / 2
                if x0 <= cx <= x1 and y0 - vertical_padding <= cy <= y1 + vertical_padding:
                    selected.add(candidate.block_id)
                    added.append(candidate.block_id)
        if added:
            spec.asset_block_ids = sorted(selected, key=lambda key: block_order[key])
            additions[spec.stable_key] = sorted(set(added), key=lambda key: block_order[key])
    return enriched, additions
