from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import httpx

from ..config import Settings
from ..domain import ParsedBlock, ParsedDocument


class MinerUError(RuntimeError):
    pass


def validate_pdf(path: Path) -> int | None:
    if path.suffix.lower() != ".pdf":
        raise ValueError("Only PDF input is supported")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("Input does not have a PDF signature")
    try:
        import pymupdf as fitz
    except ImportError:
        return None
    with fitz.open(path) as document:
        if document.needs_pass:
            raise ValueError("Encrypted PDF is not supported by the MVP")
        if document.page_count < 1:
            raise ValueError("PDF contains no pages")
        for page_index in {0, document.page_count - 1}:
            page = document.load_page(page_index)
            if page.rect.width <= 0 or page.rect.height <= 0:
                raise ValueError(f"PDF page {page_index + 1} has an invalid size")
        return document.page_count


class FixtureParser:
    """Offline adapter for tests and demonstrations.

    A real-looking PDF is accompanied by <name>.mineru.json. The fixture mirrors
    normalized MinerU page/block output and therefore exercises the complete
    downstream workflow without network calls.
    """

    name = "fixture"

    def submit(self, pdf_path: Path, data_id: str) -> dict:
        validate_pdf(pdf_path)
        sidecar = pdf_path.with_suffix(".mineru.json")
        if not sidecar.exists():
            raise MinerUError(
                f"Fixture mode needs a sidecar file: {sidecar}. "
                "Set NOODLE_PARSER=mineru to call the real API."
            )
        return {
            "external_id": f"fixture:{data_id}:{sidecar.resolve()}",
            "state": "done",
            "result": {"sidecar": str(sidecar.resolve())},
        }

    def poll(self, external_id: str) -> dict:
        sidecar = external_id.split(":", 2)[-1]
        return {"state": "done", "sidecar": sidecar}

    def download_and_normalize(
        self, external_id: str, result: dict, destination: Path, title: str
    ) -> ParsedDocument:
        del external_id
        source = Path(result["sidecar"])
        destination.mkdir(parents=True, exist_ok=True)
        copied = destination / "fixture.mineru.json"
        shutil.copyfile(source, copied)
        payload = json.loads(source.read_text(encoding="utf-8"))
        blocks: list[ParsedBlock] = []
        markdown_parts: list[str] = []
        pages = payload.get("pages", [])
        for page_index, page in enumerate(pages):
            actual_index = int(page.get("page_index", page_index))
            label = str(page.get("page_label", actual_index + 1))
            for order, raw in enumerate(page.get("blocks", [])):
                block_id = str(raw.get("id") or f"p{actual_index}-b{order}")
                text = str(raw.get("text") or "")
                block_type = str(raw.get("type") or "TEXT").upper()
                level = raw.get("level")
                blocks.append(
                    ParsedBlock(
                        block_id=block_id,
                        block_type=block_type,
                        text=text,
                        page_index=actual_index,
                        page_label=label,
                        level=int(level) if level is not None else None,
                        bbox=raw.get("bbox"),
                        asset_path=(
                            str((source.parent / raw["asset_path"]).resolve())
                            if raw.get("asset_path")
                            else None
                        ),
                        metadata=raw.get("metadata") or {},
                    )
                )
                if text:
                    prefix = (
                        "#" * max(1, min(int(level or 1), 6)) + " "
                        if block_type
                        in {
                            "TITLE",
                            "HEADING",
                        }
                        else ""
                    )
                    markdown_parts.append(prefix + text)
        return ParsedDocument(
            title=str(payload.get("title") or title),
            page_count=max(len(pages), max((b.page_index for b in blocks), default=-1) + 1),
            markdown="\n\n".join(markdown_parts),
            blocks=blocks,
            raw_artifact_path=str(copied),
            parser_name=self.name,
            parser_version=str(payload.get("parser_version") or "fixture-v1"),
        )


class MinerUParser:
    name = "mineru"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = "https://mineru.net"
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, read=120.0), follow_redirects=True)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.mineru_api_token:
            headers["Authorization"] = f"Bearer {self.settings.mineru_api_token}"
        return headers

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict:
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise MinerUError(f"MinerU error {payload.get('code')}: {payload.get('msg')}")
        return payload.get("data") or {}

    def submit(self, pdf_path: Path, data_id: str) -> dict:
        page_count = validate_pdf(pdf_path)
        if self.settings.mineru_mode == "lightweight":
            if pdf_path.stat().st_size > 10 * 1024 * 1024:
                raise MinerUError("MinerU lightweight mode accepts files up to 10 MB")
            if page_count is not None and page_count > 20:
                raise MinerUError("MinerU lightweight mode accepts PDFs up to 20 pages")
            request = {
                "file_name": pdf_path.name,
                "language": self.settings.mineru_language,
                "enable_table": True,
                "enable_formula": True,
            }
            data = self._unwrap(
                self.client.post(
                    f"{self.base_url}/api/v1/agent/parse/file",
                    json=request,
                    headers=self._headers(),
                )
            )
            with pdf_path.open("rb") as stream:
                upload = self.client.put(data["file_url"], content=stream)
                upload.raise_for_status()
            return {"external_id": data["task_id"], "state": "pending", "result": {}}

        if not self.settings.mineru_api_token:
            raise MinerUError("MINERU_API_TOKEN is required for precision mode")
        request = {
            "files": [{"name": pdf_path.name, "data_id": data_id}],
            "model_version": self.settings.mineru_model_version,
            "language": self.settings.mineru_language,
            "enable_table": True,
            "enable_formula": True,
        }
        data = self._unwrap(
            self.client.post(
                f"{self.base_url}/api/v4/file-urls/batch",
                json=request,
                headers=self._headers(),
            )
        )
        upload_url = data["file_urls"][0]
        with pdf_path.open("rb") as stream:
            upload = self.client.put(upload_url, content=stream)
            upload.raise_for_status()
        return {"external_id": data["batch_id"], "state": "pending", "result": {}}

    def poll(self, external_id: str) -> dict:
        if self.settings.mineru_mode == "lightweight":
            data = self._unwrap(
                self.client.get(
                    f"{self.base_url}/api/v1/agent/parse/{external_id}",
                    headers=self._headers(),
                )
            )
            return data

        data = self._unwrap(
            self.client.get(
                f"{self.base_url}/api/v4/extract-results/batch/{external_id}",
                headers=self._headers(),
            )
        )
        candidates = (
            data.get("extract_result")
            or data.get("extract_results")
            or data.get("results")
            or data.get("files")
            or []
        )
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not candidates:
            return {"state": data.get("state", "pending")}
        return candidates[0]

    def download_and_normalize(
        self, external_id: str, result: dict, destination: Path, title: str
    ) -> ParsedDocument:
        del external_id
        destination.mkdir(parents=True, exist_ok=True)
        if self.settings.mineru_mode == "lightweight":
            url = result.get("markdown_url")
            if not url:
                raise MinerUError("Completed MinerU result has no markdown_url")
            markdown = self.client.get(url).raise_for_status().text
            markdown_path = destination / "full.md"
            markdown_path.write_text(markdown, encoding="utf-8")
            return self._normalize_markdown_only(markdown, markdown_path, title)

        url = result.get("full_zip_url")
        if not url:
            raise MinerUError("Completed MinerU result has no full_zip_url")
        zip_path = destination / "mineru-result.zip"
        with self.client.stream("GET", url) as response:
            response.raise_for_status()
            with zip_path.open("wb") as stream:
                for chunk in response.iter_bytes():
                    stream.write(chunk)
        extract_dir = destination / "mineru-result"
        self._safe_extract(zip_path, extract_dir)
        return self._normalize_precision(extract_dir, zip_path, title)

    @staticmethod
    def _safe_extract(archive_path: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if root != target and root not in target.parents:
                    raise MinerUError(f"Unsafe path in MinerU archive: {member.filename}")
            archive.extractall(destination)

    def _normalize_precision(self, root: Path, archive_path: Path, title: str) -> ParsedDocument:
        json_files = list(root.rglob("*content_list*.json"))
        markdown_files = list(root.rglob("*.md"))
        if not json_files:
            if not markdown_files:
                raise MinerUError("MinerU archive contains neither content_list JSON nor Markdown")
            markdown = markdown_files[0].read_text(encoding="utf-8", errors="replace")
            return self._normalize_markdown_only(markdown, archive_path, title)
        payload = json.loads(json_files[0].read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else payload.get("content_list", [])
        blocks: list[ParsedBlock] = []
        markdown_parts: list[str] = []
        for order, raw in enumerate(items):
            page_index = int(raw.get("page_idx", raw.get("page_index", 0)))
            block_type = str(raw.get("type", raw.get("block_type", "TEXT"))).upper()
            level = raw.get("level")
            if level is None:
                level = raw.get("text_level")
            if block_type == "TEXT" and level is not None:
                block_type = "TITLE" if int(level) == 1 else "HEADING"
            text_value = raw.get("text") or raw.get("content") or raw.get("table_body") or ""
            if block_type in {"IMAGE", "CHART"} and not text_value:
                labels: list[str] = []
                for field in ("image_caption", "image_footnote"):
                    value = raw.get(field)
                    if isinstance(value, list):
                        labels.extend(str(item) for item in value if item)
                    elif value:
                        labels.append(str(value))
                text_value = " | ".join(dict.fromkeys(labels))
            if isinstance(text_value, (dict, list)):
                text_value = json.dumps(text_value, ensure_ascii=False)
            text_value = str(text_value)
            block_id = str(raw.get("id") or f"p{page_index}-b{order}")
            blocks.append(
                ParsedBlock(
                    block_id=block_id,
                    block_type=block_type,
                    text=text_value,
                    page_index=page_index,
                    page_label=str(page_index + 1),
                    level=int(level) if level is not None else None,
                    bbox=raw.get("bbox"),
                    asset_path=(
                        str((root / raw["img_path"]).resolve()) if raw.get("img_path") else None
                    ),
                    metadata={"mineru": raw},
                )
            )
            if text_value:
                prefix = (
                    "#" * max(1, min(int(level or 1), 6)) + " "
                    if block_type in {"TITLE", "HEADING"}
                    else ""
                )
                markdown_parts.append(prefix + text_value)
        markdown = (
            markdown_files[0].read_text(encoding="utf-8", errors="replace")
            if markdown_files
            else "\n\n".join(markdown_parts)
        )
        page_count = max((item.page_index for item in blocks), default=-1) + 1
        return ParsedDocument(
            title=title,
            page_count=page_count,
            markdown=markdown,
            blocks=blocks,
            raw_artifact_path=str(archive_path),
            parser_name=self.name,
            parser_version=self.settings.mineru_model_version,
        )

    def _normalize_markdown_only(
        self, markdown: str, artifact_path: Path, title: str
    ) -> ParsedDocument:
        blocks: list[ParsedBlock] = []
        for index, raw_line in enumerate(markdown.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            is_heading = line.startswith("#")
            text_value = line.lstrip("#").strip() if is_heading else line
            blocks.append(
                ParsedBlock(
                    block_id=f"md-{index}",
                    block_type="HEADING" if is_heading else "TEXT",
                    text=text_value,
                    page_index=0,
                    page_label="1",
                    level=len(line) - len(line.lstrip("#")) if is_heading else None,
                    metadata={"page_precision": "UNAVAILABLE"},
                )
            )
        return ParsedDocument(
            title=title,
            page_count=1,
            markdown=markdown,
            blocks=blocks,
            raw_artifact_path=str(artifact_path),
            parser_name=self.name,
            parser_version=f"{self.settings.mineru_mode}-markdown",
        )


def build_parser(settings: Settings):
    if settings.parser == "fixture":
        return FixtureParser()
    if settings.parser == "mineru":
        return MinerUParser(settings)
    raise ValueError(f"Unsupported NOODLE_PARSER: {settings.parser}")
