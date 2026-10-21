"""Immutable float32 matrices and optional server-backed Chroma collections."""

import io
from functools import lru_cache
from pathlib import Path

import numpy as np

from ..contracts import canonical, sha
from ..planning import QualityFailure


def normalize(vectors, dimensions):
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != dimensions or not np.isfinite(matrix).all():
        raise QualityFailure("Embedding dimension or finite-value mismatch")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms <= 1e-12):
        raise QualityFailure("Zero embedding vector")
    return matrix / norms[:, None]


def matrix_bytes(matrix):
    data = io.BytesIO()
    np.save(data, np.asarray(matrix, dtype=np.float32), allow_pickle=False)
    return data.getvalue()


@lru_cache(maxsize=8)
def load_matrix(path, digest):
    if sha(Path(path).read_bytes()) != digest:
        raise QualityFailure("Vector matrix hash differs")
    value = np.load(path, mmap_mode="r", allow_pickle=False)
    if value.dtype != np.float32 or value.ndim != 2 or not np.isfinite(value).all():
        raise QualityFailure("Invalid vector matrix")
    return value


class ExactVectors:
    name = "exact"

    def build(self, generation, ids, matrix):
        return None

    def search(self, generation, ids, matrix, query, allowed, limit):
        positions = np.array([i for i, key in enumerate(ids) if key in allowed], dtype=np.int64)
        if not len(positions):
            return []
        scores = matrix[positions] @ query
        order = np.argsort(-scores, kind="stable")[:limit]
        return [(ids[int(positions[i])], float(scores[i])) for i in order]

    def health(self):
        return {"backend": self.name, "status": "ok"}


class ChromaVectors:
    name = "chroma"

    def __init__(self, client):
        self.client = client

    @classmethod
    def connect(cls, host="127.0.0.1", port=8001):
        import chromadb
        from chromadb.config import Settings

        return cls(
            chromadb.HttpClient(host=host, port=port, settings=Settings(anonymized_telemetry=False))
        )

    def collection(self, generation):
        return self.client.get_or_create_collection(
            name="noodle_" + generation, embedding_function=None, metadata={"hnsw:space": "cosine"}
        )

    def build(self, generation, ids, matrix):
        collection = self.collection(generation)
        for start in range(0, len(ids), 100):
            collection.upsert(
                ids=ids[start : start + 100],
                embeddings=matrix[start : start + 100].tolist(),
                metadatas=[{"chunk_id": key} for key in ids[start : start + 100]],
            )
        if collection.count() != len(ids):
            raise QualityFailure("Chroma generation coverage mismatch")

    def search(self, generation, ids, matrix, query, allowed, limit):
        if not allowed:
            return []
        collection = self.client.get_collection(
            name="noodle_" + generation, embedding_function=None
        )
        if collection.count() != len(ids):
            raise QualityFailure("Active Chroma generation is incomplete; rebuild required")
        # Scope selection stays explicit; larger allowed sets are queried in batches.
        merged = []
        allowed = sorted(allowed)
        for start in range(0, len(allowed), 1000):
            batch = allowed[start : start + 1000]
            result = self.client.get_collection(
                name="noodle_" + generation, embedding_function=None
            ).query(
                query_embeddings=[query.tolist()],
                n_results=min(limit, len(batch)),
                where={"chunk_id": {"$in": batch}},
                include=["distances"],
            )
            merged.extend(zip(result["ids"][0], (1 - float(d) for d in result["distances"][0])))
        return sorted(merged, key=lambda r: (-r[1], r[0]))[:limit]

    def delete(self, generation, ids):
        if ids:
            self.client.get_collection(name="noodle_" + generation, embedding_function=None).delete(
                ids=ids
            )

    def health(self):
        return {"backend": self.name, "heartbeat": self.client.heartbeat()}


def profile_for(embedding, allow_test=False):
    if embedding is None:
        return None
    model = getattr(embedding, "model_name", "")
    if not model or (not allow_test and ("hash" in model.lower() or model.startswith("test"))):
        raise QualityFailure("Test embeddings are forbidden in real semantic mode")
    dimensions = getattr(embedding, "dimensions", None)
    if not isinstance(dimensions, int) or dimensions < 1:
        raise QualityFailure("Embedding dimensions must be explicit")
    endpoint = str(
        getattr(getattr(embedding, "client", None), "base_url", "injected" if allow_test else "")
    )
    value = {
        "provider": type(embedding).__name__,
        "model": model,
        "dimensions": dimensions,
        "normalization": "L2",
        "text_policy": "title-section-body-v1",
        "config": {"endpoint": endpoint},
    }
    return {"id": "profile_" + sha(canonical(value))[:32], **value}
