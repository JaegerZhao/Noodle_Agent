from __future__ import annotations

from pathlib import Path

from noodle_agent.domain import NoteSetPlan, NoteSpec, ParsedBlock, ParsedDocument
from noodle_agent.visual_routing import (
    batch_by_page_affinity,
    enrich_planned_visual_sequences,
    route_planned_visuals,
)


def _document(blocks: list[ParsedBlock]) -> ParsedDocument:
    return ParsedDocument(
        title="视觉路由测试",
        page_count=2,
        markdown="",
        blocks=blocks,
        parser_name="test",
    )


def test_mineru_caption_avoids_api_without_dropping_image(tmp_path: Path) -> None:
    image = tmp_path / "captioned.jpg"
    image.write_bytes(b"not-a-real-jpeg-but-routing-only")
    block = ParsedBlock(
        block_id="p0-image",
        block_type="IMAGE",
        page_index=0,
        page_label="1",
        bbox=[0, 0, 500, 300],
        asset_path=str(image),
        metadata={"mineru": {"image_caption": ["卷积核滑动示意图"]}},
    )
    route = route_planned_visuals(_document([block]), [block])[block.block_id]
    assert route.route == "LOCAL_METADATA"
    assert route.source_caption == "卷积核滑动示意图"


def test_only_high_confidence_tiny_fragment_is_locally_decorative(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.jpg"
    tiny.write_bytes(b"x" * 100)
    tiny_block = ParsedBlock(
        block_id="p0-tiny",
        block_type="IMAGE",
        page_index=0,
        page_label="1",
        bbox=[10, 10, 12, 12],
        asset_path=str(tiny),
    )
    page_extent = ParsedBlock(
        block_id="p0-text",
        block_type="TEXT",
        page_index=0,
        page_label="1",
        bbox=[0, 0, 1000, 1000],
    )
    route = route_planned_visuals(_document([tiny_block, page_extent]), [tiny_block])[
        tiny_block.block_id
    ]
    assert route.route == "LOCAL_DECORATIVE"


def test_attribution_only_footnote_is_not_treated_as_teaching_caption(
    tmp_path: Path,
) -> None:
    image = tmp_path / "credited.jpg"
    image.write_bytes(b"needs-real-vision")
    block = ParsedBlock(
        block_id="p0-credit",
        block_type="IMAGE",
        text="Figure copyright Example Authors. Reproduced with permission.",
        page_index=0,
        page_label="1",
        bbox=[0, 0, 500, 300],
        asset_path=str(image),
        metadata={
            "mineru": {
                "image_caption": [],
                "image_footnote": ["Figure copyright Example Authors. Reproduced with permission."],
            }
        },
    )
    route = route_planned_visuals(_document([block]), [block])[block.block_id]
    assert route.route == "API_FAST"


def test_page_affinity_keeps_fragments_in_same_batch() -> None:
    blocks = [
        ParsedBlock(block_id="p1-a", page_index=1, page_label="2", bbox=[0, 0, 1, 1]),
        ParsedBlock(block_id="p0-a", page_index=0, page_label="1", bbox=[0, 0, 1, 1]),
        ParsedBlock(block_id="p0-b", page_index=0, page_label="1", bbox=[2, 0, 3, 1]),
    ]
    batches = batch_by_page_affinity([{"block": block} for block in blocks], batch_size=2)
    assert [item["block"].block_id for item in batches[0]] == ["p0-a", "p0-b"]


def test_aligned_panels_are_affinity_grouped_but_not_called_fragments(
    tmp_path: Path,
) -> None:
    blocks: list[ParsedBlock] = []
    for index, bbox in enumerate(([100, 200, 300, 500], [330, 210, 530, 490])):
        image = tmp_path / f"panel-{index}.jpg"
        image.write_bytes(f"panel-{index}".encode())
        blocks.append(
            ParsedBlock(
                block_id=f"p0-panel-{index}",
                block_type="IMAGE",
                text=f"面板 {index + 1}",
                page_index=0,
                page_label="1",
                bbox=list(bbox),
                asset_path=str(image),
                metadata={"mineru": {"image_caption": [f"面板 {index + 1}"]}},
            )
        )
    extent = ParsedBlock(
        block_id="p0-extent",
        page_index=0,
        page_label="1",
        bbox=[0, 0, 1000, 800],
    )
    routes = route_planned_visuals(_document([*blocks, extent]), blocks)
    assert routes[blocks[0].block_id].affinity_group
    assert routes[blocks[0].block_id].affinity_group == routes[blocks[1].block_id].affinity_group
    assert routes[blocks[0].block_id].fragment_group == ""
    assert routes[blocks[0].block_id].group_type == "MULTI_PANEL"
    assert routes[blocks[1].block_id].block_id in routes[blocks[0].block_id].related_block_ids


def test_unrelated_same_page_images_are_not_grouped(tmp_path: Path) -> None:
    paths = [tmp_path / "top-left.jpg", tmp_path / "bottom-right.jpg"]
    for path in paths:
        path.write_bytes(path.name.encode())
    blocks = [
        ParsedBlock(
            block_id="p0-a",
            page_index=0,
            page_label="1",
            bbox=[20, 20, 220, 180],
            asset_path=str(paths[0]),
        ),
        ParsedBlock(
            block_id="p0-b",
            page_index=0,
            page_label="1",
            bbox=[700, 600, 950, 780],
            asset_path=str(paths[1]),
        ),
        ParsedBlock(
            block_id="p0-extent",
            page_index=0,
            page_label="1",
            bbox=[0, 0, 1000, 800],
        ),
    ]
    routes = route_planned_visuals(_document(blocks), blocks[:2])
    assert routes["p0-a"].affinity_group == ""
    assert routes["p0-b"].affinity_group == ""


def test_missing_panel_between_selected_endpoints_is_restored(tmp_path: Path) -> None:
    blocks: list[ParsedBlock] = []
    for index, bbox in enumerate(
        ([100, 200, 250, 450], [280, 220, 380, 430], [410, 200, 600, 450])
    ):
        image = tmp_path / f"sequence-{index}.jpg"
        image.write_bytes(f"sequence-{index}".encode())
        blocks.append(
            ParsedBlock(
                block_id=f"p0-{index}",
                block_type="IMAGE",
                page_index=0,
                page_label="1",
                bbox=list(bbox),
                asset_path=str(image),
            )
        )
    note_set = NoteSetPlan(
        document_summary="卷积步骤",
        notes=[
            NoteSpec(
                stable_key="conv-sequence",
                title="卷积步骤",
                question_answered="卷积如何进行？",
                target_thesis="输入经过卷积核得到输出。",
                concept_keys=["conv"],
                asset_block_ids=["p0-0", "p0-2"],
            )
        ],
    )
    enriched, additions = enrich_planned_visual_sequences(_document(blocks), note_set)
    assert enriched.notes[0].asset_block_ids == ["p0-0", "p0-1", "p0-2"]
    assert additions == {"conv-sequence": ["p0-1"]}
    assert note_set.notes[0].asset_block_ids == ["p0-0", "p0-2"]
