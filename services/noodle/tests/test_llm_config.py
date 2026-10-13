from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from noodle_agent.adapters.knowledge.common import _sections
from noodle_agent.adapters.llm import (
    OpenAIEmbedding,
    OpenAIKnowledgeAgent,
    _loads_structured_json,
)
from noodle_agent.config import Settings
from noodle_agent.domain import (
    DocumentProfile,
    KnowledgeConcept,
    KnowledgeMap,
    ParsedBlock,
    ParsedDocument,
    SearchHit,
    TopicSegment,
)


class DeepSeekConfigurationTest(unittest.TestCase):
    def test_structured_json_repairs_unescaped_latex_commands(self) -> None:
        payload = _loads_structured_json(r'{"latex":"\underbrace{x}_{group}+\beta+\frac{1}{2}"}')
        self.assertEqual(payload["latex"], r"\underbrace{x}_{group}+\beta+\frac{1}{2}")

    def test_slide_titles_remain_one_extraction_unit(self) -> None:
        blocks = []
        for page_index in range(20):
            blocks.extend(
                [
                    ParsedBlock(
                        block_id=f"h-{page_index}",
                        block_type="HEADING",
                        page_index=page_index,
                        page_label=str(page_index + 1),
                        level=2,
                        text=f"Slide {page_index + 1}",
                    ),
                    ParsedBlock(
                        block_id=f"p-{page_index}",
                        page_index=page_index,
                        page_label=str(page_index + 1),
                        text="Knowledge",
                    ),
                ]
            )
        document = ParsedDocument(
            title="Slide deck",
            page_count=20,
            markdown="",
            parser_name="test",
            blocks=blocks,
        )
        self.assertEqual(len(_sections(document)), 1)

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
                MagicMock(embedding=[0.0] * kwargs["dimensions"]) for _ in kwargs["input"]
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
    def test_task_routing_uses_flash_for_sections_and_pro_for_synthesis(self, openai) -> None:
        response = MagicMock()
        response.choices[0].message.content = '{"candidates":[]}'
        synthesis = MagicMock()
        synthesis.choices[0].message.content = '{"document_summary":"ok","notes":[]}'
        openai.return_value.chat.completions.create.side_effect = [response, synthesis]
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
        )
        agent = OpenAIKnowledgeAgent(settings)
        document = ParsedDocument(
            title="测试",
            page_count=1,
            markdown="知识点",
            parser_name="test",
            blocks=[ParsedBlock(block_id="b1", page_index=0, page_label="1", text="知识点")],
        )
        agent.build_plan(document)
        calls = openai.return_value.chat.completions.create.call_args_list
        self.assertEqual(calls[0].kwargs["model"], "deepseek-v4-flash")
        self.assertEqual(calls[1].kwargs["model"], "deepseek-v4-pro")

    @patch("noodle_agent.adapters.llm.OpenAI")
    def test_occam_generation_materializes_candidates_and_uses_one_source_plan(
        self, openai
    ) -> None:
        payloads = [
            {
                "document_type": "COURSE_MATERIAL",
                "segments": [
                    {
                        "stable_key": "convolution",
                        "title": "卷积基础",
                        "start_page_index": 0,
                        "end_page_index": 0,
                    }
                ],
            },
            {
                "candidates": [
                    {
                        "title": "卷积运算",
                        "claim": "卷积核在输入上滑动并计算局部加权和。",
                        "evidence_block_ids": ["b1"],
                        "asset_ids": [],
                    }
                ]
            },
            {
                "outline": [
                    {
                        "key": "cnn-foundations",
                        "title": "卷积神经网络基础",
                        "parent_key": None,
                    },
                    {
                        "key": "convolution-mechanism",
                        "title": "卷积运算机制",
                        "parent_key": "cnn-foundations",
                    },
                ],
                "notes": [
                    {
                        "key": "convolution-operation",
                        "title": "卷积如何计算",
                        "question": "卷积如何计算？",
                        "outline_key": "convolution-mechanism",
                        "candidate_ids": ["convolution--1--c1"],
                        "asset_ids": [],
                    }
                ],
            },
            {
                "stable_key": "convolution-operation",
                "title": "卷积如何计算",
                "thesis": "卷积通过局部滑动加权和生成特征。",
                "blocks": [
                    {
                        "block_type": "PARAGRAPH",
                        "text": "卷积核在输入上滑动，并在每个位置计算局部加权和。",
                        "evidence_block_ids": ["b1"],
                        "asset_block_ids": [],
                    }
                ],
                "source_sections": ["卷积基础"],
            },
            {
                "evaluations": [
                    {"stable_key": "convolution-operation", "passed": True, "issues": []}
                ]
            },
        ]

        def response_for(payload):
            response = MagicMock()
            response.choices[0].message.content = __import__("json").dumps(
                payload, ensure_ascii=False
            )
            return response

        openai.return_value.chat.completions.create.side_effect = [
            response_for(payload) for payload in payloads
        ]
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
            note_language="zh-CN",
        )
        agent = OpenAIKnowledgeAgent(settings)
        document = ParsedDocument(
            title="测试",
            page_count=1,
            markdown="卷积核在输入上滑动",
            parser_name="test",
            blocks=[
                ParsedBlock(
                    block_id="b1",
                    page_index=0,
                    page_label="1",
                    text="卷积核在输入上滑动并计算局部加权和。",
                )
            ],
        )
        generation = agent.build_generation(document)
        self.assertEqual(generation.schema_version, "source-note-generation-v7-occam")
        self.assertEqual(len(generation.source_note_outline.nodes), 2)
        self.assertEqual(len(generation.knowledge_map.concepts), 1)
        self.assertEqual(len(generation.note_set_plan.notes), 1)
        self.assertEqual(len(generation.final_plan.notes), 1)
        calls = openai.return_value.chat.completions.create.call_args_list
        self.assertEqual(
            [call.kwargs["model"] for call in calls],
            [
                "deepseek-v4-flash",
                "deepseek-v4-flash",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
                "deepseek-v4-flash",
            ],
        )
        self.assertEqual(
            [call.kwargs["extra_body"]["thinking"]["type"] for call in calls],
            ["disabled", "disabled", "disabled", "disabled", "disabled"],
        )

    @patch("noodle_agent.adapters.llm.OpenAI")
    def test_question_answering_uses_flash(self, openai) -> None:
        response = MagicMock()
        response.choices[
            0
        ].message.content = '{"answer":"答案","used_note_ids":["n1"],"coverage_warning":null}'
        openai.return_value.chat.completions.create.return_value = response
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
        )
        agent = OpenAIKnowledgeAgent(settings)
        hit = SearchHit(
            note_id="n1",
            note_version_id="nv1",
            title="标题",
            thesis="结论",
            body="正文",
            score=1.0,
        )
        agent.answer("问题", [hit])
        call = openai.return_value.chat.completions.create.call_args
        self.assertEqual(call.kwargs["model"], "deepseek-v4-flash")

    @patch("noodle_agent.adapters.llm.OpenAI")
    def test_question_answering_with_empty_used_ids_returns_no_citations(self, openai) -> None:
        response = MagicMock()
        response.choices[
            0
        ].message.content = (
            '{"answer":"知识库证据不足。","used_note_ids":[],"coverage_warning":null}'
        )
        openai.return_value.chat.completions.create.return_value = response
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
        )
        agent = OpenAIKnowledgeAgent(settings)
        hit = SearchHit(
            note_id="irrelevant-note",
            note_version_id="nv1",
            title="无关笔记",
            thesis="无关结论",
            body="无关正文",
            score=0.3,
        )
        answer = agent.answer("知识库之外的问题", [hit])
        self.assertEqual(answer.hits, [])

    @patch("noodle_agent.adapters.llm.OpenAI")
    def test_structured_output_repairs_unescaped_latex_backslashes(self, openai) -> None:
        response = MagicMock()
        response.choices[0].message.content = r'{"formula":"\alpha + \(x\)"}'
        openai.return_value.chat.completions.create.return_value = response
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
        )
        agent = OpenAIKnowledgeAgent(settings)
        result = agent._json_call(system="test", user={})
        self.assertEqual(result["formula"], r"\alpha + \(x\)")

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

    @patch("noodle_agent.adapters.llm.OpenAI")
    def test_large_candidate_set_uses_one_global_source_plan(self, openai) -> None:
        payloads = [
            {
                "outline": [
                    {
                        "key": "models",
                        "title": "模型",
                        "parent_key": None,
                    },
                    {
                        "key": "cnn",
                        "title": "卷积网络",
                        "parent_key": "models",
                    },
                    {
                        "key": "rnn",
                        "title": "循环网络",
                        "parent_key": "models",
                    },
                ],
                "notes": [
                    {
                        "key": "cnn-basics",
                        "title": "CNN 基础",
                        "question": "CNN 如何提取特征？",
                        "outline_key": "cnn",
                        "candidate_ids": ["cnn", "convolution"],
                        "asset_ids": ["img1"],
                    },
                    {
                        "key": "rnn-basics",
                        "title": "RNN 基础",
                        "question": "RNN 如何处理序列？",
                        "outline_key": "rnn",
                        "candidate_ids": ["rnn"],
                        "asset_ids": [],
                    },
                    {
                        "key": "empty-summary",
                        "title": "空总结页",
                        "question": "总结了什么？",
                        "outline_key": "models",
                        "candidate_ids": [],
                        "asset_ids": [],
                    },
                ],
            }
        ]

        def response_for(payload):
            response = MagicMock()
            response.choices[0].message.content = __import__("json").dumps(
                payload, ensure_ascii=False
            )
            return response

        openai.return_value.chat.completions.create.side_effect = [
            response_for(payload) for payload in payloads
        ]
        settings = MagicMock(
            llm_api_key="test-key",
            llm_base_url="https://example.invalid/v1",
            llm_fast_model="deepseek-v4-flash",
            llm_pro_model="deepseek-v4-pro",
            note_language="zh-CN",
            planning_single_call_concept_limit=2,
            planning_shard_size=2,
            planning_concurrency=1,
            generation_task_retries=0,
        )
        agent = OpenAIKnowledgeAgent(settings)
        document = ParsedDocument(
            title="模型课程",
            page_count=3,
            markdown="",
            parser_name="test",
            blocks=[
                ParsedBlock(block_id="b1", page_index=0, page_label="1", text="CNN"),
                ParsedBlock(block_id="b2", page_index=1, page_label="2", text="卷积"),
                ParsedBlock(block_id="b3", page_index=2, page_label="3", text="RNN"),
            ],
        )
        profile = DocumentProfile(
            document_type="COURSE_MATERIAL",
            document_summary="模型课程",
            learning_goal="",
            segments=[
                TopicSegment(
                    stable_key="all",
                    title="模型",
                    start_page_index=0,
                    end_page_index=2,
                    rationale="test",
                )
            ],
        )
        knowledge_map = KnowledgeMap(
            document_summary="模型课程",
            concepts=[
                KnowledgeConcept(
                    stable_key="cnn",
                    title="CNN",
                    question_answered="CNN 是什么？",
                    core_claim="CNN 提取局部特征。",
                    importance="CORE",
                    evidence_block_ids=["b1"],
                    asset_block_ids=["img1"],
                    source_sections=["CNN"],
                ),
                KnowledgeConcept(
                    stable_key="convolution",
                    title="卷积",
                    question_answered="卷积如何工作？",
                    core_claim="卷积核计算局部加权和。",
                    importance="CORE",
                    evidence_block_ids=["b2"],
                    source_sections=["CNN"],
                ),
                KnowledgeConcept(
                    stable_key="rnn",
                    title="RNN",
                    question_answered="RNN 是什么？",
                    core_claim="RNN 维护循环状态。",
                    importance="CORE",
                    evidence_block_ids=["b3"],
                    source_sections=["RNN"],
                ),
            ],
        )

        bundle = agent.plan_source_note_bundle(document, profile, knowledge_map)

        self.assertEqual(bundle.schema_version, "source-plan-v2")
        self.assertEqual(len(bundle.note_set_plan.notes), 2)
        self.assertEqual(bundle.note_set_plan.notes[0].evidence_block_ids, ["b1", "b2"])
        self.assertEqual(bundle.note_set_plan.notes[0].asset_block_ids, ["img1"])
        self.assertEqual(bundle.note_set_plan.notes[1].evidence_block_ids, ["b3"])
        self.assertEqual(openai.return_value.chat.completions.create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
