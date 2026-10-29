"""Compact old TOC range labels without altering content or source identity."""

import pytest
from test_next_source_sections import prepared  # noqa: F401

from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.learning_titles import present_titles, preserve_advanced, reading_title

LONG = '3.3 线性高斯系统 *：3.3.1 高斯贝叶斯规则 — 3.3.3 推断未知标量示例'
TOPIC = '高斯贝叶斯规则、推导（配方法）、未知标量推断'


def test_range_display_uses_existing_topic_and_preserves_star_and_qualifiers():
    plan = {'title': LONG, 'concept_outline': {'internal_topic': TOPIC}}
    assert reading_title(plan) == '3.3 ' + TOPIC + ' *'
    assert plan['title'] == LONG
    other = {'title': '3.3 线性高斯系统 *：3.3.4 推断未知向量示例 — 3.3.5 传感器融合示例',
             'concept_outline': {'internal_topic': '未知向量推断、传感器融合'}}
    assert reading_title(other) == '3.3 未知向量推断、传感器融合 *'


@pytest.mark.parametrize('title', ['2.8 随机变量的变换*', 'SVM：线性不可分条件下的软间隔优化及其正则化参数解释', LONG])
def test_titles_without_a_usable_topic_are_not_blindly_truncated(title):
    assert reading_title({'title': title}) == title


def test_new_model_label_preserves_source_star_without_duplicating_it():
    assert preserve_advanced('高斯贝叶斯推断', 'Linear Gaussian systems *') == '高斯贝叶斯推断 *'
    assert preserve_advanced('高斯贝叶斯推断 *', 'Linear Gaussian systems *') == '高斯贝叶斯推断 *'


def test_cross_section_range_does_not_keep_introduction_number_or_long_enumeration():
    plan = {'title': '4.1 引言；4.2 最大似然估计（MLE）：4.2.1 定义 — 4.2.5 示例',
            'pages': [139, 144], 'concept_outline': {'concept_title': '最大似然估计',
                'internal_topic': 'MLE的定义、合理性论证及伯努利、分类、单变量高斯分布示例'}}
    assert reading_title(plan) == '最大似然估计（第139–144页）'


def test_frozen_projection_preserves_raw_metadata_and_explicit_title_edits(prepared):  # noqa: F811
    store, lease, source = prepared
    Checkpoints(store.repository, lease).save('source.sections.plan.title-test', {},
        {'plans':[{'key':'p','title':LONG,'concept_outline':{'internal_topic':TOPIC}}]})
    note = {'title':LONG,'kind':'ATOMIC','creation_key':f"atomic:{source['revision_id']}:source-section:p"}
    projected = present_titles(store, [note])[0]
    assert projected['display_title'] == '3.3 ' + TOPIC + ' *'
    assert projected['title'] == note['title'] == LONG
    assert 'display_title' not in note
    edited = {**note,'title':'用户自定标题'}
    assert present_titles(store,[edited])[0]['display_title'] == '用户自定标题'
