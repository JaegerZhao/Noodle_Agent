"""Key figures are versioned evidence, independent of text contribution."""

import re

from pydantic import Field
from sqlalchemy import bindparam, text

from ..contracts import Contract, Text, canonical, sha
from ..planning import QualityFailure
from ..publication_assets import page_assets
from .entity_writing import IMAGE, asset_packet

POLICY = 'concept-key-figures-v1'


class SelectedFigure(Contract):
    image_id: str
    caption: Text
    after_heading: str = 'CORE_START'
    reason: Text
    replaces_image_id: str | None = None


class MediaPlan(Contract):
    selected: list[SelectedFigure] = Field(default_factory=list)


def candidates(store, fragments):
    blocked = page_assets(store)
    values = {k: v for k, v in asset_packet(store, fragments).items()
              if v['asset_ref'].removeprefix('../') not in blocked}
    by_id = {f['fragment_id']: f for f in fragments}
    for value in values.values():
        value['asset_sha256'] = value['asset_ref'].rsplit('/', 1)[-1].split('.')[0]
        value['context'] = []
        for identity in value['fragment_ids']:
            fragment = by_id[identity]
            at = fragment['text'].find(value['asset_ref'])
            value['context'].append(fragment['text'][max(0, at-350):at+len(value['asset_ref'])+450])
    evidence = sorted({identity for v in values.values() for identity in v['evidence_ids']})
    with store.db.engine.connect() as c:
        rows = c.execute(text('SELECT id,source_revision_id,page_number FROM evidence_anchors WHERE id IN :ids')
                         .bindparams(bindparam('ids', expanding=True)), {'ids': evidence}).mappings() if evidence else []
        anchors = {r['id']: dict(r) for r in rows}
    for value in values.values():
        value['sources'] = [anchors[i] for i in value['evidence_ids'] if i in anchors]
    return values


def records(markdown, assets):
    actual = {ref for _, ref in IMAGE.findall(markdown)}
    return [{**{k: v for k, v in value.items() if k != 'context'}, 'media_id': key}
            for key, value in assets.items() if value['asset_ref'] in actual]


def maintain(store, calls, title, markdown, fragments, previous=None, *, force=False, requested='AUTO'):
    assets = candidates(store, fragments)
    signature = sha(canonical([POLICY, markdown, assets, requested]))
    old = previous or {}
    receipt = {'policy': POLICY, 'candidate_sha256': signature, 'called': False,
               'selected': records(markdown, assets)}
    actual = {ref for _, ref in IMAGE.findall(markdown)}
    if not assets or requested == 'NONE' and not force or not force and (
            old.get('candidate_sha256') == signature or actual and requested not in {'KEY_FIGURE', 'BETTER_FIGURE'}):
        return markdown, receipt
    headings = re.findall(r'^#{1,6}\s+(.+)$', markdown, re.MULTILINE)
    # IDs remain in the local receipt. Repeating every anchor UUID in the model
    # prompt adds no information about what a figure explains.
    figures = {}
    for key, value in assets.items():
        pages = {}
        for source in value['sources']:
            pages.setdefault(source['source_revision_id'], set()).add(source['page_number'])
        figures[key] = {'caption': value['caption'],
            'context': list(dict.fromkeys(value['context'])),
            'source_pages': {source: sorted(p for p in numbers if p is not None)
                             for source, numbers in pages.items()},
            'already_used': value['asset_ref'] in actual}
    payload = {'title': title, 'core': markdown, 'headings': ['CORE_START', *headings], 'figures': figures}

    def check(value):
        keys = [s.image_id for s in value.selected]
        if len(set(keys)) != len(keys) or not set(keys) <= set(assets):
            raise ValueError('Select only distinct registered image IDs')
        replacements=[s.replaces_image_id for s in value.selected if s.replaces_image_id]
        if len(set(replacements))!=len(replacements) or any(s.replaces_image_id and (
                s.replaces_image_id not in assets or s.replaces_image_id==s.image_id
                or assets[s.replaces_image_id]['asset_ref'] not in actual) for s in value.selected):
            raise ValueError('Replacement must identify one distinct existing registered figure')

    try:
        plan = calls.run('next.concept_media',
            '为现有浓缩Concept选择真正帮助理解的关键图，不改写正文。只用给定figures和其来源描述，不能猜测未提供的图内容。'
            '优先模型架构、机制、流程、关键几何；长算例逐步截图、全文页面、装饰图和重复图不采用。'
            '通常少量图即可，不强制配图或固定数量。selected仅列采用的image_id、简洁中文caption、给定after_heading及reason。'
            '没有合适图则selected为空。文字重复不代表图片无价值；优先保留既有适用图。不要增加事实正文或第二轮审查。',
            {**payload,'requested':requested,
             'replacement_policy':'Only replace an already_used figure when a selected figure is clearly better; set replaces_image_id explicitly, otherwise leave null.'},
            MediaPlan, check=check, use_pro_model=False, max_attempts=1)
    except QualityFailure as exc:
        return markdown, {**receipt, 'called': True, 'warning': str(exc), 'status': 'RETAINED_VALID_CORE'}
    additions = {}
    replaced={assets[s.replaces_image_id]['asset_ref'] for s in plan.selected if s.replaces_image_id}
    if replaced:
        markdown=IMAGE.sub(lambda match:'' if match[2] in replaced else match[0],markdown)
        actual-=replaced
    for chosen in plan.selected:
        image = assets[chosen.image_id]
        if image['asset_ref'] in actual:
            continue
        anchor = chosen.after_heading if chosen.after_heading in headings else 'CORE_START'
        caption = re.sub(r'\s+', ' ', chosen.caption).replace('[', r'\[').replace(']', r'\]')
        additions.setdefault(anchor, []).append(f"![{caption}]({image['asset_ref']})")
    lines = markdown.splitlines()
    if additions.get('CORE_START'):
        # After the opening definition, rather than before the first sentence.
        split = next((i for i, line in enumerate(lines) if i and not line.strip()), len(lines))
        lines[split:split] = ['', *additions.pop('CORE_START'), '']
    output = []
    for line in lines:
        output.append(line)
        match = re.match(r'^#{1,6}\s+(.+)$', line)
        if match and match[1] in additions:
            output.extend(['', *additions.pop(match[1]), ''])
    result = '\n'.join(output)
    return result, {**receipt, 'called': True, 'selected': records(result, assets),
                    'decisions': plan.model_dump(), 'text_rewritten': False}
