from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from noodle_agent.adapters.mineru import MinerUParser
from noodle_agent.config import Settings


class MinerUNormalizerTest(unittest.TestCase):
    def test_precision_content_list_preserves_page_and_asset(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            result_dir = root / "result"
            images = result_dir / "images"
            images.mkdir(parents=True)
            (images / "figure.png").write_bytes(b"fixture-image")
            (result_dir / "content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "heading-1",
                            "type": "text",
                            "text": "模型结构",
                            "page_idx": 2,
                            "text_level": 2,
                        },
                        {
                            "id": "figure-1",
                            "type": "image",
                            "page_idx": 2,
                            "img_path": "images/figure.png",
                            "image_footnote": ["编码器结构图"],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (result_dir / "full.md").write_text("# 模型结构", encoding="utf-8")
            values = {
                "NOODLE_DATA_DIR": str(root / "data"),
                "NOODLE_PARSER": "mineru",
                "NOODLE_LLM": "fixture",
                "MINERU_MODE": "precision",
                "MINERU_API_TOKEN": "test-token",
            }
            with patch.dict(os.environ, values, clear=False):
                parser = MinerUParser(Settings.from_env(root))
                document = parser._normalize_precision(result_dir, root / "result.zip", "测试文档")
            self.assertEqual(document.page_count, 3)
            self.assertEqual(document.blocks[0].block_type, "HEADING")
            self.assertEqual(document.blocks[0].level, 2)
            self.assertEqual(document.blocks[1].page_label, "3")
            self.assertEqual(document.blocks[1].text, "编码器结构图")
            self.assertTrue(Path(document.blocks[1].asset_path).exists())


if __name__ == "__main__":
    unittest.main()
