# -*- coding: utf-8 -*-
"""真实生图链路测试：sex 场景全链路（planner LLM → translate LLM → build_prompt → slots LLM → AnimaTool）。

用法: py -3 scripts/live_sex_chain_test.py
输出: outputs/integration_test/sex_chain_<ts>.png + 各阶段 prompt 打印。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_comfyui_selfie.config_store import flatten_config, load_simple_yaml
from telegram_comfyui_selfie.service import TelegramComfyUIService
from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.image_planning import plan_roleplay_image
from telegram_comfyui_selfie.generation import do_generate


def make_live_service() -> TelegramComfyUIService:
    root = Path(".tmp/live_sex_test")
    root.mkdir(parents=True, exist_ok=True)
    svc = TelegramComfyUIService(root / "config.yml", root / "state.json")
    src = Path("data/config.yml")
    loaded = flatten_config(load_simple_yaml(src))
    svc.config.update(loaded)
    # 不污染生产日志/数据库：state 与日志全部落在 .tmp 下
    svc.config["long_memory_db_path"] = ""
    svc.config["user_log_enabled"] = False
    svc.config["user_log_dir"] = str(root / "logs")
    return svc


def setup_session(svc: TelegramComfyUIService, session_id: str) -> None:
    state = svc._get_session_state(session_id)
    state.update({
        "custom_character": "狐雪",
        "custom_series": "原创",
        "custom_role_name": "狐娘",
        "custom_bot_name": "狐雪",
        "custom_bot_self_name": "我",
        "persona_user_set": True,
        "custom_positive_prefix": (
            "1girl, long hair, blond hair, bangs between eyes, blue eyes, fox ears, "
            "fluffy fox tail, b cup, slim"
        ),
        "custom_count": "1girl, solo",
        "custom_user_gender": "male",
        "purity": 0,
        "purity_user_set": True,
    })
    session_schema.set_wardrobe(state, {})
    session_schema.set_outfit(state, "")
    svc._set_character_place(session_id, "home", "家里", 0.95, source="test")
    session_schema.set_user_place(state, key="home", updated_at=time.time(), co_located=True)


async def main() -> None:
    svc = make_live_service()
    sid = "telegram:LIVE_SEX_TEST"
    setup_session(svc, sid)
    payload_logs = []
    svc._ulog = lambda session_id, kind, text: payload_logs.append((kind, text))

    intent = "卧室里她跨坐在你身上做爱，骑乘位，两人完全赤裸，她双手撑在你的胸口，能清楚看到交合处"
    print("[1/4] plan_roleplay_image (真实 planner LLM)...")
    print(f"      intent: {intent}")
    plan = await plan_roleplay_image(
        svc,
        sid,
        mode="chat",
        intent=intent,
        mood=" intimate, flushed",
        weather_data={"desc": "晴", "temp": "26"},
    )
    print("      plan:")
    print(json.dumps(plan, ensure_ascii=False, indent=2))

    print("[2/4] _translate_to_tags (真实翻译 LLM)...")
    english = await svc._translate_to_tags(
        plan["scene"],
        session_id=sid,
        view=plan.get("view") or "",
        is_intimate=bool(plan.get("is_intimate")),
    )
    print(f"      english scene:\n      {english}")

    print("[3/4] do_generate (build_prompt + slots LLM + AnimaTool turbo_v1)...")
    t0 = time.time()
    ok, images, err = await do_generate(
        svc,
        english,
        session_id=sid,
        one_shot_appearance=plan.get("new_appearance_tags") or "",
        is_intimate=bool(plan.get("is_intimate")),
        partner_in_frame=bool(plan.get("partner_in_frame")),
        device_in_frame=bool(plan.get("device_in_frame")),
        clothing_off=plan.get("clothing_off") or "",
    )
    print(f"      耗时 {time.time() - t0:.1f}s ok={ok} images={len(images)} err={err!r}")

    slots = getattr(svc, "_last_prompt_slots", None)
    if slots is not None:
        print("      === PROMPT_SLOTS.positive ===")
        print(f"      {slots.positive}")
        print("      === PROMPT_SLOTS.negative ===")
        print(f"      {slots.negative}")
    for kind, text in payload_logs:
        if "PAYLOAD" in kind:
            print(f"      === {kind} ===")
            print(f"      {text}")

    if ok and images:
        out_dir = Path("outputs/integration_test")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"sex_chain_{ts}.png"
        path.write_bytes(images[0])
        print(f"[4/4] 保存图片: {path} ({len(images[0])} bytes)")
    else:
        print("[4/4] 生图失败")
        raise SystemExit(1)

    if svc.comfy_session and not svc.comfy_session.closed:
        await svc.comfy_session.close()


if __name__ == "__main__":
    asyncio.run(main())
