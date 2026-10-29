"""Review published topic organization; semantic links and readable trees share one source."""

import json
from typing import Literal

from pydantic import Field

from .contracts import Contract, Link, Review, Text, canonical, sha
from .eligibility import eligible
from .jobs import require_lease
from .knowledge_graph import snapshot_data
from .navigation import frozen_candidate
from .planning import QualityFailure
from .repository import Conflict, one, rows
from .segmentation import tokens


class OrganizationLink(Contract):
    owner_note_id: str
    target_note_id: str
    navigation_role: Literal["TOPIC_CHILD", "TOPIC_PARENT"]
    support_revision_ids: list[str] = Field(min_length=1)
    reason: Text
    position: int = Field(default=0, ge=0)


class RootReason(Contract):
    note_id: str
    reason: Text


class OrganizationPlan(Contract):
    links: list[OrganizationLink] = Field(default_factory=list)
    independent_roots: list[RootReason]


class OrganizationCheck(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    checked_root_ids: list[str]
    review_mode: str = "INDEPENDENT_LLM"
    checked_link_indexes: list[int] = Field(
        description="Only zero-based indexes of plan.links, not existing relations; empty plan.links requires []"
    )


def request_organization_review(store, cause=None):
    with store.repository.transaction() as c:
        epoch = c.exec_driver_sql(
            "SELECT validity_epoch FROM storage_contract WHERE id=1"
        ).scalar_one()
        return store.repository.event(
            c,
            "ORGANIZATION_REVIEW_REQUESTED",
            "concept-organization",
            None,
            {"cause": cause},
            key="organization-demand:" + str(epoch),
        )


def organization_graph(store):
    version,nodes,edges = snapshot_data(store)
    values = [nodes[k].model_dump() for k in sorted(nodes)]
    relations = [e.model_dump() for e in edges]
    signature = sha(canonical([[(n['id'],n['title'],n['description']) for n in values],
        [(e['source'],e['target'],e['relation'],e['navigation_role'],e.get('position')) for e in relations]]))
    return {'snapshot':signature, 'content_snapshot':version, 'nodes':values,'edges':relations}


def dependencies(connection, job):
    """Whole-graph organization waits for live graph producers, not note indexing."""
    from .metrics import root_for

    pending = rows(connection, "SELECT id,status FROM jobs WHERE kind IN ('GENERATE_ATOMICS','REPAIR_ATOMIC','RECONCILE_DEPENDENCIES','ANALYZE_ATOMIC','BUILD_CONCEPT') AND status IN ('PENDING','RETRY','RUNNING')")
    result = []
    for item in pending:
        root = root_for(connection,item['id'])
        control = one(connection,'SELECT state FROM job_controls WHERE job_id=:j',j=root) if root else None
        if item['status']=='RUNNING' or not control or control['state']=='RUN':
            result.append(item['id'])
    return result


def coverage(store):
    """A current full-graph receipt can cover historical organization failures."""
    graph = organization_graph(store)
    with store.db.engine.connect() as c:
        jobs = rows(c,"SELECT id,status,result_json,error_json FROM jobs WHERE kind='REVIEW_ORGANIZATION' ORDER BY rowid DESC")
    for job in jobs:
        value = json.loads(job['result_json'])
        if job['status']=='SUCCEEDED' and value.get('scope')=='ALL_CURRENT_CONCEPTS' and value.get('reviewed_graph_snapshot')==graph['snapshot']:
            return {'status':'COMPLETE','covering_job_id':job['id'],'snapshot':graph['snapshot'],'pending':0}
    def current_scope(value):
        if value.get('reviewed_graph_snapshot') == graph['snapshot']:
            return True
        focus = value.get('concept_ids', [])
        return bool(focus and value.get('reviewed_scope_snapshot') and
                    value['reviewed_scope_snapshot'] == scope_signature(neighborhood(graph, set(focus))))

    covered = {json.loads(j['result_json']).get('reviewed_scope_snapshot') for j in jobs
               if j['status'] == 'SUCCEEDED' and current_scope(json.loads(j['result_json']))}
    failed = 0
    historical_failed = 0
    for job in jobs:
        if job['status'] != 'REVIEW':
            continue
        context = json.loads(job['error_json'] or '{}').get('organization_snapshot', {})
        if current_scope(context) and context.get('reviewed_scope_snapshot') not in covered:
            failed += 1
        else:
            historical_failed += 1
    pending = sum(j['status'] in {'PENDING', 'RUNNING', 'RETRY'} for j in jobs)
    local = next((j for j in jobs if j['status']=='SUCCEEDED'
                  and json.loads(j['result_json']).get('scope')=='CHANGED_CONCEPT_NEIGHBORHOOD'
                  and current_scope(json.loads(j['result_json']))), None)
    return {'status':'PARTIAL' if failed else 'RUNNING' if any(j['status']=='RUNNING' for j in jobs) else 'WAITING',
        'covering_job_id':None,'snapshot':graph['snapshot'],'pending':pending,'failed':failed,
        'historical_failed':historical_failed,
        'local_status':'COMPLETE' if local else 'RUNNING' if pending else 'WAITING',
        'local_covering_job_id':local['id'] if local else None,
        'global_coverage':'NOT_CURRENT'}


def ready(connection, job):
    return not dependencies(connection, job)


def neighborhood(graph, focus):
    """A changed-object review may consider nearby parents without scanning every source."""
    from .concepts.catalogue import ranked
    nodes = {n['id']: n for n in graph['nodes']}
    wanted = set(focus) & nodes.keys()
    wanted.update(e[other] for e in graph['edges'] for side, other in [('source', 'target'), ('target', 'source')] if e[side] in focus)
    catalog = [{'id': n['id'], 'title': n['title'], 'domain': n.get('domain', ''),
                'summary': n.get('description', ''), 'aliases': [], 'sections': []} for n in graph['nodes']]
    for identity in sorted(set(focus) & nodes.keys()):
        wanted.update(n['id'] for n in ranked(catalog, {'title': nodes[identity]['title'], 'text': nodes[identity]['title']})[:4])
    return {'snapshot': graph['snapshot'], 'nodes': [n for n in graph['nodes'] if n['id'] in wanted],
            'edges': [e for e in graph['edges'] if e['source'] in wanted and e['target'] in wanted]}


def scope_signature(graph):
    """A local proof depends on its semantic neighborhood, not unrelated nodes."""
    return sha(canonical({'nodes': sorted((n['id'], n['title'], n.get('description', ''),
                                           n.get('domain', '')) for n in graph['nodes']),
                          'edges': sorted(canonical(e) for e in graph['edges'])}))


def scope_covers(previous, focus):
    if previous.get('scope')=='ALL_CURRENT_CONCEPTS':
        return True
    return bool(focus) and previous.get('scope')=='CHANGED_CONCEPT_NEIGHBORHOOD' and set(focus)<=set(previous.get('concept_ids',[]))


def maintain_organization(store, lease, calls):
    from .structured_quality import essential_review
    policy = "organization-essential-v1" if essential_review(calls) else "organization-v2" if getattr(calls, "policy", None) == "structured-check-v2" else "organization-v1"
    focus = sorted(set(lease.input.get('payload', {}).get('concept_ids', [])))
    scope = 'CHANGED_CONCEPT_NEIGHBORHOOD' if focus else 'ALL_CURRENT_CONCEPTS'
    scope_key = sha(canonical(focus)) if focus else None
    with store.db.engine.connect() as c:
        source = lease.input.get('payload', {}).get('source_revision')
        newer = one(c, "SELECT id FROM jobs WHERE kind='REVIEW_ORGANIZATION' AND json_extract(input_json,'$.payload.source_revision') IS :s AND created_at>(SELECT created_at FROM jobs WHERE id=:j) AND status IN ('PENDING','RUNNING','RETRY','SUCCEEDED') LIMIT 1", s=source, j=lease.job_id)
        if newer:
            return {'status': 'SUPERSEDED', 'superseded_by': newer['id']}
        current_epoch = c.exec_driver_sql(
            "SELECT validity_epoch FROM storage_contract WHERE id=1"
        ).scalar_one()
        last = one(
            c,
            "SELECT result_json FROM jobs WHERE kind='REVIEW_ORGANIZATION' AND status='SUCCEEDED' ORDER BY rowid DESC LIMIT 1",
        )
    checked = json.loads(last['result_json']) if last else {}
    graph = organization_graph(store)
    if last and scope_covers(checked,focus) and checked.get('reviewed_graph_snapshot')==graph['snapshot'] and checked.get('policy')==policy:
        return {**checked,'status':'REUSED_REVIEW','scope':scope,'scope_key':scope_key,
                'concept_ids':focus,'reviewed_epoch':current_epoch,'reuse_reason':'NO_ORGANIZATION_INPUT_CHANGE'}
    full_snapshot = graph['snapshot']
    if focus:
        graph = neighborhood(graph, set(focus))
    if (focus and checked.get('scope_key') == scope_key and checked.get('policy') == policy
            and checked.get('reviewed_scope_snapshot') == scope_signature(graph)):
        return {**checked, 'status': 'REUSED_REVIEW', 'reviewed_epoch': current_epoch,
                'reviewed_graph_snapshot': full_snapshot, 'reuse_reason': 'NO_LOCAL_ORGANIZATION_INPUT_CHANGE'}
    calls.organization_snapshot = {'scope': scope, 'concept_ids': focus,
        'reviewed_graph_snapshot': full_snapshot, 'reviewed_scope_snapshot': scope_signature(graph)}
    children = {e["target"] for e in graph["edges"] if e.get("navigation_role")}
    roots = [n["id"] for n in graph["nodes"] if n["id"] not in children]
    if len(roots) <= 1:
        return {
            "status": "SINGLE_ROOT_OR_EMPTY",
            "snapshot": graph["snapshot"],
            "reviewed_epoch": current_epoch,
            "policy": policy,
            'scope': scope, 'scope_key': scope_key, 'concept_ids': focus,
            'reviewed_graph_snapshot':graph['snapshot'],
            'reviewed_scope_snapshot':scope_signature(graph),
        }
    with store.db.engine.connect() as c:
        valid = eligible(c)
        epoch = c.exec_driver_sql(
            "SELECT validity_epoch FROM storage_contract WHERE id=1"
        ).scalar_one()
        supported = {
            n["id"]: {
                r["atomic_revision_id"]
                for r in rows(
                    c,
                    "SELECT DISTINCT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=:r",
                    r=n["revision_id"],
                )
            }
            for n in valid
            if n["kind"] == "CONCEPT"
        }
    atomics = {n["revision_id"]: n for n in valid if n["kind"] == "ATOMIC"}
    context = {
        "concepts": graph["nodes"],
        "relations": graph["edges"],
        "roots": roots,
        "atomic_cards": [],
        "inputs": {k: sorted(v) for k, v in supported.items()},
    }
    if focus:
        wanted = set().union(*(supported.get(identity, set()) for identity in focus))
        atomics = {revision: n for revision, n in atomics.items() if revision in wanted}
        context['concepts'] = [{**n, 'description': n.get('description', '')[:250]} for n in graph['nodes']]
        context['inputs'] = {n['id']: sorted(supported.get(n['id'], set()) & wanted) for n in graph['nodes']}
        context['review_scope'] = {'kind': scope, 'changed_concept_ids': focus,
            'unshown_objects_are_not_reviewed': True}
    for revision, n in atomics.items():
        candidate = frozen_candidate(store, revision)
        context["atomic_cards"].append(
            {
                "revision": revision,
                "title": n["title"],
                "description": candidate.summary,
                "hints": candidate.entity_hints.model_dump() if candidate.entity_hints else None,
            }
        )
    partitioned = tokens(canonical(context)) > 15000
    if organization_graph(store)["snapshot"] != full_snapshot:
        raise Conflict("Organization snapshot changed while collecting descriptions")
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = {(e["source"], e["target"]) for e in graph["edges"] if e.get("navigation_role")}

    def valid_plan(plan):
        seen = set()
        new_children = set()
        for link in plan.links:
            if (
                link.owner_note_id not in nodes
                or link.target_note_id not in nodes
                or link.owner_note_id == link.target_note_id
            ):
                raise QualityFailure("Unknown organization endpoints")
            if not set(link.support_revision_ids) <= supported[link.owner_note_id]:
                raise QualityFailure(
                    "Store organization relation on its actual Atomic evidence owner; child evidence requires TOPIC_PARENT"
                )
            edge = (
                (link.owner_note_id, link.target_note_id)
                if link.navigation_role == "TOPIC_CHILD"
                else (link.target_note_id, link.owner_note_id)
            )
            if edge in edges or edge in seen:
                raise QualityFailure("Do not duplicate an existing organization edge")
            seen.add(edge)
            new_children.add(edge[1])
        independent = [r.note_id for r in plan.independent_roots]
        if len(independent) != len(set(independent)) or set(independent) != (
            set(roots) - new_children
        ):
            raise QualityFailure(
                "Explain every remaining root; roots connected by proposals must not be called independent"
            )

    feedback = []
    for _ in range(2):
        if policy in {"organization-v2", "organization-essential-v1"}:
            if partitioned:
                from .organization_partitions import review_partitioned as review
            else:
                from .organization_v2 import review
            plan, check = review(store, calls, context, nodes, supported, roots, edges)
            valid_plan(plan)
            break
        plan = calls.run(
            "next.organization_plan",
            "这是阅读组织，不是严格子类断言。方法扩展可接到原方法或共同问题域，父概述没有逐字列出新方法不构成禁止条件；子方法的有效Atomic说明对象和联系即可。可结合已确认关系链选择具体上级，不发明新知识。核对完整已发布Concept图的阅读组织，只提补充关系，不写正文、不建空实体。重点检查现有根节点是否其实是另一个知识域下的方法或变体。依据Atomic简介、对象、语境和输入归属，有来源支持的方法/变体应接到合适上级；不能为了连通强归无关实体，也不把普通提及当父子关系。关系必须存到证据所属Concept：owner的inputs须含support_revision_ids；子方法证据用TOPIC_PARENT。没有漏项则links可空，independent_roots必须逐个说明仍独立的原根节点。保持已有阅读顺序，补充具体理由。",
            {**context, "prior_findings": feedback},
            OrganizationPlan,
            check=valid_plan,
            use_pro_model=True,
        )

        def complete(check, roots=roots, plan=plan):
            if set(check.checked_root_ids) != set(roots) or set(check.checked_link_indexes) != set(
                range(len(plan.links))
            ):
                raise QualityFailure(
                    "Coverage must use all original root UUIDs and ONLY plan.links indexes. expected_roots="
                    + canonical(roots)
                    + " expected_link_indexes="
                    + canonical(list(range(len(plan.links))))
                    + "; received="
                    + canonical(check.model_dump())
                )
            if check.passed != (not check.issues):
                raise QualityFailure("Organization status contradicts findings")

        check = calls.run(
            "next.organization_review",
            "checked_link_indexes仅核对plan.links的零起始索引，绝不枚举已有relations；plan.links为空时必须[]。无遗漏/无错误根的可选建议不要列issues。独立核验组织补充与全部根节点的合理性。检查原根中仍漏接的明确方法/变体，不能只核验已提的边而忽略漏项。独立根的理由必须符合Atomic简介和知识对象；没有证据时允许独立，不强连。补充边需有owner的有效输入支持、方向正确且不把阅读组织误称严格子类。checked_root_ids覆盖全部原根，checked_link_indexes覆盖每条提案。",
            {**context, "plan": plan.model_dump()},
            OrganizationCheck,
            check=complete,
            use_pro_model=True,
        )
        if check.passed:
            break
        feedback = check.issues
    else:
        raise QualityFailure("Organization review failed: " + canonical(feedback))
    by_owner = {}
    for link in plan.links:
        by_owner.setdefault(link.owner_note_id, []).append(link)
    prepared = []
    for identity, links in by_owner.items():
        base = nodes[identity]["revision"]
        candidate = frozen_candidate(store, base)
        with store.db.engine.connect() as c:
            proof = one(
                c,
                "SELECT * FROM build_artifacts WHERE storage_ref=:r",
                r=f"artifacts/builds/{base}.json",
            )
        old = json.loads(store.read(proof["storage_ref"], proof["sha256"]))
        changed = candidate.model_copy(
            update={
                "links": candidate.links
                + [
                    Link(
                        target_note_id=l.target_note_id,
                        relation="LINKS_TO",
                        status="CONFIRMED",
                        navigation_role=l.navigation_role,
                        position=l.position,
                        support_revision_ids=l.support_revision_ids,
                        basis=l.reason,
                    )
                    for l in links
                ]
            }
        )
        from .relation_overrides import apply_overrides
        changed = changed.model_copy(update={'links': apply_overrides(store, identity, changed.links)})
        report = {
            **old["review"]["report"],
            "metadata_only": True,
            "prior_revision": base,
            "organization_review": check.model_dump(),
            "organization_plan": plan.model_dump(),
        }
        prepared.append(
            (
                base,
                changed,
                Review(
                    candidate_sha256=changed.fingerprint,
                    policy_version=changed.policy_version,
                    reviewer="program.organization_acceptance" if check.review_mode != "INDEPENDENT_LLM" else "next.organization_review",
                    passed=True,
                    report=report,
                ),
            )
        )
    published = []
    with store.repository.transaction() as c:
        require_lease(c, lease)
        if (
            c.exec_driver_sql("SELECT validity_epoch FROM storage_contract WHERE id=1").scalar_one()
            != epoch
        ):
            raise Conflict("Knowledge changed during organization review")
        for base, candidate, review in prepared:

            def accepted(connection, revision, base=base, identity=candidate.note_id):
                connection.exec_driver_sql(
                    "UPDATE contribution_decisions SET applied_revision_id=? WHERE concept_id=? AND applied_revision_id=? AND status='ACCEPTED'",
                    (revision, identity, base),
                )

            published.append(
                store.publisher.publish(
                    candidate,
                    expected_revision=base,
                    review=review,
                    lease=lease,
                    accepted=accepted,
                    _connection=c,
                )
            )
        reviewed_epoch = c.exec_driver_sql(
            "SELECT validity_epoch FROM storage_contract WHERE id=1"
        ).scalar_one()
    final_graph = organization_graph(store)
    return {
        "status": "ORGANIZATION_REVIEWED",
        "policy": policy,
        "reviewed_epoch": reviewed_epoch,
        "links_added": len(plan.links),
        "published": published,
        "independent_roots": [r.model_dump() for r in plan.independent_roots],
        "check": check.model_dump(),
        'scope': scope, 'scope_key': scope_key, 'concept_ids': focus,
        'reviewed_graph_snapshot':final_graph['snapshot'],
        'reviewed_scope_snapshot':scope_signature(neighborhood(final_graph, set(focus)) if focus else final_graph),
    }
