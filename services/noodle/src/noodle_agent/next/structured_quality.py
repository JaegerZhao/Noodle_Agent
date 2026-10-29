"""Exact chapter output keys and anchored, typed review findings."""

import re
from typing import Literal

from pydantic import Field, create_model

from .contracts import Contract, canonical, sha
from .planning import QualityFailure


class QualityFinding(Contract):
    category: Literal["CANDIDATE_ERROR", "SOURCE_ISSUE", "ADVISORY"]
    kind: Literal["ASSERTION", "OMISSION"] = "ASSERTION"
    issue: str = Field(min_length=1)
    candidate_quote: str = ""
    source_quote: str = ""
    source_ids: list[str] = Field(default_factory=list)
    severity: Literal["REPAIRABLE", "SEVERE"] = "REPAIRABLE"


class DataCheck(Contract):
    """Concise recomputation from actual data, rather than a fidelity verdict."""

    plan_key: str = ""
    section_key: str = ""
    candidate_quote: str = ""
    source_ids: list[str] = Field(default_factory=list)
    recomputation: str = Field(min_length=1)
    passed: bool


class ChapterAssessment(Contract):
    findings: list[QualityFinding] = Field(default_factory=list)
    supported_fragment_ids: list[str]
    checked_image_ids: list[str] = Field(default_factory=list)
    data_checks: list[DataCheck] = Field(default_factory=list)


class AtomicFinding(QualityFinding):
    plan_key: str
    section_key: str = ""


class AssemblyAssessment(Contract):
    checked_plans: list[str]
    checked_omissions: list[str]
    findings: list[AtomicFinding] = Field(default_factory=list)
    data_checks: list[DataCheck] = Field(default_factory=list)


class ChapterMap(Contract):
    def __getitem__(self, key):
        return getattr(self, key)

    def __iter__(self):
        return iter(type(self).model_fields)


def fixed_chapters(keys, value_type):
    keys = sorted(keys)
    tag = sha(canonical([keys, value_type.__name__]))[:12]
    mapping = create_model("ChapterKeys_" + tag, __base__=ChapterMap,
                           **{key: (value_type, ...) for key in keys})
    return create_model("ChapterPacket_" + tag, __base__=Contract, chapters=(mapping, ...))


def validate_findings(findings, markdown, sources):
    for finding in findings:
        if not set(finding.source_ids) <= set(sources):
            raise QualityFailure("Finding references unknown source IDs")
        if finding.category == "CANDIDATE_ERROR":
            if finding.kind == "ASSERTION" and (not finding.candidate_quote or finding.candidate_quote not in markdown):
                raise QualityFailure("Candidate assertion finding needs an exact quote in the current candidate")
            if finding.kind == "OMISSION" and not finding.source_ids:
                raise QualityFailure("Omission finding needs actual source support")
        if finding.category == "SOURCE_ISSUE" and (not finding.source_ids or not finding.source_quote or not any(finding.source_quote in sources[i] for i in finding.source_ids)):
            raise QualityFailure("Source issue needs an exact source quote, not a request to edit immutable fragments")


def finding_protocol_issues(findings, markdown, sources):
    """An inaccurate reviewer locator is a dispute, not invalid source content.

    Only essential-v2 callers that adjudicate every substantive accusation may
    use this nonblocking form. Publication evidence is validated separately.
    """
    result = []
    for index, finding in enumerate(findings):
        try:
            validate_findings([finding], markdown, sources)
        except QualityFailure as exc:
            result.append({"finding_index": index, "issue": finding.issue, "detail": str(exc)})
    return result


def essential_review(value):
    return getattr(value, "review_policy", None) in {"essential-v1", "essential-v2", "essential-v3"} or getattr(value, "profile", {}).get("review_policy") in {"essential-v1", "essential-v2", "essential-v3"}


def scientific_content(text):
    return bool(re.search(r"\$|```|\\(?:frac|sum)|\d+\.\d+", text))


def critical_thinking(agent, operation, pro, policy):
    """Spend reasoning only on correctness-sensitive Pro decisions."""
    return bool(pro and policy in {"essential-v2", "essential-v3"}
        and str(getattr(agent, "pro_model", "")).startswith(("deepseek-v4", "deepseek-flash"))
        and operation in {"next.assembly_review", "next.local_finding_review", "next.section_repair",
                          "next.entity_review", "next.entity_repair", "next.quality_triage", "probe.evaluate_cases",
                          "next.structure_exclusion_review"})


REVIEW_RULES = (
    "审查对象是当前候选正文。findings按CANDIDATE_ERROR/SOURCE_ISSUE/ADVISORY分类。"
    "CANDIDATE_ERROR只包含仍存在于候选的实质错误或重要遗漏；ASSERTION须提供当前正文逐字candidate_quote，OMISSION须给source_ids。"
    "SOURCE_ISSUE仅指给定不可变来源材料本身的问题，须source_ids与逐字source_quote；不得要求候选写作者修改不可变fragment。"
    "来源原始材料有矛盾而候选已准确说明时，不因此判候选错误。已修改正确的断言不再重复阻断。"
    "已确认计算一致、非阻断符号/文风建议放ADVISORY；不能写无阻断错误却分类为CANDIDATE_ERROR。"
    "仅阻断事实/科学错误、误导性表述或来源内的重要遗漏；标题、措辞、排版偏好、额外背景与可选扩展只作ADVISORY。"
    "来源仅作概略说明时，候选忠实概括即合格；不要求补齐来源未给出的完整定义、选择/合并准则、证明、示例或背景，也不要求为此额外声明资料不完整。"
    "不能把来源概略或缺少外部标准知识本身列为CANDIDATE_ERROR或SOURCE_ISSUE；表达偏好和可选补充仅作ADVISORY。"
    "逐项核对公式条件与自然语言、维度与数值是否自洽，忠实复制不自动等于科学正确；不凭外部记忆补来源没有的答案。"
    "先从底层表格、数据行和实际代码重新计算，再判断正文；禁止拿候选自己声称的计数当证据。"
    "有数值算例时在data_checks给出简短的原始数据→重新计数/计算→与候选比较，并给passed；不只写核对过或与原文一致。"
    "特别核对同一数据在表格、出现次数、加一后的分子、分母、概率、最终比较中的一致性；一个词重复几次应逐行数清。"
    "正文正确而审查自己算错时不得强迫正文改错；来源算式不一致时应并列实际数据和原文说法，明确不确定性。"
)
