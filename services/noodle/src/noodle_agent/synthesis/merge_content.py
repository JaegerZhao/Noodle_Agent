"""Conservative, evidence-preserving composition for approved Concept merges."""
from __future__ import annotations

import copy
import json
import re
from typing import Any


def unique_text(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value).strip()
        key = re.sub(r"\s+", " ", value)
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def merge_scope(target: dict, source: dict, aliases: list[str]) -> dict:
    result = {}
    for key in ("includes", "excludes", "prerequisites", "aliases"):
        values = list(target.get(key) or []) + list(source.get(key) or [])
        if key == "aliases":
            values.extend(aliases)
        result[key] = unique_text(values)
    conflicts = set(result["includes"]) & set(result["excludes"])
    if conflicts:
        raise ValueError("Scope boundary conflict: " + "; ".join(sorted(conflicts)))
    return result


def merge_summary(target: str, source: str) -> str:
    # Keep distinct source statements; only remove identical sentences.
    sentences = []
    for value in (target, source):
        sentences.extend(re.split(r"(?<=[。！？])\s*|\n\n+", value.strip()))
    return " ".join(unique_text(sentences))


def content_blocks(payload: dict, plain_text: str, render_type: str) -> list[dict]:
    if isinstance(payload.get("blocks"), list) and payload["blocks"]:
        return copy.deepcopy(payload["blocks"])
    kind = render_type.upper()
    if kind == "MARKDOWN":
        kind = "PROSE"
    block = {key: copy.deepcopy(value) for key, value in payload.items() if not key.startswith("_")}
    block.update(block_key="legacy-content", type=kind)
    if kind in {"PROSE", "EXAMPLE", "CALLOUT"}:
        block["markdown"] = payload.get("markdown") or plain_text
    if not any(block.get(key) for key in ("markdown", "latex", "items", "table_rows", "steps", "code", "asset_ids")):
        block = {"block_key": "legacy-content", "type": "PROSE", "markdown": plain_text}
    return [block]


def remap_references(value: Any, contribution_map: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [remap_references(item, contribution_map) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "contribution_id":
            result[key] = contribution_map.get(str(item), item)
        elif key == "evidence_refs" and isinstance(item, list):
            result[key] = [contribution_map.get(str(ref), ref) for ref in item]
        else:
            result[key] = remap_references(item, contribution_map)
    return result


def fingerprint(block: dict) -> str:
    ignored = {"block_key", "evidence_refs", "note_block_ids", "evidence_span_ids", "asset_ids", "evidence_details"}
    value = {key: copy.deepcopy(item) for key, item in block.items() if key not in ignored and item not in (None, "", [], {})}
    for symbol in value.get("symbols", []):
        symbol["meaning"] = symbol.get("meaning") or symbol.pop("description", "")
        symbol.pop("description", None)
    # Do not normalize formulas or code: whitespace and case may be meaningful.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def combine_blocks(target: list[dict], source: list[dict]) -> tuple[list[dict], dict[str, str], int]:
    result = copy.deepcopy(target)
    signatures = {fingerprint(block): block for block in result}
    keys = {str(block.get("block_key")) for block in result}
    key_map: dict[str, str] = {}
    deduplicated = 0
    for incoming in source:
        block = copy.deepcopy(incoming)
        old_key = str(block.get("block_key") or "content")
        signature = fingerprint(block)
        existing = signatures.get(signature)
        if existing is not None:
            key_map[old_key] = existing["block_key"]
            for field in ("evidence_refs", "note_block_ids", "evidence_span_ids", "asset_ids"):
                existing[field] = list(dict.fromkeys([*(existing.get(field) or []), *(block.get(field) or [])]))
            deduplicated += 1
            continue
        key = old_key
        suffix = 2
        while key in keys:
            key = f"{old_key[:210]}-{suffix}"
            suffix += 1
        block["block_key"] = key
        keys.add(key)
        key_map[old_key] = key
        signatures[signature] = block
        result.append(block)
    return result, key_map, deduplicated
