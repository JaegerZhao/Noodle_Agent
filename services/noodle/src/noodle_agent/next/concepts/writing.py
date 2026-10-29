"""Write only one changed chapter; apply the P2 Markdown and math gates."""

import re

from ..contracts import canonical
from ..generation import Generation
from ..generation_contracts import Draft, DraftSection
from ..markdown_checks import section_headings
from ..planning import QualityFailure
from .contracts import SectionCheck, SectionDraft


def write_chapter(calls, key, title, question, scope, fragments, previous=None):
    if len(canonical(fragments)) > 36000:
        raise QualityFailure(
            "Complete chapter evidence exceeds context budget; no input was omitted"
        )
    issues = []
    for attempt in range(3):
        data = {
            "question": question,
            "scope": scope.model_dump(),
            "fragments": fragments,
            "old_reference": previous,
            "issues": issues,
        }
        draft = calls.run(
            "next.concept_write",
            "综合全部当前有效原子片段，围绕指定问题组织中文章节，不拼接笔记。旧正文只作差异参考，不提供事实支持；保留仍有支持的公式、条件和重要知识，移除失去支持的事实。不复述编辑过程，不新增来源没有的结论。不要把当前片段未展开的内容写成“本知识点不包含”“来源没有给出”等永久否定或范围说明，直接组织当前有支持的知识。数学用$或$$，不用代码反引号；正文不含H1/H2或HTML，图片只用输入已有相对路径。",
            data,
            SectionDraft,
        )
        assets = set(
            re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", "\n".join(f["text"] for f in fragments))
        )
        ids = {f["fragment_id"] for f in fragments}
        try:
            value = Draft(
                title=title,
                summary=question,
                sections=[
                    DraftSection(
                        key=key,
                        title=title,
                        question=question,
                        scope=scope,
                        markdown=section_headings(draft.markdown),
                        units=sorted(ids),
                    )
                ],
            )
            checked = Generation.checked_draft(value, ids, assets)
            markdown = checked.sections[0].markdown
            review = calls.run(
                "next.concept_review",
                "独立检查知识章节是否忠于全部有效片段。必须核对每个提供的fragment_id；若某片段不适合该章，报告归属问题并将passed设为false，不能以不在问题范围为由静默省略后仍称全部通过。保全重要知识、适用条件、公式和数值，区分图像角色；旧正文不作独立证据。不得把局部来源未展开的内容写成整个知识对象的永久缺失，不要求补写来源没有的知识。列出实际合理覆盖的全部fragment_id，仅在无实质错误时passed。",
                {**data, "markdown": markdown},
                SectionCheck,
            )
            if not review.passed or review.issues or set(review.supported_fragment_ids) != ids:
                raise QualityFailure(str(review.model_dump()))
            return markdown, {"writer_called": True, "review": review.model_dump()}
        except ValueError as exc:
            issues.append(str(exc))
    raise QualityFailure("Concept chapter failed review: " + str(issues))
