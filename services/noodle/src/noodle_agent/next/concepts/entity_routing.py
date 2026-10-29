"""One bounded source identity plan, then deterministic per-Atomic contributions."""

import json
import re

from sqlalchemy import text

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..jobs import require_lease
from ..navigation import frozen_candidate
from ..planning import QualityFailure
from ..repository import Conflict, identifier, insert, now, one, rows
from ..segmentation import tokens
from .coordinator import schedule
from .entity_context import BOUNDARY_GUIDANCE, BOUNDARY_POLICY, source_context
from .entity_contracts import POLICY, ROLE_TITLES, EntityPlanCheck, RoutingDecision
from .fragments import fragments
from .identity_keys import reference_keys
from .routing_budget import bounded_packet


def normalized(value):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value.casefold())


def card_for(store, row):
    candidate = frozen_candidate(store, row['revision_id'])
    size = min(120, max(16, 240 // max(1, len(candidate.sections))))
    manifest = fragments(store, store.get_note(row['id']))
    fragment_cards = []
    for fragment in manifest:
        headings = re.findall(r'^#{3,6}\s+(.+)$', fragment['text'], re.MULTILINE)
        fragment_cards.append({'fragment_id': fragment['fragment_id'], 'section_id': fragment['section_id'],
                               'heading': '；'.join(headings[:3]), 'excerpt': fragment['text'][:160]})
    return {'id': row['id'], 'revision': row['revision_id'],
            'plan_key': (row.get('creation_key') or '').split(':source-section:', 1)[-1] if ':source-section:' in (row.get('creation_key') or '') else None,
            'title': row['title'], 'description': candidate.summary,
            'hints': candidate.entity_hints.model_dump() if candidate.entity_hints else {},
            'figures': [{'caption': match[0], 'section_id': s.section_id}
                        for s in candidate.sections for match in re.findall(r'!\[([^\]]*)\]\(([^)]+)\)', s.markdown)],
            'sections': [{'id': s.section_id, 'title': s.title, 'excerpt': s.markdown[:size]} for s in candidate.sections],
            'fragments': fragment_cards}


def cohort(store, note, batch=None):
    revision = note["revision"]["id"]
    key = (note.get("creation_key") or "").split(":")
    source_revision = key[1] if len(key) > 2 and key[0] == "atomic" else None
    with store.db.engine.connect() as c:
        valid = eligible(c)
        routed = {
            r["atomic_revision_id"]
            for r in rows(c, "SELECT atomic_revision_id FROM atomic_entity_routes WHERE status IN ('ACCEPTED','SKIPPED')")
        }
        if not source_revision:
            evidence = one(
                c,
                "SELECT e.source_revision_id FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id WHERE se.revision_id=:r LIMIT 1",
                r=revision,
            )
            source_revision = evidence["source_revision_id"]
    selected = [
        r
        for r in valid
        if r["kind"] == "ATOMIC"
        and (
            (r.get("creation_key") or "").startswith(f"atomic:{source_revision}:")
            or r["revision_id"] == revision
        )
    ]
    if batch:
        order = {a["revision_id"]: i for i, a in enumerate(batch["atomics"])}
        selected = [r for r in selected if r["revision_id"] in order]
        selected.sort(key=lambda r: order[r["revision_id"]])
    else:
        selected.sort(key=lambda r: (r.get("creation_key") or "", r["id"]))
    result = []
    for row in selected:
        if not batch and row["revision_id"] in routed:
            continue
        result.append(card_for(store, row))
    if not result:
        raise QualityFailure("Entity planning requires checked Atomic descriptions and hints")
    with store.db.engine.connect() as c:
        checkpoint = one(c, "SELECT k.artifact_ref,k.artifact_sha256 FROM job_checkpoints k JOIN jobs j ON j.id=k.job_id WHERE j.kind='GENERATE_ATOMICS' AND json_extract(j.input_json,'$.revision_id')=:r AND k.stage LIKE 'source.sections.plan%' ORDER BY k.created_at DESC LIMIT 1", r=source_revision)
    if checkpoint:
        frozen = json.loads(store.read(checkpoint['artifact_ref'], checkpoint['artifact_sha256']))
        outlined = {p['key']:p.get('concept_outline') for p in frozen.get('plans', [])}
        for card in result:
            card['concept_outline'] = outlined.get(card.get('plan_key'))
    if batch:
        return source_revision, result
    # Bound a long textbook cohort without truncating any individual note description.
    current = next(card for card in result if card["revision"] == revision)
    chosen = [current]
    for card in result:
        if card is current:
            continue
        if len(chosen) >= 40 or tokens(canonical([*chosen, card])) > 10000:
            break
        chosen.append(card)
    selected_ids = {card["id"] for card in chosen}
    return source_revision, [card for card in result if card["id"] in selected_ids]


def validate_plan(plan, cards, catalog):
    by_id = {c["id"]: c for c in cards}
    if set(plan.routes) != set(by_id):
        raise QualityFailure("Entity plan must route every Atomic exactly once")
    entities = {e.identity_key: e for e in plan.entities}
    if len(entities) != len(plan.entities):
        raise QualityFailure("Repeated entity identity_key")
    existing = {c["id"]: c for c in catalog}
    names = {}
    for key, entity in entities.items():
        if entity.reuse_id and entity.reuse_id not in existing:
            raise QualityFailure("Unknown reused identity")
        if entity.reuse_id:
            old = existing[entity.reuse_id]
            if entity.domain != old["domain"] or (entity.storage_identity_key or entity.identity_key) != old.get(
                "definition", {}
            ).get("identity_key"):
                raise QualityFailure("Reuse must preserve existing domain and identity_key")
        if re.search(
            r"(?:优缺点|优点与局限|代码实现|计算示例|使用示例|的原理|的定义)$", entity.title
        ):
            raise QualityFailure("A content facet cannot be a separate entity: " + entity.title)
        for name in [entity.title, *entity.aliases]:
            name = (entity.domain, normalized(name))
            if name in names and names[name] != key:
                raise QualityFailure(
                    "Equivalent names need one entity; ambiguous abbreviations must be qualified"
                )
            names[name] = key
        for old in catalog:
            same = normalized(old["title"]) in {
                normalized(entity.title),
                *map(normalized, entity.aliases),
            }
            if same and entity.reuse_id != old["id"] and old["domain"] == entity.domain:
                raise QualityFailure("Existing canonical entity must be reused: " + old["id"])
    used = set()
    for identity, route in plan.routes.items():
        if route.entity_key is None:
            if route.sections and not route.selected_contributions:
                raise QualityFailure("Unassigned Atomic cannot contain accepted section placements")
            if not route.selected_contributions:
                continue
        if route.entity_key and route.entity_key not in entities:
            raise QualityFailure("Unknown entity route")
        if set(route.sections) != {s["id"] for s in by_id[identity]["sections"]}:
            raise QualityFailure(
                "Every Atomic section needs a placement; no silent contribution loss"
            )
        if route.entity_key:
            used.add(route.entity_key)
        for selection in route.selected_contributions:
            if selection.entity_key not in entities or not set(selection.section_ids) <= set(route.sections):
                raise QualityFailure('Unknown selected contribution target or section')
            if not set(selection.fragment_ids) <= {f['fragment_id'] for f in by_id[identity].get('fragments', [])}:
                raise QualityFailure('Unknown selected frozen fragment')
            used.add(selection.entity_key)
    if any(key not in used and entity.reuse_id is None for key, entity in entities.items()):
        raise QualityFailure("Do not create unsupported empty entities")
    adjacency = {}
    endpoints = set(entities) | set(existing) | {
        c.get("definition", {}).get("identity_key") for c in catalog
    }
    endpoints.update(reference_keys(catalog).values())
    accepted = []
    unresolved = list(plan.unresolved_relations)

    def reaches(start, target, seen=None):
        seen = set() if seen is None else seen
        if start == target:
            return True
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(child, target, seen) for child in adjacency.get(start, []))

    for relation in plan.relations:
        reason = None
        if (
            relation.source_key not in endpoints
            or relation.target_key not in endpoints
            or relation.source_key == relation.target_key
        ):
            reason = "UNKNOWN_ENDPOINT"
        elif not set(relation.support_atomic_ids) <= set(by_id):
            reason = "UNKNOWN_SUPPORT"
        elif any(
            relation.source_key not in {plan.routes[i].entity_key, *(s.entity_key for s in plan.routes[i].selected_contributions if s.purpose not in {'CONTEXT_ONLY', 'CONFLICT'})} for i in relation.support_atomic_ids
        ):
            reason = "SUPPORT_OWNER_MISMATCH"
        elif not set(relation.target_support_atomic_ids) <= set(by_id) or any(relation.target_key not in {plan.routes[i].entity_key, *(s.entity_key for s in plan.routes[i].selected_contributions if s.purpose not in {'CONTEXT_ONLY', 'CONFLICT'})} for i in relation.target_support_atomic_ids):
            reason = "TARGET_SUPPORT_OWNER_MISMATCH"
        if not reason and relation.navigation_role:
            parent, child = (
                (relation.source_key, relation.target_key)
                if relation.navigation_role == "TOPIC_CHILD"
                else (relation.target_key, relation.source_key)
            )
            if reaches(child, parent):
                reason = "ORGANIZATION_CYCLE"
            else:
                adjacency.setdefault(parent, []).append(child)
        if reason:
            unresolved.append({"relation": relation.model_dump(), "reason": reason, "status": "REVIEW"})
        else:
            accepted.append(relation)
    plan.relations[:] = accepted
    plan._unresolved_relations = unresolved


def decide(calls, payload, cards, catalog):
    from .entity_contracts import EntityPlan, Route
    from .routing_protocol import expand

    wire = None
    structured = getattr(calls, "policy", None) == "structured-check-v2"
    if structured:
        from .canonical_relations import CanonicalDecision, expand_canonical
    from ..structured_quality import essential_review
    exact = structured and essential_review(calls)
    if exact:
        from .exact_routing import canonical_decision, contract

    def compile_wire(value):
        checked_wire = canonical_decision(value) if exact else value
        candidate = expand_canonical(checked_wire, cards, catalog) if structured else expand(checked_wire, cards, catalog)
        from .object_normalization import normalize
        normalize(candidate, catalog)
        if payload.get('source_context', {}).get('object_resolution_revision'):
            for identity, route in list(candidate.routes.items()):
                if route.entity_key and route.object_match is None:
                    candidate._unresolved_atomics[identity] = 'Object resolution requires an explicit object_match'
                    candidate.routes[identity] = Route(reason=candidate._unresolved_atomics[identity])
        from .entity_context import apply_outline
        apply_outline(candidate, cards, payload.get('source_context', {}), catalog)
        validate_plan(candidate, cards, catalog)
        return checked_wire, candidate
        # Unknown semantic targets remain local REVIEW; other valid A members proceed.
    def valid_wire(value):
        compile_wire(value)
    try:
        wire = calls.run(
            "next.entity_plan",
            "为本闭合批次做一次实体归属，不写正文。仅用A/C/N短引用。每个A给REUSE、CREATE、DISTRIBUTE、SOURCE_ONLY或REVIEW及原因。"
            + BOUNDARY_GUIDANCE +
            "复用C不在new_entities重述定义；新实体N必须有独立知识价值。CREATE/REUSE每项必须显式填写target，分别为已声明N引用/已有C引用，不能只声明新实体却遗漏target。"
            "纯课程标识、行政导论、版权不具有独立知识价值，若上游仍送来应SOURCE_ONLY且target=null，不为它们创建课程实体。定义、实现、例子、限制是实体内部角色。"
            "默认全有效章节归主实体，role给默认角色；只有例外才填sections的S引用。比较内容选择有来源支持的共同对象，否则可保留来源笔记。"
            "Concept是浓缩知识框架。已有核心覆盖的重复知识、新算例、实现代码或长推导，contribution_mode=EVIDENCE，只增加来源与深入阅读；"
            "只有新核心定义、必要条件、重要变体/限制才CORE并说明净新增；新建实体使用CORE。教材优先建立基础，补充材料不重复抄入全部内容。"
            "对象身份与实际覆盖分别判断：SAME_OBJECT/INTERNAL_VARIANT不表示内容已覆盖。已有核心仅列算法名称，不等于覆盖其关键区别。"
            "区分通用家族和具体算法：通用决策树核心可以浓缩对照ID3/C4.5/CART；但声明对象为CART的核心不是通用决策树，C4.5/ID3是它的兄弟算法，不能因共享贪心框架便标INTERNAL_VARIANT或把旧CART改名吞并兄弟对象。"
            "若输入足以说明C4.5的增益率、多路划分等独立机制，可建立C4.5对象并连接已有相关方法；若只一句提及，则留在来源或作为有明确目的的比较证据，不伪称既有CART正文已覆盖。"
            "SUPPORT_ONLY须列出已有正文实际覆盖的新材料要点；只有算法名称的提及不构成覆盖证明。重要机制尚缺时用CORE_DELTA补入其正确对象，避免把重要区别降格为重复例子。"
            "text_contribution明确CORE_DELTA、SUPPORT_ONLY、FURTHER_READING等文字用途；只在无法另作判断时AUTO，兼容旧contribution_mode。media_contribution独立判断AUTO/NONE/KEY_FIGURE/BETTER_FIGURE：文字重复也可贡献模型架构等关键图；长算例截图不构成框架图片新增。无需为图片另建知识点。"
            "selections只列需要例外处理的S小节：target用C/N，purpose为CORE_DELTA、SUPPORT_ONLY、FURTHER_READING、CONTEXT_ONLY或CONFLICT。"
            "同一小节出现selections后替代该小节的默认主归属；需要支持两个对象时分别列出目的。未列小节仍归主实体。"
            "混合Atomic可把不同小节分别归不同对象，不复制全文；长算例、推导与代码用FURTHER_READING，不当成核心净新增。"
            "同一个S小节包含多个对象时，用selections.fragments填写F短引用，只选择对应冻结片段；不用复制整个S或整个Atomic。"
            "Atomic跨彼此独立对象时优先DISTRIBUTE，target=null，按S或F给各对象选择贡献；未选片段留在Atomic。"
            "不要为了给一篇Atomic安排唯一主归属而发明“A与B”的组合Concept。EM用于GMM与黑箱优化可分配到不同对象，不能仅照抄并列来源标题。"
            "identity_updates只修需要纠正的旧C名称、定义、真正同义别名和对象边界；保留规范ID，不把已收错的主题写回边界。"
            "SUPPORT_ONLY必须有已看到的正文覆盖依据；core_coverage.coverage_unknown=true不能因为scope提到该主题就判断重复。"
            "关系中的使用、应用和求解工具用USES，变体用VARIANT_OF，不把这些关系改成TOPIC目录边。"
            "outline_action=KEEP表示沿用来源对象，REVISE表示纠正混合或错误提纲；不要把“无需修订”这样的说明当作修订动作。"
            + ("assignments是按必填A键组织的对象，每个A恰好一个决定；DISTRIBUTE可用selections向多个对象贡献，不能重复assignment。背景关联放relations。" if exact else "")
            + ("关系TOPIC明确parent_ref与child_ref；一般关系明确source_ref/target_ref。supports每项atomic_ref使用A，endpoint声明PARENT/CHILD或SOURCE/TARGET，必须与该A本次归属实体一致。"
             "可以分别引用两端证据；程序决定实际证据owner和存储方向。只提可由当前C或本次实际被A支持的N构成的关系，普通提及不创建空实体。" if structured else
             "关系可引用任意输入C或本次N；support_atomic_ids用A引用，证据须属于source_key。子方法证据连接上级用TOPIC_PARENT。")
            + "相似度和同名不足以证明同一实体，按领域消歧。不为导航创建空实体，不丢弃未决输入。",
            payload, contract([c["ref"] for c in payload["atomics"]]) if exact else CanonicalDecision if structured else RoutingDecision,
            **({"check": valid_wire} if exact else {}), use_pro_model=True, max_attempts=2,
        )
        wire, plan = compile_wire(wire)

        if essential_review(calls):
            checked = EntityPlanCheck(passed=True, checked_atomic_ids=[c["ref"] for c in payload["atomics"]],
                                      review_mode="SINGLE_PRO_PLAN_AND_PROGRAM_VALIDATION")
            return plan, checked, wire.model_dump()

        def complete(value):
            expected = {c["ref"] for c in payload["atomics"]}
            if set(value.checked_atomic_ids) != expected or len(value.checked_atomic_ids) != len(expected):
                raise QualityFailure("Entity check must cover every A reference")
            if value.passed != (not value.issues):
                raise QualityFailure("Entity check contradicts its issues")

        checked = calls.run(
            "next.entity_plan_review",
            "独立核对本批实体归属的重要语义错误，不重写规划。只检查对象、领域、贡献角色与已知实体是否一致，不强求所有Atomic都创建Concept。"
            "逐项checked_atomic_ids使用A短引用；issues必须指出受影响A编号。不因可选关系待审否决合法归属。无实质错误passed=true。",
            {**payload, "plan": wire.model_dump()}, EntityPlanCheck, check=complete,
            use_pro_model=True, max_attempts=2,
        )
        if not checked.passed:
            for issue in checked.issues:
                affected = {int(i) for i in re.findall(r"\bA(\d+)\b", issue)}
                if not affected or any(i >= len(cards) for i in affected):
                    affected = set(range(len(cards)))
                for index in affected:
                    identity = cards[index]["id"]
                    plan._unresolved_atomics[identity] = issue
                    plan.routes[identity] = Route(reason=issue)
            used = {r.entity_key for r in plan.routes.values()}
            used.update(s.entity_key for r in plan.routes.values() for s in r.selected_contributions)
            plan.entities[:] = [e for e in plan.entities if e.identity_key in used]
            validate_plan(plan, cards, catalog)
        return plan, checked, wire.model_dump()
    except QualityFailure as exc:
        # A terminal checked failure is a reusable decision, not a new plan per event.
        reason = str(exc)[:3000]
        plan = EntityPlan(entities=[], routes={c["id"]: Route(reason=reason) for c in cards})
        plan._unresolved_atomics = {c["id"]: reason for c in cards}
        checked = EntityPlanCheck(passed=False, issues=[reason], checked_atomic_ids=[a["ref"] for a in payload["atomics"]])
        return plan, checked, wire.model_dump() if wire else {"error": reason}


def ensure_plan(store, lease, note, calls, retriever=None):
    from .analysis import catalogue

    source_revision, cards = cohort(store, note, lease.input.get("payload", {}).get("closed_routing_batch"))
    all_cards = cards
    maintenance = lease.input.get('payload', {}).get('concept_maintenance')
    with store.db.engine.connect() as c:
        recovery = one(c, "SELECT id FROM events WHERE event_type='JOB_RETRY_REQUESTED' AND entity_id=:j ORDER BY rowid DESC LIMIT 1", j=lease.job_id)
    snapshot = sha(canonical({"policy": POLICY, "boundary_policy": BOUNDARY_POLICY, "source": source_revision, "atomics": cards, "review_policy": "canonical-routing-v2" if getattr(calls, "policy", None) == "structured-check-v2" else "short-routing-v1", "review_scope": getattr(calls, "review_policy", None), "wire_contract": "fixed-atomic-keys-v1" if getattr(calls, "review_policy", None) in {"essential-v1", "essential-v2", "essential-v3"} else "list-v1", "recovery": recovery["id"] if recovery else None}))
    with store.repository.transaction() as c:
        require_lease(c, lease)
        if maintenance:
            snapshot = sha(canonical([snapshot, maintenance]))
        receipt = one(c, "SELECT * FROM concept_entity_plans WHERE snapshot_hash=:h", h=snapshot)
        if receipt and receipt["state"] == "READY":
            return json.loads(store.read(receipt["artifact_ref"], receipt["artifact_sha256"]))
        if receipt and receipt["lease_token"] != lease.token:
            owner = one(
                c,
                "SELECT id FROM jobs WHERE id=:j AND status='RUNNING' AND lease_owner=:t AND lease_until>:n",
                j=receipt["job_id"],
                t=receipt["lease_token"],
                n=now(),
            )
            if owner:
                raise Conflict("Entity plan currently owned by another live lease")
        c.execute(
            text(
                "INSERT INTO concept_entity_plans(snapshot_hash,source_revision_id,state,job_id,lease_token,created_at) VALUES(:h,:s,'PLANNING',:j,:t,:n) ON CONFLICT(snapshot_hash) DO UPDATE SET job_id=:j,lease_token=:t"
            ),
            {"h": snapshot, "s": source_revision, "j": lease.job_id, "t": lease.token, "n": now()},
        )
        catalog = catalogue(store, c)
        if maintenance:
            exclusions=set(maintenance.get('resolution_context', {}).get('excluded_candidate_ids', []))
            catalog=[item for item in catalog if item['id'] not in exclusions]
        settled = {r["atomic_revision_id"] for r in rows(c, "SELECT atomic_revision_id FROM atomic_entity_routes WHERE status IN ('ACCEPTED','SKIPPED')")}
        cards = [card for card in cards if maintenance or card["revision"] not in settled]
    with store.db.engine.connect() as c:
        context = source_context(store, c, source_revision, cards, catalog)
    # Packet size is a soft efficiency target. Required identities and relevant
    # checked cores may use the larger envelope instead of losing evidence.
    catalog, payload, packet_receipt = bounded_packet(cards, catalog, context, budget=24000, maximum=48000)
    if maintenance:
        payload['source_context']['object_resolution_revision'] = True
        if maintenance.get('resolution_context'):
            payload['source_context']['maintenance_goal']=maintenance['resolution_context']
    context = payload['source_context']
    if getattr(calls, 'journal', None):
        calls.journal.save('entity-routing.packet.' + snapshot, {'snapshot': snapshot},
            {'cards': cards, 'catalog': catalog, 'payload': payload, 'packet_receipt': packet_receipt})
    if hasattr(calls,'begin_unit'):
        calls.begin_unit(limit=2,key='entity-routing:'+snapshot)
    plan, checked, wire = decide(calls, payload, cards, catalog)
    mapping = {c["id"]: c["id"] for c in catalog}
    refs = reference_keys(catalog)
    mapping.update({refs[f'C{i}']: c['id'] for i, c in enumerate(catalog)})
    for entity in plan.entities:
        mapping[entity.identity_key] = entity.reuse_id or store.content.create_note(
            "CONCEPT",
            entity.title,
            domain=entity.domain,
            aliases=entity.aliases,
            creation_key=f"concept:{entity.domain}:{entity.storage_identity_key or entity.identity_key}",
            lease=lease,
        )
    artifact = {
        "policy": POLICY,
        "boundary_policy": BOUNDARY_POLICY,
        "source_context": context,
        "packet_receipt": packet_receipt,
        "snapshot": snapshot,
        "cards": cards,
        "batch_manifest": all_cards,
        "plan": plan.model_dump(),
        "review": checked.model_dump(),
        "mapping": mapping,
        "unresolved_relations": plan.unresolved_relations,
        "unresolved_atomics": plan._unresolved_atomics,
        "wire_decision": wire,
        "protocol_issues": plan._protocol_issues,
        'normalizations':plan._normalizations,
    }
    data = canonical(artifact).encode()
    ref = f"artifacts/entity-plans/{sha(data)}.json"
    store.put(ref, data)
    with store.repository.transaction() as c:
        require_lease(c, lease)
        owner = one(
            c, "SELECT lease_token FROM concept_entity_plans WHERE snapshot_hash=:h", h=snapshot
        )
        if owner["lease_token"] != lease.token:
            raise Conflict("Entity planning lease changed")
        for card in cards:
            if not store.content.valid_revision(c, card["revision"]):
                raise Conflict("Atomic cohort changed while planning")
        retired = set()
        if maintenance:
            from .maintenance import retire_inputs
            retired = retire_inputs(store, c, maintenance, cards, plan)
        for entity in plan.entities:
            target = mapping[entity.identity_key]
            stored_key = entity.storage_identity_key or entity.identity_key
            definition = entity.model_dump(exclude={"reuse_id", 'storage_identity_key'})
            definition['identity_key'] = stored_key
            definition["_entity_policy"] = POLICY
            definition['_source_role'] = context.get('source_role', 'SUPPLEMENT')
            if not entity.reuse_id:
                definition['_identity_boundary'] = {'title': entity.title, 'description': entity.description,
                    'aliases': entity.aliases, 'scope': entity.scope.model_dump(), 'policy': BOUNDARY_POLICY}
            c.execute(
                text(
                    "INSERT OR IGNORE INTO concept_identity_claims(domain,identity_key,concept_id,definition_json) VALUES(:d,:k,:id,:v)"
                ),
                {
                    "d": entity.domain,
                    "k": stored_key,
                    "id": target,
                    "v": canonical(definition),
                },
            )
            claim = one(
                c,
                "SELECT concept_id FROM concept_identity_claims WHERE domain=:d AND identity_key=:k",
                d=entity.domain,
                k=stored_key,
            )
            if claim["concept_id"] != target:
                raise Conflict("Equivalent entity reserved concurrently; replan")
        for update in plan.identity_updates:
            old = one(c, 'SELECT definition_json FROM concept_identity_claims WHERE concept_id=:id', id=update['concept_id'])
            definition = json.loads(old['definition_json'])
            boundary = {k: update[k] for k in ('title', 'description', 'aliases', 'scope')}
            boundary['policy'] = BOUNDARY_POLICY
            definition.update({**boundary, '_identity_boundary': boundary})
            c.execute(text('UPDATE concept_identity_claims SET definition_json=:v WHERE concept_id=:id'),
                      {'v': canonical(definition), 'id': update['concept_id']})
        c.execute(
            text(
                "UPDATE concept_entity_plans SET state='READY',artifact_ref=:r,artifact_sha256=:s WHERE snapshot_hash=:h"
            ),
            {"r": ref, "s": sha(data), "h": snapshot},
        )
        for card in cards:
            if maintenance and card['id'] in plan._unresolved_atomics:
                continue
            route = plan.routes[card["id"]]
            value = {
                'maintenance_run_id': maintenance['run_id'] if maintenance else None,
                'contribution_policy': POLICY + ':' + maintenance['snapshot'][:16] if maintenance else POLICY,
                'source_role': context.get('source_role', 'SUPPLEMENT'),
                'source_revision_id': source_revision,
                "route": route.model_dump(),
                "relations": [
                    r.model_dump() for r in plan.relations if r.source_key in {route.entity_key, *(s.entity_key for s in route.selected_contributions)}
                ],
                "mapping": mapping,
                "atomic_id": card["id"],
                "hint_signature": hint_signature(card["hints"]),
                "unresolved_relations": [] if getattr(calls, "policy", None) == "structured-check-v2" else plan.unresolved_relations,
                "relation_suggestions": [{**v, "status": "SUGGESTED"} for v in plan.unresolved_relations] if getattr(calls, "policy", None) == "structured-check-v2" else [],
                "protocol_issues": plan._protocol_issues,
                'normalizations':plan._normalizations,
            }
            c.execute(
                text(
                    "INSERT INTO atomic_entity_routes(atomic_revision_id,plan_hash,concept_id,status,route_json,policy) VALUES(:r,:p,:c,:s,:j,:v) ON CONFLICT(atomic_revision_id) DO UPDATE SET plan_hash=:p,concept_id=:c,status=:s,route_json=:j,policy=:v WHERE (atomic_entity_routes.status='REVIEW' OR :maintenance=1) AND atomic_entity_routes.plan_hash!=:p"
                ),
                {
                    "r": card["revision"],
                    "p": snapshot,
                    "c": mapping.get(route.entity_key),
                    "s": "REVIEW" if card["id"] in plan._unresolved_atomics else "ACCEPTED" if route.entity_key or route.selected_contributions else "SKIPPED",
                    "j": canonical(value),
                    "v": POLICY,
                    'maintenance': int(bool(maintenance)),
                },
            )
        if maintenance:
            from .maintenance import schedule_retired
            schedule_retired(store, c, retired, lease.job_id)
        if not maintenance:
            affected = {mapping[key] for route in plan.routes.values()
                for key in [route.entity_key, *(s.entity_key for s in route.selected_contributions)]
                if key in mapping}
            if affected:
                store.repository.event(c,"ORGANIZATION_REVIEW_REQUESTED","concept-organization",None,
                    {"source_revision":source_revision, 'concept_ids':sorted(affected)},key="organization-plan:"+snapshot)
    return artifact


def hint_signature(hints):
    return sha(canonical({k: hints.get(k, "") for k in ("intent", "subject", "context")}))


def known_role(title):
    patterns = [
        ("comparison", r"比较|对比|联系|区别"),
        ("limitations", r"局限|优缺点|优点|缺点|适用|限制"),
        ("examples", r"例|实现|代码|实践|sklearn|gensim"),
        ("procedure", r"步骤|算法|流程"),
        ("formulas", r"公式|计算|参数|条件"),
        ("overview", r"概述|简介|定义"),
        ("principles", r"原理|机制|推导|principle"),
    ]
    return next(
        (role for role, pattern in patterns if re.search(pattern, title, re.IGNORECASE)), None
    )


def confirmed_comparison_parent(store, note, hints):
    """Use only title comparands and independently supported direct organization edges."""
    from .analysis import catalogue
    with store.db.engine.connect() as c:
        catalog = catalogue(store, c)
        concepts = {item["id"]: item for item in catalog}
        subjects = set()
        for item in catalog:
            names = [item["title"], *item["aliases"], *item.get("definition", {}).get("aliases", [])]
            for name in names:
                pattern = re.escape(name)
                if re.search(r"[a-zA-Z]", name):
                    pattern = r"(?<![a-zA-Z0-9])" + pattern + r"(?![a-zA-Z0-9])"
                if re.search(pattern, note["title"], re.IGNORECASE):
                    subjects.add(item["id"])
                    break
        if len(subjects) < 2:
            return None
        parents = {identity: set() for identity in subjects}
        for row in rows(c, "SELECT n.id AS source_id,n.current_revision_id,l.target_note_id,l.basis_json FROM links l JOIN notes n ON n.current_revision_id=l.from_revision_id WHERE l.status='CONFIRMED'"):
            data = json.loads(row["basis_json"])
            role = data.get("navigation_role")
            if not role or not store.content.valid_revision(c, row["current_revision_id"]):
                continue
            if any(not store.content.valid_revision(c, revision) for revision in data.get("support_revision_ids", [])):
                continue
            parent, child = (row["source_id"], row["target_note_id"]) if role == "TOPIC_CHILD" else (row["target_note_id"], row["source_id"])
            if child in parents and parent not in subjects:
                parents[child].add(parent)
        common = set.intersection(*parents.values())
        context = normalized(hints.get("context", "") + " " + hints.get("subject", ""))
        matching = []
        for identity in common:
            parent = concepts[identity]
            names = [parent["title"], *parent["aliases"], *parent.get("definition", {}).get("aliases", [])]
            if any(normalized(name) and normalized(name) in context for name in names):
                matching.append(identity)
        return matching[0] if len(matching) == 1 else None


def confirmed_route(store, lease, note):
    """Reuse a checked binding or a contextual, confirmed common parent, never raw top-1."""
    candidate = frozen_candidate(store, note["revision"]["id"])
    hints = candidate.entity_hints.model_dump()
    signature = hint_signature(hints)
    if hints["intent"] == "COMPARE":
        target = confirmed_comparison_parent(store, note, hints)
        if target is None:
            return False
        roles = {s.section_id: "comparison" for s in candidate.sections}
        reason = "Confirmed title comparands share this direct parent in the matching context"
    else:
        if hints["intent"] != "EXPLAIN" or not hints["subject"] or not hints["context"]:
            return False
        roles = {s.section_id: known_role(s.title) for s in candidate.sections}
        if not all(roles.values()):
            return False
        with store.db.engine.connect() as c:
            previous = rows(c, "SELECT r.*,n.visibility FROM atomic_entity_routes r JOIN notes n ON n.id=r.concept_id WHERE r.status='ACCEPTED' AND n.visibility='ACTIVE'")
            matches = {r["concept_id"] for r in previous if json.loads(r["route_json"]).get("hint_signature") == signature
                and json.loads(r['route_json'])['route'].get('binding', {}).get('boundary_policy') == BOUNDARY_POLICY
                and not json.loads(r['route_json'])['route'].get('selected_contributions')
                and store.content.valid_revision(c, r["atomic_revision_id"])}
        if len(matches) != 1:
            return False
        target = matches.pop()
        reason = "Reused checked subject/context mapping"
    revision = note["revision"]["id"]
    with store.db.engine.connect() as c:
        support = one(
            c,
            "SELECT e.source_revision_id FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id WHERE se.revision_id=:r LIMIT 1",
            r=revision,
        )
        context = source_context(store, c, support['source_revision_id'], [], [])
    value = {
        'source_role': context.get('source_role', 'SUPPLEMENT'),
        'source_revision_id': support['source_revision_id'],
        "route": {
            "entity_key": target,
            "sections": roles,
            "reason": reason,
        },
        "relations": [],
        "mapping": {target: target},
        "atomic_id": note["id"],
        "hint_signature": signature,
    }
    data = canonical(value).encode()
    ref = "artifacts/entity-routes/" + sha(data) + ".json"
    store.put(ref, data)
    snapshot = sha(canonical([POLICY, revision, target, signature]))
    with store.repository.transaction() as c:
        require_lease(c, lease)
        if not store.content.valid_revision(c, revision):
            raise Conflict("Atomic changed during confirmed routing")
        c.execute(
            text(
                "INSERT OR IGNORE INTO concept_entity_plans(snapshot_hash,source_revision_id,state,job_id,lease_token,artifact_ref,artifact_sha256,created_at) VALUES(:h,:s,'READY',:j,:t,:r,:a,:n)"
            ),
            {
                "h": snapshot,
                "s": support["source_revision_id"],
                "j": lease.job_id,
                "t": lease.token,
                "r": ref,
                "a": sha(data),
                "n": now(),
            },
        )
        c.execute(
            text(
                "INSERT OR IGNORE INTO atomic_entity_routes(atomic_revision_id,plan_hash,concept_id,status,route_json,policy) VALUES(:r,:p,:c,'ACCEPTED',:j,:v)"
            ),
            {"r": revision, "p": snapshot, "c": target, "j": canonical(value), "v": POLICY},
        )
    return True


def analyze_entity(store, lease, calls, note, retriever=None):
    revision = note["revision"]["id"]
    with store.db.engine.connect() as c:
        route = one(c, "SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r", r=revision)
    maintenance = lease.input.get('payload', {}).get('concept_maintenance')
    if maintenance and (not route or json.loads(route['route_json']).get('maintenance_run_id') != maintenance['run_id']):
        planned = ensure_plan(store, lease, note, calls, retriever)
        if note['id'] in planned.get('unresolved_atomics', {}):
            return {'status': 'REVIEW', 'reason': planned['unresolved_atomics'][note['id']]}
        with store.db.engine.connect() as c:
            route = one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision)
    if route is None:
        if not confirmed_route(store, lease, note):
            ensure_plan(store, lease, note, calls, retriever)
        with store.db.engine.connect() as c:
            route = one(
                c, "SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r", r=revision
            )
    if route["status"] == "SKIPPED":
        diagnostics = json.loads(route["route_json"])
        return {
            "status": "SOURCE_ONLY",
            "reason": diagnostics["route"]["reason"],
            "unresolved_relations": diagnostics.get("unresolved_relations", []),
            "protocol_issues": diagnostics.get("protocol_issues", []),
        }
    if route["status"] == "REVIEW":
        with store.db.engine.connect() as c:
            recovery = one(c, "SELECT id FROM events WHERE event_type='JOB_RETRY_REQUESTED' AND entity_id=:j ORDER BY rowid DESC LIMIT 1", j=lease.job_id)
        if recovery:
            ensure_plan(store, lease, note, calls, retriever)
            with store.db.engine.connect() as c:
                route = one(c, "SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r", r=revision)
        if route["status"] == "REVIEW":
            return {"status": "REVIEW", "reason": json.loads(route["route_json"])["route"]["reason"]}
    value = json.loads(route["route_json"])
    decision_policy = value.get('contribution_policy', POLICY)
    target = route["concept_id"]
    manifest = fragments(store, note)
    processed = 0
    from .selection import contributions
    selected = list(contributions(value, manifest, target))
    targets = {item[0] for item in selected if item[0] and item[1] not in {'CONTEXT_ONLY', 'CONFLICT'}}
    with store.repository.transaction() as c:
        require_lease(c, lease)
        if not store.content.valid_revision(c, revision):
            raise Conflict("Atomic changed during entity contribution")
        for target, purpose, fragment, reason in selected:
            exists = one(
                c,
                "SELECT id FROM contribution_decisions WHERE atomic_revision_id=:r AND fragment_id=:f AND policy=:p AND concept_id IS :target",
                r=revision,
                f=fragment["fragment_id"],
                p=decision_policy,
                target=target,
            )
            if exists:
                continue
            role = value["route"]["sections"][fragment["section_id"]]
            topic_mode = getattr(calls, 'topic_policy', None) == 'stable-topics-v1'
            topic = value['route'].get('topic') or note['title']
            section = (value['route'].get('topic_key') or 'entity_topic_' + sha(normalized(topic))[:20]) if topic_mode else 'entity_' + role
            concise = getattr(calls, 'content_policy', None) == 'concise-foundation-v1'
            if concise:
                section = 'concept_core'
            exact = None
            # A literal duplicate is safe to reuse; semantic similarity alone is never sufficient.
            for old in rows(
                c,
                "SELECT d.* FROM contribution_decisions d JOIN note_revisions r ON r.id=d.atomic_revision_id JOIN notes n ON n.current_revision_id=r.id WHERE d.concept_id=:id AND d.section_id=:s AND d.status='ACCEPTED'",
                id=target,
                s=section,
            ):
                previous = json.loads(old["fragment_json"])
                if previous["sha256"] == fragment["sha256"] and store.content.valid_revision(
                    c, old["atomic_revision_id"]
                ):
                    exact = old
                    break
            decision = {
                "action": 'LINK_ONLY' if purpose == 'FURTHER_READING' else 'CONFLICT' if purpose == 'CONFLICT' else 'NO_CHANGE' if purpose == 'CONTEXT_ONLY' else "ADD_EVIDENCE" if exact or purpose == 'SUPPORT_ONLY' else "UPDATE_SECTION",
                "concept_id": target,
                "section_id": section,
                "section_title": '知识框架' if concise else topic if topic_mode else ROLE_TITLES[role],
                "section_question": '核心定义、框架、公式条件与适用边界' if concise else topic if topic_mode else ROLE_TITLES[role],
                "role": "CONTRAST" if role == "comparison" else "SUPPORT",
                "topic": value["route"].get("topic", ""),
                "reason": reason,
                'purpose': purpose,
                'media_contribution': value['route'].get('media_contribution', 'AUTO'),
                '_local_conflict': purpose == 'CONFLICT',
                'binding_version': value['route'].get('binding', {}).get('version'),
                'source_role': value.get('source_role', 'UNKNOWN'),
                'source_revision_id': value.get('source_revision_id'),
                "_entity_policy": POLICY,
                '_topic_policy': 'stable-topics-v1' if topic_mode else 'legacy-roles',
                '_content_policy': 'concise-foundation-v1' if concise else 'detailed-v1',
            }
            insert(
                c,
                "contribution_decisions",
                id=identifier(),
                atomic_revision_id=revision,
                fragment_id=fragment["fragment_id"],
                policy=decision_policy,
                concept_id=target,
                section_id=section,
                action=decision["action"],
                status='REVIEW' if purpose == 'CONFLICT' else 'SKIPPED' if purpose == 'CONTEXT_ONLY' else "ACCEPTED",
                fragment_json=canonical(fragment),
                decision_json=canonical(decision),
                applied_revision_id=None,
                created_at=now(),
            )
            processed += 1
        if not lease.input.get("payload", {}).get("closed_routing_batch"):
            for target in sorted(targets):
                schedule(store, c, target, lease.job_id)
    return {
        "status": "ANALYZED",
        "concept_id": route['concept_id'],
        'concept_ids': sorted(targets),
        "processed_fragments": processed,
        "routing": "entity-plan-once",
        "unresolved_relations": value.get("unresolved_relations", []),
        "protocol_issues": value.get("protocol_issues", []),
    }


def links_for(store, concept_id, items):
    from ..contracts import Link

    revisions = {i["atomic_revision_id"] for i in items}
    proposals = {}
    with store.db.engine.connect() as c:
        for revision in revisions:
            row = one(
                c,
                "SELECT route_json FROM atomic_entity_routes WHERE atomic_revision_id=:r",
                r=revision,
            )
            if not row:
                continue
            route = json.loads(row["route_json"])
            for relation_index, relation in enumerate(route["relations"]):
                source = route["mapping"][relation["source_key"]]
                target = route["mapping"][relation["target_key"]]
                if source != concept_id or route["atomic_id"] not in relation["support_atomic_ids"]:
                    continue
                key = (target, relation["relation"], relation["navigation_role"], relation.get('semantic_direction'))
                proposals.setdefault(key, {"reason": relation["reason"], "supports": set(), "position": relation.get("position", relation_index)})[
                    "supports"
                ].add(revision)
    return [
        Link(
            target_note_id=target,
            relation=kind,
            status="CONFIRMED",
            basis=v["reason"],
            navigation_role=role,
            position=v["position"],
            support_revision_ids=sorted(v["supports"]),
            semantic_direction=direction,
        )
        for (target, kind, role, direction), v in sorted(proposals.items(), key=lambda p: (p[1]["position"], canonical(p[0])))
    ]
