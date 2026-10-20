"""Cold PDF parsing, durable remote submission, assets and visual provenance."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import fitz

from .contracts import canonical, sha
from .planning import QualityFailure
from .repository import one


class ParsePending(Exception):
    pass


class PdfParsing:
    def __init__(
        self,
        store,
        parser,
        journal,
        *,
        vision=None,
        profile=None,
        progress=None,
        concurrency=4,
        batch_size=4,
    ):
        self.store, self.parser, self.journal = store, parser, journal
        self.vision = vision
        self.profile = profile or {"parser": parser.name}
        self.progress = progress or (lambda *_: None)
        self.concurrency = max(1, min(concurrency, 8))
        self.batch_size = max(1, min(batch_size, 4))

    def run(self, revision_id: str):
        with self.store.db.engine.connect() as c:
            source = one(
                c,
                "SELECT r.*,s.title,s.kind FROM source_revisions r JOIN sources s ON s.id=r.source_id WHERE r.id=:id",
                id=revision_id,
            )
            if (
                not source
                or source["kind"] != "PDF"
                or not self.store.sources.source_valid(c, revision_id)
            ):
                raise QualityFailure("Generation requires the current non-deleted PDF revision")
        original = self.store.read(source["blob_ref"], source["sha256"])
        with fitz.open(stream=original, filetype="pdf") as pdf:
            physical_pages = len(pdf)
            substantive_pages = {
                page.number + 1
                for page in pdf
                if page.get_text().strip() or page.get_images() or page.get_drawings()
            }
        inputs = {"revision": revision_id, "sha256": source["sha256"], "profile": self.profile}
        cached = self.journal.load("parse.normalized", inputs)
        if cached is not None:
            return cached
        submission = self.journal.load("parse.submit", inputs)
        if submission is None:
            self.journal.intent("parse.submit", inputs)
            submission = self.parser.submit(self.store.path(source["blob_ref"]), revision_id)
            submission["submitted_at"] = datetime.now(UTC).isoformat()
            self.journal.save("parse.submit", inputs, submission)
        elapsed = (
            datetime.now(UTC) - datetime.fromisoformat(submission["submitted_at"])
        ).total_seconds()
        if elapsed > 3600:
            raise QualityFailure(
                "PDF parsing exceeded one hour; inspect the existing provider task"
            )
        raw = self.journal.load("parse.download", inputs)
        destination = self.store.path(f"artifacts/parser/{self.journal.lease.job_id}")
        if raw is None:
            result = (
                submission
                if submission.get("state") == "done"
                else self.parser.poll(submission["external_id"])
            )
            state = str(result.get("state", "pending")).lower()
            if state in {"failed", "error"}:
                raise QualityFailure(
                    "PDF provider reported a failed parse; inspect private task receipt"
                )
            if state != "done":
                raise ParsePending(state)
            payload = result.get("result") or result
            document = self.parser.download_and_normalize(
                submission["external_id"], payload, destination, source["title"]
            )
            raw = self.journal.save("parse.download", inputs, document.model_dump(mode="json"))
        blocks, seen = [], set()
        for block in raw["blocks"]:
            key = block["block_id"]
            if key in seen:
                raise QualityFailure("Parser produced duplicate block IDs")
            seen.add(key)
            if block.get("metadata", {}).get("page_precision") == "UNAVAILABLE":
                raise QualityFailure(
                    "Page-precise parsing is required; markdown-only parse cannot become PDF evidence"
                )
            page = block["page_index"] + 1
            if page < 1 or page > physical_pages:
                raise QualityFailure("Invalid parser page number")
            item = {
                "id": key,
                "page": page,
                "kind": block["block_type"],
                "text": block["text"],
                "bbox": block.get("bbox"),
                "metadata": block.get("metadata", {}),
            }
            if block.get("asset_path"):
                path = Path(block["asset_path"]).resolve()
                if not path.is_relative_to(destination.resolve()) or not path.is_file():
                    raise QualityFailure("Parser asset is outside its output directory or missing")
                content = path.read_bytes()
                suffix = path.suffix.lower()
                if (
                    suffix not in {".png", ".jpg", ".jpeg", ".webp"}
                    or len(content) > 20 * 1024 * 1024
                ):
                    raise QualityFailure("Unsupported or excessive image asset")
                asset_ref = f"assets/{sha(content)}{suffix}"
                self.store.put(asset_ref, content)
                item["asset_ref"] = asset_ref
            if item["text"].strip() or item.get("asset_ref"):
                blocks.append(item)
        represented = {block["page"] for block in blocks}
        missing = substantive_pages - represented
        if missing:
            raise QualityFailure(f"Parser omitted nonblank PDF pages: {sorted(missing)}")
        self.enrich_visuals(blocks, source)
        if not blocks:
            raise QualityFailure("Parser returned no usable blocks")
        artifact = {
            "title": source["title"],
            "page_count": physical_pages,
            "page_coverage": {
                "represented": sorted(represented),
                "blank": sorted(set(range(1, physical_pages + 1)) - substantive_pages),
            },
            "blocks": blocks,
            "parser": raw["parser_name"],
            "parser_version": raw["parser_version"],
            "profile": self.profile,
        }
        parse_id = self.store.sources.record_parse(
            revision_id,
            raw["parser_version"] or raw["parser_name"],
            self.profile,
            artifact,
            lease=self.journal.lease,
        )
        return self.journal.save(
            "parse.normalized", inputs, {"parse_id": parse_id, "artifact": artifact}
        )

    def enrich_visuals(self, blocks, source):
        assets = [b for b in blocks if b.get("asset_ref")]
        if not assets:
            return
        if self.vision is None or not self.vision.enabled:
            raise QualityFailure(
                "Visual assets require an enabled visual provider before publication"
            )
        inputs, values, pending = {}, {}, []
        for index, block in enumerate(blocks):
            if not block.get("asset_ref"):
                continue
            context = {
                "title": source["title"],
                "page": block["page"],
                "caption": block["text"],
                "nearby_text": [
                    b["text"]
                    for b in blocks[max(0, index - 3) : index + 4]
                    if b["page"] == block["page"] and b["text"]
                ],
            }
            key = block["id"]
            inputs[key] = {
                "asset_ref": block["asset_ref"],
                "context": context,
                "profile": self.profile,
                "visual_policy": "contextual-batch-v2",
            }
            cached = self.journal.load(f"visual.v2.{sha(key)[:20]}", inputs[key])
            if cached is not None:
                values[key] = cached
            else:
                pending.append(
                    {
                        "external_block_id": key,
                        "image_path": self.store.path(block["asset_ref"]),
                        "context": context,
                    }
                )
        self.progress("visual", {"completed": len(values), "total": len(assets)})
        batches = [
            pending[i : i + self.batch_size] for i in range(0, len(pending), self.batch_size)
        ]

        def analyze(batch):
            try:
                result = self.vision.analyze_batch(batch)
            except (ValueError, TypeError) as exc:
                if len(batch) == 1:
                    raise QualityFailure(
                        f"Malformed visual response for {batch[0]['external_block_id']}"
                    ) from exc
                middle = len(batch) // 2
                return {**analyze(batch[:middle]), **analyze(batch[middle:])}
            expected = {item["external_block_id"] for item in batch}
            if set(result) != expected:
                raise QualityFailure("Visual response omitted or invented an asset")
            saved = {}
            for key, visual in result.items():
                value = visual.model_dump(mode="json")
                saved[key] = self.journal.save(f"visual.v2.{sha(key)[:20]}", inputs[key], value)
            return saved

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(analyze, batch) for batch in batches]
            for future in as_completed(futures):
                values.update(future.result())
                self.progress("visual", {"completed": len(values), "total": len(assets)})
        for block in assets:
            visual = values[block["id"]]
            if not 0 <= visual["confidence"] <= 1 or visual["image_sha256"] != sha(
                self.store.path(block["asset_ref"]).read_bytes()
            ):
                raise QualityFailure("Visual provenance or confidence is invalid")
            block["visual"] = visual
            description = canonical(
                {
                    k: visual[k]
                    for k in (
                        "caption",
                        "ocr_text",
                        "factual_observations",
                        "spatial_relations",
                        "confidence",
                        "is_decorative",
                    )
                }
            )
            block["text"] = (block["text"] + "\n[视觉识别，需结合原图核对]\n" + description).strip()
            block["evidence_origin"] = "DERIVED_VISION"
