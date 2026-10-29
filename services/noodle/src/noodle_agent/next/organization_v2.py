"""Canonical reading-tree proposals; roots and storage directions are program-owned."""

from pydantic import Field

from .contracts import Contract, canonical, sha
from .organization import OrganizationCheck, OrganizationLink, OrganizationPlan, RootReason
from .planning import QualityFailure


class TopicProposal(Contract):
    parent_note_id: str
    child_note_id: str
    support_revision_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)
    position: int = Field(default=0, ge=0)


class OrganizationProposal(Contract):
    links: list[TopicProposal] = Field(default_factory=list)


class OrganizationAssessment(OrganizationCheck):
    independent_roots: list[RootReason]


def compile_proposal(proposal, nodes, supported, roots, existing, *, diagnostics=None):
    graph = {}
    for parent, child in existing:
        graph.setdefault(parent, set()).add(child)

    def reaches(start, target, seen=None):
        seen = set() if seen is None else seen
        if start == target:
            return True
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(c, target, seen) for c in graph.get(start, ()))

    result, edges, children = [], set(existing), set()
    for link in proposal.links:
        parent, child = link.parent_note_id, link.child_note_id
        if parent not in nodes or child not in nodes or parent == child:
            if diagnostics is not None:
                diagnostics.append({'code':'INVALID_NAVIGATION_ENDPOINT_SKIPPED', 'proposal':link.model_dump()})
                continue
            raise QualityFailure("Unknown canonical parent/child endpoint")
        proof = set(link.support_revision_ids)
        if not proof <= supported.get(parent, set()) | supported.get(child, set()):
            if diagnostics is not None:
                diagnostics.append({'code':'UNOWNED_NAVIGATION_SUPPORT_SKIPPED', 'proposal':link.model_dump()})
                continue
            raise QualityFailure("Canonical relation contains evidence belonging to neither endpoint")
        if (parent, child) in edges:
            continue
        if reaches(child, parent):
            if diagnostics is not None:
                diagnostics.append({'code':'CYCLIC_NAVIGATION_EDGE_SKIPPED', 'proposal':link.model_dump()})
                continue
            raise QualityFailure("Canonical organization proposal introduces a cycle")
        edges.add((parent, child))
        graph.setdefault(parent, set()).add(child)
        children.add(child)
        child_proof = proof & supported.get(child, set())
        owner, target, role, selected = (child, parent, "TOPIC_PARENT", child_proof) if child_proof else (parent, child, "TOPIC_CHILD", proof)
        result.append(OrganizationLink(owner_note_id=owner, target_note_id=target,
            navigation_role=role, support_revision_ids=sorted(selected), reason=link.reason, position=link.position))
    return result, sorted(set(roots) - children)


def review(store, calls, context, nodes, supported, roots, existing):
    from .structured_quality import essential_review
    essential = essential_review(calls)
    if hasattr(calls,'begin_unit'):
        calls.begin_unit(limit=2 if getattr(calls,'review_policy',None)=='essential-v3' else 5,
            key='organization-neighborhood:' + sha(canonical({'roots':sorted(roots),
                'concept_versions':sorted((n.get('id'),n.get('revision')) for n in context.get('concepts',[])),
                'atomic_revisions':sorted(a['revision'] for a in context.get('atomic_cards',[]))})))
    feedback = []
    for _ in range(2):
        def valid(value):
            compile_proposal(value, nodes, supported, roots, existing, diagnostics=[] if essential else None)

        proposal = calls.run("next.organization_plan",
            "核对当前已发布Concept的阅读组织，只提出有来源支持的上位问题parent_note_id到具体方法/子主题child_note_id。"
            "明确父子语义方向，不输出owner、TOPIC_CHILD/TOPIC_PARENT或根列表，程序推导这些字段。"
            "support_revision_ids可以来自任一端有效inputs，须确实支持所提关系；不为连通强归无关对象，不把普通提及当父子关系。"
            "使用、应用场景、求解工具和计算依赖不属于目录包含，应保留独立入口；不要将PCA应用于词嵌入写成词嵌入包含PCA。"
            "只补缺失边；不创建实体、正文或来源。已有边无需再提。",
            {**context, "prior_findings": feedback}, OrganizationProposal, check=valid, use_pro_model=True)
        diagnostics = [] if essential else None
        links, remaining = compile_proposal(proposal, nodes, supported, roots, existing, diagnostics=diagnostics)
        if diagnostics:
            binding = {'proposal_sha256':sha(canonical(proposal.model_dump())),
                'nodes':sorted(nodes), 'supported':{k:sorted(v) for k,v in supported.items()}, 'existing':sorted(existing)}
            value = {'policy':'optional-navigation-normalization-v1', 'skipped':diagnostics,
                'accepted_links':len(links), 'network_call':False}
            if getattr(calls, 'journal', None):
                calls.journal.save('organization.normalizations.' + sha(canonical(binding)), binding, value)
            else:
                data = canonical({'binding':binding, **value}).encode('utf-8')
                store.put(f'artifacts/organization-normalizations/{sha(data)}.json', data)

        if essential:
            reasons = [RootReason(note_id=i, reason="本轮未提出来源支持的上级连接，暂保留独立阅读入口") for i in remaining]
            return OrganizationPlan(links=links, independent_roots=reasons), OrganizationAssessment(
                passed=True, checked_root_ids=roots, checked_link_indexes=list(range(len(links))),
                independent_roots=reasons, review_mode="SINGLE_MODEL_PROPOSAL_AND_PROGRAM_VALIDATION")

        def checked(value, remaining=remaining, links=links):
            if set(value.checked_root_ids) != set(roots) or len(value.checked_root_ids) != len(roots) or set(value.checked_link_indexes) != set(range(len(links))) or len(value.checked_link_indexes) != len(links):
                raise QualityFailure("Organization assessment must check exact original roots and normalized proposed links")
            ids = [r.note_id for r in value.independent_roots]
            if set(ids) != set(remaining) or len(ids) != len(remaining):
                raise QualityFailure("Explain exactly the program-computed remaining_root_ids")
            if value.passed != (not value.issues):
                raise QualityFailure("Organization assessment status contradicts blockers")

        check = calls.run("next.organization_review",
            "独立核验阅读组织及遗漏。program_links的owner/方向已由程序依据证据归属转换，语义关系以canonical_proposal的parent/child为准。"
            "checked_root_ids覆盖全部原roots，checked_link_indexes只覆盖program_links零起始索引。"
            "independent_roots仅逐一解释程序给出的remaining_root_ids，不自行重新选择根；若发现语义问题，在issues指出。"
            "只报告阻断的错误或重要漏边，合理独立根可保留，不为凑树强连。",
            {**context, "canonical_proposal": proposal.model_dump(), "program_links": [l.model_dump() for l in links], "remaining_root_ids": remaining},
            OrganizationAssessment, check=checked, use_pro_model=True)
        if check.passed:
            return OrganizationPlan(links=links, independent_roots=check.independent_roots), check
        feedback = check.issues
    raise QualityFailure("Canonical organization review rejected: " + str(feedback))
