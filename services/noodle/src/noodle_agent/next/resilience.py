"""Local recovery boundaries; never turn missing evidence into successful content."""

import re

from .planning import QualityFailure


class SevereQualityFailure(QualityFailure):
    """Unreadable, wholly unrelated content or a grounded severe review finding."""


def enabled(value):
    return (getattr(value, "review_policy", None) or getattr(value, "profile", {}).get("review_policy")) == "essential-v3"


def transient_provider_failure(exc):
    import httpx
    from openai import APIConnectionError, APIStatusError

    return isinstance(exc,(httpx.TransportError,TimeoutError,ConnectionError,APIConnectionError)) or (
        isinstance(exc,httpx.HTTPStatusError) and (exc.response.status_code==429 or exc.response.status_code>=500)
    ) or (isinstance(exc,APIStatusError) and (exc.status_code==429 or exc.status_code>=500))


def readable(markdown):
    """Reject only clear empty/corrupt output, not brief notes or formula pages."""
    text = markdown.strip()
    if not text or not any(c.isalnum() for c in text):
        raise SevereQualityFailure("Empty or unreadable generated content")
    if text.count("\ufffd") > max(3, len(text) // 5) or len(re.findall(r"锟斤拷|ï¿½", text)) >= 3:
        raise SevereQualityFailure("Generated content is dominated by decoding corruption")


def warning(record, code, detail):
    record.setdefault("warnings", []).append({"code": code, "detail": detail})


def grounded_severe(finding, markdown, sources):
    from .structured_quality import validate_findings

    if finding.category != "CANDIDATE_ERROR" or finding.severity != "SEVERE":
        return False
    try:
        validate_findings([finding], markdown, sources)
    except QualityFailure:
        return False
    return True


REVIEW_POLICY = (
    "本阶段只做这一次内容审查。severity=SEVERE仅用于整体答非所问、内容大面积捏造或核心结论整体与实际来源相反等不可继续的问题，"
    "必须给当前候选和实际来源的具体依据；空白和明显乱码也属严重问题。局部算式、条件、解释、遗漏、示例代码等可定点修复的问题用REPAIRABLE。"
    "来源讲述粗略、表达偏好、来源外补充、审核定位或回显字段错误不属严重问题，不因这些问题中断。"
    "非严重候选问题交给一次修复，修复后只做来源/资源/格式程序验收，不再语义复审；不能为追求完整性增加审核轮次。"
)
