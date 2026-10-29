from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from noodle_agent.adapters.llm import (
    OpenAIEmbedding,
    OpenAIKnowledgeAgent,
    _loads_structured_json,
)
from noodle_agent.config import Settings


class DeepSeekConfigurationTest(unittest.TestCase):
    def test_structured_json_repairs_unescaped_latex_commands(self) -> None:
        payload = _loads_structured_json(
            '{"latex":"\\underbrace{x}_{group}+\\beta+\\frac{1}{2}"}'
        )
        self.assertEqual(
            payload["latex"], "\\underbrace{x}_{group}+\\beta+\\frac{1}{2}"
        )

    def test_provider_aliases_and_default_model_routing(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            values = {
                "NOODLE_DATA_DIR": str(Path(raw) / "data"),
                "NOODLE_LLM": "deepseek",
                "DeepSeek_API_KEY": "test-key",
                "DeepSeek_BASE_URL": "https://example.invalid/v1",
            }
            with patch.dict(os.environ, values, clear=False):
                settings = Settings.from_env(Path(raw))
        self.assertEqual(settings.llm_api_key, "test-key")
        self.assertEqual(settings.llm_fast_model, "deepseek-v4-flash")
        self.assertEqual(settings.llm_pro_model, "deepseek-v4-pro")

    def test_dashscope_embedding_defaults_are_independent_from_llm(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            values = {
                "NOODLE_DATA_DIR": str(Path(raw) / "data"),
                "DASHSCOPE_API_KEY": "dashscope-test-key",
            }
            with patch.dict(os.environ, values, clear=False):
                settings = Settings.from_env(Path(raw))
        self.assertEqual(settings.embedding_api_key, "dashscope-test-key")
        self.assertEqual(settings.embedding_model, "text-embedding-v4")
        self.assertEqual(
            settings.embedding_base_url,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(settings.embedding_dimensions, 1024)

    @patch("noodle_agent.adapters.knowledge.common.OpenAI")
    def test_text_embedding_v4_batches_at_ten_rows(self, openai) -> None:

        def create(**kwargs):
            response = MagicMock()
            response.data = [
                MagicMock(embedding=[0.0] * kwargs["dimensions"])
                for _ in kwargs["input"]
            ]
            return response

        openai.return_value.embeddings.create.side_effect = create
        settings = MagicMock(
            embedding_api_key="test-key",
            embedding_base_url="https://example.invalid/v1",
            embedding_model="text-embedding-v4",
            embedding_dimensions=1024,
        )
        embedding = OpenAIEmbedding(settings)
        vectors = embedding.embed([f"text-{index}" for index in range(23)])
        calls = openai.return_value.embeddings.create.call_args_list
        self.assertEqual([len(call.kwargs["input"]) for call in calls], [10, 10, 3])
        self.assertTrue(all(call.kwargs["dimensions"] == 1024 for call in calls))
        self.assertEqual(len(vectors), 23)

    @patch("noodle_agent.adapters.llm.OpenAI")
    def test_structured_output_repairs_unescaped_latex_backslashes(
        self, openai
    ) -> None:
        response = MagicMock()
        response.choices[0].message.content = '{"formula":"\\alpha + \\(x\\)"}'
        openai.return_value.chat.completions.create.return_value = response
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
        )
        agent = OpenAIKnowledgeAgent(settings)
        result = agent._json_call(system="test", user={})
        self.assertEqual(result["formula"], "\\alpha + \\(x\\)")

    @patch("noodle_agent.adapters.llm.OpenAI")
    def test_structured_output_recovers_gbk_latin1_mojibake(self, openai) -> None:
        response = MagicMock()
        mojibake = "全连接网络".encode("gbk").decode("latin-1")
        response.choices[0].message.content = __import__("json").dumps(
            {"title": mojibake}, ensure_ascii=False
        )
        openai.return_value.chat.completions.create.return_value = response
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
        )
        agent = OpenAIKnowledgeAgent(settings)
        result = agent._json_call(system="test", user={})
        self.assertEqual(result["title"], "全连接网络")


def test_compact_uuid_references_roundtrip():
    from noodle_agent.adapters.llm import compact_reference_ids

    identity = "11111111-2222-4333-8444-555555555555"
    payload = {"evidence_refs": [identity], "title": identity}
    encoded, decode = compact_reference_ids(payload)
    assert encoded["title"] == identity and encoded["evidence_refs"] != [identity]
    assert decode(encoded) == payload
