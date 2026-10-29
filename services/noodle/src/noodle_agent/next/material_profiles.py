"""Local PDF material assessment; container, source revision and genre stay separate."""

import re
from statistics import median

import fitz

POLICY = "adaptive-source-v1"
GUIDANCE = {
    "SLIDES": "课件按连续完整学习主题规划，续页、公式、训练、算例和实现属于内部内容；不要一页一篇。纯封面、版权、课程标识可排除，但同页摘要/知识内容必须保留。",
    "BOOK": "书籍按可信目录章/节作为处理范围，章是独立执行批次而非新来源。尽可能沿目录分块，只在主题完整性或证据预算需要时细分；不跨章混合，不代作习题答案。",
    "PAPER": "论文按完整论证归纳，不按原文章节逐节翻译。摘要、引言与相关背景并入方法主题开篇，不单独生成摘要或背景Atomic。方法架构、机制、复杂度和必要训练条件尽量组织为综合主题；实验结论、消融、适用边界可作为另一完整主题，短论文通常少量综合Atomic，复杂论文依独立贡献调整，不以固定篇数打回。小组件用段落/列表承接，减少目录小标题。保留关键公式、假设、实验条件、重要数值、作者结论范围以及全部学术图表；原表保留，正文提炼结论不重复抄表。摘要和引言的实质不能作为版权排除；参考文献作导航不独立包装为Atomic，实质附录保留。",
    "ARTICLE": "文章按自然标题与完整论证/教程问题组织，短文可以一篇，不把段落角色各建Atomic；保留论据、条件、代码和实例。",
    "BLOG": "博客教程按问题、前置条件、完整操作/代码案例组织，保留适用版本、作者主张和限制；导航广告不作为知识，不能删除实质引言。",
    "UNKNOWN": "类型未确定，按可信来源结构与完整连续主题规划；不因UNKNOWN中断，不固定篇数，不丢失实质内容。",
}


def assess_source(content, artifact, outline, override=None):
    with fitz.open(stream=content, filetype="pdf") as pdf:
        count = len(pdf)
        sampled = sorted({0, min(1, count - 1), min(2, count - 1), count // 2, max(0, count - 2), count - 1})
        texts = [pdf[i].get_text() for i in sampled]
        landscape = sum(pdf[i].rect.width > pdf[i].rect.height * 1.1 for i in sampled) / len(sampled)
    headings = [b["text"] for b in artifact["blocks"] if b["kind"].upper() in {"TITLE", "HEADING"}]
    opening = " ".join(texts[:3] + headings[:25]).casefold()
    all_heads = " ".join(headings).casefold()
    chapters = [n for n in outline.nodes if n.kind == "CHAPTER" and n.basis != "FALLBACK"]
    paper = bool(re.search(r"\babstract\b|摘要", opening) and re.search(r"\breferences\b|参考文献|\bexperiments?\b|\bresults?\b", " ".join(texts).casefold() + all_heads))
    if override:
        kind, confidence, reason = override, "HIGH", "Explicit material override"
        if kind not in GUIDANCE:
            raise ValueError("Unknown material override")
    elif count >= 80 and len(chapters) >= 2:
        kind, confidence, reason = "BOOK", "HIGH", "Long document with multiple real chapter anchors"
    elif paper and count <= 80 and landscape < .5:
        kind, confidence, reason = "PAPER", "HIGH", "Abstract and scholarly structure in a short portrait document"
    elif count >= 3 and landscape >= .6:
        kind, confidence, reason = "SLIDES", "HIGH", "Predominantly landscape presentation pages"
    elif count <= 80 and re.search(r"\bread(?:ing)? time\b|\bposted on\b|阅读时长", opening):
        kind, confidence, reason = "BLOG", "MEDIUM", "Article-style publication signals"
    elif any(n.kind != "DOCUMENT" for n in outline.nodes) or len(headings) >= 2:
        kind, confidence, reason = "ARTICLE", "MEDIUM", "Readable heading structure without stronger material signals"
    else:
        kind, confidence, reason = "UNKNOWN", "LOW", "Insufficient material signals; use generic complete topics"
    route = {"BOOK": "BOOK_TOC", "SLIDES": "SLIDES_TOPICS", "PAPER": "PAPER_ARGUMENT", "ARTICLE": "ARTICLE_TOPICS", "BLOG": "ARTICLE_TOPICS"}.get(kind, "GENERIC_TOPICS")
    return {"kind": kind, "confidence": confidence, "container": "PDF", "profile_policy": POLICY,
        "selected_route": route, "reason": reason, "planning_guidance": GUIDANCE[kind],
        "signals": {"page_count": count, "sample_pages": [i + 1 for i in sampled], "landscape_ratio": round(landscape, 3),
                    "median_sample_characters": median(map(len, texts)), "chapter_anchors": len(chapters), "paper_structure": paper},
        "layout_policy": POLICY, "writing_policy": "source-faithful-v1"}
