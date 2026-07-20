from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_comfyui_selfie.generation import (
    _collect_output_images,
    build_workflow,
    do_generate_locked,
)
from tests.support import make_project_temp_dir


class GenerationWorkflowTestCase(unittest.TestCase):
    def test_custom_workflow_replaces_only_string_values_without_breaking_json(self):
        root = make_project_temp_dir("workflow")
        path = root / "custom.json"
        template = {
            "prompt": {"inputs": {"text": "{{positive}}", "nested": ["neg={{negative}}", 7]}},
            "{{positive}}": "键名不应替换",
            "seed": {"inputs": {"seed": "{{seed}}"}},
        }
        path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        service = SimpleNamespace(config={"comfyui_workflow_file": str(path)})
        positive = 'quoted "text" \\ path\n下一行'
        negative = 'bad "quote" \\ slash\n换行'

        workflow = build_workflow(service, positive, negative, 123)

        self.assertEqual(workflow["prompt"]["inputs"]["text"], positive)
        self.assertEqual(workflow["prompt"]["inputs"]["nested"][0], f"neg={negative}")
        self.assertEqual(workflow["prompt"]["inputs"]["nested"][1], 7)
        self.assertEqual(workflow["{{positive}}"], "键名不应替换")
        self.assertEqual(workflow["seed"]["inputs"]["seed"], "123")

    def test_invalid_custom_workflow_raises_instead_of_falling_back(self):
        root = make_project_temp_dir("workflow_bad")
        path = root / "broken.json"
        path.write_text('{"broken": ', encoding="utf-8")
        service = SimpleNamespace(config={"comfyui_workflow_file": str(path)})

        with self.assertRaisesRegex(RuntimeError, "自定义 ComfyUI 工作流加载失败"):
            build_workflow(service, "positive", "negative", 1)

    def test_collect_output_images_scans_all_nodes_and_honors_configuration(self):
        outputs = {
            "preview": {"images": [{"filename": "a.png", "subfolder": "x"}]},
            "meta": {"text": ["ignored"]},
            "save": {"images": [
                {"filename": "b.png"},
                {"filename": "a.png", "subfolder": "x"},
            ]},
        }

        self.assertEqual(
            [item["filename"] for item in _collect_output_images(outputs)],
            ["a.png", "b.png"],
        )
        self.assertEqual(
            [item["filename"] for item in _collect_output_images(outputs, {"save"})],
            ["b.png", "a.png"],
        )

    def test_native_generation_downloads_images_from_dynamic_output_nodes(self):
        async def run():
            class Response:
                def __init__(self, *, data=None, body=b"", status=200):
                    self.data = data
                    self.body = body
                    self.status = status

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def json(self):
                    return self.data

                async def read(self):
                    return self.body

            class Session:
                closed = False

                def post(self, url, **kwargs):
                    return Response(data={"prompt_id": "p1"})

                def get(self, url, **kwargs):
                    if "/history/" in url:
                        return Response(data={"p1": {"outputs": {
                            "91": {"images": [{"filename": "first.png"}]},
                            "123": {"images": [{"filename": "second.png", "subfolder": "nested"}]},
                        }}})
                    filename = kwargs.get("params", {}).get("filename", "")
                    return Response(body=filename.encode("utf-8"))

            service = SimpleNamespace(
                config={"image_backend": "native"},
                comfy_session=Session(),
                comfyui_url="http://comfy.invalid",
            )
            with (
                patch("telegram_comfyui_selfie.generation.build_prompt", return_value=("p", "n")),
                patch("telegram_comfyui_selfie.generation.build_workflow", return_value={"node": {}}),
                patch("telegram_comfyui_selfie.generation.asyncio.sleep", new=AsyncMock()),
            ):
                ok, images, error = await do_generate_locked(service, "scene")

            self.assertTrue(ok)
            self.assertEqual(images, [b"first.png", b"second.png"])
            self.assertEqual(error, "")

        asyncio.run(run())
