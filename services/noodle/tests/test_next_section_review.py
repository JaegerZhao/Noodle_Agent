"""Section isolation, one-shot recovery and unchanged valid drafts; no providers."""

import copy
from collections import Counter
from threading import Barrier, Lock

import pytest

from noodle_agent.next.assembly_generation import SourceSectionPipeline
from noodle_agent.next.cost_policy import model_choice
from noodle_agent.next.section_review import review_sections, section_records


def record():
    return {'key': 'atomic-a', 'draft': {'title': '分类树', 'summary': '分类树的两个部分。',
        'sections': [{'key': 'a', 'title': 'CART', 'markdown': 'CART的原始正确内容。'},
                     {'key': 'b', 'title': 'ID3', 'markdown': 'ID3的原始正确内容。'}],
        'assignments': {'u1': 'a', 'u2': 'b'}, 'omissions': {'u3': '纯页脚'}},
        'inputs': {'units': [{'unit_id': 'u1', 'text': 'CART来源。', 'page': 1},
                             {'unit_id': 'u2', 'text': 'ID3来源。', 'page': 2},
                             {'unit_id': 'u3', 'text': '页脚', 'page': 1}], 'image_paths': []},
        'contexts': [], 'evidence': {'u1': 'e1', 'u2': 'e2', 'u3': 'e3'}}


class Generation:
    def __init__(self, mode='unchanged', parallel=False):
        self.profile = {'checking_policy': 'single-check-v1'}
        self.mode, self.calls = mode, []
        self.lock = Lock()
        self.barrier = Barrier(2, timeout=5) if parallel else None

    def call(self, stage, operation, instruction, content, contract, **kwargs):
        assert kwargs['max_attempts'] == 1
        with self.lock:
            self.calls.append((operation, copy.deepcopy(content), kwargs))
        if operation == 'next.assembly_review':
            if self.barrier:
                self.barrier.wait()
            assembly = content['assembly'][0]
            section = assembly['section_index'][0]
            if self.mode == 'review-down':
                raise TimeoutError('offline fixture')
            result = {'checked_plans': ['atomic-a'], 'checked_omissions': list(assembly['proposed_omissions']),
                'findings': [{'plan_key': 'atomic-a', 'section_key': section['key'],
                              'category': 'CANDIDATE_ERROR', 'issue': '核对条件',
                              'candidate_quote': '审查误报：此句不在原稿中'}]}
        elif operation == 'next.section_repair':
            key = content['draft']['sections'][0]['key']
            if self.mode == 'repair-down':
                raise TimeoutError('offline fixture')
            if self.mode == 'bad-json':
                raise ValueError('Invalid JSON response')
            body = '<script>bad</script>' if self.mode in {'bad-format', 'pro-down'} else content['draft']['sections'][0]['markdown']
            if self.mode == 'semantic-error':
                body = '$1+1=3$'
            result = {'sections': [{'key': key, 'markdown': body}]}
        elif operation == 'next.section_format_recovery':
            if self.mode == 'pro-down':
                raise TimeoutError('offline fixture')
            result = {'markdown': 'Pro输出的格式正确的最终小节正文。'}
        else:
            raise AssertionError(operation)
        return contract.model_validate(result)


def pipeline(mode='unchanged', parallel=False):
    value = object.__new__(SourceSectionPipeline)
    value.g = Generation(mode, parallel)
    value.document_profile = {'kind': 'SLIDES'}
    return value


def test_parallel_sections_have_local_evidence_and_do_not_repair_or_defer_unchanged_text():
    value = record()
    before = copy.deepcopy(value['draft'])
    pipe = pipeline(parallel=True)
    records, report, _ = review_sections(pipe, [value])
    assert records[0]['draft'] == before and report.passed
    assert Counter(c[0] for c in pipe.g.calls) == {'next.assembly_review': 2, 'next.section_repair': 2}
    assert all(not call[2]['use_pro_model'] for call in pipe.g.calls)
    for operation, packet, _ in pipe.g.calls:
        if operation == 'next.assembly_review':
            assert len(packet['assembly']) == 1
            assert len(packet['assembly'][0]['section_index']) == 1
            key = packet['assembly'][0]['section_index'][0]['key']
            assert ('ID3的原始' not in packet['assembly'][0]['article_section']) if key == 'a' else ('CART的原始' not in packet['assembly'][0]['article_section'])
    assert len(value['section_review_receipts']) == 2
    assert all(r['unchanged'] and not r['post_repair_semantic_review'] for r in value['section_review_receipts'])


@pytest.mark.parametrize('mode', ['bad-format', 'bad-json'])
def test_only_local_format_errors_use_pro_once_and_never_re_review(mode):
    value = record()
    pipe = pipeline(mode)
    records, _, _ = review_sections(pipe, [value])
    assert all(s['markdown'].startswith('Pro输出') for s in records[0]['draft']['sections'])
    assert Counter(c[0] for c in pipe.g.calls) == {
        'next.assembly_review': 2, 'next.section_repair': 2, 'next.section_format_recovery': 2}
    recovery = [c for c in pipe.g.calls if c[0] == 'next.section_format_recovery']
    assert all(c[2]['use_pro_model'] for c in recovery)
    assert all(len(c[1]['original_section']['sections']) == 1 for c in recovery)


@pytest.mark.parametrize('mode', ['review-down', 'repair-down', 'pro-down'])
def test_provider_failure_preserves_checked_original_instead_of_deferring(mode):
    value = record()
    before = copy.deepcopy(value['draft'])
    pipe = pipeline(mode)
    result, _, _ = review_sections(pipe, [value])
    assert result[0]['draft'] == before and result[0]['warnings']
    assert not hasattr(pipe, 'deferred')
    if mode != 'pro-down':
        assert not any(c[0] == 'next.section_format_recovery' for c in pipe.g.calls)


def test_omissions_are_owned_by_one_section_and_format_recovery_has_an_explicit_pro_route(monkeypatch):
    local = section_records(record())
    assert [list(r['draft']['omissions']) for r in local] == [['u3'], []]
    monkeypatch.setenv('NOODLE_NEXT_PRO_USAGE', 'planning-pro')
    assert model_choice('next.section_format_recovery', {}, True) == (True, 'section-format-recovery-pro')
    assert not model_choice('next.assembly_review', {}, True)[0]
    assert not model_choice('next.section_repair', {}, True)[0]
    monkeypatch.setenv('NOODLE_NEXT_PRO_USAGE', 'disabled')
    assert not model_choice('next.section_format_recovery', {}, True)[0]


def test_user_authorized_semantic_errors_do_not_start_pro_or_another_review():
    pipe = pipeline('semantic-error')
    result, final, _ = review_sections(pipe, [record()])
    assert final.passed
    assert all(s['markdown'] == '$1+1=3$' for s in result[0]['draft']['sections'])
    assert Counter(c[0] for c in pipe.g.calls) == {'next.assembly_review': 2, 'next.section_repair': 2}


def test_structured_visual_nodes_and_tables_compile_losslessly_without_another_vision_call():
    import json

    from noodle_agent.adapters.vision import text_items
    value = {'branch': 'yes', 'table': {'rows': [['Class 1', '22'], ['Class 2', '23']]}}
    assert json.loads(text_items([value])[0]) == value
    assert text_items([['Class 1', '22']]) == ['Class 1\t22']
