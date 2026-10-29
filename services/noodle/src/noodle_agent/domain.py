from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ParsedBlock(BaseModel):
    block_id: str
    block_type: str = "TEXT"
    text: str = ""
    page_index: int
    page_label: str
    level: int | None = None
    bbox: list[float] | None = None
    asset_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    title: str
    page_count: int
    markdown: str
    blocks: list[ParsedBlock]
    raw_artifact_path: str | None = None
    parser_name: str
    parser_version: str | None = None


class VisualUnderstanding(BaseModel):
    visual_asset_id: str = ""
    external_block_id: str
    image_sha256: str
    model: str
    prompt_version: str = "visual-understanding-v2"
    asset_type: str = "OTHER"
    is_decorative: bool = False
    essential: bool = False
    caption: str = ""
    teaching_role: str = ""
    ocr_text: list[str] = Field(default_factory=list)
    factual_observations: list[str] = Field(default_factory=list)
    spatial_relations: list[str] = Field(default_factory=list)
    suggested_placement: str = ""
    confidence: float = 0.0
    analysis_tier: str = "API_FAST"
    route_reason: str = ""
    is_fragment: bool = False
    fragment_group: str = ""
    group_id: str = ""
    group_type: str = "SINGLE"
    related_asset_ids: list[str] = Field(default_factory=list)
