from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..composition import Component
from ..db import (
    AtomicNote,
    NoteVersion,
    Source,
    SourceNote,
    SourceNoteVersion,
    SourceVersion,
)
from .common import (
    _safe_filename,
    _write_text_atomic,
)


class ExportingModule(Component):
    def export_markdown(self, source_version_id: str | None = None) -> list[dict[str, Any]]:
        with self.owner.db.session() as session:
            query = (
                select(SourceVersion.id)
                .join(AtomicNote, AtomicNote.source_version_id == SourceVersion.id)
                .where(
                    AtomicNote.visibility == "ACTIVE",
                    AtomicNote.active_version_id.is_not(None),
                )
                .distinct()
            )
            if source_version_id:
                query = query.where(SourceVersion.id == source_version_id)
            version_ids = list(session.scalars(query).all())
        if source_version_id and not version_ids:
            raise ValueError(f"No active notes for source_version_id: {source_version_id}")
        return [self.owner._export_source_markdown(version_id) for version_id in version_ids]

    def _export_source_markdown(self, source_version_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            source_version = session.get(SourceVersion, source_version_id)
            if source_version is None:
                raise ValueError(f"Unknown source_version_id: {source_version_id}")
            source = session.get(Source, source_version.source_id)
            rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(
                    AtomicNote.source_version_id == source_version_id,
                    AtomicNote.visibility == "ACTIVE",
                )
                .order_by(NoteVersion.title)
            ).all()
            note_ids = [note.id for note, _version in rows]
            release_version_id = session.scalar(
                select(SourceNoteVersion.id)
                .join(SourceNote, SourceNote.id == SourceNoteVersion.source_note_id)
                .where(
                    SourceNote.source_id == source.id,
                    SourceNote.active_version_id == SourceNoteVersion.id,
                )
            )

        directory_name = (
            f"{_safe_filename(source.title, fallback='source')}--{source_version_id[:8]}"
            f"--{(release_version_id or 'atomic')[:8]}"
        )
        destination = self.owner.settings.export_dir / directory_name
        assets_dir = destination / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        index_links: list[str] = []
        exported_files: list[str] = []

        def render_asset(asset: dict[str, Any], fallback_title: str) -> list[str]:
            source_asset = self.owner.blobs.path_for(asset["storage_key"])
            asset_name = Path(asset["storage_key"]).name
            target_asset = assets_dir / asset_name
            if not target_asset.exists():
                shutil.copy2(source_asset, target_asset)
            alt = str(asset.get("alt_text") or asset.get("caption") or fallback_title)
            alt = " ".join(re.sub(r"<[^>]+>", " ", alt).split())
            if not alt or len(alt) > 160 or re.fullmatch(r"(?:0\s*)+", alt):
                alt = fallback_title
            alt = alt.replace("[", "(").replace("]", ")")
            return [
                f"![{alt}](assets/{asset_name})",
                f"*来源页：{asset.get('page_label', '')}*",
                "",
            ]

        def render_block(
            block: dict[str, Any],
            fallback_title: str,
            rendered_assets: set[str] | None = None,
        ) -> list[str]:
            kind = str(block.get("type") or "PARAGRAPH").upper()
            lines: list[str] = []
            if kind == "BULLETED_LIST":
                lines.extend(f"- {item}" for item in block.get("items", []) if str(item).strip())
            elif kind == "NUMBERED_LIST":
                lines.extend(
                    f"{index}. {item}"
                    for index, item in enumerate(block.get("items", []), start=1)
                    if str(item).strip()
                )
            elif kind == "TABLE":

                def table_cell(value: Any) -> str:
                    # A LaTeX norm uses ``\|`` while Markdown tables also use
                    # pipe as a cell delimiter. Convert the math command to an
                    # equivalent pipe-free LaTeX command before escaping any
                    # remaining literal table pipes.
                    cell = str(value).replace(r"\|", r"\Vert{}")
                    return cell.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>")

                headers = [table_cell(item) for item in block.get("table_headers", [])]
                rows = block.get("table_rows", [])
                if headers:
                    lines.extend(
                        [
                            "| " + " | ".join(headers) + " |",
                            "| " + " | ".join("---" for _ in headers) + " |",
                        ]
                    )
                    lines.extend(
                        "| " + " | ".join(table_cell(item) for item in row) + " |" for row in rows
                    )
            elif kind == "FORMULA":
                if block.get("text"):
                    lines.extend(["$$", str(block["text"]).strip(), "$$"])
            elif kind == "CODE":
                if block.get("text"):
                    language = re.sub(r"[^A-Za-z0-9_+.-]", "", str(block.get("language") or ""))
                    lines.extend([f"```{language}", str(block["text"]).strip(), "```"])
            elif kind == "CALLOUT":
                lines.extend(
                    f"> {line}" for line in str(block.get("text") or "").splitlines() if line
                )
            elif kind != "IMAGE" and block.get("text"):
                lines.append(str(block["text"]).strip())
            if lines:
                lines.append("")
            for asset in block.get("assets", []):
                asset_key = str(asset.get("storage_key") or "")
                if rendered_assets is not None and asset_key in rendered_assets:
                    continue
                lines.extend(render_asset(asset, fallback_title))
                if rendered_assets is not None and asset_key:
                    rendered_assets.add(asset_key)
            return lines

        for order, note_id in enumerate(note_ids, start=1):
            note = self.owner.get_note(note_id)
            filename = f"{order:02d}-{_safe_filename(note['title'])}--{note['version_id'][:8]}.md"
            evidence_pages = list(
                dict.fromkeys(
                    str(item["page_label"])
                    for item in note["evidence"]
                    if item.get("page_label") is not None
                )
            )
            frontmatter = [
                "---",
                f"note_id: {json.dumps(note['note_id'], ensure_ascii=False)}",
                f"version_id: {json.dumps(note['version_id'], ensure_ascii=False)}",
                f"source_id: {json.dumps(source.id, ensure_ascii=False)}",
                f"source_version_id: {json.dumps(source_version.id, ensure_ascii=False)}",
                f"source_title: {json.dumps(source.title, ensure_ascii=False)}",
                f"source_file: {json.dumps(source_version.original_name, ensure_ascii=False)}",
                f"quality_status: {json.dumps(note['quality'], ensure_ascii=False)}",
                f"review_status: {json.dumps(note['review'], ensure_ascii=False)}",
                f"tags: {json.dumps(note['tags'], ensure_ascii=False)}",
                f"evidence_pages: {json.dumps(evidence_pages, ensure_ascii=False)}",
                "---",
            ]
            body = [*frontmatter, "", f"# {note['title']}", ""]
            rendered_note_assets: set[str] = set()
            for block in note["blocks"]:
                body.extend(render_block(block, note["title"], rendered_note_assets))

            remaining_note_assets = [
                asset
                for asset in note["assets"]
                if str(asset.get("storage_key") or "") not in rendered_note_assets
            ]
            if remaining_note_assets:
                body.extend(["", "## 相关图片", ""])
                grouped_assets: dict[str, dict[str, Any]] = {}
                for asset in remaining_note_assets:
                    group = grouped_assets.setdefault(
                        asset["storage_key"], {"asset": asset, "pages": []}
                    )
                    if asset.get("page_label") is not None:
                        group["pages"].append(str(asset["page_label"]))
                for group in grouped_assets.values():
                    asset = group["asset"]
                    rendered = render_asset(asset, note["title"])
                    pages = "、".join(dict.fromkeys(group["pages"]))
                    if pages:
                        rendered[-2] = f"*来源页：{pages}*"
                    body.extend(rendered)

            if note["evidence"]:
                body.extend(["", "## 来源与证据", ""])
                for evidence in note["evidence"]:
                    quote = " ".join(str(evidence.get("quote") or "").split())
                    body.append(f"- 第 {evidence['page_label']} 页：{quote or '图片或结构化证据'}")

            file_path = destination / filename
            _write_text_atomic(file_path, "\n".join(body).rstrip() + "\n")
            index_links.append(f"- [{note['title']}]({filename.replace(' ', '%20')})")
            exported_files.append(str(file_path))

        with self.owner.db.session() as session:
            source_note_id = session.scalar(
                select(SourceNote.id).where(SourceNote.source_id == source.id)
            )
        source_note_path: Path | None = None
        if source_note_id:
            source_note = self.owner.get_source_note(source_note_id)
            source_note_lines = [
                "---",
                f"source_note_id: {json.dumps(source_note['source_note_id'], ensure_ascii=False)}",
                f"version_id: {json.dumps(source_note['version_id'], ensure_ascii=False)}",
                f"source_id: {json.dumps(source.id, ensure_ascii=False)}",
                f"source_version_id: {json.dumps(source_version.id, ensure_ascii=False)}",
                f"unit_count: {source_note['unit_count']}",
                "---",
                "",
                f"# {source_note['title']}",
                "",
                f"> {source_note['summary']}",
                "",
            ]
            rendered_source_assets: set[str] = set()

            def render_outline(
                nodes: list[dict[str, Any]],
                *,
                level_offset: int = 0,
                number_prefix: tuple[int, ...] = (),
            ) -> None:
                for node_index, node in enumerate(nodes, start=1):
                    node_number = (*number_prefix, node_index)
                    number_path = ".".join(str(value) for value in node_number)
                    number_label = f"{number_path}." if len(node_number) == 1 else number_path
                    heading_level = min(6, node["level"] + 1 - level_offset)
                    source_note_lines.extend(
                        [f"{'#' * heading_level} {number_label} {node['title']}", ""]
                    )
                    notes = node.get("notes", [])
                    for note_index, note in enumerate(notes, start=1):
                        # A leaf with one atomic unit reads naturally as one
                        # section. Multiple units retain numbered subheadings.
                        same_heading = (
                            str(note["title"]).strip().casefold()
                            == str(node["title"]).strip().casefold()
                        )
                        if len(notes) > 1 and not same_heading:
                            note_heading = min(6, heading_level + 1)
                            note_number = f"{number_path}.{note_index}"
                            source_note_lines.extend(
                                [
                                    f"{'#' * note_heading} {note_number} {note['title']}",
                                    "",
                                ]
                            )
                        for block in note["blocks"]:
                            source_note_lines.extend(
                                render_block(
                                    block,
                                    note["title"],
                                    rendered_source_assets,
                                )
                            )
                        for asset in note.get("assets", []):
                            asset_key = str(asset.get("storage_key") or "")
                            if asset_key in rendered_source_assets:
                                continue
                            source_note_lines.extend(render_asset(asset, note["title"]))
                            if asset_key:
                                rendered_source_assets.add(asset_key)
                    render_outline(
                        node.get("children", []),
                        level_offset=level_offset,
                        number_prefix=node_number,
                    )

            rendered_outline = source_note["outline"]
            level_offset = 0
            if (
                len(rendered_outline) == 1
                and rendered_outline[0]["title"].strip().casefold()
                == source_note["title"].strip().casefold()
                and not rendered_outline[0].get("notes")
            ):
                rendered_outline = rendered_outline[0].get("children", [])
                level_offset = 1
            render_outline(rendered_outline, level_offset=level_offset)
            source_note_path = destination / "source-note.md"
            _write_text_atomic(
                source_note_path,
                "\n".join(source_note_lines).rstrip() + "\n",
            )

        index = [
            f"# {source.title}",
            "",
            f"- 原文件：`{source_version.original_name}`",
            f"- 来源版本：`{source_version.id}`",
            f"- 原子笔记：{len(exported_files)} 篇",
            "",
            "## 完整笔记",
            "",
            "- [按学习大纲组织的完整笔记](source-note.md)" if source_note_path else "- 尚未生成",
            "",
            "## 笔记目录",
            "",
            *index_links,
            "",
        ]
        _write_text_atomic(destination / "index.md", "\n".join(index))
        return {
            "source_version_id": source_version_id,
            "directory": str(destination),
            "index": str(destination / "index.md"),
            "note_count": len(exported_files),
            "source_note": str(source_note_path) if source_note_path else None,
            "files": exported_files,
        }
