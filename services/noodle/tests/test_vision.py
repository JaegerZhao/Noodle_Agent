from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from noodle_agent.config import Settings
from noodle_agent.db import VisionCallEvent
from noodle_agent.services import NoodleService


class VisionProviderTest(unittest.TestCase):
    @patch("noodle_agent.adapters.vision.OpenAI")
    def test_doubao_image_understanding_is_structured_and_observable(self, openai) -> None:
        response = MagicMock()
        response.id = "vision-response-1"
        response.usage = None
        response.choices[0].message.content = json.dumps(
            {
                "asset_type": "DIAGRAM",
                "is_decorative": False,
                "essential": True,
                "caption": "卷积核在二维输入上滑动。",
                "teaching_role": "解释局部连接与空间关系",
                "ocr_text": ["convolution"],
                "factual_observations": ["图中存在输入网格和局部窗口"],
                "spatial_relations": ["局部窗口覆盖输入的一部分"],
                "suggested_placement": "卷积运算步骤之后",
                "confidence": 0.93,
            },
            ensure_ascii=False,
        )
        openai.return_value.chat.completions.create.return_value = response

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            image_path = root / "diagram.png"
            image_path.write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
                )
            )
            env = {
                "NOODLE_DATA_DIR": str(root / "data"),
                "NOODLE_PARSER": "fixture",
                "NOODLE_LLM": "fixture",
                "NOODLE_SKILLS_DIR": str(Path(__file__).resolve().parents[1] / "skills"),
                "DOUBAO_API_KEY": "test-doubao-key",
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env(root)
                service = NoodleService(settings)
                service.init()
                result = service.vision.analyze(
                    image_path,
                    external_block_id="img-cnn-1",
                    context={"document_title": "CNN", "page": "8"},
                )

                self.assertEqual(result.ocr_text, ["convolution"])
                self.assertEqual(result.factual_observations, ["图中存在输入网格和局部窗口"])
                self.assertEqual(result.spatial_relations, ["局部窗口覆盖输入的一部分"])
                self.assertEqual(result.suggested_placement, "卷积运算步骤之后")
                self.assertTrue(result.essential)
                self.assertEqual(result.asset_type, "DIAGRAM")
                call = openai.return_value.chat.completions.create.call_args.kwargs
                self.assertEqual(call["model"], "doubao-seed-2-1-turbo-260628")
                self.assertEqual(call["reasoning_effort"], "low")
                self.assertEqual(call["max_tokens"], 900)
                image_part = next(
                    item
                    for item in call["messages"][1]["content"]
                    if item["type"] == "image_url"
                )
                image_url = image_part["image_url"]["url"]
                self.assertTrue(image_url.startswith("data:image/png;base64,"))
                with service.db.session() as session:
                    event = session.scalar(select(VisionCallEvent))
                    self.assertIsNotNone(event)
                    self.assertEqual(event.status, "COMPLETED")
                    self.assertEqual(event.response_id, "vision-response-1")
                service.close()

    @patch("noodle_agent.adapters.vision.OpenAI")
    def test_multiple_images_share_one_compact_api_call(self, openai) -> None:
        response = MagicMock()
        response.id = "vision-batch-1"
        response.usage = None
        response.choices[0].message.content = json.dumps(
            {
                "images": [
                    {
                        "asset_id": "img-a",
                        "type": "DIAGRAM",
                        "decorative": False,
                        "essential": True,
                        "caption": "输入与卷积核",
                        "role": "解释卷积",
                        "confidence": 0.9,
                        "is_fragment": True,
                        "fragment_group": "page-1",
                        "related_asset_ids": ["img-b"],
                    },
                    {
                        "asset_id": "img-b",
                        "type": "DECORATIVE",
                        "decorative": True,
                        "essential": False,
                        "caption": "",
                        "role": "",
                        "confidence": 0.98,
                        "is_fragment": True,
                        "fragment_group": "page-1",
                        "related_asset_ids": ["img-a"],
                    },
                ]
            },
            ensure_ascii=False,
        )
        openai.return_value.chat.completions.create.return_value = response

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            paths = []
            for name in ("a.png", "b.png"):
                path = root / name
                path.write_bytes(
                    base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
                    )
                )
                paths.append(path)
            env = {
                "NOODLE_DATA_DIR": str(root / "data"),
                "NOODLE_PARSER": "fixture",
                "NOODLE_LLM": "fixture",
                "NOODLE_SKILLS_DIR": str(Path(__file__).resolve().parents[1] / "skills"),
                "DOUBAO_API_KEY": "test-doubao-key",
            }
            with patch.dict(os.environ, env, clear=True):
                service = NoodleService(Settings.from_env(root))
                service.init()
                results = service.vision.analyze_batch(
                    [
                        {
                            "image_path": paths[0],
                            "external_block_id": "img-a",
                            "context": {"page": "1"},
                        },
                        {
                            "image_path": paths[1],
                            "external_block_id": "img-b",
                            "context": {"page": "1"},
                        },
                    ]
                )
                self.assertEqual(openai.return_value.chat.completions.create.call_count, 1)
                self.assertTrue(results["img-a"].essential)
                self.assertTrue(results["img-b"].is_decorative)
                self.assertEqual(results["img-a"].fragment_group, "page-1")
                self.assertEqual(results["img-a"].group_type, "FRAGMENT")
                self.assertEqual(results["img-a"].group_id, "page-1")
                service.close()


if __name__ == "__main__":
    unittest.main()


def test_malformed_batch_splits_without_losing_successful_siblings():
    from noodle_agent.adapters.vision import resilient_vision_batch
    from noodle_agent.domain import VisualUnderstanding
    calls = []
    def call(items):
        ids = [item["external_block_id"] for item in items]
        calls.append(ids)
        if len(ids) > 1:
            raise ValueError("Malformed JSON batch")
        if ids[0] == "bad":
            raise RuntimeError("Unrecoverable image")
        return {ids[0]: VisualUnderstanding(external_block_id=ids[0], image_sha256="hash", model="fixture")}
    results, failures = resilient_vision_batch(
        [{"external_block_id": value} for value in ["a", "bad", "c", "d"]],
        call, max_retries=1, sleep=lambda _: None,
    )
    assert set(results) == {"a", "c", "d"}
    assert [failure["block_id"] for failure in failures] == ["bad"]
    assert calls.count(["a", "bad", "c", "d"]) == 1
    assert calls.count(["bad"]) == 2


def test_page_context_is_shared_and_wrong_asset_ids_are_rejected(tmp_path):
    from noodle_agent.adapters.vision import DoubaoVisionProvider
    provider = object.__new__(DoubaoVisionProvider)
    provider.client = MagicMock()
    provider.model_name = "fixture"
    provider.system_prompt = "fixture"
    provider.reasoning_effort = "low"
    provider.max_output_tokens = 900
    provider._record = MagicMock()
    image = tmp_path / "crop.png"
    image.write_bytes(b"image")
    page = tmp_path / "page.png"
    page.write_bytes(b"full-page")
    response = MagicMock()
    response.choices[0].message.content = json.dumps({"images": [{"asset_id": "a", "caption": "Offline"}, {"asset_id": "b", "caption": "Online"}]})
    provider.client.chat.completions.create.return_value = response
    items = [{"image_path": image, "external_block_id": key, "page_context_path": page} for key in ["a", "b"]]
    results = provider.analyze_batch(items)
    assert set(results) == {"a", "b"}
    parts = provider.client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert len([part for part in parts if part["type"] == "image_url"]) == 3
    response.choices[0].message.content = json.dumps({"images": [{"asset_id": "wrong", "caption": "Misassigned"}]})
    with __import__("pytest").raises(ValueError, match="asset IDs"):
        provider.analyze_batch(items[:1])
