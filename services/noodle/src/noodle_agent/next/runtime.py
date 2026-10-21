"""Composition for the new application; never constructs the legacy service."""

import os
from dataclasses import replace

import httpx
from openai import APIConnectionError, APIStatusError
from sqlalchemy.exc import IntegrityError

from ..adapters.llm import OpenAIEmbedding, OpenAIKnowledgeAgent
from .audit import ProviderAudit
from .checkpoints import Checkpoints
from .concepts.analysis import analyze
from .concepts.builder import build
from .concepts.coordinator import reconcile
from .events import dispatch
from .model_calls import Calls
from .repository import Conflict
from .retrieval.answering import Answerer, ModelReranker
from .retrieval.indexing import Indexer, request_index
from .retrieval.search import Searcher
from .retrieval.vectors import ChromaVectors, ExactVectors
from .worker import keep_lease

KINDS = ("RECONCILE_DEPENDENCIES", "ANALYZE_ATOMIC", "BUILD_CONCEPT", "INDEX_NOTE", "BUILD_INDEX")


def real_agent(settings, telemetry=None):
    if settings.llm not in {"openai", "openai-compatible", "deepseek"} or not settings.llm_api_key:
        raise ValueError(
            "Real structured model configuration is required; fixture substitution is disabled"
        )
    return OpenAIKnowledgeAgent(replace(settings, llm_max_retries=0), telemetry)


def backend_from_env():
    name = os.getenv("NOODLE_NEXT_VECTOR_BACKEND", "exact")
    if name == "exact":
        return ExactVectors()
    if name == "chroma":
        return ChromaVectors.connect(
            os.getenv("NOODLE_CHROMA_HOST", "127.0.0.1"),
            int(os.getenv("NOODLE_CHROMA_PORT", "8001")),
        )
    raise ValueError("Unknown next vector backend")


class Application:
    def __init__(
        self,
        store,
        *,
        settings=None,
        agent=None,
        embedding=None,
        backend=None,
        index_mode="HYBRID",
        allow_test_embeddings=False,
    ):
        self.store = store
        self.settings = settings
        self.agent = agent
        self.index_mode = index_mode
        if settings and settings.embedding_api_key and settings.embedding_model:
            embedding = OpenAIEmbedding(settings)
        self.indexer = Indexer(store, embedding, backend or ExactVectors(), allow_test_embeddings)
        self.calls = Calls(store, agent) if agent else None
        reranker = (
            ModelReranker(self.calls)
            if self.calls and os.getenv("NOODLE_NEXT_RERANK", "off") == "on"
            else None
        )
        self.searcher = Searcher(self.indexer, reranker)
        self.answerer = Answerer(self.searcher, self.calls)

    def run_once(self, worker_id="next-maintenance"):
        dispatch(self.store)
        lease = None
        kind = None
        for kind in KINDS:
            lease = self.store.jobs.claim(worker_id, kind=kind)
            if lease:
                break
        if not lease:
            return None
        try:
            with keep_lease(self.store.jobs, lease):
                journal = Checkpoints(self.store.repository, lease)
                if kind in {"ANALYZE_ATOMIC", "BUILD_CONCEPT"}:
                    agent = self.agent or real_agent(
                        self.settings, ProviderAudit(self.store.repository, lease)
                    )
                    calls = Calls(self.store, agent, journal)
                if kind == "ANALYZE_ATOMIC":
                    result = analyze(self.store, lease, calls)
                elif kind == "BUILD_CONCEPT":
                    result = build(self.store, lease, calls)
                elif kind == "RECONCILE_DEPENDENCIES":
                    result = reconcile(self.store, lease.job_id)
                elif kind == "INDEX_NOTE":
                    result = request_index(self.store, lease.job_id)
                else:
                    result = self.indexer.build(
                        self.index_mode, lease, lease.input["payload"]["generation"]
                    )
            self.store.jobs.finish(lease, result)
            return {"job_id": lease.job_id, "kind": kind, "status": "SUCCEEDED", "result": result}
        except Exception as exc:  # noqa: BLE001 - durable worker boundary records all failures
            retry = isinstance(
                exc,
                (
                    Conflict,
                    IntegrityError,
                    httpx.TransportError,
                    ConnectionError,
                    TimeoutError,
                    APIConnectionError,
                ),
            ) or (
                isinstance(exc, APIStatusError)
                and (exc.status_code == 429 or exc.status_code >= 500)
            )
            status = self.store.jobs.fail(
                lease, {"code": type(exc).__name__, "detail": str(exc)[:2000]}, retry=retry
            )
            return {
                "job_id": lease.job_id,
                "kind": kind,
                "status": status,
                "error": type(exc).__name__,
                "detail": str(exc)[:1000],
            }
