"""Conservative, local visual routing; captions alone never replace image evidence."""

import json
import re
from importlib.resources import files

from .contracts import sha

PAGE_PROMPTS = {
    'source-page-evidence-v1': 'source_page_evidence.v1.md',
    'source-page-evidence-v2': 'source_page_evidence.v2.md',
}
UNCERTAIN = re.compile(
    r'无法(?:辨认|识别|确定|看清|读取)|不能辨认|看不清|不确定|难以辨认|不可辨认|'
    r'模糊不清|未能识别|\b(?:unreadable|illegible|uncertain|unresolved)\b|'
    r'\b(?:cannot|can.t|unable to)\s+(?:read|identify|determine|discern)\b', re.IGNORECASE,
)


def prompt_hashes():
    return {version: sha(files('noodle_agent').joinpath('prompts', name).read_text(encoding='utf-8'))
            for version, name in PAGE_PROMPTS.items()}


def page_prompt_version(context):
    return ('source-page-evidence-v2' if context.get('evidence_policy') == 'native-delta-v1'
            else 'source-page-evidence-v1')


def facts(visual):
    return [str(item) for key in ('ocr_text', 'factual_observations', 'spatial_relations')
            for item in visual.get(key, []) if str(item).strip()]


def uncertain(visual):
    text = '\n'.join(facts(visual) + [str(visual.get(k) or '') for k in ('caption', 'teaching_role')])
    return bool(visual.get('unresolved_regions') or visual.get('unresolved_asset_ids')
                or visual.get('evidence_complete') is False or UNCERTAIN.search(text))


def native_text(units):
    return '\n'.join(u.text for u in units if not u.asset_ref)


def complete_native_text(text):
    import ast

    if len(text.strip()) < 80 or '\ufffd' in text or '锟斤拷' in text:
        return False
    if text.count('{') != text.count('}'):
        return False
    # Validate code blocks separately: ordinary prose around code is not Python.
    code = re.compile(r'^\s*(?:import |from \w+.* import |def \w+\(|class \w+)', re.MULTILINE)
    snippets = re.findall(r'```(?:python|py)?\s*\n(.*?)```', text, re.DOTALL)
    for snippet in snippets or [text]:
        if code.search(snippet):
            try:
                ast.parse(snippet)
            except SyntaxError:
                return False
    return True


def asset_needs_vision(unit, native=''):
    if not unit.asset_ref:
        return False
    visual = getattr(unit, 'visual', None) or {}
    if uncertain(visual):
        return True
    if visual.get('is_decorative') and not visual.get('essential'):
        return False
    # Previously interpreted, independent text/code/formula images can reuse
    # their actual extraction. Unknown images and diagrams still need a page.
    return not (
        visual.get('group_type', 'SINGLE') in {'SINGLE', 'TEXT_ONLY', 'CODE'}
        and visual.get('asset_type') in {'TEXT', 'CODE', 'FORMULA', 'TABLE'}
        and visual.get('confidence', 0) >= .9 and visual.get('ocr_text')
        and all(re.sub(r'\s+', '', text) in re.sub(r'\s+', '', unit.text + '\n' + native)
                for text in visual['ocr_text'] if text.strip())
    )


def page_evidence_complete(context, units):
    """v2 may legitimately return no delta, but an empty page cannot count as covered."""
    try:
        detail = json.loads(context['text'])
        version = detail.get('prompt_version')
        if version not in PAGE_PROMPTS or detail.get('confidence', 0) < .9 or uncertain(detail):
            return False
        inputs = context.get('input', {})
        if inputs.get('image_sha256') and detail.get('image_sha256') != inputs['image_sha256']:
            return False
        if inputs.get('model') and detail.get('model') != inputs['model']:
            return False
        if version == 'source-page-evidence-v1':
            return bool(detail.get('ocr_text'))
        native = native_text(units)
        if inputs.get('native_sha256') != sha(native):
            return False
        return bool(facts(detail)) or complete_native_text(native)
    except (KeyError, TypeError, ValueError):
        return False
