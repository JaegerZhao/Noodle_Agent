"""Bounded source-outline and confirmed identity context; never reads PDF pages."""

import json

from ..contracts import canonical
from ..repository import one, rows
from ..segmentation import tokens

BOUNDARY_POLICY = "entity-boundary-v6"
BOUNDARY_GUIDANCE = (
    "先通观source_context的来源主题及本批A，按稳定知识对象规划，再给A分配主题与角色；"
    "concept_outline是此前依据整章制定的候选知识对象提纲，不是不可纠正的归属事实；同对象的A共用实体，内部主题承接变体。"
    "object_match必须说明本A主对象与target是SAME_OBJECT、INTERNAL_VARIANT、USES、DIFFERENT_OBJECT或UNCERTAIN；"
    "先根据对象名称、所回答的核心问题和本A实际内容判断身份，再看core_coverage判断净新增；先后顺序不能反过来。"
    "旧Concept可能已经收错了内容；正文或旧范围提到A，只能证明它曾收录A，不能证明两者是同一对象。"
    "LEGACY_UNRESOLVED候选仅保留声明名称作为身份线索，不能从其污染的旧scope、summary或目录主题推断身份。"
    "旧边界未迁移不表示旧对象不存在，也不表示它仅含线性/基础形式。先按声明对象与当前A识别身份，"
    "再用实际checked core判断内容覆盖；旧正文中误收的别的对象不能反过来改写其身份。"
    "总对象已存在时，核化、硬/软间隔等同一方法的内部形式通常复用该对象；"
    "另一候选若只是此前已拆出的简短变体，也不能仅因已存在窄实体而继续固化分散。"
    "FOUNDATION来源建立稳定方法对象，SUPPLEMENT来源补充它的内部形式；候选同时有教材支持的总方法与补充来源窄变体时，"
    "先比较总方法实际核心是否已容纳该形式，再决定是否真的需要独立对象，不能只按本A标题精确匹配窄C。"
    "例如已有SVM核心同时讲硬间隔、软间隔和核技巧，线性SVM与核SVM的课件说明应共同归到该SVM，"
    "既有窄C可由正常贡献迁移失去支持后退出，不以它们已经存在作为保留拆分的理由。"
    "旧身份名称/别名/边界不准确时用identity_updates按C引用修订，identity_key和已有ID仍由程序保留；"
    "例如分布核心可介绍逻辑回归应用，但分布和分类模型仍是不同对象；禁止以“旧scope包含它”作为同一对象理由。"
    "仅前两类允许复用C。逻辑回归使用伯努利分布不代表它就是伯努利分布；CNN与MLP都属于神经网络也不代表两者是同一对象。"
    "若提纲混合不同对象或过窄，在outline_revision_reason说明修订依据，冻结提纲不被改写。"
    "不要因为成功批次暂缺一个A就把共同对象拆成窄实体；同来源已有outline_bindings应优先复用。"
    "来源目录是组织线索，不是必须建立的实体，也不要求每份资料只有一个Concept。"
    "同一对象的动机、原理、数学表述、推导、条件、算例、工具实现通常组成一篇综合Concept，不能按Atomic标题各建实体。"
    "区分实体identity、内部主题topic、章节role；新实体名称用对象名称，不用“某对象原理/推导/实现示例”命名身份。"
    "同一方法的基础形式、适用场景变体和简短扩展介绍，优先作为该对象的内部主题；"
    "只有对象或核心问题确实独立、且当前材料足以支撑独立组织时才拆成实体，并在reason说明为何内部章节不能合理容纳。"
    "不同方法即使相关也不能强并；例如矩阵分解方法与利用分解的降维方法应按对象和问题区分。"
    "identity_boundary和scope仅表达对象边界，不是正文覆盖证明。core_coverage来自指定修订的实际正文，"
    "coverage_unknown=true表示未看到完整核心，不能据范围、摘要或未显示的段落断言已覆盖。"
    "same_source_bindings包含已解析绑定版本，优先复用同一对象；不能仅因新材料包含不同公式或标题就CREATE。"
    "复用同批已声明N安放同一对象的其他A，不需要重复声明近义N；已有C可扩充内部主题，不为补目录创建空父实体。"
    "new_entities.scope_includes写简短且有输入依据的可容纳主题范围，不编造来源尚无的正文；"
    "变体/子方法不是上级对象的同义别名，aliases仅写真正同义名称。"
    "assignment.topic写本A在该实体内的具体主题；role只是默认角色，含公式、步骤、算例的混合A要用sections逐一安排例外，"
    "不能把所有小节一律标principles。内部主题不再通过独立Concept及TOPIC边表达。"
    "若candidate已有internal_topics，适合扩充同一主题时复用其topic_key，并使用其标题作为topic；"
    "否则topic写一个足够综合的内部学习主题，公式、推导、条件和例子尽量属于该主题，不按每个来源小节重建主题。"
)


def source_context(store, connection, revision, cards, catalog):
    source = one(connection, "SELECT s.title FROM sources s JOIN source_revisions r ON r.source_id=s.id WHERE r.id=:r", r=revision)
    context = {"policy": BOUNDARY_POLICY, "source_title": source["title"] if source else "", "topics": [], "same_source_bindings": []}
    checkpoint = one(connection, """SELECT k.* FROM job_checkpoints k JOIN jobs j ON j.id=k.job_id
        WHERE json_extract(j.input_json,'$.revision_id')=:r AND j.kind='GENERATE_ATOMICS'
        AND k.stage LIKE 'source.sections.plan%' AND k.state='DONE' ORDER BY k.created_at DESC LIMIT 1""", r=revision)
    if checkpoint:
        plan = json.loads(store.read(checkpoint["artifact_ref"], checkpoint["artifact_sha256"]))
        context['material_kind'] = plan.get('document_profile', {}).get('kind', 'UNKNOWN')
        context['source_role'] = plan.get('source_role') if plan.get('source_role') in {'FOUNDATION', 'SUPPLEMENT'} else 'FOUNDATION' if context['material_kind'] == 'BOOK' else 'SUPPLEMENT'
        plans = plan.get("plans", [])
        keys = {c.get('plan_key') for c in cards if c.get('plan_key')}
        if not keys:
            ids = {c['id'] for c in cards}
            keys = {n['creation_key'].split(':source-section:', 1)[1] for n in rows(connection, "SELECT id,creation_key FROM notes WHERE kind='ATOMIC' AND creation_key LIKE :p", p=f'atomic:{revision}:%') if n['id'] in ids and ':source-section:' in (n['creation_key'] or '')}
        parents = {p.get("chapter_key") or p.get("parent_id") for p in plans if p.get("key") in keys}
        ordered = sorted(enumerate(plans), key=lambda pair: (pair[1].get("key") not in keys,
            (pair[1].get('chapter_key') or pair[1].get("parent_id")) not in parents, pair[0]))
        for _, p in ordered:
            item = {"title": p["title"], "parent_title": p.get("parent_title", ""), "in_current_batch": p.get("key") in keys,
                'plan_key': p.get('key'), 'chapter_key': p.get('chapter_key'), 'outline_node_id': p.get('outline_node_id'),
                'concept_outline': p.get('concept_outline')}
            trial = [*context["topics"], item]
            if len(trial) > 40 or tokens(canonical(trial)) > 2500:
                break
            context["topics"] = trial
        context["total_planned_topics"] = len(plans)
        context["topics_truncated"] = len(context["topics"]) < len(plans)
        context["outline_sha256"] = checkpoint["artifact_sha256"]
    refs = {c["id"]: f"C{i}" for i, c in enumerate(catalog)}
    source_bindings = rows(connection, """SELECT n.id atomic_id,n.title,r.concept_id,r.route_json FROM atomic_entity_routes r
        JOIN note_revisions a ON a.id=r.atomic_revision_id JOIN notes n ON n.current_revision_id=a.id
        WHERE r.status='ACCEPTED' AND n.visibility='ACTIVE' AND a.status='PUBLISHED' AND n.creation_key LIKE :prefix ORDER BY n.creation_key""", prefix=f"atomic:{revision}:%")
    current_keys = {(c.get('concept_outline') or {}).get('concept_key') for c in cards} - {None, ''}
    # A long book must not fill the bounded context with its first chapters.
    source_bindings.sort(key=lambda b: json.loads(b['route_json'])['route'].get('binding', {}).get('source_hint_key') not in current_keys)
    seen_bindings = set()
    for binding in source_bindings:
        if binding['atomic_id'] in {card['id'] for card in cards}:
            continue
        if binding["concept_id"] not in refs:
            continue
        value = json.loads(binding["route_json"])["route"]
        item = {"atomic_title": binding["title"], "concept_ref": refs[binding["concept_id"]], "topic": value.get("topic", "")}
        if value.get('binding', {}).get('boundary_policy') == BOUNDARY_POLICY:
            item['resolved_binding'] = value['binding']
            pair = (value['binding'].get('source_hint_key'), value['binding']['entity_key'])
            if pair in seen_bindings:
                continue
            seen_bindings.add(pair)
        else:
            continue
        trial = [*context["same_source_bindings"], item]
        if len(trial) > 40 or tokens(canonical(trial)) > 2000:
            break
        context["same_source_bindings"] = trial
    return context


def foundation_ready(store, connection, job):
    """Wait only for a matching planned book object, never block Atomic/index work."""
    payload = json.loads(job['input_json'])
    source = payload.get('revision_id')
    if payload.get('payload', {}).get('routing_batch'):
        source = payload['payload']['routing_batch'].get('source_revision_id') or source
    candidate = one(connection, "SELECT artifact_ref,artifact_sha256 FROM job_checkpoints k JOIN jobs j ON j.id=k.job_id WHERE j.kind='GENERATE_ATOMICS' AND json_extract(j.input_json,'$.revision_id')=:r AND k.stage LIKE 'source.sections.plan%' ORDER BY k.created_at DESC LIMIT 1", r=source)
    if not candidate:
        return True
    plan = json.loads(store.read(candidate['artifact_ref'], candidate['artifact_sha256']))
    if plan.get('document_profile', {}).get('kind') == 'BOOK' or not plan.get('display_titles') and not any(p.get('concept_outline') for p in plan.get('plans', [])):
        return True
    objects = {(p.get('concept_outline') or {}).get('concept_key') for p in plan.get('plans', [])} - {None, ''}
    producers = rows(connection, "SELECT j.id,k.artifact_ref,k.artifact_sha256 FROM jobs j JOIN job_checkpoints k ON k.job_id=j.id WHERE j.kind='GENERATE_ATOMICS' AND j.status IN ('RUNNING','PENDING','RETRY') AND k.stage LIKE 'source.sections.plan%' AND NOT EXISTS(SELECT 1 FROM job_controls ctl WHERE ctl.job_id=j.id AND ctl.state!='RUN')")
    for producer in producers:
        other = json.loads(store.read(producer['artifact_ref'], producer['artifact_sha256']))
        if other.get('document_profile', {}).get('kind') != 'BOOK':
            continue
        for topic in other.get('plans', []):
            identity = (topic.get('concept_outline') or {}).get('concept_key')
            if identity in objects and not one(connection, "SELECT 1 FROM concept_identity_claims ci JOIN notes n ON n.id=ci.concept_id WHERE ci.identity_key=:k AND n.current_revision_id IS NOT NULL", k=identity):
                return False
    return True


def apply_outline(plan, cards, context, catalog=()):
    """Resolve explicit decisions; retain the old adapter for frozen wire records."""
    from .object_binding import resolve_bindings
    explicit = {identity for identity, route in plan.routes.items() if route.object_match is not None or route.selected_contributions}
    resolve_bindings(plan, cards, context, catalog)
    from .entity_contracts import Entity
    from .identity_keys import reference_keys
    outlines = {p['plan_key']: p.get('concept_outline') for p in context.get('topics', [])}
    entities = {e.identity_key: e for e in plan.entities}
    chosen = {}
    remapped = {}
    refs = reference_keys(catalog)
    for card in cards:
        if card['id'] in explicit:
            continue
        hint = card.get('concept_outline') or outlines.get(card.get('plan_key')) or {}
        key = hint.get('concept_key')
        route = plan.routes.get(card['id'])
        if not key or not route or route.entity_key not in entities:
            continue
        entity = entities[route.entity_key]
        group = (entity.domain, key)
        if group not in chosen:
            existing = next((c for c in catalog if c.get('definition', {}).get('identity_key') == key and c['domain'] == entity.domain), None)
            if existing:
                definition = existing.get('definition', {})
                routing_key = refs[f'C{catalog.index(existing)}']
                entity = Entity(title=existing['title'], domain=existing['domain'], identity_key=routing_key,
                    storage_identity_key=key if routing_key != key else None,
                    description=existing.get('summary') or definition.get('description') or existing['title'],
                    question=definition.get('question') or existing['title'], scope=definition.get('scope') or {'includes':[existing['title']]},
                    aliases=existing.get('aliases', []), reuse_id=existing['id'])
            elif not entity.reuse_id:
                name = hint.get('concept_title') or entity.title
                entity = entity.model_copy(update={'title': name, 'identity_key': key,
                    'question': name, 'aliases': entity.aliases if name == entity.title else []})
            entities[entity.identity_key] = entity
            chosen[group] = entity.identity_key
        old = route.entity_key
        target = chosen[group]
        if old in entities and target in entities and old != target:
            current, supplemental = entities[target], entities[old]
            if not current.reuse_id:
                entities[target] = current.model_copy(update={'scope': current.scope.model_copy(update={
                    'includes': list(dict.fromkeys(current.scope.includes + supplemental.scope.includes))})})
        plan.routes[card['id']] = route.model_copy(update={'entity_key': target, 'topic':hint.get('internal_topic') or route.topic})
        remapped[old] = target
        if old != target:
            plan._normalizations.append({'code':'FROZEN_OUTLINE_GROUP','atomic_id':card['id'],'from':old,'to':target})
    plan.relations[:] = [r.model_copy(update={'source_key':remapped.get(r.source_key,r.source_key),
        'target_key':remapped.get(r.target_key,r.target_key)}) for r in plan.relations
        if remapped.get(r.source_key,r.source_key) != remapped.get(r.target_key,r.target_key)]
    used = {r.entity_key for r in plan.routes.values() if r.entity_key}
    used.update(s.entity_key for r in plan.routes.values() for s in r.selected_contributions)
    plan.entities[:] = list({e.identity_key:e for e in entities.values() if e.identity_key in used}.values())
