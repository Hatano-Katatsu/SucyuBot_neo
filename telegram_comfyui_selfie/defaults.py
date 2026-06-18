WEEKDAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

MENU_BODY = (
    "推荐先设置这几项：\n"
    "  1. /角色 <角色名>      设定角色，自动补全人设、外型并存档\n"
    "  2. /纯良度 0~10|auto   控制角色边界；auto 交给系统判断\n"
    "  3. /天气设置 <城市>    让聊天和推送知道所在地天气\n"
    "  4. /推送频率 <次数>    每天主动发图次数；0 为关闭\n\n"
    "日常最常用：\n"
    "  /自拍                 按当前聊天情境生成一张图\n"
    "  /新场景               切掉上一轮话题，避免旧场景继续串进来\n"
    "  /记忆                 查看当前角色的长期记忆\n"
    "  /人设查看             查看角色、人设、外型、地区、纯良度\n\n"
    "详细菜单：\n"
    "  /菜单 设置     基础配置和首次使用路线\n"
    "  /菜单 角色     角色档案、人设、外型、画风\n"
    "  /菜单 生图     自拍、提示词、ComfyUI 状态\n"
    "  /菜单 记忆     长期记忆查看、搜索、删除\n"
    "  /菜单 推送     主动推送频率和调度\n"
    "  /菜单 动线     每日动线、城市地点、天气和用户当前位置\n"
    "  /菜单 上下文   新场景和短期注意\n"
    "  /菜单 调试     检查、测试和管理入口\n"
    "  /菜单 全部     完整命令列表"
)

MENU_TOPICS = {
    "设置": (
        "首次使用建议按这个顺序来：\n"
        "  /角色 <角色名>      设定当前扮演角色，并自动补全人设和身体特征\n"
        "  /纯良度 0~10|auto   设定角色边界；数字越高越保守，auto 为自动判断\n"
        "  /天气设置 <城市>    写入当前会话所在地和时区，用于聊天、自拍和推送\n"
        "  /推送频率 <次数>    设定每天主动发图次数，0 表示关闭\n\n"
        "想微调时再用：\n"
        "  /人格 <文本>        直接改角色说话方式、性格和习惯\n"
        "  /外型               查看或修改穿搭、物种特征、发型瞳色\n"
        "  /个性设置 <项> <值>  修改角色命名、自称、关系等细节\n"
        "  /人设查看           检查当前全部个性化设置"
    ),
    "角色": (
        "角色和人设：\n"
        "  /角色 <角色名>          设定角色，自动补全人设和身体特征，并自动存档\n"
        "  /角色 list              列出角色池里的档案\n"
        "  /角色 load <名称>       切换到已有角色，并清空上一角色的短期对话\n"
        "  /角色 delete <名称>     删除指定角色档案\n"
        "  /角色 clearup           仅清空角色池，不改变当前人设\n"
        "  /角色 reset             一键恢复全局默认（含角色池与对话）\n\n"
        "细节调整：\n"
        "  /人格 <文本>            改角色性格、语气、习惯\n"
        "  /外型                   查看或修改穿搭、发型、瞳色、配饰等\n"
        "  /画风 查看|添加|删除|切换  管理画风池\n"
        "  /个性设置 <项> <值>      改称呼、自称、关系、地区等\n"
        "  /角色 reset             硬重置：清空人设/角色/外型/称呼/地区/纯良度/角色池，并重置对话"
    ),
    "生图": (
        "图片相关：\n"
        "  /自拍              根据当前聊天情境生成一张图\n"
        "  聊天中想看照片、穿搭或当前场景时，模型也会自动调用配图工具\n"
        "  /提示词            查看最终提示词拼接示例\n"
        "  /生图状态          查看 ComfyUI 连通性、模型和参数\n"
        "  /测试生图 <文本>   直接用文本测试 ComfyUI 生图链路\n"
        "  /turbo on|off      切换 Turbo 加速\n\n"
        "图片会优先参考当前角色、人设、外型、天气、最近对话和长期偏好。"
    ),
    "记忆": (
        "长期记忆按“当前会话 + 当前角色”隔离：\n"
        "  /记忆              查看当前角色的长期记忆\n"
        "  /记忆 搜索 <词>    搜索相关记忆\n"
        "  /记忆 删除 <ID>    删除一条记忆\n"
        "  /记忆 清空 确认    清空当前角色的长期记忆\n"
        "  /记住 <内容>       手动写入一条长期记忆\n"
        "  /忘记 <ID或关键词> 删除指定记忆，关键词会先列出候选\n\n"
        "系统也会自动提取稳定偏好、关系、边界和长期设定；当前衣服、地点、临时话题不会当成长期记忆。"
    ),
    "推送": (
        "主动推送：\n"
        "  /推送频率 <次数>     设定每天主动发图次数，0 为关闭\n"
        "  /调度                查看今日推送计划\n"
        "  /测试推送 [mode]     强制触发推送，mode 可用 normal/morning/ntr\n"
        "  /天气设置 <城市>     推送会参考城市、天气和本地时间\n\n"
        "推送图会避开刚聊完的旧场景，除非用户明确要求延续。"
    ),
    "动线": (
        "每日动线与空间状态：\n"
        "  /天气设置 <城市>      设置当前会话城市、时区和天气来源；启用城市地点增强时，会生成公园、家中、公司、学校、商场、大街等地点目录\n"
        "  /天气 [城市]          查看当前或指定城市天气\n"
        "  /推送频率 <次数>      主动推送会按现实时间、星期/节假日、天气和城市地点选择角色所在场所\n"
        "  /人设查看             查看当前城市、时区、空间关系和个性化设定\n"
        "  /个性设置 关系 <文本>  写入额外空间关系，例如同居、异地、同公司、同学校；它会作为高级覆盖项，不替代自动动线\n\n"
        "聊天时用户可以直接说“我在商场/公司/家里/路上”，系统会短时间记住用户位置，并和角色动线一起判断同城、异地、约见或发自拍。"
    ),
    "上下文": (
        "短期注意和场景边界：\n"
        "  /新场景              开启新的短期场景，之后默认不主动延续旧话题、旧动作和旧图片\n"
        "  /上下文重置          /新场景 的别名\n"
        "  /清空上下文          /新场景 的别名\n\n"
        "系统也会在用户说“换个话题、下一幕、不聊这个、说点别的”等明显切换语句时自动切场景。\n"
        "长期记忆不会被 /新场景 清掉，只是不让上一轮短期剧情继续污染当前轮。"
    ),
    "调试": (
        "检查和管理：\n"
        "  /天气 [城市]       查看城市天气\n"
        "  /生图状态          查看 ComfyUI 连通性和生图参数\n"
        "  /提示词            查看最终提示词拼接示例\n"
        "  /测试生图 <文本>   测试生图链路\n"
        "  /测试推送 [mode]   测试主动推送\n"
        "  /调度              查看今日推送计划\n"
        "  /管理 [面板]       打开管理入口（角色池/会话/位置）"
    ),
    "全部": (
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
        "  /个性设置 [项] [值] 角色命名、自称、关系等设置\n"
        "  /角色 reset        硬重置：清空人设/角色/外型/称呼/地区/纯良度/角色池，并重置对话\n\n"
        "生图\n"
        "  /测试生图 [text]   直接用文本测试 ComfyUI 生图\n"
        "  /turbo on/off      切换 Turbo 加速\n"
        "  /提示词            查看最终提示词拼接示例\n"
        "  /生图状态          查看 ComfyUI 连通性和参数\n\n"
        "天气\n"
        "  /天气 [城市]       查看城市天气\n"
        "  /天气设置 [城市]   设置当前会话天气城市和时区\n\n"
        "动线/世界\n"
        "  /菜单 动线         查看每日动线、城市地点、天气和用户位置如何影响聊天、生图与推送\n"
        "  /个性设置 关系 <文本>  可写额外空间关系，高级覆盖自动动线\n\n"
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
        "  /菜单 或 /帮助     显示快速菜单"
    ),
}

MENU_TOPIC_ALIASES = {
    "setup": "设置",
    "set": "设置",
    "config": "设置",
    "配置": "设置",
    "新手": "设置",
    "开始": "设置",
    "人设": "角色",
    "人格": "角色",
    "外型": "角色",
    "外貌": "角色",
    "persona": "角色",
    "character": "角色",
    "char": "角色",
    "image": "生图",
    "img": "生图",
    "photo": "生图",
    "pic": "生图",
    "自拍": "生图",
    "拍照": "生图",
    "图片": "生图",
    "memory": "记忆",
    "mem": "记忆",
    "remember": "记忆",
    "push": "推送",
    "schedule": "推送",
    "调度": "推送",
    "world": "动线",
    "route": "动线",
    "routes": "动线",
    "location": "动线",
    "place": "动线",
    "动线": "动线",
    "世界": "动线",
    "位置": "动线",
    "地点": "动线",
    "context": "上下文",
    "ctx": "上下文",
    "scene": "上下文",
    "场景": "上下文",
    "debug": "调试",
    "status": "调试",
    "test": "调试",
    "检查": "调试",
    "管理": "调试",
    "all": "全部",
    "full": "全部",
    "list": "全部",
    "命令": "全部",
    "完整": "全部",
}

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
    "user_log_enabled": True,
    "user_log_dir": "",
    "chat_reply_length": "",
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
    "world_runtime_enabled": True,
    "world_city_places_enabled": True,
    "world_city_places_ttl_days": "30",
    "world_user_place_ttl_hours": "4",
    "world_holiday_dates": "",
    "world_workday_dates": "",
}
