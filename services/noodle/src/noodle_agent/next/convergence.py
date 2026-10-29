"""Read-only business completion checks, separate from queue success."""

import json

from .eligibility import eligible
from .repository import one, rows


def superseded_review(store, c, job):
    """Explain replacement coverage without rewriting the durable queue status."""
    if job["kind"] != "ANALYZE_ATOMIC" or job["status"] != "REVIEW":
        return None
    from .concepts.entity_contracts import POLICY
    from .concepts.fragments import fragments

    data = json.loads(job["input_json"])
    batch = data.get("payload", {}).get("routing_batch")
    inputs = batch["atomics"] if batch else [{"note_id": data["entity_id"], "revision_id": data["revision_id"]}]
    coverage = []
    changed = False
    for item in inputs:
        head = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=item["note_id"])
        if not head or not head["current_revision_id"] or not store.content.valid_revision(c, head["current_revision_id"]):
            return None
        current = head["current_revision_id"]
        route = one(c, "SELECT r.*,p.job_id covering_job FROM atomic_entity_routes r JOIN concept_entity_plans p ON p.snapshot_hash=r.plan_hash WHERE r.atomic_revision_id=:r", r=current)
        if not route or route["status"] == "REVIEW" or any(json.loads(route["route_json"]).get(k) for k in ("unresolved_relations", "protocol_issues")):
            return None
        if route["status"] == "ACCEPTED":
            expected = {f["fragment_id"] for f in fragments(store, store.get_note(item["note_id"]))}
            accepted = {r["fragment_id"] for r in rows(c, "SELECT fragment_id FROM contribution_decisions WHERE atomic_revision_id=:r AND policy=:p AND status IN ('ACCEPTED','SKIPPED','REVIEW')", r=current, p=json.loads(route['route_json']).get('contribution_policy', POLICY))}
            if not expected <= accepted:
                return None
        changed |= current != item["revision_id"] or route["covering_job"] != job["id"]
        coverage.append({"input_revision_id": item["revision_id"], "current_revision_id": current, "plan_hash": route["plan_hash"], "covering_job_id": route["covering_job"]})
    return {"job_id": job["id"], "queue_status": job["status"], "business_status": "SUPERSEDED", "covered_inputs": coverage} if changed and coverage else None


def convergence_report(store, *, require_routes=True, require_idle=True):
    with store.db.engine.connect() as c:
        valid = eligible(c)
        valid_ids = {n["revision_id"] for n in valid}
        published = rows(
            c,
            "SELECT n.id,n.title,n.kind,n.current_revision_id FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.visibility='ACTIVE' AND n.kind IN ('ATOMIC','CONCEPT') AND r.status='PUBLISHED'",
        )
        invalid = [dict(n) for n in published if n["current_revision_id"] not in valid_ids]
        jobs = rows(c, "SELECT * FROM jobs WHERE status!='SUCCEEDED'")
        partial = []
        for completed in rows(c, "SELECT id,result_json FROM jobs WHERE kind='GENERATE_ATOMICS' AND status='SUCCEEDED'"):
            failed = json.loads(completed["result_json"]).get("coverage", {}).get("failed_plans", [])
            if failed:
                partial.append({"job_id": completed["id"], "failed_plans": failed})
        superseded = [v for job in jobs if (v := superseded_review(store, c, job))]
        from .organization import coverage as organization_coverage
        structure = organization_coverage(store)
        if structure['status']=='COMPLETE':
            superseded.extend({'job_id':j['id'],'queue_status':j['status'],'business_status':'SUPERSEDED',
                'covering_job_id':structure['covering_job_id'],'covered_graph_snapshot':structure['snapshot']}
                for j in jobs if j['kind']=='REVIEW_ORGANIZATION' and j['status']=='REVIEW')
        covered_jobs = {v["job_id"] for v in superseded}
        unsettled = [{k: job[k] for k in ("id", "kind", "status")} for job in jobs if job["id"] not in covered_jobs]
        from .organization import dependencies
        dependency_waits = {job["id"]: dependencies(c, job) for job in jobs if job["kind"] == "REVIEW_ORGANIZATION" and job["status"] in {"PENDING", "RETRY"}}
        queued = [
            dict(r)
            for r in rows(c, "SELECT id,event_type,status FROM events WHERE status='QUEUED'")
        ]
        routes = {
            r["atomic_revision_id"]
            for r in rows(c, "SELECT atomic_revision_id FROM atomic_entity_routes")
        }
        routing_reviews = [dict(r) for r in rows(c, "SELECT atomic_revision_id,status,route_json FROM atomic_entity_routes") if r["atomic_revision_id"] in valid_ids and (r["status"] == "REVIEW" or any(json.loads(r["route_json"]).get(k) for k in ("unresolved_relations", "protocol_issues")))]
        suggestions = [{"atomic_revision_id": r["atomic_revision_id"], "suggestions": json.loads(r["route_json"]).get("relation_suggestions", [])} for r in rows(c, "SELECT atomic_revision_id,route_json FROM atomic_entity_routes") if r["atomic_revision_id"] in valid_ids and json.loads(r["route_json"]).get("relation_suggestions")]
        unrouted = []
        if require_routes:
            from .navigation import frozen_candidate

            for n in valid:
                if (
                    n["kind"] == "ATOMIC"
                    and frozen_candidate(store, n["revision_id"]).entity_hints
                    and n["revision_id"] not in routes
                ):
                    unrouted.append(n["id"])
        return {
            "passed": not partial and not invalid
            and not unrouted
            and not routing_reviews
            and (not require_idle or not unsettled and not queued),
            "eligible_notes": len(valid),
            "partial_generations": partial,
            'knowledge_structure':structure,
            "eligible_counts": {
                k: sum(n["kind"] == k for n in valid) for k in ["ATOMIC", "CONCEPT"]
            },
            "ineligible_published_notes": invalid,
            "unrouted_atomic_ids": unrouted,
            "routing_reviews": routing_reviews,
            "optional_relation_suggestions": suggestions,
            "unsettled_jobs": unsettled,
            "superseded_jobs": superseded,
            "dependency_waits": {k: v for k, v in dependency_waits.items() if v},
            "queued_events": queued,
        }
