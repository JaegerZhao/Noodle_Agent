"""Composition for the new application; never constructs the legacy service."""

import os
from contextlib import nullcontext
from dataclasses import replace

import httpx
from openai import APIConnectionError, APIStatusError
from sqlalchemy.exc import IntegrityError

from ..adapters.llm import OpenAIEmbedding, OpenAIKnowledgeAgent
from ..config import Settings
from .audit import ProviderAudit
from .checkpoints import Checkpoints
from .concepts.analysis import analyze
from .concepts.builder import build
from .concepts.coordinator import reconcile
from .events import dispatch
from .model_calls import Calls
from .repository import Conflict
from .retrieval.answering import Answerer
from .retrieval.indexing import Indexer, request_index
from .retrieval.options import RetrievalOptions
from .retrieval.reranking import reranker_from_env
from .retrieval.search import Searcher
from .retrieval.vectors import ChromaVectors, ExactVectors
from .worker import keep_lease

KINDS = (
    'APPLY_CONCEPT_MAINTENANCE',
    "REPAIR_ATOMIC",
    "RECONCILE_DEPENDENCIES",
    "ANALYZE_ATOMIC",
    "BUILD_CONCEPT",
    "REVIEW_ORGANIZATION",
    'MAINTAIN_CONCEPT_STRUCTURE',
    "INDEX_NOTE",
    "BUILD_INDEX",
    "UPDATE_MAPS",
)


def real_agent(settings, telemetry=None):
    if settings.llm not in {"openai", "openai-compatible", "deepseek"} or not settings.llm_api_key:
        raise ValueError(
            "Real structured model configuration is required; fixture substitution is disabled"
        )
    from .provider_limits import LimitedAgent
    return LimitedAgent(OpenAIKnowledgeAgent(replace(settings, llm_max_retries=0), telemetry))


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
        job_kinds=None,
    ):
        self.store = store
        self.job_kinds = tuple(KINDS if job_kinds is None else job_kinds)
        if not self.job_kinds or set(self.job_kinds) - set(KINDS):
            raise ValueError("Unknown or empty maintenance stage kinds")
        self._next_kind = 0
        self.settings = settings
        self.agent = agent
        self.index_mode = index_mode
        if settings and settings.embedding_api_key and settings.embedding_model:
            embedding = OpenAIEmbedding(settings)
        self.indexer = Indexer(store, embedding, backend or ExactVectors(), allow_test_embeddings)
        self.calls = Calls(store, agent, policy="structured-check-v2" if settings else None,
                           review_policy="essential-v3" if settings else None) if agent else None
        reranker = reranker_from_env(self.calls, store)
        self.searcher = Searcher(self.indexer, reranker, options=RetrievalOptions.from_env())
        self.answerer = Answerer(self.searcher, self.calls)

    def run_once(self, worker_id="next-maintenance"):
        dispatch(self.store)
        lease = None
        kind = None
        for offset in range(len(self.job_kinds)):
            position = (self._next_kind + offset) % len(self.job_kinds)
            kind = self.job_kinds[position]
            from .concepts.coordinator import build_ready
            from .concepts.entity_context import foundation_ready
            from .organization import ready as organization_ready
            readiness = {"BUILD_CONCEPT": lambda c,j: build_ready(c,j,self.store), "REVIEW_ORGANIZATION": organization_ready,
                         'MAINTAIN_CONCEPT_STRUCTURE': organization_ready,
                         'ANALYZE_ATOMIC': lambda c,j: foundation_ready(self.store,c,j)}
            lease = self.store.jobs.claim(worker_id, kind=kind, ready=readiness.get(kind),
                lease_seconds=900 if kind == 'APPLY_CONCEPT_MAINTENANCE' else 180)
            if lease:
                self._next_kind = (position + 1) % len(self.job_kinds)
                break
        if not lease:
            return None
        calls = None
        try:
            # The import is one fenced SQLite write transaction. A separate
            # heartbeat cannot acquire that same write lock; use its bounded
            # long lease instead, and retain normal per-publication lease checks.
            with nullcontext() if kind == 'APPLY_CONCEPT_MAINTENANCE' else keep_lease(self.store.jobs, lease):
                journal = Checkpoints(self.store.repository, lease)
                if self.indexer.embedding is not None and hasattr(
                    self.indexer.embedding, "telemetry"
                ):
                    self.indexer.embedding.telemetry = ProviderAudit(self.store.repository, lease)
                    self.indexer.embedding.operation = (
                        "next.concept_embedding" if kind == "ANALYZE_ATOMIC" else "next.embedding"
                    )

                if kind in {"REPAIR_ATOMIC", "ANALYZE_ATOMIC", "BUILD_CONCEPT", "REVIEW_ORGANIZATION", 'MAINTAIN_CONCEPT_STRUCTURE'}:
                    agent = self.agent or real_agent(
                        self.settings, ProviderAudit(self.store.repository, lease)
                    )
                    calls = Calls(self.store, agent, journal, policy="structured-check-v2" if self.settings else None,
                                  review_policy="essential-v3" if self.settings else None)
                    if isinstance(self.settings, Settings):
                        calls.topic_policy = 'stable-topics-v1'
                        calls.compact_evidence = True
                        calls.content_policy = 'concise-foundation-v1'
                if kind == 'APPLY_CONCEPT_MAINTENANCE':
                    from .concepts.maintenance_bundle import apply as apply_transfer
                    result = apply_transfer(self.store, lease)
                elif kind == "REPAIR_ATOMIC":
                    from .quality_repair import repair_atomic
                    result = repair_atomic(self.store, lease, calls)
                elif kind == "ANALYZE_ATOMIC":
                    from .concepts.catalogue import IdentityRetriever

                    retriever = (
                        IdentityRetriever(self.store, self.indexer.embedding)
                        if self.indexer.embedding
                        else None
                    )
                    result = analyze(self.store, lease, calls, retriever)
                elif kind == "BUILD_CONCEPT":
                    result = build(self.store, lease, calls)
                elif kind == "REVIEW_ORGANIZATION":
                    from .organization import maintain_organization
                    result = maintain_organization(self.store,lease,calls)
                elif kind == 'MAINTAIN_CONCEPT_STRUCTURE':
                    from .concepts.structure_maintenance import maintain
                    result = maintain(self.store, lease, calls)
                elif kind == "RECONCILE_DEPENDENCIES":
                    result = reconcile(self.store, lease.job_id)
                elif kind == "UPDATE_MAPS":
                    from .navigation import refresh_maps
                    from .projection_batch import run_batch

                    result = run_batch(self.store, lease, kind, lambda: refresh_maps(self.store))
                elif kind == "INDEX_NOTE":
                    from .projection_batch import run_batch
                    result = run_batch(self.store, lease, kind, lambda: request_index(self.store, lease.job_id))
                else:
                    result = self.indexer.build(
                        self.index_mode, lease, lease.input["payload"]["generation"]
                    )
            if result.get("status") == "PARTIAL":
                self.store.jobs.defer(lease, result)
                return {"job_id": lease.job_id, "kind": kind, "status": "PENDING", "result": result}
            if result.get("status") in {"REVIEW", "PARTIAL_REVIEW"} or result.get("unresolved_relations") or result.get("protocol_issues"):
                self.store.jobs.fail(lease, {"code": "PARTIAL_REVIEW", "result": result}, retry=False)
                return {"job_id": lease.job_id, "kind": kind, "status": "REVIEW", "result": result}
            self.store.jobs.finish(lease, result)
            return {"job_id": lease.job_id, "kind": kind, "status": "SUCCEEDED", "result": result}
        except Exception as exc:  # noqa: BLE001 - durable worker boundary records all failures
            from .quality_repair import UpstreamRepairPending
            if isinstance(exc, UpstreamRepairPending):
                result = {"status":"WAITING_FOR_UPSTREAM_REPAIR", "detail":str(exc)}
                self.store.jobs.defer(lease, result)
                return {"job_id":lease.job_id,"kind":kind,"status":"PENDING","result":result}
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
            error = {"code": type(exc).__name__, "detail": str(exc)[:2000]}
            context = getattr(calls, 'organization_snapshot', None)
            if kind == 'REVIEW_ORGANIZATION' and context:
                error['organization_snapshot'] = context
            status = self.store.jobs.fail(lease, error, retry=retry)
            return {
                "job_id": lease.job_id,
                "kind": kind,
                "status": status,
                "error": type(exc).__name__,
                "detail": str(exc)[:1000],
            }
