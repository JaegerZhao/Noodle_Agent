"""Reading labels are projections; source ranges and frozen revisions stay intact."""

import json
import re
from functools import lru_cache
from pathlib import Path

from .contracts import sha
from .repository import Conflict, one

# Only the framework's repeated-parent TOC range label needs the compatibility
# projection. A long scientific title alone is not grounds to shorten it.
_NUMBER = r'(?:\d+|[A-Z])(?:\.\d+)+'
_TOC_RANGE = re.compile(r'[:：]\s*' + _NUMBER + r'\s+.+?\s+[—–]\s*' + _NUMBER + r'\s+')
_ADVANCED = re.compile(r'\*(?=\s*(?:[:：]|$))')


def preserve_advanced(title, source_title):
    return title.rstrip() + ' *' if _ADVANCED.search(source_title) and not _ADVANCED.search(title) else title


def reading_title(plan):
    """Use an already generated topic label for old concatenated range titles."""
    title = plan['title']
    topic = plan.get('concept_outline', {}).get('internal_topic', '').strip()
    if not _TOC_RANGE.search(title) or not topic or _TOC_RANGE.search(topic):
        return title
    concept = plan.get('concept_outline', {}).get('concept_title', '').strip()
    pages = plan.get('pages', [])
    if concept and pages and (len(topic) > 28 or '；' in title.split('：', 1)[0]):
        # A merged range has no single section number. Keep the known object and
        # exact source span rather than inventing an abbreviated technical claim.
        span = str(min(pages)) if min(pages) == max(pages) else f'{min(pages)}–{max(pages)}'
        return preserve_advanced(f'{concept}（第{span}页）', title)
    # Retain one original section number, every topic qualifier and the star.
    number = re.match(r'^(?:\d+(?:\.\d+)*|[A-Z](?:\.\d+)*)\s+', title)
    return preserve_advanced((number[0] if number else '') + topic, title)


@lru_cache(maxsize=32)
def _frozen_titles(root, ref, digest):
    data = (Path(root) / ref).read_bytes()
    if sha(data) != digest:
        raise Conflict('Frozen title plan hash mismatch')
    plan = json.loads(data)
    return {p['key']: (p['title'], reading_title(p)) for p in plan.get('plans', [])}


def present_titles(store, notes):
    """Add display_title at the API boundary, never mutate stored note metadata."""
    titles = {}
    result = []
    for note in notes:
        key = (note.get('creation_key') or '').split(':')
        display = note['title']
        if note.get('kind') == 'ATOMIC' and len(key) == 4 and key[0] == 'atomic' and key[2] == 'source-section':
            revision = key[1]
            if revision not in titles:
                with store.db.engine.connect() as c:
                    receipt = one(c, "SELECT cp.artifact_ref,cp.artifact_sha256 FROM jobs j JOIN job_checkpoints cp ON cp.job_id=j.id WHERE j.kind='GENERATE_ATOMICS' AND json_extract(j.input_json,'$.revision_id')=:r AND cp.stage LIKE 'source.sections.plan.%' AND cp.state='DONE' ORDER BY j.created_at DESC,cp.created_at DESC LIMIT 1", r=revision)
                titles[revision] = _frozen_titles(str(store.root), receipt['artifact_ref'], receipt['artifact_sha256']) if receipt else {}
            original, projected = titles[revision].get(key[3], (None, display))
            # A title explicitly changed after generation keeps its own wording.
            if display == original:
                display = projected
        result.append({**note, 'display_title': display})
    return result
