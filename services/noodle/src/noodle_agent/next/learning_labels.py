"""One chapter-level naming and identity outline, independent of execution timing."""

from collections import defaultdict

from pydantic import Field

from .contracts import Contract, Text, canonical, sha
from .segmentation import tokens


class Label(Contract):
    title_zh: Text
    concept_key: str = ''
    concept_title: str = ''
    internal_topic: str = ''


class Labels(Contract):
    document_title_zh: str = ''
    chapter_title_zh: str = ''
    titles: dict[str, Label] = Field(default_factory=dict)
    outline_titles: dict[str, str] = Field(default_factory=dict)


def localize(generation, plans, units, outline):
    if getattr(generation, 'profile', {}).get('content_policy') != 'concise-foundation-v1':
        return plans
    grouped = defaultdict(list)
    by_unit = {u.unit_id: u for u in units}
    for plan in plans:
        grouped[plan.get('chapter_key') or 'document'].append(plan)
    nodes = {n.node_id: n for n in outline.nodes}
    generation.display_titles = {}
    generation.document_display_title = ''
    for chapter, members in grouped.items():
        cards = [{'key': p['key'], 'source_title': p['title'],
                  'headings': [by_unit[i].text for i in p['unit_ids'] if by_unit[i].kind.upper() in {'TITLE', 'HEADING'}][:18],
                  'opening': '\n'.join(by_unit[i].text for i in p['unit_ids'][:3])[:600]}
                 for p in members]
        result = generation.call('source.labels.' + chapter, 'next.plan_labels',
            '为这一自然章/整份短资料一次性制定中文学习标题和稳定Concept归并提纲，不写正文。'
            'titles以输入key为键，每项title_zh使用中文为主，可保留SVM、PCA、Transformer等缩写。'
            '按真实内容概括本篇学习主题，纠正过宽目录标题。标题通常12–28个中文字，必要的科学限定可以保留，不机械截字。'
            '最多保留一层必要的原章节编号，来源星号*保留。不要将父标题、起始子节编号/标题、结束子节编号/标题拼成范围标题，也不要枚举整串目录。'
            '例如“3.3 Linear Gaussian systems *：3.3.1 Bayes rule for Gaussians — 3.3.3 Example: Inferring an unknown scalar”'
            '应概括为“3.3 高斯贝叶斯规则与标量推断 *”；另一篇向量估计与传感器融合应明确自己的主题。'
            '完整来源范围由source_title、页码和单元ID保留，阅读标题无需承担来源索引；原始英文名另存，不重复整句英文。'
            'concept_key是本来源内稳定的知识对象键，concept_title为简短中文对象名，internal_topic为该对象内部学习主题。'
            '同一方法的动机、数学式、硬/软形式、简短扩展、实现和算例优先共享concept_key；'
            '只在知识对象/核心问题独立时分开，不能每个Atomic自动建立一个Concept。'
            '例如硬间隔、软间隔、简短核SVM介绍共用support_vector_machine/支持向量机，分别作为内部主题。'
            '纯习题和行政前言可不给concept_key，不代作习题答案。不能给未生成部分编造知识。'
            'document_title_zh给整份来源资料一个准确简洁的中文名称，chapter_title_zh是本范围章标题的中文表达；'
            'outline_titles按给定目录ID翻译原始目录标题，保留编号与缩写。'
            '不改变key、顺序或来源范围。',
            {'source_title': getattr(generation, 'document_source_title', ''),
             'chapter': nodes[chapter].title if chapter in nodes else '', 'topics': cards,
             'outline': [{'id': n.node_id, 'title': n.title} for n in outline.nodes
                         if chapter in nodes and nodes[chapter].start <= n.start <= nodes[chapter].end or n.parent_id is None]}, Labels,
            use_pro_model=True, max_attempts=2)
        generation.display_titles.update({key: title for key, title in result.outline_titles.items() if key in nodes})
        generation.document_display_title = generation.document_display_title or result.document_title_zh
        if chapter in nodes and result.chapter_title_zh:
            generation.display_titles[chapter] = result.chapter_title_zh
        for p in members:
            value = result.titles.get(p['key'])
            p['source_title'] = p['title']
            if value:
                from .learning_titles import preserve_advanced, reading_title
                p['title'] = preserve_advanced(value.title_zh, p['source_title'])
                p['concept_outline'] = {**value.model_dump(exclude={'title_zh'}),
                                        'source_scope': chapter, 'policy': 'source-identity-outline-v1'}
                p['title'] = reading_title(p)
            if result.chapter_title_zh:
                p['chapter_display_title'] = result.chapter_title_zh
    return plans


def merge_short_tails(boundaries, units, maximum=7500):
    """Join a short same-parent tail; never cross chapters or exclude source units."""
    positions = {u.unit_id: i for i, u in enumerate(units)}
    result, costs = [], []
    for i, b in enumerate(boundaries.sections):
        end = positions[boundaries.sections[i+1].start_unit] if i+1 < len(boundaries.sections) else len(units)
        cost = sum(tokens(u.text) for u in units[positions[b.start_unit]:end])
        if result:
            old = result[-1]
            same = old.chapter_key == b.chapter_key and bool(b.chapter_key)
            siblings = old.topic_key == b.topic_key or old.parent_title == b.parent_title and bool(b.parent_title)
            if same and siblings and old.disposition == b.disposition == 'CONTENT' and min(cost, costs[-1]) < 700 and costs[-1]+cost <= maximum and not any(w in (old.title+' '+b.title).lower() for w in ['exercises', '习题']):
                result[-1] = old.model_copy(update={'title': old.title+'；'+b.title, 'topic_key': old.topic_key or 'merged_'+sha(canonical([old.start_unit,b.start_unit]))[:16]})
                costs[-1] += cost
                continue
        result.append(b)
        costs.append(cost)
    return boundaries.model_copy(update={'sections': result})
