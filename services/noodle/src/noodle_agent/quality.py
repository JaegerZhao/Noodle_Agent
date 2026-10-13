from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .domain import NoteBlockDraft, NoteDraft, NoteSpec


def note_content_sha256(note: NoteDraft) -> str:
    """Hash exactly the semantic candidate that a quality verdict applies to."""

    payload = json.dumps(
        note.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def public_visual_context(raw: dict[str, Any]) -> dict[str, Any]:
    """Expose only stable, writing-relevant visual fields to an LLM."""

    allowed = (
        "external_block_id",
        "asset_type",
        "is_decorative",
        "essential",
        "caption",
        "teaching_role",
        "ocr_text",
        "factual_observations",
        "spatial_relations",
        "suggested_placement",
        "confidence",
        "analysis_tier",
        "is_fragment",
        "fragment_group",
        "group_id",
        "group_type",
        "related_asset_ids",
    )
    return {key: raw.get(key) for key in allowed if key in raw}


def required_asset_ids(
    spec: NoteSpec,
    vision_context: dict[str, dict[str, Any]] | None,
) -> list[str]:
    """Retain every planner-selected visual unless analysis explicitly rejects it.

    The planner has already decided that these assets support the note. A vision
    result may veto decoration/low-information repetition, but a missing vision
    result must not silently discard source material.
    """

    required: list[str] = []
    for block_id in spec.asset_block_ids:
        understanding = (vision_context or {}).get(block_id)
        if understanding and (
            bool(understanding.get("is_decorative"))
            or str(understanding.get("asset_type") or "").upper() == "DECORATIVE"
        ):
            continue
        required.append(block_id)
    return list(dict.fromkeys(required))


def used_asset_ids(note: NoteDraft) -> set[str]:
    return {block_id for block in note.blocks for block_id in block.asset_block_ids}


def deduplicate_asset_placements(note: NoteDraft) -> NoteDraft:
    """Keep one canonical placement for each source asset within a note.

    A multimodal writer can mention the same MinerU block in two IMAGE blocks.
    That is a mechanical layout defect, not a reason to discard the entire
    knowledge unit.  Preserve the first placement, merge evidence and useful
    caption metadata into it, and remove only the redundant placement.
    """

    blocks = [block.model_copy(deep=True) for block in note.blocks]
    owner_by_asset: dict[str, NoteBlockDraft] = {}
    normalized: list[NoteBlockDraft] = []
    for block in blocks:
        unique_assets = list(dict.fromkeys(block.asset_block_ids))
        retained: list[str] = []
        for asset_id in unique_assets:
            owner = owner_by_asset.get(asset_id)
            if owner is None:
                retained.append(asset_id)
                owner_by_asset[asset_id] = block
                continue
            owner.evidence_block_ids = list(
                dict.fromkeys([*owner.evidence_block_ids, *block.evidence_block_ids])
            )
            if not owner.caption.strip() and block.caption.strip():
                owner.caption = block.caption
            if not owner.purpose.strip() and block.purpose.strip():
                owner.purpose = block.purpose
        block.asset_block_ids = retained
        block.evidence_block_ids = list(dict.fromkeys(block.evidence_block_ids))
        if block.block_type.upper() == "IMAGE" and not retained:
            continue
        normalized.append(block)
    return note.model_copy(update={"blocks": normalized})


def ensure_required_visual_blocks(
    note: NoteDraft,
    spec: NoteSpec,
    vision_context: dict[str, dict[str, Any]] | None,
) -> NoteDraft:
    """Deterministically retain planner-selected non-decorative visuals.

    LLMs occasionally omit one member of a multi-panel source figure even when
    explicitly instructed to keep it. Missing source media is a data-loss bug,
    so the harness repairs the block structure instead of repeatedly asking the
    model to remember an ID.
    """

    missing = [
        block_id
        for block_id in required_asset_ids(spec, vision_context)
        if block_id not in used_asset_ids(note)
    ]
    if not missing:
        return note
    grouped: dict[str, list[str]] = {}
    for block_id in missing:
        understanding = (vision_context or {}).get(block_id) or {}
        group_key = str(understanding.get("group_id") or block_id)
        grouped.setdefault(group_key, []).append(block_id)

    placements: list[tuple[int, NoteBlockDraft]] = []
    for block_ids in grouped.values():
        understandings = [(vision_context or {}).get(block_id) or {} for block_id in block_ids]
        caption = next(
            (
                str(item.get("caption") or "").strip()
                for item in understandings
                if str(item.get("caption") or "").strip()
            ),
            "",
        )
        role = next(
            (
                str(item.get("teaching_role") or "").strip()
                for item in understandings
                if str(item.get("teaching_role") or "").strip()
            ),
            "",
        )
        placement_hint = " ".join(
            str(item.get("suggested_placement") or "").strip() for item in understandings
        ).strip()
        searchable = f"{caption} {role} {placement_hint}"
        terms = {token.casefold() for token in re.findall(r"[A-Za-z0-9_+-]{2,}", searchable)}
        for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", searchable):
            terms.update(
                phrase[index : index + width]
                for width in (2, 3)
                for index in range(max(0, len(phrase) - width + 1))
            )
        best_index = len(note.blocks) - 1
        best_score = 0
        for index, block in enumerate(note.blocks):
            if block.block_type.upper() == "IMAGE":
                continue
            block_text = " ".join(
                [block.text, block.caption, block.purpose, *block.items]
            ).casefold()
            score = sum(1 for term in terms if term in block_text)
            if score > best_score:
                best_index = index
                best_score = score
        purpose = role or (
            spec.visual_requirements[0] if spec.visual_requirements else "补充正文中的来源图示"
        )
        placements.append(
            (
                max(best_index, -1),
                NoteBlockDraft(
                    block_type="IMAGE",
                    caption=caption or role or f"来源图示：{spec.title}",
                    purpose=purpose,
                    evidence_block_ids=list(spec.evidence_block_ids),
                    asset_block_ids=block_ids,
                ),
            )
        )

    # Insert from the end so an earlier insertion does not shift later anchors.
    for anchor, image_block in sorted(placements, key=lambda item: item[0], reverse=True):
        note.blocks.insert(anchor + 1, image_block)
    return note


def normalize_note_blocks(note: NoteDraft) -> NoteDraft:
    """Normalize provider formatting without rewriting authored meaning."""

    for block in note.blocks:
        kind = block.block_type.upper()
        text = block.text.strip()
        if kind == "FORMULA":
            if (text.startswith("$$") and text.endswith("$$") and len(text) >= 4) or (
                text.startswith(r"\[") and text.endswith(r"\]")
            ):
                text = text[2:-2].strip()
        elif kind == "CODE" and text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            opening = lines[0][3:].strip() if lines else ""
            if opening and not block.language:
                block.language = opening
            text = "\n".join(lines[1:-1]).strip()
        elif kind in {"PARAGRAPH", "EXAMPLE", "CALLOUT"}:
            text = re.sub(r"\\\((.+?)\\\)", r"$\1$", text, flags=re.DOTALL)
            text = re.sub(r"\\\[(.+?)\\\]", r"$$\1$$", text, flags=re.DOTALL)
        block.text = re.sub(r"\n{3,}", "\n\n", text)
    return deduplicate_prose_blocks(note)


def deduplicate_prose_blocks(note: NoteDraft) -> NoteDraft:
    """Remove exact repeated prose in one unit while retaining evidence."""
    seen: dict[tuple[str, str], NoteBlockDraft] = {}
    blocks = []
    for original in note.blocks:
        block = original.model_copy(deep=True)
        key = (block.block_type.upper(), block.text.strip())
        eligible = key[0] in {"PARAGRAPH", "EXAMPLE", "CALLOUT"} and bool(key[1])
        eligible = eligible and not any(
            [
                block.asset_block_ids,
                block.items,
                block.table_headers,
                block.table_rows,
                block.caption,
                block.purpose,
            ]
        )
        if eligible and key in seen:
            owner = seen[key]
            owner.evidence_block_ids = list(
                dict.fromkeys([*owner.evidence_block_ids, *block.evidence_block_ids])
            )
            continue
        blocks.append(block)
        if eligible:
            seen[key] = block
    return note.model_copy(update={"blocks": blocks})
