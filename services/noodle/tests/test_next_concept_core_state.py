"""Source evolution and formatting do not add another model review stage."""

import json

from noodle_agent.next.concepts.core_state import compact_markdown, foundation_state


def item(key, role, source, action='UPDATE_SECTION'):
    return {'id': key, 'atomic_revision_id': key, 'fragment_id': 'f-' + key,
            'action': action, 'decision_json': json.dumps({'source_role': role, 'source_revision_id': source})}


def test_late_foundation_rebases_once_but_same_book_delta_and_support_do_not():
    supplement = item('s', 'SUPPLEMENT', 'paper')
    book = item('b', 'FOUNDATION', 'book')
    provisional = foundation_state([supplement], {}, {})
    assert provisional['state'] == 'PROVISIONAL'
    report = {'section_contributions': {'core': ['s']}, 'foundation': provisional}
    foundation = foundation_state([supplement, book], {}, report)
    assert foundation['state'] == 'FOUNDATION_READY' and foundation['rebase']
    report = {'section_contributions': {'core': ['s', 'b']}, 'foundation': foundation}
    assert not foundation_state([supplement, book], {}, report)['rebase']
    assert not foundation_state([supplement, book, item('b2', 'FOUNDATION', 'book')], {}, report)['rebase']
    assert not foundation_state([supplement, book, item('b3', 'FOUNDATION', 'another-book', 'ADD_EVIDENCE')], {}, report)['rebase']
    assert foundation_state([supplement, book, item('b4', 'FOUNDATION', 'another-book')], {}, report)['rebase']
    assert foundation_state([supplement], {}, report)['state'] == 'PROVISIONAL'


def test_reading_does_not_become_foundation_and_previous_roles_survive_claim_upgrade():
    reading = item('r', 'FOUNDATION', 'book', 'LINK_ONLY')
    assert foundation_state([reading], {}, {})['state'] == 'PROVISIONAL'
    legacy = item('legacy', 'UNKNOWN', None)
    previous = foundation_state([legacy], {'_source_role': 'SUPPLEMENT'}, {})
    result = foundation_state([legacy], {'_source_role': 'FOUNDATION'}, {'foundation': previous})
    assert not result['foundation']


def test_format_only_wrapper_changes_preserve_equations_inline_labels_and_code():
    before = '# SVM\n\n## 知识框架\n\n**核心机制**\n\n**定义**：分类方法。\n\n$$x^2$$\n\n```md\n# SVM\n**核心机制**\n```'
    after = compact_markdown(before, 'SVM')
    assert after.startswith('### 核心机制')
    assert '**定义**：分类方法。' in after and '$$x^2$$' in after
    assert '```md\n# SVM\n**核心机制**\n```' in after
    assert compact_markdown(after, 'SVM') == after


def test_formatting_does_not_transform_labels_inside_display_math():
    source = '$$\n**symbol**\n$$\n\n\\[\n# SVM\n\\]\n\n**核心机制**'
    assert compact_markdown(source, 'SVM') == source.removesuffix('**核心机制**') + '### 核心机制'
