"""One Flash review/repair per section; one bounded Pro format recovery only."""

import copy
import os

import httpx
from openai import APIError
from pydantic import Field

from .contracts import Contract, canonical, sha
from .parallel import checked_parallel
from .resilience import readable, warning
from .section_contracts import AssemblyCheck, DraftPatch, SourceDraft
from .structured_quality import REVIEW_RULES, AssemblyAssessment, AtomicFinding

POLICY = 'section-flash-pro-v1'
CALL_ERRORS = (ValueError, TypeError, httpx.HTTPError, APIError, TimeoutError, ConnectionError)
PROVIDER_ERRORS = (httpx.HTTPError, APIError, TimeoutError, ConnectionError)


class SectionBody(Contract):
    markdown: str = Field(min_length=1)


def section_records(record):
    """Every source omission is checked once, near its related section/pages."""
    draft = SourceDraft.model_validate(record['draft'])
    units = record['inputs']['units']
    pages = {u['unit_id']: u.get('page') for u in units}
    owners = {s.key: {pages[u] for u, key in draft.assignments.items() if key == s.key}
              for s in draft.sections}
    omitted = {key: {} for key in owners}
    for unit, reason in draft.omissions.items():
        owner = next((key for key, values in owners.items() if pages.get(unit) in values),
                     next(iter(owners), None))
        if owner is not None:
            omitted[owner][unit] = reason
    if not draft.sections:
        item = copy.deepcopy(record)
        item['warnings'] = []
        item['inputs'].setdefault('image_paths', [])
        return [item]
    result = []
    for index, section in enumerate(draft.sections):
        assignments = {u: key for u, key in draft.assignments.items() if key == section.key}
        local = draft.model_copy(update={'sections': [section], 'assignments': assignments,
                                          'omissions': omitted[section.key]})
        ids = set(assignments) | set(local.omissions)
        selected = [u for u in units if u['unit_id'] in ids]
        selected_pages = {u.get('page') for u in selected}
        item = {**record, 'draft': local.model_dump(), 'warnings': [],
                'contexts': [c for c in record.get('contexts', []) if c['page'] in selected_pages],
                'inputs': {**record['inputs'], 'units': selected,
                           'image_paths': record['inputs'].get('image_paths', [])},
                'section_context': {'atomic_title': draft.title, 'summary': draft.summary,
                    'section_key': section.key, 'section_title': section.title,
                    'sibling_titles': [s.title for s in draft.sections],
                    'preceding_paragraph': draft.sections[index - 1].markdown.split('\n\n')[0]
                        if index else ''}}
        result.append(item)
    return result


def section_packet(pipeline, item):
    packet = pipeline.make_packet([item])
    context_pages = {c['page'] for c in item.get('contexts', [])}
    for assembly in packet['assembly']:
        assembly['source_units'] = [model_unit(u, context_pages) for u in assembly['source_units']]
    packet['section_context'] = item.get('section_context', {})
    packet['review_scope'] = (
        '只审查当前section及其实际来源。相邻段落、简介和其他小节标题仅作上下文；'
        '不可因其他小节正文未输入而报遗漏。候选引文必须来自article_section，不能复制来源错误冒充候选错误。'
        '所有意见在此一次返回；后续只修复一次，不复审。')
    from .review_packets import deduplicate_evidence
    return deduplicate_evidence(packet, compact_refs=True)


def review_one(pipeline, item):
    from .assembly_generation import (
        apply_source_patch,
        checked_source_draft,
        page_materials,
        render_draft,
    )
    from .repair_protocol import check_patch_scope, repair_packet
    from .segmentation import tokens

    generation = pipeline.g
    original = SourceDraft.model_validate(item['draft'])
    section_key = original.sections[0].key if original.sections else 'omissions'
    tag = sha(canonical([POLICY, section_key]))[:16]
    audit = {'policy': POLICY, 'section_key': section_key, 'review_calls_limit': 1,
             'repair_calls_limit': 1, 'pro_calls_limit': 1,
             'before_sha256': sha(canonical(item['draft'])), 'post_repair_semantic_review': False}
    packet, deduplicated = section_packet(pipeline, item)
    context_pages = {c['page'] for c in item.get('contexts', [])}
    audit['deduplicated_sources'] = deduplicated
    audit['review_input_tokens_estimate'] = tokens(canonical(packet))
    report = AssemblyAssessment(checked_plans=[item['key']], checked_omissions=list(original.omissions))
    try:
        report = generation.call(f"source.review.{item['key']}.{tag}", 'next.assembly_review',
            REVIEW_RULES + packet['review_scope'], packet, AssemblyAssessment,
            use_pro_model=False, max_attempts=1)
        audit['review_available'] = True
    except CALL_ERRORS as exc:
        audit['review_available'] = False
        warning(item, 'SECTION_REVIEW_UNAVAILABLE', {'error': type(exc).__name__, 'detail': str(exc)[:2000]})
    audit['review'] = report.model_dump()
    findings = [f for f in report.findings if f.plan_key == item['key']]
    for check in report.data_checks:
        if not check.passed and check.plan_key == item['key']:
            findings.append(AtomicFinding(plan_key=item['key'], section_key=section_key,
                category='CANDIDATE_ERROR', issue=check.recomputation,
                candidate_quote=check.candidate_quote, source_ids=check.source_ids))
    repairs = []
    seen = set()
    for finding in findings:
        warning(item, 'SECTION_REVIEW_FINDING', finding.model_dump())
        if finding.category != 'CANDIDATE_ERROR':
            continue
        issue = finding.model_dump()
        issue['section_key'] = section_key if original.sections else ''
        issue['source_units'] = [u for u in finding.source_ids
                                 if u in set(original.assignments) | set(original.omissions)]
        issue['candidate_quote_matched'] = bool(finding.candidate_quote and
                                                finding.candidate_quote in render_draft(item['draft']))
        identity = canonical(issue)
        if identity not in seen:
            repairs.append(issue)
            seen.add(identity)
    patch = None
    format_errors = []
    attempted = False
    repair_request = repair_packet(item, repairs) if repairs else None
    if repair_request:
        repair_request['source_units'] = [model_unit(u, context_pages) for u in repair_request['source_units']]
        repair_request['page_contexts'] = page_materials(item['contexts'])
    if repairs:
        attempted = True
        try:
            patch = generation.call(f"source.repair.{item['key']}.{tag}", 'next.section_repair',
                '对照当前section和实际来源处理本次意见，仅修复一次。审查可能误报；正文正确时可原样返回或sections=[]，'
                '不要求产生差异，不要求填写不修改理由。不要改其他小节、添加外部知识或删除有效配图。'
                '返回完整替换正文及必要的来源归属变更，不返回评审通过/失败。',
                repair_request, DraftPatch, use_pro_model=False, max_attempts=1)
        except PROVIDER_ERRORS as exc:
            warning(item, 'SECTION_REPAIR_UNAVAILABLE', {'error': type(exc).__name__, 'detail': str(exc)[:2000]})
        except (ValueError, TypeError) as exc:
            format_errors.append(f'{type(exc).__name__}: {str(exc)[:2000]}')
    selected = [u['unit_id'] for u in item['inputs']['units']]
    chosen = original
    try:
        if patch is not None:
            check_patch_scope(repair_request, patch)
            chosen = apply_source_patch(original, patch, selected, item['inputs']['image_paths'],
                figures=item['inputs'].get('figures', []), units=item['inputs']['units'],
                material_kind=pipeline.document_profile.get('kind'), content_checks=False)
        else:
            chosen = checked_source_draft(original, selected, item['inputs']['image_paths'], content_checks=False)
        if chosen.sections:
            readable('\n'.join(s.markdown for s in chosen.sections))
    except (ValueError, TypeError) as exc:
        format_errors.append(str(exc)[:2000])
        chosen = original
    audit['repair_attempted'] = attempted
    audit['format_errors'] = format_errors
    if format_errors:
        # Pro receives one local section, its source and the first-stage findings.
        # It is never a second semantic reviewer and never starts another loop.
        audit['pro_recovery_attempted'] = True
        recovery = {**item.get('section_context', {}), 'original_section': original.model_dump(),
                    'source_units': [model_unit(u, context_pages) for u in item['inputs']['units']],
                    'page_contexts': page_materials(item['contexts']),
                    'allowed_images': item['inputs']['image_paths'], 'review_findings': repairs,
                    'format_errors': format_errors,
                    'first_repair': patch.model_dump() if patch else None}
        try:
            body = generation.call(f"source.repair.{item['key']}.format-pro.{tag}",
                'next.section_format_recovery',
                '直接返回当前section的最终中文Markdown正文。判断一阶段意见是否误报，结合来源保留正确内容或定点修复；'
                '只解决当前小节，不扩写整篇，不输出审查报告。保留有效图片、公式和代码；使用允许的图片路径。'
                '这次输出之后不再进行LLM审查或语义修复。', recovery, SectionBody,
                use_pro_model=True, max_attempts=1)
            readable(body.markdown)
            replacement = original.sections[0].model_copy(update={'markdown': body.markdown}) if original.sections else None
            if replacement is None:
                raise ValueError('No existing section to bind the recovered body')
            recovered = original.model_copy(update={'sections': [replacement]})
            chosen = checked_source_draft(recovered, selected, item['inputs']['image_paths'], content_checks=False)
            audit['pro_recovery_used'] = True
        except CALL_ERRORS as exc:
            # Never publish malformed recovery output; retain the checked draft.
            chosen = checked_source_draft(original, selected, item['inputs']['image_paths'], content_checks=False)
            warning(item, 'SECTION_RECOVERY_FALLBACK', {'error': type(exc).__name__, 'detail': str(exc)[:2000]})
            audit['retained_original'] = True
    audit['after_sha256'] = sha(canonical(chosen.model_dump()))
    audit['unchanged'] = audit['before_sha256'] == audit['after_sha256']
    return chosen, report, item['warnings'], audit


def model_unit(unit, context_pages):
    """Keep source text and identities; omit geometry and duplicate vision payloads."""
    fields = {'unit_id', 'page', 'kind', 'text', 'asset_ref'}
    if unit.get('page') not in context_pages:
        fields.add('visual')
    return {key: value for key, value in unit.items() if key in fields}


def review_sections(pipeline, records):
    """Merge section-local decisions in source order; no repair-based deferral."""
    tasks = [(record, item) for record in records for item in section_records(record)]
    workers = max(1, min(4, int(os.getenv('NOODLE_NEXT_SECTION_REVIEW_WORKERS', '3'))))
    results = sorted(checked_parallel(lambda pair: review_one(pipeline, pair[1]), tasks, workers))
    assembled = {r['key']: [] for r in records}
    reports = AssemblyAssessment(checked_plans=[], checked_omissions=[])
    for index, (local, report, warnings, audit) in results:
        record, item = tasks[index]
        assembled[record['key']].extend(local.sections)
        if not record['draft'].get('summary') and local.summary:
            record['draft']['summary'] = local.summary
        if not record['draft'].get('entity_hints') and local.entity_hints:
            record['draft']['entity_hints'] = local.entity_hints.model_dump()
        ids = set(item['draft']['assignments']) | set(item['draft']['omissions'])
        for field in ('assignments', 'omissions'):
            record['draft'][field] = {u: v for u, v in record['draft'][field].items() if u not in ids}
            record['draft'][field].update(getattr(local, field))
        record.setdefault('warnings', []).extend(warnings)
        record.setdefault('section_review_receipts', []).append(audit)
        reports.findings.extend(report.findings)
        reports.data_checks.extend(report.data_checks)
    for record in records:
        record['draft']['sections'] = [s.model_dump() for s in assembled[record['key']]]
        record['content_review_mode'] = 'SECTION_FLASH_SINGLE_REVIEW'
        record['post_repair_semantic_review'] = False
        record['single_review_report'] = {'policy': POLICY,
                                         'sections': record.get('section_review_receipts', [])}
        reports.checked_plans.append(record['key'])
        reports.checked_omissions.extend(record['draft']['omissions'])
    final = AssemblyCheck(passed=True, checked_plans=reports.checked_plans,
                          checked_omissions=list(dict.fromkeys(reports.checked_omissions)))
    return records, final, sha(canonical(pipeline.make_packet(records)))
