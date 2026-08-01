from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

from telegram_comfyui_selfie import TelegramComfyUIService
from telegram_comfyui_selfie.config_store import flatten_config, load_simple_yaml


os.environ.setdefault("SUCYUBOT_TEST_FAST_SQLITE", "1")

TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

# 测试临时目录放在系统临时目录下：本机批量删除保护（>50 文件/次）对 OS tmp 目录豁免，
# 避免 .tmp/tests 无限累积后单次 rmtree 被拦截。目录名带 run_id，
# 跨运行清理时只保留当前运行的结果，自动替换上一次。
TEST_TMP_ROOT = Path(tempfile.gettempdir()) / "sucyubot_tests"
_TEST_TMP_RUN_ID = f"{os.getpid()}_{int(time.time() * 1000)}"
_TEST_TMP_READY = False
_TEST_TMP_COUNTER = 0


def _prune_stale_runs() -> None:
    """删除非当前测试运行的残留目录，跨运行只保留最新一次结果。"""
    if not TEST_TMP_ROOT.exists():
        return
    marker = f"_{_TEST_TMP_RUN_ID}_"
    for child in TEST_TMP_ROOT.iterdir():
        try:
            if child.is_dir():
                if marker not in child.name:
                    shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


def make_project_temp_dir(prefix: str = "case") -> Path:
    """为测试创建临时目录；跨运行自动替换上一次结果，不整树删除根目录。"""
    global _TEST_TMP_READY, _TEST_TMP_COUNTER
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    if not _TEST_TMP_READY:
        _prune_stale_runs()
        _TEST_TMP_READY = True
    _TEST_TMP_COUNTER += 1
    path = TEST_TMP_ROOT / f"{prefix}_{_TEST_TMP_RUN_ID}_{_TEST_TMP_COUNTER}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=False)
    return path


def make_mock_request(app, path, method="GET", admin=False, query=None):
    from aiohttp.test_utils import make_mocked_request

    req = make_mocked_request(method, path, app=app, headers={"Content-Type": "application/json"})
    if admin:
        req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
    return req


class ServiceFixtureMixin:
    """共享服务 fixture；本类不继承 TestCase，避免 discovery 重复收集。"""

    def make_service(self):
        root = make_project_temp_dir("service")
        cfg = root / "config.json"
        state = root / "state.json"
        cfg.write_text(json.dumps({"telegram_bot_token": "TEST"}, ensure_ascii=False), encoding="utf-8")
        return TelegramComfyUIService(cfg, state)

    def make_service_from_current_config(self):
        root = make_project_temp_dir("live_config")
        project_root = Path(__file__).resolve().parents[1]
        source_cfg = project_root / "data" / "config.yml"
        if not source_cfg.exists():
            source_cfg = project_root / "data" / "config.json"
        state = root / "state.json"
        svc = TelegramComfyUIService(root / "config.yml", state)
        if source_cfg.exists():
            if source_cfg.suffix.lower() in (".yml", ".yaml"):
                loaded = flatten_config(load_simple_yaml(source_cfg))
            else:
                loaded = json.loads(source_cfg.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                svc.config.update(loaded)
        svc.config["long_memory_db_path"] = ""
        svc.config["user_log_enabled"] = False
        svc.config["user_log_dir"] = str(root / "logs")
        return svc

    def mock_image_planner_messages(self, svc, payload):
        svc._call_llm_messages = AsyncMock(return_value={
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {},
        })
        return svc._call_llm_messages
