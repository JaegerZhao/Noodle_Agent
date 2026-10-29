"""Model routing affects cost, never publication eligibility or review count."""

import os
import re

POLICY = 'balanced-cost-v1'
PLANNING_PRO = {'next.structure', 'next.structure_exclusion_review', 'next.plan_labels',
                'next.entity_plan', 'next.concept_decide'}
REVIEWS = {'next.assembly_review', 'next.entity_review', 'next.local_finding_review'}
COMPACT_REVIEW = (
    '\n采用紧凑审查输出：仍完整检查本包，正常通过时只返回Schema要求的覆盖ID，findings和data_checks可为空。'
    '只输出实质错误、重要遗漏或真实来源矛盾；不输出通过项的逐条解释、文风建议或ADVISORY。'
    '错误保留精确定位、必要原文证据和简短原因；需要复算的错误保留实际计算，不能为缩短输出漏掉问题。'
)


def enabled():
    return os.getenv('NOODLE_NEXT_COST_POLICY', POLICY) == POLICY


def model_mode():
    """Use Pro for user-selected planning/identity decisions, Flash for content work."""
    mode = os.getenv('NOODLE_NEXT_PRO_USAGE', 'planning-pro')
    if mode not in {'planning-pro', 'disabled', 'paper-plan-only', 'selective'}:
        raise ValueError('NOODLE_NEXT_PRO_USAGE must be planning-pro, disabled, paper-plan-only or selective')
    return mode


def pro_enabled():
    return model_mode() != 'disabled'


def candidate_text(content):
    """Judge the candidate, not repeated source packets or JSON schema prose."""
    if not isinstance(content, dict):
        return str(content)
    if 'assembly' in content:
        return '\n'.join(a.get('article_section', '') for a in content['assembly'])
    chapters = content.get('chapters', [])
    if isinstance(chapters, list) and chapters:
        texts=[]
        for chapter in chapters:
            current=chapter.get('markdown') or chapter.get('previous') or ''
            old=(chapter.get('accepted_core') or {}).get('markdown')
            if old:
                # The previous core has a valid proof and unchanged support.
                # Route by changed paragraphs, while the checker still receives
                # the complete candidate and necessary source context.
                before={p.strip() for p in re.split(r'\n\s*\n',old)}
                current='\n\n'.join(p for p in re.split(r'\n\s*\n',current) if p.strip() not in before)
            texts.append(current)
        return '\n'.join(texts)
    return str(content.get('markdown', ''))


def complex_reasoning(text):
    # A decimal, section number, code fence or one ordinary formula is not a
    # reason to buy Pro. Keep proofs and substantial symbolic reasoning on Pro.
    math = re.findall(r'\$\$(.*?)\$\$|(?<!\$)\$([^$\n]+)\$', text, re.DOTALL)
    math_text = '\n'.join(a or b for a, b in math)
    proof = (re.search(r'^#{1,6}[^\n]{0,45}(?:证明|推导|对偶|KKT|海森|雅可比|\b(?:proof|derivation|Hessian|Jacobian)\b)', text, re.IGNORECASE | re.MULTILINE)
             or re.search(r'(?:证明|推导)(?:与推导|过程|如下|步骤)?[：:]|KKT.{0,4}条件|拉格朗日.{0,4}函数|对偶.{0,4}优化|\b(?:proof|derivation)\s*:', text, re.IGNORECASE))
    symbolic = len(re.findall(r'\\(?:int|partial|nabla|prod|begin\{(?:align|gather|[bp]?matrix))', math_text))
    return bool((proof and len(math_text) >= 350) or (symbolic >= 5 and len(math_text) >= 900)
                or (text.count('$$') >= 20 and len(math_text) >= 2500))


def model_choice(operation, content, requested_pro):
    mode = model_mode()
    if operation == 'next.section_format_recovery' and mode != 'disabled':
        return True, 'section-format-recovery-pro'
    if mode == 'planning-pro' and operation in PLANNING_PRO:
        return True, 'planning-and-identity-pro'
    if mode == 'paper-plan-only' and operation == 'next.structure' and (content.get('material_profile') or {}).get('kind') == 'PAPER':
        return True, 'paper-object-planning'
    if mode != 'selective':
        return False, 'flash-only-policy'
    if not enabled():
        return requested_pro, 'legacy-request'
    if operation == 'next.local_finding_review':
        return requested_pro, 'located-dispute'
    if operation in REVIEWS:
        if content.get('prior_findings') or any(c.get('repair_findings') for c in content.get('chapters', []) if isinstance(c, dict)):
            return True, 'located-repair-review'
        pro = complex_reasoning(candidate_text(content))
        return pro, 'complex-reasoning' if pro else 'routine-single-review'
    if operation in {'next.plan_labels', 'next.structure_exclusion_review'}:
        # Naming and the existing one-time range confirmation do not require
        # mathematical reasoning. Keep their contracts and coverage checks.
        return False, 'routine-planning-support'
    if operation == 'next.structure' and (content.get('material_profile') or {}).get('kind') == 'PAPER':
        # Paper planning groups source topics; it does not verify the proofs.
        # Difficult candidate derivations still use the review route above.
        return False, 'paper-topic-planning'
    if operation == 'next.organization_plan':
        return False, 'navigation-organization'
    if operation == 'next.entity_plan':
        if content.get('source_context', {}).get('object_resolution_revision'):
            return True, 'bounded-object-resolution-revision'
        cards = content.get('atomics', [])
        outlines = {t.get('plan_key'): t.get('concept_outline') or {}
                    for t in content.get('source_context', {}).get('topics', [])}
        if cards and all((c.get('concept_outline') or outlines.get(c.get('plan_key'), {})).get('concept_key') for c in cards):
            return False, 'frozen-identity-outline'
    # Preserve other requested source planning and located content repairs.
    return requested_pro, 'requested-model'


def review_instruction(operation, instruction):
    return instruction + COMPACT_REVIEW if enabled() and operation in REVIEWS else instruction
