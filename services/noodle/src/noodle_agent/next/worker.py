"""An isolated next-v1 consumer; no legacy service or legacy database access."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace

from ..adapters.llm import OpenAIKnowledgeAgent
from ..adapters.mineru import build_parser
from ..adapters.vision import DoubaoVisionProvider
from .audit import ProviderAudit
from .generation import Generation
from .parsing import ParsePending
from .planning import QualityFailure
from .repository import Conflict
from .task_control import PauseRequested


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

        def progress(stage, info):
            from sqlalchemy import text

            from .contracts import canonical
            from .jobs import require_lease
            from .repository import now

            with self.store.repository.transaction() as c:
                require_lease(c, lease)
                c.execute(
                    text(
                        "INSERT INTO job_progress(job_id,stage,details_json,updated_at) VALUES(:id,:s,:d,:t) ON CONFLICT(job_id) DO UPDATE SET stage=excluded.stage,details_json=excluded.details_json,updated_at=excluded.updated_at"
                    ),
                    {"id": lease.job_id, "s": stage, "d": canonical(info), "t": now()},
                )
                from .repository import identifier, insert

                insert(
                    c,
                    "job_stage_events",
                    id=identifier(),
                    job_id=lease.job_id,
                    stage=stage,
                    detail_json=canonical(info),
                    created_at=now(),
                )
            self.progress(stage, info)

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
                        mineru_mode="precision",
                        llm_max_retries=0,
                        vision_max_retries=0,
                        vision_max_output_tokens=max(2400, self.settings.vision_max_output_tokens),
                    )
                    parser = build_parser(settings)
                    from .provider_limits import LimitedAgent
                    agent = LimitedAgent(OpenAIKnowledgeAgent(settings, telemetry=audit))
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
                # Resume with the exact frozen profile; new real jobs use the
                # source-aligned route. Injected fixtures remain explicitly legacy.
                import json

                from .repository import one

                with self.store.db.engine.connect() as c:
                    frozen = one(
                        c,
                        "SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=:j AND stage='profile' AND state='DONE'",
                        j=lease.job_id,
                    )
                if frozen:
                    profile = json.loads(
                        self.store.read(frozen["artifact_ref"], frozen["artifact_sha256"])
                    )
                elif self.settings:
                    import os

                    route = os.getenv("NOODLE_NEXT_GENERATION_LAYOUT", "source-sections-v1")
                    if route != "source-sections-v1":
                        raise QualityFailure("New jobs require source-sections-v1; historical jobs retain their frozen profile")
                    if route == "source-sections-v1":
                        profile["generation_layout"] = route
                        layout = os.getenv("NOODLE_NEXT_CONCEPT_LAYOUT", "entity-v2")
                        if layout != "entity-v2":
                            raise QualityFailure("New jobs require entity-v2; historical jobs retain their frozen profile")
                        profile["concept_layout"] = layout
                        profile["execution_policy"] = "pipeline-throughput-v1"
                        profile["checking_policy"] = "single-check-v1"
                        profile["planning_mode"] = "adaptive-source-v1"
                        profile["review_policy"] = "essential-v3"
                        profile['atomic_review_layout'] = 'section-flash-pro-v1'
                        from .cost_policy import model_mode
                        profile['pro_usage'] = model_mode()
                        profile["planning_model"] = settings.llm_pro_model if model_mode() in {'planning-pro','selective'} else settings.llm_fast_model
                        profile["quality_policy"] = "structured-quality-v2"
                        profile['concept_topics'] = 'stable-topics-v1'
                        profile['content_policy'] = 'concise-foundation-v1'
                        profile['figure_binding_policy'] = 'source-figure-bindings-v1'
                        profile['evidence_encoding'] = 'source-ranges-v1'
                        profile['cost_observation_policy'] = 'observe-continue-v1'
                if self.settings and vision is not None:
                    import os
                    roots = json.loads(os.getenv('NOODLE_NEXT_VISION_CACHE_RUNS', '[]'))
                    if roots:
                        from .visual_replay import VerifiedPageCache, verified_page_manifest
                        with self.store.db.engine.connect() as c:
                            source = one(c, 'SELECT sha256 FROM source_revisions WHERE id=:r', r=lease.input['revision_id'])
                        manifest = verified_page_manifest(roots, source['sha256'], settings)
                        vision = VerifiedPageCache(vision, manifest, source_sha256=source['sha256'], emit=lambda v: progress('visual-cache-hit', v))
                progress("claimed", {"job_id": lease.job_id})
                result = Generation(
                    self.store,
                    parser,
                    agent,
                    lease,
                    vision=vision,
                    profile=profile,
                    progress=progress,
                    concurrency=self.settings.draft_concurrency
                    if self.settings
                    else self.draft_concurrency,
                ).run()
            from .task_control import boundary

            boundary(self.store, lease)
            self.store.jobs.finish(lease, result)
            return {
                "job_id": lease.job_id,
                "status": "SUCCEEDED",
                "note_count": len(result["notes"]),
                "source_map": result["source_map"],
                "completion_status": "PARTIAL" if result.get("coverage", {}).get("failed_plans") else "COMPLETE",
                "deferred_atomics": result.get("coverage", {}).get("failed_plans", []),
            }
        except PauseRequested:
            from .task_control import park

            park(self.store, lease)
            return {"job_id": lease.job_id, "status": "PAUSED"}
        except ParsePending as exc:
            self.store.jobs.defer(lease, {"stage": "PARSE_PENDING", "state": str(exc)})
            return {"job_id": lease.job_id, "status": "PENDING", "stage": "PARSE_PENDING"}
        except Exception as exc:  # noqa: BLE001 - persist worker-boundary failures; never publish a fallback
            from .resilience import transient_provider_failure

            retryable = transient_provider_failure(exc)
            status = self.store.jobs.fail(
                lease, {"code": type(exc).__name__, "detail": str(exc)[:2000]}, retry=retryable
            )
            return {
                "job_id": lease.job_id,
                "status": status,
                "error": type(exc).__name__,
                "detail": str(exc)[:1000],
            }
