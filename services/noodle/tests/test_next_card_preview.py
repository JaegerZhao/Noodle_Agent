"""Existing media-first Concepts need readable previews without regeneration."""

from types import SimpleNamespace

import pytest

from noodle_agent.next.card_preview import stored_excerpt, text_excerpt
from noodle_agent.next.concepts.core_state import core_summary
from noodle_agent.next.contracts import sha
from noodle_agent.next.repository import InvalidKnowledge


def test_preview_skips_media_and_headers_but_keeps_emphasis_and_complete_math():
    body='# 梯度\n\n## 框架\n\n![图示](../assets/image.jpg)\n![图2](../assets/two.jpg)\n\n**梯度**满足 $g(x)=2x$，给出局部变化方向。\n\n后续正文。'
    expected='**梯度**满足 $g(x)=2x$，给出局部变化方向。'
    assert text_excerpt(body,'梯度')==expected
    assert core_summary([SimpleNamespace(markdown=body)],'梯度')==expected
    assert text_excerpt('# 仅图片\n\n![图](../assets/image.jpg)','仅图片')=='仅图片'
    assert text_excerpt('定义。\n\n<!-- noodle:derived -->来源列表','标题')=='定义。'
    assert text_excerpt('### 定义\n**梯度**是局部变化方向。\n\n后续。','标题')=='**梯度**是局部变化方向。'


def test_preview_cache_is_hash_bound_and_cannot_read_outside_library(tmp_path):
    path=tmp_path/'body.md'
    path.write_text('可阅读的正文。',encoding='utf8')
    assert stored_excerpt(str(tmp_path),'body.md',sha(path.read_bytes()),'标题')=='可阅读的正文。'
    with pytest.raises(InvalidKnowledge,match='hash'):
        stored_excerpt(str(tmp_path),'body.md','incorrect','标题')
    with pytest.raises(InvalidKnowledge,match='escaped'):
        stored_excerpt(str(tmp_path),'../outside.md','incorrect','标题')
