"""Independent adjudication for heuristic semantic guards, never hard code checks."""

from typing import Literal

from pydantic import Field

from .contracts import Contract, Text


class GuardVerdict(Contract):
    status: Literal["CONFIRMED", "FALSE_POSITIVE"]
    reason: Text


class GuardCheck(Contract):
    findings: dict[str, GuardVerdict] = Field(default_factory=dict)


GUARD_REVIEW_INSTRUCTION = (
    "独立核对启发式语义规则是否真正命中当前正文错误，不改正文。CONFIRMED表示错误断言仍无条件存在或前后矛盾；"
    "FALSE_POSITIVE用于正文已正确限定条件、明确否定错误、清楚标明引用原文说法存在不一致，或指控没有实际依据。"
    "不能仅因末尾追加说明就放行前文无条件错误。依据实际正文与冻结来源，不凭规则结论或外部记忆。"
    "审查意见不是事实：若它与实际来源公式相反，或只是要求确认已经一致的计算，应判FALSE_POSITIVE。"
    "由给定公式直接得到的基础代数解释不需原文逐字复述；不同统计/符号口径已并列说明时不强迫改成另一种熟悉公式。"
    "指控中的定位引文或source_ids也可能写错；必须在实际markdown和所给真实fragments/evidence中找到错误才可CONFIRMED，不能把指控当正文。"
    "候选已注明原资料错误并正确解释时，不能再次以原资料有错要求修复Atomic或Concept。"
    "来源仅作概略说明而候选忠实概括时，要求补齐外部定义、选择/合并准则或声明资料不完整属于FALSE_POSITIVE。"
    "每个F编号返回status与具体reason；只有实际正文与来源能证实的实质错误才CONFIRMED，证据不足判FALSE_POSITIVE。硬结构或代码检查不得豁免。"
)


def adjudicate_chapter(store, calls, title, markdown, fragments, problems):
    from .contracts import sha
    from .planning import QualityFailure
    from .quality_repair import evidence_packet

    cases = {"F" + str(i): {"issue": issue} for i, issue in enumerate(problems)}
    evidence = evidence_packet(store, [e for f in fragments for e in f["evidence_ids"]])

    def complete(value):
        if set(value.findings) != set(cases):
            raise QualityFailure("Chapter guard adjudication did not cover every issue")

    checked = calls.run(
        "next.local_finding_review",
        GUARD_REVIEW_INSTRUCTION,
        {"cases": cases, "title": title, "markdown": markdown, "fragments": fragments, "evidence": evidence},
        GuardCheck,
        check=complete,
        use_pro_model=True,
    )
    unresolved = [cases[k]["issue"] for k, v in checked.findings.items() if v.status == "CONFIRMED"]
    adjudications = [
        {
            "issue": cases[k]["issue"],
            "reason": v.reason,
            "review_operation": "next.local_finding_review",
        }
        for k, v in checked.findings.items()
        if v.status == "FALSE_POSITIVE"
    ]
    return unresolved, adjudications, sha(markdown)
