"""Narrow regression guards for confirmed failure modes; never rewrite source text."""

import re

from .markdown_checks import without_fences


def critical_problems(title, markdown, summary=""):
    prose = without_fences(markdown)
    full = title + "\n" + prose
    issues = []
    if re.search(r"^(?:本轮|修复了|已修复|修订后|根据审查|本次修复)", summary.strip()):
        issues.append("简介必须介绍知识内容，不是修复、审查或编辑日志；按修订后正文重新概括。")
    if re.search(r"PCA|主成分", full, re.IGNORECASE):
        if any(
            re.search(r"(?:构造|寻找|是|为).{0,18}与原始特征正交", line)
            and not re.search(r"不是|并非|不能|不应|错误|原文称", line)
            for line in prose.splitlines()
        ):
            issues.append(
                "PCA 主成分方向相互正交，不是与原始特征轴全都正交。第一主成分为一个方向，不因原空间三维而变成平面。原资料错误不得无说明当成正确结论。"
            )
        if (
            re.search(r"u_?\{?1|u₁", prose)
            and "协方差" in prose
            and not re.search(r"中心化|去均值|已减均值|cent(?:er|re)", prose, re.IGNORECASE)
        ):
            issues.append(
                "给出 PCA 第一主成分优化式时明确中心化和单位向量约束、协方差矩阵最大特征值条件；不能说任意特征向量都最优。"
            )
        if (
            "transpose" in markdown
            and "fit_transform(tf_idf_vect)" in markdown
            and re.search(r"1078.{0,12}(?:降到|降至|减少到|到)\s*50", summary)
        ):
            issues.append(
                "这段代码输入已转置为词项行、推文列；PCA 的样本行是词项、特征列是推文。不能把原向量1078个词项当作这次PCA输入特征维度，更不能宣称由此将推文词项特征从1078降到50；components_也按实际推文列解释。"
            )
    if "simple_preprocess" in markdown:
        for line in prose.splitlines():
            if (
                "deacc" in line
                and re.search(r"(?:移除|去除|删除|去掉).{0,5}标点", line)
                and not re.search(r"不(?:是|能|应)|并非|原(?:文|注释).{0,10}(?:误|不准确)", line)
            ):
                issues.append(
                    "不能把 deacc=True 直接解释成去标点。保留课件调用和原注释时可注明它是原注释，正文只描述分词流程；无需添加无来源的参数扩展。"
                )
                break
        if "re.sub" in markdown and "[@#" in markdown:
            for line in prose.splitlines():
                if re.search(r"(?:所有|全部)标点", line) and not re.search(
                    r"并非|不是|不(?:是|能)|仅|部分|原文称|原文说", line
                ):
                    issues.append(
                        "示例正则只移除字符类列出的部分标点，不是所有标点；按实际代码描述，其他字符如连字符可能保留。"
                    )
                    break
    if re.search(r"SVD|奇异值", full, re.IGNORECASE):
        if any(
            re.search(r"V(?:\^|\^\{)?T.{0,180}文档与词项之间", line)
            and not re.search(r"不是|并非|不能|不应|错误", line)
            for line in prose.splitlines()
        ):
            issues.append(
                "V^T 的轴含义随输入方向变化；在词项×文档约定中关联潜在维度与文档，不应称为文档与词项矩阵。图页使用文档×词项时明确约定切换。"
            )
        if re.search(r"展示.{0,50}D\s*=.{0,60}截断形式", prose, re.DOTALL) and not re.search(
            r"一般|近似|approx", prose
        ):
            issues.append(
                "原图的等号应与一般截断近似区分；除非明确精确秩条件，否则截断重建不等于原矩阵。"
            )
    return list(dict.fromkeys(issues))


def source_findings(records):
    from .section_contracts import Finding

    result = []
    for record in records:
        draft = record["draft"]
        markdown = "\n\n".join(s["markdown"] for s in draft["sections"])
        for issue in critical_problems(draft["title"], markdown, draft.get("summary", "")):
            result.append(Finding(plan_key=record["key"], issue=issue))
    return result


def published_problems(store, note):
    """Honor only adjudications bound by this immutable publication's candidate hash."""
    import json

    from .contracts import Candidate, Review, sha
    issues = critical_problems(note["title"],note["markdown"],note["revision"].get("summary", ""))
    revision = note["revision"]["id"]
    from .repository import one
    with store.db.engine.connect() as c:
        proof = one(c, "SELECT storage_ref,sha256 FROM build_artifacts WHERE storage_ref=:r",r=f"artifacts/builds/{revision}.json")
    if not proof:
        return issues
    payload = json.loads(store.read(proof["storage_ref"],proof["sha256"]))
    candidate = Candidate.model_validate(payload["candidate"])
    review = Review.model_validate(payload["review"])
    if not review.passed or candidate.fingerprint != review.candidate_sha256 or sha(note["markdown"]) != payload["markdown_sha256"]:
        return issues
    waived = {w["issue"] for w in review.report.get("source_guard_adjudications",[]) if w.get("reason") and w.get("review_operation") == "next.local_finding_review"}
    sections = {s.section_id:s for s in candidate.sections}
    for identity,check in review.report.get("checks",{}).items():
        if identity in sections and check.get("guard_markdown_sha256") == sha(sections[identity].markdown):
            waived.update(w["issue"] for w in check.get("guard_adjudications",[]) if w.get("reason") and w.get("review_operation") == "next.local_finding_review")
    return [issue for issue in issues if issue not in waived]
