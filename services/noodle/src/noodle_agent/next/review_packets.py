"""Lossless evidence partitions for one logical Atomic check, without rechecks."""

import copy

from .contracts import canonical, sha
from .planning import QualityFailure
from .segmentation import tokens

POLICY = 'atomic-review-packets-v1'


def deduplicate_evidence(original, *, compact_refs=False):
    packet = copy.deepcopy(original)
    sources = {u['unit_id']: u['text'] for a in packet['assembly'] for u in a['source_units']}
    deduplicated = []
    for page in packet.get('source_pages', []):
        for field, values in page['material'].items():
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                if not isinstance(value, str) or not value.strip():
                    continue
                owner = next((key for key, text in sources.items() if value.strip() in text), None)
                if owner:
                    values[index] = {'duplicate_source_unit': owner, 'text_sha256': sha(value)}
                    if compact_refs:
                        values[index].pop('text_sha256')
                    deduplicated.append({'page': page['page'], 'field': field, 'index': index,
                        'unit_id': owner, 'text_sha256': sha(value)})
    return packet, deduplicated


def review_packets(original, budget=24000):
    packet, deduplicated = deduplicate_evidence(original)
    # Common case stays exactly one call. Keep the complete candidate as context
    # for each evidence partition, so symbols/conditions can be compared globally.
    receipt = {'policy': POLICY, 'input_sha256': sha(canonical(original)),
        'original_tokens': tokens(canonical(original)), 'deduplicated': deduplicated}
    if tokens(canonical(packet)) <= budget:
        packets = [packet]
    else:
        base = copy.deepcopy(packet)
        atoms = []
        for i, assembly in enumerate(base['assembly']):
            atoms.extend(('unit', i, u) for u in assembly.pop('source_units'))
            assembly['source_units'] = []
        atoms.extend(('page', None, p) for p in base.pop('source_pages', []))
        base['source_pages'] = []
        base['review_scope'] = ('这是一次逻辑审查的证据分区。完整候选仅作上下文；只检查本包实际来源支持的内容和省略项，'
            '其他分区未出现的来源不属于缺失证据，不重复审查其结论。duplicate_source_unit表示同一原文已在来源单元中提供。'
            'continuation字段为同一来源的无损连续片段，不是多个Atomic。所有分区汇总后仅修复一次，不复审。')
        if tokens(canonical(base)) > budget - 1000:
            # This value is a cost/partition target, not the provider's context
            # limit. Retain the complete candidate and partition its evidence.
            budget = tokens(canonical(base)) + 2000
            receipt['expanded_for_complete_candidate'] = True
            receipt['effective_partition_target'] = budget

        def append(target, atom):
            kind, index, value = atom
            (target['assembly'][index]['source_units'] if kind == 'unit' else target['source_pages']).append(value)

        def split(atom):
            kind, index, value = atom
            # A source unit or one OCR list may itself be large. Split its text
            # deterministically; offsets/hash permit exact reconstruction.
            field = 'text' if kind == 'unit' else None
            if field:
                text = value[field]
            else:
                # One page is split by material field/list item first, never by
                # dropping its last observations or truncating a formula.
                material = value['material']
                entries = [(key, v) for key, values in material.items() for v in (values if isinstance(values, list) else [values])]
                if len(entries) > 1:
                    return [('page', None, {**value, 'material': {key: [v] if isinstance(material[key], list) else v}}) for key, v in entries]
                if not entries or not isinstance(entries[0][1], str):
                    raise QualityFailure('Unpartitionable review evidence metadata')
                field, text = entries[0]
            if len(text) < 2:
                raise QualityFailure('Review evidence cannot fit with candidate context')
            midpoint = len(text) // 2
            start = value.get('continuation', {}).get('start', 0)
            digest = value.get('continuation', {}).get('sha256', sha(text))
            result = []
            for offset, piece in [(0, text[:midpoint]), (midpoint, text[midpoint:])]:
                part = copy.deepcopy(value)
                part['continuation'] = {'start': start + offset, 'end': start + offset + len(piece), 'sha256': digest}
                if kind == 'unit':
                    part[field] = piece
                else:
                    part['material'] = {field: [piece] if isinstance(value['material'][field], list) else piece}
                result.append((kind, index, part))
            return result

        packets, current = [], copy.deepcopy(base)
        pending = list(reversed(atoms))
        occupied = False
        while pending:
            atom = pending.pop()
            trial = copy.deepcopy(current)
            append(trial, atom)
            if tokens(canonical(trial)) <= budget - 100:
                current, occupied = trial, True
            elif occupied:
                packets.append(current)
                current, occupied = copy.deepcopy(base), False
                pending.append(atom)
            else:
                pending.extend(reversed(split(atom)))
        if occupied:
            packets.append(current)
        if not packets:
            raise QualityFailure('No evidence in oversized review packet')
        for i, value in enumerate(packets):
            value['partition'] = {'index': i, 'total': len(packets)}
            for assembly in value['assembly']:
                present = {u['unit_id'] for u in assembly['source_units']}
                assembly['proposed_omissions'] = {u: reason for u, reason in assembly['proposed_omissions'].items() if u in present}
    receipt['packets'] = [{'sha256': sha(canonical(p)), 'tokens': tokens(canonical(p))} for p in packets]
    receipt['partitions'] = len(packets)
    return packets, receipt
