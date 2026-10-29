"""Offline replay of frozen visual outputs; cache misses never contact a provider."""

import json
import sqlite3
from pathlib import Path
from threading import Lock

from ..domain import VisualUnderstanding
from .contracts import sha
from .planning import QualityFailure
from .visual_policy import PAGE_PROMPTS, page_prompt_version, prompt_hashes


class ReplayVision:
    name = "frozen-vision-replay"
    enabled = True

    def __init__(self, manifest):
        self.source_sha256 = manifest["source_sha256"]
        self.model_name = manifest["model"]
        self.records = {}
        self.hits = 0
        self.network_calls = 0
        self._lock = Lock()
        for record in manifest["records"]:
            value = VisualUnderstanding.model_validate(record["value"])
            if value.model != self.model_name or value.image_sha256 != record["image_sha256"]:
                raise QualityFailure("Frozen visual model/image mismatch")
            key = (record["kind"], record["page"], record["image_sha256"])
            if key in self.records and self.records[key] != value:
                raise QualityFailure("Ambiguous frozen visual output")
            self.records[key] = value

    def analyze(self, image_path, *, external_block_id, context=None):
        context = context or {}
        key = (context.get("extraction_mode"), context.get("page"), sha(image_path.read_bytes()))
        value = self.records.get(key)
        if value is None or value.external_block_id != external_block_id:
            raise QualityFailure(f"FROZEN_VISION_CACHE_MISS: {key}; network fallback disabled")
        with self._lock:
            self.hits += 1
        return value.model_copy(deep=True)

    def analyze_batch(self, items):
        return {
            item["external_block_id"]: self.analyze(
                item["image_path"],
                external_block_id=item["external_block_id"],
                context=item.get("context"),
            )
            for item in items
        }


def verified_page_manifest(roots, source_sha256, settings):
    """Only successful source-page responses with matching configuration are eligible."""
    hashes = prompt_hashes()
    selected, rejected = {}, []
    keys = ("vision_model", "vision_base_url", "vision_reasoning_effort", "vision_max_output_tokens")
    for folder in roots:
        root = Path(folder).resolve()
        info = json.loads((root / "run-input.json").read_text(encoding="utf-8"))
        config = json.loads((root / "runtime-config-redacted.json").read_text(encoding="utf-8"))
        versions = set()
        for version, filename in PAGE_PROMPTS.items():
            for snapshot in ('execution-code', 'frozen-code'):
                prompt_path = root / snapshot / 'services/noodle/src/noodle_agent/prompts' / filename
                if prompt_path.is_file():
                    if sha(prompt_path.read_text(encoding='utf-8')) == hashes[version]:
                        versions.add(version)
                    break
        if info["source_sha256"] != source_sha256 or any(config.get(k) != getattr(settings, k) for k in keys) or not versions:
            rejected.append({"root": str(root), "reason": "SOURCE_MODEL_CONFIGURATION_OR_PROMPT_MISMATCH"})
            continue
        connection = sqlite3.connect((root / "library/noodle.db").as_uri() + "?mode=ro", uri=True)
        source = connection.execute("SELECT sha256,blob_ref FROM source_revisions WHERE id=?", (info["source"]["revision_id"],)).fetchone()
        if not source or source[0] != source_sha256 or sha((root / "library" / source[1]).read_bytes()) != source_sha256:
            connection.close()
            raise QualityFailure("Visual cache source bytes do not match their immutable manifest")
        receipts = []
        for job_id, ref, expected in connection.execute("SELECT job_id,storage_ref,sha256 FROM build_artifacts WHERE kind='VISION_CALL'"):
            data = (root / "library" / ref).read_bytes()
            if sha(data) != expected:
                raise QualityFailure("Visual provider receipt hash mismatch")
            receipt = json.loads(data)
            if receipt["status"] == "COMPLETED" and receipt["model"] == settings.vision_model and receipt["prompt_version"] in versions:
                receipts.append({**receipt, 'job_id': job_id})
        checkpoints = connection.execute("SELECT job_id,artifact_ref,artifact_sha256 FROM job_checkpoints WHERE "
            "(stage LIKE 'page-context.source-v3.%' OR stage LIKE 'page-context.source-v4.%') AND state='DONE'").fetchall()
        connection.close()
        for job_id, ref, expected in checkpoints:
            data = (root / "library" / ref).read_bytes()
            if sha(data) != expected:
                raise QualityFailure("Visual checkpoint hash mismatch")
            record = json.loads(data)
            value = VisualUnderstanding.model_validate_json(record["text"])
            image = root / "library" / record["asset_ref"]
            if value.model != settings.vision_model or sha(image.read_bytes()) != value.image_sha256:
                raise QualityFailure("Visual response is not bound to its model and actual image")
            if value.prompt_version not in versions:
                continue
            # Provider telemetry hashes the ASCII image-hash list, even for one page.
            matches = [r for r in receipts if r['job_id'] == job_id and
                       r['prompt_version'] == value.prompt_version and r['image_sha256'] == sha(value.image_sha256)]
            # An old checkpoint does not retain the full request. Never guess
            # between multiple native-text contexts for the same page/job.
            if len({r['input_sha256'] for r in matches}) != 1:
                continue
            receipt = matches[-1]
            selected.setdefault((record["page"], value.image_sha256, value.prompt_version, receipt['input_sha256']), {"page": record["page"],
                "image_sha256": value.image_sha256, "input_sha256": receipt["input_sha256"],
                "prompt_version": value.prompt_version, "prompt_sha256": hashes[value.prompt_version],
                "value": value.model_dump(mode="json"), "origin_root": str(root), "origin_ref": ref,
                "historical_tokens": receipt["total_tokens"], "historical_ms": receipt["duration_ms"]})
    return {"source_sha256": source_sha256, "model": settings.vision_model,
        "prompt_sha256": hashes['source-page-evidence-v1'],
        'prompt_sha256s': {r['prompt_version']: hashes[r['prompt_version']] for r in selected.values()},
        "records": list(selected.values()), "rejected_roots": rejected}


class VerifiedPageCache:
    """Replay exact source/model/prompt/image/context matches; fresh vision handles misses."""

    enabled = True

    def __init__(self, provider, manifest, *, source_sha256, emit=lambda value: None):
        hashes = prompt_hashes()
        recorded = manifest.get('prompt_sha256s', {'source-page-evidence-v1': manifest.get('prompt_sha256')})
        if manifest["source_sha256"] != source_sha256 or manifest["model"] != provider.model_name or any(hashes.get(k) != v for k, v in recorded.items()):
            raise QualityFailure("Verified page cache does not match the requested source/model/prompt")
        self.provider, self.emit, self.model_name = provider, emit, provider.model_name
        self.records = {}
        for record in manifest['records']:
            value = VisualUnderstanding.model_validate(record['value'])
            version = value.prompt_version
            if version not in recorded or record.get('prompt_sha256', recorded[version]) != hashes[version]:
                raise QualityFailure('Verified page cache does not match the requested source/model/prompt')
            self.records[(record['page'], record['image_sha256'], version, record['input_sha256'])] = record

    @staticmethod
    def input_hash(external_block_id, context):
        text = "逐张分析下面图片。必须为每个 asset_id 返回一个 images 数组元素。\n" + "图片上下文：" + json.dumps(
            [{"asset_id": external_block_id, "context": context}], ensure_ascii=False)
        return sha(text)

    def analyze(self, image_path, *, external_block_id, context=None, **kwargs):
        context = context or {}
        version = page_prompt_version(context)
        record = self.records.get((context.get("page"), sha(image_path.read_bytes()), version, self.input_hash(external_block_id, context)))
        if context.get("extraction_mode") == "SOURCE_PAGE" and record:
            value = VisualUnderstanding.model_validate(record["value"])
            if value.external_block_id == external_block_id and value.model == self.model_name and value.image_sha256 == record["image_sha256"]:
                self.emit({"page": context["page"], "origin_root": record["origin_root"], "origin_ref": record["origin_ref"],
                    "image_sha256": record["image_sha256"], "historical_tokens": record["historical_tokens"], "historical_ms": record["historical_ms"]})
                return value.model_copy(deep=True)
        return self.provider.analyze(image_path, external_block_id=external_block_id, context=context, **kwargs)

    def analyze_batch(self, items):
        return self.provider.analyze_batch(items)
