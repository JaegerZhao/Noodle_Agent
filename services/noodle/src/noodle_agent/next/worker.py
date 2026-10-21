"""An isolated next-v1 consumer; no legacy service or legacy database access."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace

import httpx

from ..adapters.llm import OpenAIKnowledgeAgent
from ..adapters.mineru import build_parser
from ..adapters.vision import DoubaoVisionProvider
from .audit import ProviderAudit
from .generation import Generation
from .parsing import ParsePending
from .planning import QualityFailure
from .repository import Conflict


@contextmanager
def keep_lease(queue, lease):
    stopped = threading.Event()
    failures = []

    def beat():
        while not stopped.wait(15):
            try:
                queue.heartbeat(lease, lease_seconds=180)
            except Exception as exc:  # noqa: BLE001 - persist worker-boundary failures; never publish a fallback
                failures.append(exc)
                return

    queue.heartbeat(lease, lease_seconds=180)
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
        if failures:
            raise Conflict("Background heartbeat lost ownership") from failures[0]
    finally:
        stopped.set()
        thread.join(timeout=2)


class GenerationWorker:
    kind = "GENERATE_ATOMICS"

    def __init__(
        self,
        store,
        *,
        settings=None,
        parser=None,
        agent=None,
        vision=None,
        progress=None,
        draft_concurrency=1,
    ):
        self.store, self.settings = store, settings
        self.draft_concurrency = draft_concurrency
        self.parser, self.agent, self.vision = parser, agent, vision
        self.progress = progress or (lambda *_: None)

    def run_once(self, worker_id="next-generation"):
        from .events import dispatch

        dispatch(self.store)
        lease = self.store.jobs.claim(worker_id, kind=self.kind)
        if lease is None:
            return None
        try:
            with keep_lease(self.store.jobs, lease):
                if self.settings:
                    if (
                        self.settings.llm not in {"openai", "deepseek"}
                        or self.settings.parser != "mineru"
                    ):
                        raise QualityFailure(
                            "Real next generation requires configured MinerU and OpenAI-compatible providers; fixture substitution is disabled"
                        )
                    audit = ProviderAudit(self.store.repository, lease)
                    # Keep connection retries bounded by the durable job retry policy.
                    settings = replace(
                        self.settings,
                        llm_max_retries=0,
                        vision_max_retries=0,
                        vision_max_output_tokens=max(2400, self.settings.vision_max_output_tokens),
                    )
                    parser = build_parser(settings)
                    agent = OpenAIKnowledgeAgent(settings, telemetry=audit)
                    vision = (
                        DoubaoVisionProvider(settings, audit) if settings.vision_api_key else None
                    )
                    profile = {
                        "parser": parser.name,
                        "parse_mode": settings.mineru_mode,
                        "parse_model": settings.mineru_model_version,
                        "language": settings.mineru_language,
                        "model": settings.llm_fast_model,
                        "llm_endpoint": settings.llm_base_url,
                        "vision_model": settings.vision_model,
                        "vision_endpoint": settings.vision_base_url,
                        "vision_prompt": "visual-understanding-v5",
                        "pipeline": "generation-next-v1",
                        "writing": self.store.repository.policy_version,
                    }
                else:
                    parser, agent, vision = self.parser, self.agent, self.vision
                    profile = {
                        "parser": parser.name,
                        "model": "injected-test",
                        "pipeline": "generation-next-v1",
                        "writing": self.store.repository.policy_version,
                    }
                self.progress("claimed", {"job_id": lease.job_id})
                result = Generation(
                    self.store,
                    parser,
                    agent,
                    lease,
                    vision=vision,
                    profile=profile,
                    progress=self.progress,
                    concurrency=self.settings.draft_concurrency
                    if self.settings
                    else self.draft_concurrency,
                ).run()
            self.store.jobs.finish(lease, result)
            return {
                "job_id": lease.job_id,
                "status": "SUCCEEDED",
                "note_count": len(result["notes"]),
                "source_map": result["source_map"],
            }
        except ParsePending as exc:
            self.store.jobs.defer(lease, {"stage": "PARSE_PENDING", "state": str(exc)})
            return {"job_id": lease.job_id, "status": "PENDING", "stage": "PARSE_PENDING"}
        except Exception as exc:  # noqa: BLE001 - persist worker-boundary failures; never publish a fallback
            retryable = isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))
            retryable = retryable or (
                isinstance(exc, httpx.HTTPStatusError)
                and (exc.response.status_code == 429 or exc.response.status_code >= 500)
            )
            # OpenAI wraps httpx errors without inheriting TransportError.
            from openai import APIConnectionError, APIStatusError

            retryable = (
                retryable
                or isinstance(exc, APIConnectionError)
                or (
                    isinstance(exc, APIStatusError)
                    and (exc.status_code == 429 or exc.status_code >= 500)
                )
            )
            status = self.store.jobs.fail(
                lease, {"code": type(exc).__name__, "detail": str(exc)[:2000]}, retry=retryable
            )
            return {
                "job_id": lease.job_id,
                "status": status,
                "error": type(exc).__name__,
                "detail": str(exc)[:1000],
            }
