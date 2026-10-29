"""Independent budgets for cheap recall, reranking and answer context."""

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class RetrievalOptions:
    recall_k: int = 50
    rerank_candidates: int = 10
    rerank_tokens: int = 8000
    rerank_document_tokens: int = 3500
    context_tokens: int = 4000
    expansion_tokens: int = 800
    per_note: int = 3

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Invalid retrieval budget: {field.name}")
        if self.recall_k > 500 or self.rerank_candidates > 100:
            raise ValueError("Candidate budget exceeds supported bound")

    @classmethod
    def from_env(cls):
        return cls(
            **{
                field.name: int(os.getenv("NOODLE_NEXT_" + field.name.upper(), str(field.default)))
                for field in fields(cls)
            }
        )
