from __future__ import annotations

from noodle_agent.domain import (
    KnowledgeConcept,
    KnowledgeMap,
    KnowledgePlan,
    NoteBlockDraft,
    NoteDraft,
    NoteSetPlan,
    NoteSpec,
    ParsedBlock,
    ParsedDocument,
    SourceNoteOutlineNodeSpec,
    SourceNoteOutlinePlan,
)
from noodle_agent.planning import normalize_note_set_assets, normalize_note_set_boundaries
from noodle_agent.quality import (
    deduplicate_asset_placements,
    ensure_required_visual_blocks,
    normalize_note_blocks,
    public_visual_context,
)
from noodle_agent.services import NoodleService


def _document() -> ParsedDocument:
    return ParsedDocument(
        title="CNN",
        page_count=1,
        markdown="MLP lacks translation invariance",
        parser_name="test",
        blocks=[
            ParsedBlock(
                block_id="p1-text",
                block_type="TEXT",
                page_index=0,
                page_label="1",
                text="MLP lacks translation invariance.",
            ),
            ParsedBlock(
                block_id="p1-image",
                block_type="IMAGE",
                page_index=0,
                page_label="1",
                text="same object, shifted position, different response",
                asset_path="figure.png",
            ),
        ],
    )


def _spec() -> NoteSpec:
    return NoteSpec(
        stable_key="mlp-translation",
        title="MLP缺乏平移不变性",
        question_answered="为什么MLP不适合图像？",
        target_thesis="位置变化会改变MLP响应。",
        concept_keys=["mlp-translation"],
        evidence_block_ids=["p1-text", "p1-image"],
        asset_block_ids=["p1-image"],
    )


def test_single_level_outline_is_wrapped_without_rebinding_notes() -> None:
    outline = SourceNoteOutlinePlan(
        title="卷积神经网络（CNN）",
        summary="CNN 课程笔记",
        nodes=[
            SourceNoteOutlineNodeSpec(
                stable_key="basics",
                title="卷积基础",
                level=1,
                source_sections=["chapter-1"],
            ),
            SourceNoteOutlineNodeSpec(
                stable_key="pooling",
                title="池化层",
                level=1,
                source_sections=["chapter-2"],
            ),
        ],
    )
    note_set = NoteSetPlan(
        document_summary="CNN 课程笔记",
        notes=[
            _spec().model_copy(update={"outline_node_key": "basics"}),
        ],
    )

    normalized, rebound, repairs = NoodleService._normalize_minimum_outline_depth(
        outline, note_set
    )

    assert repairs[0]["type"] == "WRAP_SINGLE_LEVEL_OUTLINE"
    assert normalized.nodes[0].level == 1
    assert normalized.nodes[0].title == normalized.title
    assert all(node.level == 2 for node in normalized.nodes[1:])
    assert all(
        node.parent_key == normalized.nodes[0].stable_key
        for node in normalized.nodes[1:]
    )
    assert rebound.notes[0].outline_node_key == "basics"
    assert NoodleService._validate_source_note_outline(normalized) == []


def test_empty_outline_is_recovered_from_note_specs() -> None:
    outline = SourceNoteOutlinePlan(title="", summary="CNN", nodes=[])
    note_set = NoteSetPlan(
        document_summary="CNN",
        notes=[_spec().model_copy(update={"outline_node_key": ""})],
    )

    normalized, rebound, repairs = NoodleService._normalize_minimum_outline_depth(
        outline, note_set
    )

    assert repairs[0]["type"] == "BUILD_OUTLINE_FROM_NOTE_SET"
    assert normalized.title == "CNN"
    assert len(normalized.nodes) == 2
    assert normalized.nodes[1].parent_key == normalized.nodes[0].stable_key
    assert rebound.notes[0].outline_node_key == normalized.nodes[1].stable_key
    assert NoodleService._validate_source_note_outline(normalized) == []


def test_duplicate_asset_placement_is_repaired_without_dropping_note() -> None:
    note = NoteDraft(
        stable_key="convolution-basics",
        title="卷积基础",
        thesis="卷积核通过滑动计算提取局部模式。",
        blocks=[
            NoteBlockDraft(
                block_type="IMAGE",
                caption="卷积计算示意图",
                evidence_block_ids=[],
                asset_block_ids=["p1-image"],
            ),
            NoteBlockDraft(
                block_type="IMAGE",
                purpose="辅助解释卷积核滑动",
                evidence_block_ids=["p1-text"],
                asset_block_ids=["p1-image"],
            ),
        ],
    )

    repaired = deduplicate_asset_placements(note)
    passed, failures = NoodleService._validate_plan(
        _document(),
        KnowledgePlan(document_summary="CNN", notes=[repaired]),
    )

    assert failures == []
    assert len(passed) == 1
    assert len(repaired.blocks) == 1
    assert repaired.blocks[0].asset_block_ids == ["p1-image"]
    assert repaired.blocks[0].evidence_block_ids == ["p1-text"]


def test_planned_non_decorative_visual_cannot_be_silently_dropped() -> None:
    note = NoteDraft(
        stable_key="mlp-translation",
        title="MLP缺乏平移不变性",
        thesis="位置变化会改变MLP响应。",
        blocks=[
            NoteBlockDraft(
                text="同一物体移动后，MLP的响应可能明显变化。",
                evidence_block_ids=["p1-text"],
            )
        ],
    )
    _, failures = NoodleService._validate_plan(
        _document(),
        KnowledgePlan(document_summary="CNN", notes=[note]),
        NoteSetPlan(document_summary="CNN", notes=[_spec()]),
        {"p1-image": {"is_decorative": False, "essential": False}},
    )
    assert failures
    assert "MISSING_REQUIRED_ASSET:p1-image" in failures[0]["detail"]


def test_missing_visual_is_grouped_and_placed_near_related_text() -> None:
    spec = _spec()
    spec.asset_block_ids = ["panel-a", "panel-b"]
    note = NoteDraft(
        stable_key=spec.stable_key,
        title=spec.title,
        thesis=spec.target_thesis,
        blocks=[
            NoteBlockDraft(text="先说明多层感知机的参数结构。"),
            NoteBlockDraft(text="平移后，同一目标在不同位置会产生不同响应。"),
            NoteBlockDraft(text="因此卷积使用参数共享。"),
        ],
    )
    repaired = ensure_required_visual_blocks(
        note,
        spec,
        {
            "panel-a": {
                "group_id": "translation-panels",
                "caption": "同一目标平移后的响应对比",
                "teaching_role": "解释平移后响应为何变化",
            },
            "panel-b": {
                "group_id": "translation-panels",
                "caption": "同一目标平移后的响应对比",
                "teaching_role": "解释平移后响应为何变化",
            },
        },
    )
    image_blocks = [block for block in repaired.blocks if block.block_type == "IMAGE"]
    assert len(image_blocks) == 1
    assert image_blocks[0].asset_block_ids == ["panel-a", "panel-b"]
    assert repaired.blocks.index(image_blocks[0]) == 2


def test_explicitly_decorative_visual_may_be_omitted() -> None:
    note = NoteDraft(
        stable_key="mlp-translation",
        title="MLP缺乏平移不变性",
        thesis="位置变化会改变MLP响应。",
        blocks=[
            NoteBlockDraft(
                text="同一物体移动后，MLP的响应可能明显变化。",
                evidence_block_ids=["p1-text"],
            )
        ],
    )
    passed, failures = NoodleService._validate_plan(
        _document(),
        KnowledgePlan(document_summary="CNN", notes=[note]),
        NoteSetPlan(document_summary="CNN", notes=[_spec()]),
        {"p1-image": {"is_decorative": True, "essential": False}},
    )
    assert not failures
    assert passed == [note]


def test_formula_delimiters_and_internal_visual_id_are_normalized() -> None:
    note = NoteDraft(
        stable_key="formula",
        title="公式",
        thesis="公式",
        blocks=[
            NoteBlockDraft(
                block_type="FORMULA",
                text="$$W_{out}=W-F+1$$",
                evidence_block_ids=["p1-text"],
            )
        ],
    )
    assert normalize_note_blocks(note).blocks[0].text == "W_{out}=W-F+1"
    assert "visual_asset_id" not in public_visual_context(
        {
            "visual_asset_id": "internal-uuid",
            "external_block_id": "p1-image",
            "caption": "位置变化对比",
        }
    )


def test_code_fence_is_normalized_into_language_and_raw_code() -> None:
    note = NoteDraft(
        stable_key="code",
        title="卷积实现",
        thesis="代码块单独保存语言，不在正文中嵌套围栏。",
        blocks=[
            NoteBlockDraft(
                block_type="CODE",
                text="```python\noutput = conv2d(inputs, kernel)\n```",
                evidence_block_ids=["p1-text"],
            )
        ],
    )
    block = normalize_note_blocks(note).blocks[0]
    assert block.language == "python"
    assert block.text == "output = conv2d(inputs, kernel)"


def test_inline_latex_parentheses_are_normalized_for_markdown() -> None:
    note = NoteDraft(
        stable_key="inline-formula",
        title="卷积示例",
        thesis="行内公式使用 Markdown 数学定界符。",
        blocks=[
            NoteBlockDraft(
                block_type="EXAMPLE",
                text=r"输入 \(x=[1,2]\) 产生输出 \(z=[3,4]\)。",
                evidence_block_ids=["p1-text"],
            )
        ],
    )
    assert normalize_note_blocks(note).blocks[0].text == (
        "输入 $x=[1,2]$ 产生输出 $z=[3,4]$。"
    )


def test_deterministic_review_allows_one_note_to_cover_related_concepts() -> None:
    knowledge_map = KnowledgeMap(
        document_summary="卷积基础",
        concepts=[
            KnowledgeConcept(
                stable_key="convolution-definition",
                title="卷积定义",
                question_answered="什么是卷积？",
                core_claim="卷积核在局部窗口中计算加权和。",
                concept_type="DEFINITION",
                evidence_block_ids=["d1"],
            ),
            KnowledgeConcept(
                stable_key="convolution-purpose",
                title="卷积的作用",
                question_answered="卷积为什么适合图像？",
                core_claim="局部连接和参数共享减少参数并提取局部模式。",
                concept_type="PURPOSE",
                evidence_block_ids=["d2"],
            ),
        ],
    )
    note_set = NoteSetPlan(
        document_summary="卷积基础",
        notes=[
            NoteSpec(
                stable_key="convolution-basics",
                title="卷积是什么，以及为什么适合图像",
                question_answered="卷积如何工作，为什么用于图像？",
                target_thesis="卷积以局部连接和参数共享提取空间模式。",
                concept_keys=["convolution-definition", "convolution-purpose"],
                evidence_block_ids=["d1", "d2"],
            )
        ],
    )
    review = NoodleService._deterministic_note_set_review(knowledge_map, note_set)
    assert review.passed
    assert len(review.coverage) == 2
    assert all(item.note_keys == ["convolution-basics"] for item in review.coverage)


def test_combined_plan_contracts_are_repaired_without_llm() -> None:
    knowledge_map = KnowledgeMap(
        document_summary="CNN",
        concepts=[
            KnowledgeConcept(
                stable_key="cnn-basics",
                title="CNN basics",
                question_answered="What is a CNN?",
                core_claim="CNNs share local filters.",
                evidence_block_ids=["p1-text"],
                source_sections=["Core layers"],
            ),
            KnowledgeConcept(
                stable_key="implementation",
                title="Implementation",
                question_answered="How is it implemented?",
                core_claim="Check tensor shapes.",
                evidence_block_ids=["p1-text"],
                source_sections=["Core layers"],
            ),
        ],
    )
    outline = SourceNoteOutlinePlan(
        title="CNN",
        summary="CNN",
        nodes=[
            SourceNoteOutlineNodeSpec(stable_key="root", title="CNN", level=1),
            SourceNoteOutlineNodeSpec(
                stable_key="core", title="Core layers", level=2, parent_key="root"
            ),
            SourceNoteOutlineNodeSpec(
                stable_key="conv", title="Convolution", level=3, parent_key="core"
            ),
        ],
    )
    note_set = NoteSetPlan(
        document_summary="CNN",
        notes=[
            NoteSpec(
                stable_key="cnn-basics-note",
                title="CNN basics",
                question_answered="What is a CNN?",
                target_thesis="CNNs share local filters.",
                outline_node_key="core",
                concept_keys=["cnn-basics"],
                evidence_block_ids=["p1-text", "unknown-block"],
                source_sections=["Core layers"],
            )
        ],
    )

    repaired_outline, repaired_set, repairs = (
        NoodleService._normalize_combined_plan_contracts(
            _document(), knowledge_map, outline, note_set
        )
    )

    repaired_note = repaired_set.notes[0]
    assert repaired_note.concept_keys == ["cnn-basics", "implementation"]
    assert repaired_note.evidence_block_ids == ["p1-text"]
    assert repaired_note.outline_node_key != "core"
    leaf = next(
        node
        for node in repaired_outline.nodes
        if node.stable_key == repaired_note.outline_node_key
    )
    assert leaf.parent_key == "core"
    assert leaf.level == 3
    assert {item["type"] for item in repairs} == {
        "MERGE_UNACCOUNTED_CONCEPT",
        "CREATE_NOTE_LEAF",
    }


def test_unbound_outline_leaf_is_pruned_but_parent_branch_is_kept() -> None:
    outline = SourceNoteOutlinePlan(
        title="CNN",
        summary="卷积课程",
        nodes=[
            SourceNoteOutlineNodeSpec(stable_key="root", title="卷积", level=1),
            SourceNoteOutlineNodeSpec(
                stable_key="used",
                title="卷积基础",
                level=2,
                parent_key="root",
            ),
            SourceNoteOutlineNodeSpec(
                stable_key="empty",
                title="空章节",
                level=2,
                parent_key="root",
            ),
        ],
    )
    note_set = NoteSetPlan(
        document_summary="CNN",
        notes=[
            NoteSpec(
                stable_key="convolution",
                title="卷积基础",
                question_answered="什么是卷积？",
                target_thesis="卷积提取局部模式。",
                outline_node_key="used",
                concept_keys=["convolution"],
                evidence_block_ids=["d1"],
            )
        ],
    )
    pruned = NoodleService._prune_unbound_outline_leaves(outline, note_set)
    assert [node.stable_key for node in pruned.nodes] == ["root", "used"]


def test_missing_required_visual_is_reconciled_into_note_blocks() -> None:
    note = NoteDraft(
        stable_key="mlp-translation",
        title="MLP缺乏平移不变性",
        thesis="位置变化会改变MLP响应。",
        blocks=[
            NoteBlockDraft(
                text="同一物体移动后，MLP的响应可能变化。",
                evidence_block_ids=["p1-text"],
            )
        ],
    )
    reconciled = ensure_required_visual_blocks(
        note,
        _spec(),
        {
            "p1-image": {
                "is_decorative": False,
                "caption": "同一物体位于不同位置时产生不同响应",
                "teaching_role": "解释位置变化",
            }
        },
    )
    images = [block for block in reconciled.blocks if block.block_type == "IMAGE"]
    assert len(images) == 1
    assert images[0].asset_block_ids == ["p1-image"]
    assert "不同响应" in images[0].caption


def test_history_is_split_from_definition_using_knowledge_map_types() -> None:
    note_set = NoteSetPlan(
        document_summary="虹膜识别",
        notes=[
            NoteSpec(
                stable_key="iris-definition-history",
                title="虹膜识别定义与历史",
                question_answered="什么是虹膜识别？",
                target_thesis="虹膜识别定义与历史。",
                concept_keys=["iris-definition", "iris-history"],
                evidence_block_ids=["d1", "h1"],
            )
        ],
    )
    knowledge_map = KnowledgeMap(
        document_summary="虹膜识别",
        concepts=[
            KnowledgeConcept(
                stable_key="iris-definition",
                title="虹膜识别定义",
                question_answered="什么是虹膜识别？",
                core_claim="虹膜识别测量虹膜图案。",
                concept_type="DEFINITION",
                evidence_block_ids=["d1"],
            ),
            KnowledgeConcept(
                stable_key="iris-history",
                title="虹膜识别发展历史",
                question_answered="虹膜识别何时提出？",
                core_claim="虹膜识别概念于1939年提出。",
                concept_type="HISTORY",
                evidence_block_ids=["h1"],
            ),
        ],
    )
    normalized, changes = normalize_note_set_boundaries(note_set, knowledge_map)
    assert len(normalized.notes) == 2
    assert normalized.notes[0].concept_keys == ["iris-definition"]
    assert normalized.notes[1].concept_keys == ["iris-history"]
    assert normalized.notes[1].evidence_block_ids == ["h1"]
    assert changes["iris-definition-history"] == [
        "iris-definition-history",
        "iris-definition-history-history",
    ]


def test_text_misclassified_as_visual_is_reclassified_as_evidence() -> None:
    document = _document().model_copy(deep=True)
    document.blocks.append(
        ParsedBlock(
            block_id="p1-overlay-text",
            block_type="TEXT",
            page_index=0,
            page_label="1",
            text="Extraction of face features",
        )
    )
    spec = _spec().model_copy(deep=True)
    spec.asset_block_ids.append("p1-overlay-text")
    normalized, changes = normalize_note_set_assets(
        document,
        NoteSetPlan(document_summary="CNN", notes=[spec]),
    )
    normalized_spec = normalized.notes[0]
    assert normalized_spec.asset_block_ids == ["p1-image"]
    assert "p1-overlay-text" in normalized_spec.evidence_block_ids
    assert changes[spec.stable_key]["reclassified_as_evidence"] == ["p1-overlay-text"]


def test_structured_table_asset_is_rendered_as_evidence_not_duplicate_image() -> None:
    document = _document().model_copy(deep=True)
    document.blocks.append(
        ParsedBlock(
            block_id="p1-table",
            block_type="TABLE",
            page_index=0,
            page_label="1",
            text="<table><tr><td>Layer</td><td>Output</td></tr></table>",
            asset_path="table-screenshot.png",
        )
    )
    spec = _spec().model_copy(deep=True)
    spec.asset_block_ids.append("p1-table")
    normalized, changes = normalize_note_set_assets(
        document,
        NoteSetPlan(document_summary="CNN", notes=[spec]),
    )
    normalized_spec = normalized.notes[0]
    assert "p1-table" not in normalized_spec.asset_block_ids
    assert "p1-table" in normalized_spec.evidence_block_ids
    assert changes[spec.stable_key]["reclassified_as_evidence"] == ["p1-table"]


def test_non_visual_asset_reference_fails_deterministic_validation() -> None:
    note = NoteDraft(
        stable_key="mlp-translation",
        title="MLP 缺乏平移不变性",
        thesis="位置变化会改变 MLP 响应。",
        blocks=[
            NoteBlockDraft(
                block_type="IMAGE",
                caption="错误地把文本当作图片",
                evidence_block_ids=["p1-text"],
                asset_block_ids=["p1-text"],
            )
        ],
    )
    _, failures = NoodleService._validate_plan(
        _document(), KnowledgePlan(document_summary="CNN", notes=[note])
    )
    assert "NON_VISUAL_ASSET:p1-text" in failures[0]["detail"]


def test_exact_prose_dedup_preserves_evidence_and_distinct_content():
    note = NoteDraft(stable_key="swin", title="Swin", thesis="Architecture", blocks=[
        NoteBlockDraft(text="Same caption.", evidence_block_ids=["p1"]),
        NoteBlockDraft(text="Other explanation.", evidence_block_ids=["p2"]),
        NoteBlockDraft(text="Same caption.", evidence_block_ids=["p3"]),
        NoteBlockDraft(block_type="FORMULA", text="x + y"),
        NoteBlockDraft(block_type="FORMULA", text="x + y"),
    ])
    result = normalize_note_blocks(note)
    assert len(result.blocks) == 4
    assert result.blocks[0].evidence_block_ids == ["p1", "p3"]
    assert len(note.blocks) == 5
