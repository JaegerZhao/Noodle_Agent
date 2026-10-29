"""Content eligibility before topic planning; exclusions remain in coverage receipts."""

from .generation_contracts import Exclusion

POLICY_VERSION = "knowledge-content-v1"
SELECTION_POLICY = """
先判断内容是否值得写入技术知识笔记，再划分主题。每个单元必须有明确去向，排除不是遗漏。
ADMINISTRATIVE：版权、许可、ISBN/出版编目、印刷地点、献词、致谢名单、作者履历、
版本/印次与勘误清单、课程评分与提交日期、仅说明本书代码仓库/网站入口/复现链接的内容。
NAVIGATION：封面书名、目录、页眉页脚页码、章节名称列表、参考文献条目和索引（仅作引用导航）；
配套资源说明不能因为提到 Python、神经网络或其他技术词就成为知识主题。
封面图的介绍与印刷错误说明属于资料说明，不单独构建模型知识笔记。
TOPIC：有实际解释的定义、原理、公式及条件、算法步骤、技术比较、例子、实验与限制。
不能仅因位置在前言、引言、附录或参考文献附近而排除；其中实质教学内容必须保留。
不能按关键词整段删除：例如讨论版权的技术方法、软件许可证比较本身可能就是正文知识。
混合单元若含实质教学内容应纳入主题，但正文只讲知识，不复述夹带的致谢/出版信息。
文献条目不独立成笔记；支持正文论断的引文、必要出处随主题保留。练习中的问题/条件可保留，
不得编造答案。DECORATIVE 仅允许已由视觉判断为装饰的图片，不能将教学图当装饰。
排除需给出与原内容对应的具体理由。不要为凑全覆盖而改写非教学资料。
""".strip()


def filter_layout(units):
    """Only parser-labelled layout is deterministic; semantic/mixed text goes to review."""
    retained, excluded = [], []
    for unit in units:
        if unit.kind.upper() in {"HEADER", "FOOTER", "PAGE_NUMBER"}:
            excluded.append(
                Exclusion(
                    unit_id=unit.unit_id,
                    category="NAVIGATION",
                    reason=f"解析器标注为 {unit.kind}，属于版面导航，不生成独立知识笔记。",
                )
            )
        else:
            retained.append(unit)
    return retained, excluded


def section_contexts(units):
    """Headings are hints, never blanket exclusions of an entire preface/appendix."""
    heading = ""
    result = {}
    for unit in units:
        if unit.kind.upper() in {"HEADING", "TITLE"}:
            heading = unit.text[:300]
        result[unit.unit_id] = heading
    return result
