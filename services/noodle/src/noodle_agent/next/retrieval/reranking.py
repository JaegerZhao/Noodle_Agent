"""Native DashScope rerank adapter with checked indices and no document echo."""

import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from ..contracts import canonical, sha
from ..planning import QualityFailure
from ..segmentation import tokens
from .context import ranking_document


@dataclass
class RerankResult:
    ordered_ids: list[str]
    scores: dict[str, float] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


class DashScopeReranker:
    name = "dashscope"

    def __init__(
        self, api_key, url, *, model="qwen3-rerank", timeout=8.0, store=None, transport=None
    ):
        if not api_key or not url or urlsplit(url).scheme != "https":
            raise ValueError("DashScope rerank requires a Key and full HTTPS endpoint")
        if not 0 < timeout <= 60:
            raise ValueError("Rerank timeout must be in (0, 60]")
        self.api_key, self.url, self.model = api_key, url, model
        self.timeout, self.store, self.transport = timeout, store, transport

    def payload(self, query, passages):
        return {
            "model": self.model,
            "input": {"query": query, "documents": [ranking_document(p) for p in passages]},
            "parameters": {"top_n": len(passages), "return_documents": False},
        }

    def estimate_request_tokens(self, query, passages):
        payload = self.payload(query, passages)
        pair_tokens = len(passages) * tokens(query) + sum(
            tokens(d) for d in payload["input"]["documents"]
        )
        return max(tokens(canonical(payload)), pair_tokens)

    def rank(self, query, passages):
        return self.rank_with_details(query, passages).ordered_ids

    def rank_with_details(self, query, passages):
        if not passages:
            return RerankResult([])
        payload = self.payload(query, passages)
        with httpx.Client(
            timeout=self.timeout, transport=self.transport, follow_redirects=False
        ) as client:
            response = client.post(
                self.url, headers={"Authorization": "Bearer " + self.api_key}, json=payload
            )
        if response.status_code != 200:
            # Do not include provider bodies/headers, which may contain credentials or text.
            raise QualityFailure(f"DashScope rerank HTTP {response.status_code}")
        try:
            data = response.json()
            rows = data["output"]["results"]
            if not isinstance(rows, list) or len(rows) != len(passages):
                raise ValueError("Incomplete ranking")
            indices, scores = [], {}
            for row in rows:
                index, score = row["index"], row["relevance_score"]
                if type(index) is not int or not 0 <= index < len(passages):
                    raise ValueError("Unknown ranking index")
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(score)
                ):
                    raise ValueError("Invalid relevance score")
                indices.append(index)
                scores[passages[index]["chunk_id"]] = float(score)
            if len(set(indices)) != len(passages):
                raise ValueError("Duplicate ranking index")
        except (ValueError, TypeError, KeyError) as exc:
            raise QualityFailure("Invalid DashScope rerank response") from exc
        ids = [passages[i]["chunk_id"] for i in indices]
        stats = {
            "provider": self.name,
            "model": self.model,
            "documents": len(passages),
            "request_tokens_estimate": self.estimate_request_tokens(query, passages),
            "usage": data.get("usage", {}),
            "request_id": data.get("request_id"),
        }
        if self.store:
            receipt = canonical({"request": payload, "response": data}).encode()
            self.store.put(f"artifacts/rerank/{sha(receipt)}.json", receipt)
        return RerankResult(ids, scores, stats)


def reranker_from_env(calls=None, store=None, *, provider=None):
    if provider is None and os.getenv("NOODLE_NEXT_RERANK", "off") != "on":
        return None
    provider = (provider or os.getenv("NOODLE_NEXT_RERANK_PROVIDER", "llm")).lower()
    if provider == "llm":
        from .answering import ModelReranker

        return ModelReranker(calls) if calls else None
    if provider != "dashscope":
        raise ValueError("Unknown rerank provider")
    return DashScopeReranker(
        os.getenv("RERANK_API_KEY") or os.getenv("DASHSCOPE_API_KEY"),
        os.getenv("RERANK_URL"),
        model=os.getenv("RERANK_MODEL", "qwen3-rerank"),
        timeout=float(os.getenv("RERANK_TIMEOUT_SECONDS", "8")),
        store=store,
    )
