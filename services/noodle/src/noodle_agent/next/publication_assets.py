"""Evidence pages and publishable figures have independent, deterministic uses."""

import re
from functools import lru_cache

POLICY = 'figure-publication-v1'
IMAGE = re.compile(r'!\[[^\]]*\]\(([^\s)]+)\)')


def path_key(ref):
    return ref.removeprefix('../')


def page_assets(store):
    """Read only this library's recorded page contexts, never another run's vision."""
    from .repository import rows
    with store.db.engine.connect() as c:
        checkpoints = rows(c, "SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE stage LIKE 'page-context.%' AND state='DONE'")
    return set(_page_assets(str(store.root), tuple(sorted((r['artifact_ref'], r['artifact_sha256']) for r in checkpoints))))


@lru_cache(maxsize=8)
def _page_assets(root, receipts):
    import json
    from pathlib import Path

    from .contracts import sha
    from .repository import Conflict
    result = set()
    for ref, digest in receipts:
        data = (Path(root) / ref).read_bytes()
        if sha(data) != digest:
            raise Conflict('Page-context receipt hash differs')
        value = json.loads(data)
        if value.get('asset_ref'):
            result.add(path_key(value['asset_ref']))
    return frozenset(result)


def figure_paths(units, contexts):
    blocked = {path_key(c['asset_ref']) for c in contexts}
    result = []
    for unit in units:
        if unit.asset_ref and path_key(unit.asset_ref) not in blocked:
            visual = unit.visual or {}
            if visual.get('is_decorative') or visual.get('group_type') in {'SOURCE_PAGE', 'FULL_PAGE', 'TEXT_ONLY', 'CODE'}:
                continue
            ref = '../' + path_key(unit.asset_ref)
            if ref not in result:
                result.append(ref)
    return result


def without_images(markdown, blocked, diagnostics=None):
    def replace(match):
        if path_key(match[1]) not in blocked:
            return match[0]
        if diagnostics is not None:
            diagnostics.append({'code': 'CONTEXT_IMAGE_NOT_PUBLISHED', 'asset_ref': match[1], 'policy': POLICY})
        return ''
    return IMAGE.sub(replace, markdown)


def omit_unregistered_images(markdown, allowed, diagnostics=None):
    """Never invent a mapping for an unknown image; let the normal review see text only."""
    from .markdown_checks import without_fences
    approved = {path_key(ref) for ref in allowed}
    prose = without_fences(markdown)
    def replace(match):
        if path_key(match[1]) in approved:
            return match[0]
        if diagnostics is not None:
            diagnostics.append({'code':'UNREGISTERED_IMAGE_OMITTED', 'asset_ref':match[1],
                'policy':POLICY, 'reason':'No exact registered asset; no guessed replacement'})
        # Preserve the writer's caption as ordinary text for the existing
        # semantic check. Do not silently bind it to an unrelated real picture.
        return re.match(r'!\[([^\]]*)\]', match[0])[1]
    return ''.join(IMAGE.sub(replace, original) if visible.strip() else original
        for original, visible in zip(markdown.splitlines(keepends=True), prose.splitlines(keepends=True), strict=True))


def model_contexts(contexts):
    # Raw assets stay in immutable receipts; a text writer cannot publish them.
    import json
    fields = {'ocr_text', 'caption', 'factual_observations', 'spatial_relations', 'teaching_role', 'confidence', 'essential'}
    return [{'page': c['page'], 'evidence_ref': c.get('evidence_id'),
             'material': {k: v for k, v in json.loads(c['text']).items() if k in fields}}
            for c in contexts]
