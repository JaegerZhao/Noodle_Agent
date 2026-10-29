"""Resolve figure identities from immutable parser captions, without model calls.

Reading/export projections keep the published revision and its evidence untouched.
The same catalogue resolves figure: IDs before future drafts are published.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from .contracts import sha
from .markdown_checks import article_heading_projection
from .repository import Conflict, one, rows

POLICY = 'source-figure-bindings-v2'
FIGURE = re.compile(r'(Figure|Fig\.?|图|Table|Tab\.?|表)\s*\$?\s*(\d+(?:\s*\.\s*\d+)*)(?:\s*\(?([a-z])\)?(?=\s|[:：.,，。)]|$))?', re.IGNORECASE)
IMAGE = re.compile(r'!\[((?:\\.|[^\]\\])*)\]\(([^\s)]+)\)')
PANEL = re.compile(r'^\s*\(?([a-z])\)?\s*$', re.IGNORECASE)


def numbers(text):
    return [(number, panel) for _, number, panel in references(text)]


def references(text):
    return [('TABLE' if m[1].casefold() in {'table', 'tab.', 'tab', '表'} else 'FIGURE',
             re.sub(r'\s+', '', m[2]), (m[3] or '').lower()) for m in FIGURE.finditer(text)]


def catalogue(artifact):
    """Only adjacent, aligned, labelled panels can inherit a shared figure caption."""
    figures, pending = [], []
    for block in artifact.get('blocks', []):
        if not block.get('asset_ref'):
            pending = []
            continue
        native = block.get('metadata', {}).get('mineru', {})
        captions = [block.get('text', ''), *native.get('image_caption', []), *native.get('chart_caption', []), *native.get('table_caption', [])]
        caption = next((text for text in captions if re.match(r'^\s*(?:\([a-z]\)\s*\|\s*)?(?:Figure|Fig\.?|图|Table|Tab\.?|表)\s*\d', text, re.IGNORECASE)), '')
        panel = next((PANEL.fullmatch(text)[1].lower() for text in captions if PANEL.fullmatch(text)), '')
        ref = '../' + block['asset_ref'].removeprefix('../')
        item = {'asset_ref': ref, 'block_id': block['id'], 'page': block['page'], 'panel': panel}
        if not caption:
            pending = [*pending, (item, block.get('bbox'))] if panel else []
            continue
        identities = references(caption)
        if not identities:
            pending = []
            continue
        parts = []
        for previous, bbox in pending:
            current_bbox = block.get('bbox')
            if previous['page'] == block['page'] and bbox and current_bbox and abs(bbox[1] - current_bbox[1]) <= 30:
                parts.append(previous)
        parts.append(item)
        kind, number, _ = identities[0]
        figures.append({'figure_id': 'figure_' + sha('|'.join(p['block_id'] for p in parts))[:20],
                        'kind': kind, 'number': number, 'caption': caption, 'page': block['page'], 'panels': parts})
        pending = []
    return figures


@lru_cache(maxsize=8)
def frozen_catalogue(root, ref, digest):
    data = (Path(root) / ref).read_bytes()
    if sha(data) != digest:
        raise Conflict('Figure catalogue source hash mismatch')
    return catalogue(json.loads(data)['artifact'])


def source_catalogue(store, source_revision):
    with store.db.engine.connect() as c:
        receipt = one(c, "SELECT k.artifact_ref,k.artifact_sha256 FROM job_checkpoints k JOIN jobs j ON j.id=k.job_id WHERE j.kind='GENERATE_ATOMICS' AND json_extract(j.input_json,'$.revision_id')=:r AND k.stage='parse.normalized' AND k.state='DONE' ORDER BY k.created_at DESC LIMIT 1", r=source_revision)
    return frozen_catalogue(str(store.root), receipt['artifact_ref'], receipt['artifact_sha256']) if receipt else []


def writing_figures(store, source_revision, units, allowed):
    ids = {u.block_id for u in units}
    mentioned = {(kind, n) for u in units for kind, n, _ in references(u.text)}
    selected = [f for f in source_catalogue(store, source_revision) if (f.get('kind', 'FIGURE'), f['number']) in mentioned or any(p['block_id'] in ids for p in f['panels'])]
    # Referenced figures can float just before the source section in a book.
    for figure in selected:
        for panel in figure['panels']:
            if panel['asset_ref'] not in allowed:
                allowed.append(panel['asset_ref'])
    return selected


def bind_markdown(markdown, figures, *, evidence_text='', evidence_pages=(), diagnostics=None):
    by_number = {}
    by_id = {f['figure_id']: f for f in figures}
    by_path = {p['asset_ref']: f for f in figures for p in f['panels']}
    for figure in figures:
        by_number.setdefault((figure.get('kind', 'FIGURE'), figure['number']), []).append(figure)
    expected = {(kind, n) for kind, n, _ in references(evidence_text)}

    def replace(match):
        alt, path = match[1], match[2]
        selected, panel = None, ''
        if path.startswith('figure:'):
            target = path.split(':', 1)[1]
            selected = by_id.get(target) or by_path.get('../' + target.removeprefix('../'))
        explicit = references(alt)
        if len(explicit) == 1:
            kind, number, panel = explicit[0]
            candidates = by_number.get((kind, number), [])
            if len(candidates) == 1 and (not evidence_text or (kind, number) in expected):
                selected = candidates[0]
        current = by_path.get(path)
        if not current and not path.startswith('figure:'):
            return match[0]
        # For unnumbered alt text, only repair a demonstrably foreign-page image
        # when this section cites exactly one source figure. No semantic guessing.
        if not selected and current and current['page'] not in evidence_pages and len(expected) == 1:
            candidates = by_number.get(next(iter(expected)), [])
            if len(candidates) == 1:
                selected = candidates[0]
        if not selected:
            return match[0]
        if current and selected['figure_id'] == current['figure_id'] and not path.startswith('figure:'):
            return match[0]
        panels = [p for p in selected['panels'] if not panel or p['panel'] == panel]
        if not panels:
            return match[0]
        replacement = '\n\n'.join(f"![{alt}{'（' + p['panel'] + '）' if len(panels) > 1 else ''}]({p['asset_ref']})" for p in panels)
        if diagnostics is not None and replacement != match[0]:
            diagnostics.append({'policy': POLICY, 'figure_id': selected['figure_id'], 'number': selected['number'],
                                'from': path, 'to': [p['asset_ref'] for p in panels]})
        return replacement
    return IMAGE.sub(replace, markdown)


def retain_paper_figures(draft, figures, units, diagnostics):
    """Place parser-labelled academic assets by exact source ownership, before review.

    This does not invent prose, choose an image by appearance, or insert page contexts.
    Captions are immutable parser text; normal content review sees the complete result.
    """
    if not draft.sections:
        return draft
    draft = draft.model_copy(deep=True)
    units = [u.model_dump() if hasattr(u,'model_dump') else u for u in units]
    unit_by_id = {u['unit_id']: u for u in units}
    sections = list(draft.sections)
    known = {p['asset_ref'].removeprefix('../') for f in figures for p in f['panels']}
    seen = set()
    def once(match):
        path = match[2].removeprefix('../')
        if path not in known:
            return match[0]
        if path in seen:
            if diagnostics is not None:
                diagnostics.append({'policy':POLICY,'code':'PAPER_DUPLICATE_FIGURE_REMOVED','asset_ref':path})
            return ''
        seen.add(path)
        return match[0]
    sections = [s.model_copy(update={'markdown':IMAGE.sub(once,s.markdown)}) for s in sections]
    used = {path.removeprefix('../') for s in sections for _, path in IMAGE.findall(s.markdown)}
    for figure in figures:
        matching = [u for u in units if any(p['block_id'] == u['block_id'] for p in figure['panels'])]
        # Referenced floating figures remain available to the writer, but only
        # their primary source owner automatically inserts them.
        if not matching:
            continue
        owner = next((draft.assignments.get(u['unit_id']) for u in matching if draft.assignments.get(u['unit_id'])), None)
        if owner is None:
            cited = [draft.assignments[u] for u in draft.assignments if u in unit_by_id
                and (figure.get('kind', 'FIGURE'), figure['number']) in {(k,n) for k,n,_ in references(unit_by_id[u]['text'])}]
            owner = cited[0] if cited else sections[0].key
            # Keep figure evidence with its inserted caption, including an asset
            # the writer accidentally marked as an omission.
        for u in matching:
            if u['unit_id'] in draft.omissions:
                draft.omissions.pop(u['unit_id'])
                draft.assignments[u['unit_id']] = owner
        index = next(i for i,s in enumerate(sections) if s.key == owner)
        additions = []
        for panel in figure['panels']:
            path = panel['asset_ref']
            if path.removeprefix('../') in used:
                continue
            caption = figure['caption'].replace('[', '\\[').replace(']', '\\]').replace('\n', ' ')
            additions.append(f'![{caption}]({path})')
            used.add(path.removeprefix('../'))
            if diagnostics is not None:
                diagnostics.append({'policy':POLICY,'code':'PAPER_FIGURE_RETAINED','figure_id':figure['figure_id'], 'section_key':owner})
        if additions:
            sections[index] = sections[index].model_copy(update={'markdown':sections[index].markdown+'\n\n'+'\n\n'.join(additions)})
    return draft.model_copy(update={'sections':sections})


def display_projection(store, note):
    """Separate display field: raw Markdown/hash/section offsets remain verifiable."""
    if note['kind'] != 'ATOMIC':
        return note
    with store.db.engine.connect() as c:
        evidence = rows(c, "SELECT e.section_id,a.source_revision_id,a.page_number,a.quoted_text FROM section_evidence e JOIN evidence_anchors a ON a.id=e.evidence_id WHERE e.revision_id=:r", r=note['revision']['id'])
    sources = {e['source_revision_id'] for e in evidence}
    if len(sources) != 1:
        return note
    figures = source_catalogue(store, next(iter(sources)))
    changes, replacements = [], []
    for section in note['sections']:
        selected = [e for e in evidence if e['section_id'] == section['section_id']]
        start, end = section['start_offset'], section['end_offset']
        original = note['markdown'][start:end]
        display = bind_markdown(original, figures, evidence_text='\n'.join(e['quoted_text'] for e in selected if not e['quoted_text'].lstrip().startswith('{')),
                                evidence_pages={e['page_number'] for e in selected}, diagnostics=changes)
        replacements.append((start, end, display))
    body = note['markdown']
    for start, end, display in reversed(replacements):
        body = body[:start] + display + body[end:]
    structural_body = body
    without_duplicate = article_heading_projection(body)
    if without_duplicate != body:
        changes.append({'policy':'article-heading-projection-v1', 'kind':'REPEATED_CONTAINER_HEADING'})
        body = without_duplicate
    if not changes:
        return note
    omitted_headings = [note['sections'][0]['section_id']] if note['sections'] and any(c.get('kind') == 'REPEATED_CONTAINER_HEADING' for c in changes) else []
    return {**note, 'display_markdown': structural_body, 'display_article_markdown':body,
            'display_article_omitted_section_headings':omitted_headings, 'display_projection': {'policy': POLICY,
            'base_revision_id': note['revision']['id'], 'base_sha256': note['revision']['markdown_sha256'],
            'display_sha256': sha(structural_body), 'article_sha256':sha(body), 'changes': changes}}
