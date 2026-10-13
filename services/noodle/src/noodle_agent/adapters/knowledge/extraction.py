from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...composition import Component
from ...domain import (
    DocumentProfile,
    KnowledgeConcept,
    KnowledgeConceptCandidate,
    KnowledgeMap,
    KnowledgePlan,
    KnowledgeWindow,
    NoteDraft,
    ParsedBlock,
    ParsedDocument,
    TopicSegment,
)
from .common import (
    _block_catalog,
    _prompt,
    _sections,
    _slug,
)


class ExtractionModule(Component):
    def build_plan(self, document: ParsedDocument) -> KnowledgePlan:
        local_candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for section_title, blocks in _sections(document):
            evidence = [
                {
                    "block_id": block.block_id,
                    "type": block.block_type,
                    "page": block.page_label,
                    "text": block.text,
                    "asset_available": bool(block.asset_path),
                }
                for block in blocks
                if block.text or block.asset_path
            ]
            if not evidence:
                skipped.append({"section": section_title, "reason": "LOW_INFORMATION"})
                continue
            result = self.owner._json_call(
                system=(
                    "你是严谨的课程助教。分析局部章节，提取可独立查询的知识候选，不机械复述章节。"
                    f"{self.owner._language_instruction}"
                    "只能引用输入中的 block_id。输出 JSON："
                    '{"candidates":[{"title":"","thesis":"","body":"","evidence_block_ids":[],'
                    '"asset_block_ids":[],"tags":[]}],'
                    '"skip_reason":null}。知识不足时 candidates 为空并写 skip_reason。'
                ),
                user={"document": document.title, "section": section_title, "blocks": evidence},
            )
            candidates = result.get("candidates") or []
            if not candidates:
                skipped.append(
                    {
                        "section": section_title,
                        "reason": result.get("skip_reason") or "LOW_INFORMATION",
                    }
                )
            for candidate in candidates:
                candidate["source_section"] = section_title
                local_candidates.append(candidate)

        result = self.owner._json_call(
            system=(
                "你是整门课程的优秀助教。对局部知识候选进行全局合并、拆分、去重，"
                "生成紧凑但可独立理解的原子笔记。章节只是结构先验，不是笔记边界。"
                f"{self.owner._language_instruction}"
                "不得引入候选外的事实，不得虚构 evidence_block_ids 或 asset_block_ids。输出 JSON："
                '{"document_summary":"","notes":[{"stable_key":"","title":"","thesis":"","'
                '"blocks":[{"block_type":"PARAGRAPH","text":"","evidence_block_ids":[],'
                '"asset_block_ids":[]}],'
                '"tags":[],"source_sections":[]}]}。'
            ),
            user={"document": document.title, "candidates": local_candidates},
            model=self.owner.pro_model,
        )
        notes = []
        for index, raw in enumerate(result.get("notes") or []):
            raw.setdefault("stable_key", _slug(str(raw.get("title") or f"note-{index}")))
            notes.append(NoteDraft.model_validate(raw))
        return KnowledgePlan(
            document_summary=str(result.get("document_summary") or document.title),
            notes=notes,
            skipped_sections=skipped,
        )

    def profile_document(self, document: ParsedDocument) -> DocumentProfile:
        pages: dict[int, dict[str, Any]] = {
            index: {"page_index": index, "headings": [], "excerpt": [], "asset_count": 0}
            for index in range(document.page_count)
        }
        for block in document.blocks:
            page = pages.setdefault(
                block.page_index,
                {
                    "page_index": block.page_index,
                    "headings": [],
                    "excerpt": [],
                    "asset_count": 0,
                },
            )
            if block.block_type in {"TITLE", "HEADING"} and block.text:
                page["headings"].append(block.text[:200])
            elif block.text and sum(len(item) for item in page["excerpt"]) < 240:
                page["excerpt"].append(block.text[:160])
            if block.asset_path:
                page["asset_count"] += 1

        result = self.owner._json_call(
            system=f"{_prompt('document_profiler.v2.md')}\n{self.owner._language_instruction}",
            user={
                "document": document.title,
                "page_count": document.page_count,
                "pages": list(pages.values()),
            },
            model=self.owner.fast_model,
            operation="profile_document_v2",
            prompt_version="document-profiler-v2",
        )
        raw_segments = result.get("segments") or []
        segments: list[TopicSegment] = []
        cursor = 0
        for index, raw in enumerate(
            sorted(raw_segments, key=lambda item: int(item.get("start_page_index", 0)))
        ):
            if cursor >= document.page_count:
                break
            end = min(
                document.page_count - 1,
                max(cursor, int(raw.get("end_page_index", cursor))),
            )
            title = str(raw.get("title") or f"主题区段 {index + 1}").strip()
            segments.append(
                TopicSegment(
                    stable_key=str(raw.get("stable_key") or _slug(title)),
                    title=title,
                    start_page_index=cursor,
                    end_page_index=end,
                    rationale=str(raw.get("rationale") or ""),
                )
            )
            cursor = end + 1
        if not segments:
            segments = [
                TopicSegment(
                    stable_key=_slug(document.title),
                    title=document.title,
                    start_page_index=0,
                    end_page_index=max(0, document.page_count - 1),
                    rationale="文档画像未返回有效区段，使用完整文档回退区段。",
                )
            ]
        elif cursor < document.page_count:
            segments[-1].end_page_index = document.page_count - 1
        return DocumentProfile(
            document_type=str(result.get("document_type") or "COURSE_MATERIAL"),
            document_summary=document.title,
            learning_goal="",
            segments=segments,
        )

    @staticmethod
    def _blocks_for_segment(document: ParsedDocument, segment: TopicSegment) -> list[ParsedBlock]:
        return [
            block
            for block in document.blocks
            if segment.start_page_index <= block.page_index <= segment.end_page_index
            and block.block_type not in {"HEADER", "FOOTER", "PAGE_NUMBER"}
        ]

    @staticmethod
    def _extraction_windows(
        document: ParsedDocument,
        segment: TopicSegment,
        *,
        character_budget: int = 24_000,
    ) -> list[tuple[str, list[ParsedBlock]]]:
        """Bound model inputs without turning transport windows into note boundaries."""
        blocks = ExtractionModule._blocks_for_segment(document, segment)
        pages: dict[int, list[ParsedBlock]] = defaultdict(list)
        for block in blocks:
            pages[block.page_index].append(block)
        windows: list[tuple[str, list[ParsedBlock]]] = []
        current: list[ParsedBlock] = []
        current_chars = 0
        for page_index in sorted(pages):
            page_blocks = pages[page_index]
            page_chars = sum(min(len(block.text), 3_000) + 120 for block in page_blocks)
            if current and current_chars + page_chars > character_budget:
                windows.append((segment.title, current))
                current = []
                current_chars = 0
            current.extend(page_blocks)
            current_chars += page_chars
        if current:
            windows.append((segment.title, current))
        return windows or [(segment.title, blocks)]

    def knowledge_windows(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> list[KnowledgeWindow]:
        result: list[KnowledgeWindow] = []
        for segment in profile.segments:
            windows = self.owner._extraction_windows(document, segment)
            for window_index, (_title, blocks) in enumerate(windows, start=1):
                result.append(
                    KnowledgeWindow(
                        stable_key=f"{segment.stable_key}--{window_index}",
                        segment_key=segment.stable_key,
                        segment_title=segment.title,
                        window_index=window_index,
                        window_count=len(windows),
                        block_ids=[block.block_id for block in blocks],
                    )
                )
        return result

    def extract_knowledge_window(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        window: KnowledgeWindow,
    ) -> tuple[list[KnowledgeConceptCandidate], str | None]:
        wanted = set(window.block_ids)
        evidence = _block_catalog(
            [block for block in document.blocks if block.block_id in wanted],
            max_text_chars=3_000,
        )
        if not evidence:
            return [], "LOW_INFORMATION"
        result = self.owner._json_call(
            system=f"{_prompt('candidate_extractor.v2.md')}\n{self.owner._language_instruction}",
            user={
                "document": document.title,
                "segment": window.model_dump(),
                "blocks": evidence,
            },
            model=self.owner.fast_model,
            operation="extract_source_candidates_v2",
            prompt_version="candidate-extractor-v2",
        )
        concepts: list[KnowledgeConceptCandidate] = []
        allowed_ids = set(window.block_ids)
        for index, raw in enumerate(result.get("candidates") or []):
            title = str(raw.get("title") or f"{window.segment_title} 概念 {index + 1}")
            evidence_ids = list(
                dict.fromkeys(str(item) for item in raw.get("evidence_block_ids") or [])
            )
            asset_ids = list(dict.fromkeys(str(item) for item in raw.get("asset_ids") or []))
            invalid_ids = (set(evidence_ids) | set(asset_ids)) - allowed_ids
            if invalid_ids:
                raise RuntimeError(
                    f"Candidate extractor returned ids outside {window.stable_key}: "
                    + ",".join(sorted(invalid_ids))
                )
            if not evidence_ids:
                continue
            concepts.append(
                KnowledgeConceptCandidate(
                    stable_key=f"{window.stable_key}--c{index + 1}",
                    title=title,
                    question_answered=title,
                    core_claim=str(raw.get("claim") or title),
                    concept_type="CONCEPT",
                    importance="SUPPORTING",
                    evidence_block_ids=evidence_ids,
                    asset_block_ids=asset_ids,
                    source_sections=[window.segment_title],
                )
            )
        return concepts, result.get("skip_reason")

    def merge_knowledge_map(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        local_concepts: list[KnowledgeConceptCandidate],
        skipped: list[dict[str, str]],
    ) -> KnowledgeMap:
        concepts = [
            KnowledgeConcept(
                stable_key=item.stable_key,
                title=item.title,
                question_answered=item.title,
                core_claim=item.core_claim,
                concept_type="CONCEPT",
                importance="SUPPORTING",
                evidence_block_ids=list(dict.fromkeys(item.evidence_block_ids)),
                asset_block_ids=list(dict.fromkeys(item.asset_block_ids)),
                source_sections=list(dict.fromkeys(item.source_sections)),
            )
            for item in local_concepts
        ]
        return KnowledgeMap(
            document_summary=document.title,
            concepts=concepts,
            skipped_sections=skipped,
        )

    def build_knowledge_map(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> KnowledgeMap:
        local_concepts: list[KnowledgeConceptCandidate] = []
        skipped: list[dict[str, str]] = []
        for window in self.owner.knowledge_windows(document, profile):
            concepts, skip_reason = self.owner.extract_knowledge_window(document, profile, window)
            local_concepts.extend(concepts)
            if not concepts:
                skipped.append(
                    {
                        "section": window.segment_title,
                        "reason": str(skip_reason or "LOW_INFORMATION"),
                    }
                )
        return self.owner.merge_knowledge_map(document, profile, local_concepts, skipped)
