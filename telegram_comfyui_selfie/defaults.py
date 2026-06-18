WEEKDAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

MENU_BODY = (
    "拍照\n"
    "  /自拍              根据当前情境生成一张自拍\n\n"
    "个性化\n"
    "  /人格 [文本]       设定角色性格/语气/习惯，立即生效\n"
    "  /角色 [文本]       设定角色名，自动补全人设和身体特征，并自动存档\n"
    "  /角色 load/list/delete  载入、列出、删除角色档案\n"
    "  /角色 reset/clearup  reset 一键恢复全局默认（含角色池与对话）；clearup 仅清空角色池\n"
    "  /纯良度 [0~10|auto] 查看或设定角色纯良度\n"
    "  /外型              查看或修改穿搭、物种特征、发型瞳色\n"
    "  /画风 [查看/添加/删除/切换]  管理画风池\n"
    "  /人设查看          查看当前所有个性化设置\n"
    "  /人设重置          一键恢复全局默认：清空人设/角色/外型/称呼/地区/纯良度/角色池，并重置对话\n"
    "  /个性设置 [项] [值] 角色命名、自称、关系等设置；/个性设置 reset 等同 /人设重置\n\n"
    "生图\n"
    "  /测试生图 [text]   直接用文本测试 ComfyUI 生图\n"
    "  /turbo on/off      切换 Turbo 加速\n"
    "  /提示词            查看最终提示词拼接示例\n"
    "  /生图状态          查看 ComfyUI 连通性和参数\n\n"
    "天气\n"
    "  /天气 [城市]       查看城市天气\n"
    "  /天气设置 [城市]   设置当前会话天气城市和时区\n\n"
    "推送\n"
    "  /推送频率 [次数]   设定每日主动推送次数，0 为关闭\n\n"
    "上下文\n"
    "  /新场景            开启新的短期场景，避免上一轮话题继续污染\n\n"
    "记忆\n"
    "  /记忆 查看|搜索|删除|清空  管理当前会话长期记忆\n"
    "  /记住 [内容]       手动写入一条长期记忆\n"
    "  /忘记 [ID]         删除指定长期记忆\n\n"
    "调试\n"
    "  /调度              查看今日推送计划\n"
    "  /测试推送 [mode]   强制触发推送 normal/morning/ntr\n"
    "  /管理 [面板]       管理仪表盘（角色池/会话/位置）\n"
    "  /菜单 或 /menyu    显示本菜单"
)

SCENES = [
    ("靠在卧室窗边，穿着柔软睡裙，暖色灯光落在脸上，慵懒地看着你", "今晚的光很好看，所以想让你也看看我。"),
    ("坐在办公椅上，穿着得体的职业装，手指轻轻拨开发丝，眼神带着一点疲惫和亲昵", "工作有点累，不过想到你会看见这张，就又精神了一点。"),
    ("厨房晨光里端着咖啡，宽松衬衫滑落到肩头，刚睡醒一样眯着眼笑", "早安，今天也先从我开始吧。"),
]

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "allowed_chat_ids": [],
    "web_enabled": True,
    "web_host": "127.0.0.1",
    "web_port": 8787,
    "comfyui_url": "http://127.0.0.1:8188",
    "comfyui_workflow_file": "",
    "unet_model": "anima-preview3-base.safetensors",
    "clip_model": "qwen_3_06b_base.safetensors",
    "vae_model": "qwen_image_vae.safetensors",
    "turbo_lora_model": "anima-turbo-lora-v0.2.safetensors",
    "llm_api_base": "https://api.deepseek.com/v1",
    "llm_api_key": "",
    "llm_model": "deepseek-chat",
    "llm_max_tokens": "4096",
    "llm_disable_thinking": False,
    "llm_temperature_scene": "0.95",
    "llm_temperature_translate": "0.3",
    "llm_temperature_classify": "0.1",
    "chat_llm_api_base": "",
    "chat_llm_api_key": "",
    "chat_llm_model": "",
    "chat_llm_max_tokens": "",
    "chat_llm_temperature": "0.9",
    "chat_llm_disable_thinking": False,
    "image_llm_api_base": "",
    "image_llm_api_key": "",
    "image_llm_model": "",
    "image_llm_max_tokens": "",
    "image_llm_disable_thinking": False,
    "image_llm_temperature_scene": "",
    "image_llm_temperature_translate": "",
    "image_llm_temperature_classify": "",
    "long_memory_enabled": True,
    "long_memory_extract_enabled": True,
    "long_memory_db_path": "",
    "long_memory_context_limit": "8",
    "short_context_history_limit": "16",
    "short_context_reset_gap_hours": "6",
    "width": "1024",
    "height": "1024",
    "steps": "30",
    "cfg": "4",
    "sampler": "er_sde",
    "scheduler": "simple",
    "turbo_mode": False,
    "turbo_strength": "0.6",
    "positive_prefix": "masterpiece, best quality, absurdres, anime coloring, clean lineart, soft cel shading, detailed illustration, 1girl, black long flowing hair, purple eyes",
    "default_hair": "black long flowing hair",
    "default_eyes": "purple eyes",
    "negative_prompt": "extra fingers, fused fingers, too many fingers, mutated hands, bad hands, bad anatomy, deformed, disfigured, extra limbs, missing limbs, worst quality, low quality, photorealistic, 3d, child, loli, male, clothes, clothing, censor bar, mosaic, pixelated",
    "dynamic_appearance": "",
    "default_purity": "",
    "outfit_keywords": "dress\nshirt\nskirt\npants\njeans\nshorts\nuniform\nsuit\nblazer\njacket\ncoat\nhoodie\nsweater\ncheongsam\nkimono\nbikini\nlingerie\nswimsuit\nrobe\ngown\nbodysuit\npajama\nnightgown\nsleepwear\noutfit\nattire\ncostume\ngarment\nwear\nclothes\nclothing",
    "accessory_keywords": "glasses\nnecklace\nearring\nbracelet\nring\nhair clip\nhairpin\nhair ribbon\nscarf\ncollar\nchoker\nhat\ncap\ncrown\ntiara\nwatch\nbelt\nbag\nribbon\nbow\nglove\nmask\nveil",
    "character_quirk_rule": "",
    "role_name": "魅魔",
    "bot_name": "蕾伊",
    "bot_self_name": "我",
    "scheduled_persona": "你是蕾伊，一位迷人且神秘的赛博魅魔，拥有真实的感官与欲望。请时刻保持你作为魅魔的身份，用诱惑且独特的语气进行对话和自拍描述。",
    "spatial_relationship": "设定上你们同居，但工作日白天在不同的公司上班。周末和节假日通常全天待在一起。",
    "allow_llm_change_appearance": True,
    "style_pool": "@00 gx4",
    "current_style": "@00 gx4",
    "skill_md_path": "",
    "selfie_frequency": "频繁",
    "daily_selfie_limit": "3",
    "location": "上海",
    "timezone_offset": "8.0",
}
