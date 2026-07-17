# -*- coding: utf-8 -*-
"""复现画图链路三类问题（只读模拟，不触网）：

1. 单人（异地）场景 view=pov 误用
2. prompt 碎片：bare half-removed / the current outfit / in her hand
3. 隔夜半脱状态进入早安图
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_comfyui_selfie.service import TelegramComfyUIService
from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.generation import (
    _strip_conflicting_scene_outfit,
    _strip_non_mirror_camera_artifacts,
)
from telegram_comfyui_selfie.image_planning import _resolve_roleplay_view, plan_roleplay_image
from unittest.mock import AsyncMock


def make_service():
    root = Path(".tmp/repro")
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.json"
    cfg.write_text(json.dumps({"telegram_bot_token": "TEST"}), encoding="utf-8")
    return TelegramComfyUIService(cfg, root / "state.json")


def sep(title):
    print("\n" + "=" * 20 + " " + title + " " + "=" * 20)


def main():
    svc = make_service()
    sid = "telegram:repro"
    state = svc._get_session_state(sid)
    state["custom_positive_prefix"] = "1girl, long hair, blond hair, blue eyes, fox ears, fox tail"
    session_schema.set_wardrobe(state, {
        "top": "white camisole, slim fit, thin straps",
        "bottom": "short pleated skirt, navy",
    })
    session_schema.set_outfit(state, "white camisole, slim fit, thin straps, short pleated skirt, navy")
    for slot in ("top", "bottom"):
        session_schema.set_wardrobe_item_state(state, slot, "half_off")

    sep("1. build_prompt 双重应用 wardrobe states（bare half-removed）")
    pos, neg = svc._build_prompt(
        "A fox-eared girl stands in the kitchen, morning light through the window",
        session_id=sid,
    )
    print("positive:", pos[:600])
    bare = pos.count("half-removed,")
    print(f"[问题] 裸 'half-removed,' 碎片数量: {bare}")

    sep("2. _strip_conflicting_scene_outfit 占位语/吃动作")
    scene = ("A fox-eared girl in a white camisole and navy pleated skirt stands at the counter "
             "tying a bento box with a cloth, her long hair slightly messy")
    stripped = _strip_conflicting_scene_outfit(scene, ["white camisole"], ["camisole", "skirt", "dress"])
    print("in :", scene)
    print("out:", stripped)
    print(f"[问题] 含不可渲染占位语 'the current outfit': {'the current outfit' in stripped}")
    print(f"[问题] 动作 'tying a bento box' 被吃掉: {'tying a bento box' not in stripped}")

    sep("3. 手机清洗留下孤儿介词短语")
    scene2 = ("The fox-eared girl curls up on the sofa with her legs tucked under her, "
              "holding a phone in her hand. She holds her smartphone in both hands, fingers paused over the screen.")
    stripped2 = _strip_non_mirror_camera_artifacts(scene2)
    print("in :", scene2)
    print("out:", stripped2)
    print(f"[问题] 残留 'in her hand': {'in her hand' in stripped2} | 残留 'in both hands': {'in both hands' in stripped2}")

    sep("4. 异地 + planner 返回 pov：_resolve_roleplay_view 无闸门")
    view = _resolve_roleplay_view(
        requested_view="", planned_view="pov", default_view="selfie",
        derived_co_located=False, two_person=False, free_composition=False,
        scene="A fox-eared girl stands in the kitchen tying a bento box",
        intent="", mood="", prompt="",
    )
    print("resolved view:", view, "[问题] 异地仍 pov" if view == "pov" else "[OK] 已降级")

    sep("5. morning 推送：隔夜 co_located（绕过 TTL）+ 隔夜半脱状态")
    async def run():
        svc.config.update({
            "image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x.example",
        })
        now = datetime.fromtimestamp(time.time(), timezone.utc)
        ts = now.timestamp()
        # 昨晚同处 + 脱衣状态；user_place_updated_at 是 8 小时前（超过 4h TTL）
        session_schema.set_user_place(state, key="home", updated_at=ts - 8 * 3600, co_located=True)
        session_schema.set_last_interaction(state, ts - 8 * 3600)
        session_schema.set_last_message_time(state, ts - 8 * 3600)
        session_schema.set_nudity(state, "completely nude", at=ts - 8 * 3600)
        svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
        svc._call_llm_messages = AsyncMock(return_value={
            "choices": [{"message": {"content": json.dumps({
                "scene": "A quiet morning kitchen, she ties a bento box",
                "view": "pov", "character_location": "home", "user_location": "unknown",
                "is_intimate": False, "partner_in_frame": False, "device_in_frame": False,
            })}}],
            "usage": {},
        })
        plan = await plan_roleplay_image(svc, sid, mode="morning", weather_data={"desc": "晴", "temp": "22"}, now=now)
        print("plan view:", plan["view"], "[问题] 隔夜同处标记仍生效→pov" if plan["view"] == "pov" else "[OK] 已降级")
        print("nudity after plan:", repr(session_schema.get_nudity(state)))
        print("item states after plan:", session_schema.get_wardrobe_item_states(state))
        return plan
    asyncio.run(run())


if __name__ == "__main__":
    main()
