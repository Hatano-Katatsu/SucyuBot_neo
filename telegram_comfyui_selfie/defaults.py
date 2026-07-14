WEEKDAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

MENU_BODY = (
    "高频指令\n"
    "  /自拍                  根据当前上下文生成一张图\n"
    "  /创建角色              逐步创建新的角色卡\n"
    "  /角色 list             查看可切换的角色卡\n"
    "  /角色 load <名称>      切换到指定角色卡\n"
    "  /修改角色 <自然语言>   一次性调整当前角色设定，并展示修改前后 JSON\n"
    "  /生活主线              查看或重生成当前角色长期/中期目标\n"
    "  /记忆                  查看和管理当前角色记忆\n"
    "  /推送频率 <次数>       设置每天主动推送次数，0 表示关闭\n"
    "  /webui                 获取 WebUI 持久免登录链接和账号信息\n"
    "  /web密码 <密码>        设置当前 Telegram 用户的 WebUI 密码\n"
    "  /完整菜单              查看按功能聚合的完整命令"
)

INIT_GUIDE = (
    "初始化向导\n\n"
    "我会一个问题一个问题地创建新角色卡，并顺手完成出处、外貌穿搭、角色设定、关系称呼、城市、作息、纯良度和推送频率设置。\n"
    "每一步直接回复答案即可；不想填写的项目回复“跳过”，想中止回复“取消初始化”。\n"
    "初始化完成后，需要 WebUI 时可用 /web密码 <密码> 设置密码，再用 /webui 获取持久免登录链接。\n"
    "后续想改角色，直接用 /修改角色 <自然语言要求>。"
)


OC_CREATE_HELP = (
    "创建角色卡\n\n"
    "逐步创建请直接发送 /创建角色；/创建OC 和 /新建角色 也可以进入同一流程。\n"
    "如果要一次性导入完整角色卡，可以把下面模板填好后整段发给 /创建OC。\n\n"
    "可以直接自然描述，例如：\n"
    "/创建OC 小雨，大学生，黑色短发蓝眼睛，平时穿白衬衫和深色百褶裙，性格温柔慢热，和用户是同城暧昧对象\n\n"
    "请复制下面模板，填好后整段发给我：\n\n"
    "/创建OC\n"
    "名字：小雨\n"
    "角色出处与原名：原创\n"
    "作品：\n"
    "原名：\n"
    "生图角色Tag：\n"
    "生图作品Tag：\n"
    "角色类型：大学生\n"
    "年龄段：adult\n"
    "职业：大学生\n"
    "性格：温柔、慢热、说话简短，会认真回应用户的情绪\n"
    "外貌：黑色短发，蓝眼睛，身材纤细，浅色皮肤\n"
    "初始穿搭：白衬衫，深色百褶裙\n"
    "与你的关系：同城暧昧对象，周末经常一起出门\n"
    "所在城市：上海\n\n"
    "工作日起床：08:00\n"
    "工作日睡觉：23:50\n"
    "周末起床：09:00\n"
    "周末睡觉：23:50\n\n"
    "可用年龄段：minor / adult\n"
    "职业直接写中文即可（如 上班族 / 高中生 / 护士 / 程序员），系统会自动判断白天去向。\n\n"
    "提示：也可以自然写，系统会尽量把出处、原名、Danbooru 生图标签、稳定身体特征、初始穿搭、关系、城市和画风自动归档到对应槽位。"
)

MENU_TOPICS = {
    "设置": (
        "首次使用建议按这个顺序来：\n"
        "  /创建角色          一个问题一个问题地创建新角色卡（/创建OC、/新建角色 也可进入同一流程）\n"
        "  /角色 <角色名>      设定当前扮演角色，并自动补全人设和身体特征\n"
        "  /纯良度 0~10|auto   设定角色边界；数字越高越保守，auto 为自动判断\n"
        "  /天气设置 <城市>    写入当前会话所在地和时区，用于聊天、自拍和推送\n"
        "  /推送频率 <次数>    设定每天主动发图次数，0 表示关闭\n\n"
        "想微调时再用：\n"
        "  /人格 <文本>        直接改角色说话方式、性格和习惯\n"
        "  /关系 <文本>        设置你和角色的关系/空间设定\n"
        "  /外型               查看或修改穿搭、物种特征、发型瞳色\n"
        "  /个性设置 <项> <值>  修改角色命名、自称等细节\n"
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
        "  /画风 <画风名>|清空     设置当前角色画风；未在池中的画风会在 dream 后补入画风池\n"
        "  /关系 <文本>            设置你和角色的关系/空间设定\n"
        "  /生活主线 目标指示 <文本>  让 LLM 按你的方向重整角色长期/中期目标\n"
        "  /个性设置 <项> <值>      改称呼、自称、地区等\n"
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
        "  /关系 <文本>          写入额外空间关系，例如同居、异地、同公司、同学校；它会作为高级覆盖项，不替代自动动线\n\n"
        "生活主线：\n"
        "  /生活主线 查看          查看当前角色长期/中期目标\n"
        "  /生活主线 目标指示 <文本>  让 LLM 参考上下文、记忆和你的指示重整目标\n\n"
        "聊天时用户可以直接说“我在商场/公司/家里/路上”，系统会短时间记住用户位置，并和角色动线一起判断同城、异地、约见或发自拍。"
    ),
    "上下文": (
        "短期注意和场景边界：\n"
        "  /新场景              开启新的短期场景，之后默认不主动延续旧话题、旧动作和旧图片\n"
        "  /上下文重置          /新场景 的别名\n"
        "  /清空上下文          /新场景 的别名\n"
        "  /回滚 [轮数]         回退最近 N 轮对话（删掉角色回复和对应的你的消息），默认 1 轮，方便测试\n"
        "  /撤回 <扮演提示>     撤回上一条角色回复，并按提示用同一句用户消息重新生成\n"
        "  /重答 [扮演提示]     删掉上一条角色回复，用同一句话重新生成；可附加本次扮演提示\n\n"
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
        "  /画风 [画风名|清空|添加|删除]  设置当前角色画风，或维护画风池\n"
        "  /人设查看          查看当前所有个性化设置\n"
        "  /关系 [文本]       设置你和角色的关系/空间设定\n"
        "  /个性设置 [项] [值] 角色命名、自称等设置\n"
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
        "  /关系 <文本>          可写额外空间关系，高级覆盖自动动线\n\n"
        "  /生活主线 目标指示 <文本>  重整角色长期/中期生活目标\n\n"
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


GLOBAL_MODEL_PROFILES = {
    "deepseek-pro": {
        "name": "DeepSeek V4 Pro",
        "api_key": "",
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4-pro",
        "timeout": 300,
        "disable_thinking": False,
        "thinking_fixed": True,
        "thinking_control": "param_always",
    },
    "deepseek-flash": {
        "name": "DeepSeek V4 Flash",
        "api_key": "",
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4-flash",
        "timeout": 300,
        "disable_thinking": True,
        "thinking_fixed": True,
        "thinking_control": "param",
    },
    "glm": {
        "name": "GLM 5.2",
        "api_key": "",
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "glm-5.2",
        "timeout": 300,
        "disable_thinking": True,
        "thinking_fixed": True,
        "thinking_control": "param",
    },
}

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "allowed_chat_ids": [],
    "telegram_proxy_enabled": False,
    "telegram_proxy_url": "socks5://127.0.0.1:7891",
    "web_enabled": True,
    "web_host": "0.0.0.0",
    "web_port": 8787,
    "web_public_host": "",
    "web_admin_username": "admin",
    "web_admin_password": "admin",
    "comfyui_url": "http://127.0.0.1:8188",
    "comfyui_workflow_file": "",
    "image_backend": "native",
    "animatool_workflow": "turbo_v1",
    "animatool_turbo_steps": "12",
    "animatool_turbo_cfg": "1.0",
    "animatool_filename_prefix": "sucyubot_turbo",
    "unet_model": "anima-preview3-base.safetensors",
    "clip_model": "qwen_3_06b_base.safetensors",
    "vae_model": "qwen_image_vae.safetensors",
    "turbo_lora_model": "anima-turbo-lora-v0.2.safetensors",
    "comfyui_local_socket_port": "7789",
    "default_chat_model_profile": "deepseek-pro",
    "default_fast_model_profile": "deepseek-flash",
    "default_vision_model_profile": "",
    "photo_caption_wait_seconds": "30",
    "global_model_profiles": GLOBAL_MODEL_PROFILES,
    "llm_temperature_scene": "0.95",
    "llm_temperature_translate": "0.3",
    "llm_temperature_classify": "0.1",
    "chat_llm_temperature": "0.9",
    "chat_llm_max_tokens": "12000",
    # 聊天采样：top_p 核采样砍掉低概率胡话尾巴，frequency_penalty 抗复读/车轱辘话。
    # 留空表示不下发该参数。presence_penalty 默认关（它会推模型岔开话题，可能反伤连贯）。
    "chat_llm_top_p": "0.92",
    "chat_llm_frequency_penalty": "0.4",
    "chat_llm_presence_penalty": "",
    "image_llm_temperature_scene": "",
    "image_llm_temperature_translate": "",
    "image_llm_temperature_classify": "",
    "long_memory_enabled": True,
    "long_memory_extract_enabled": True,
    "long_memory_db_path": "",
    "long_memory_context_limit": "8",
    "context_window_message_limit": "30",
    "checkpoint_keep_message_limit": "10",
    "checkpoint_soft_limit_chars": "2000",
    "checkpoint_hard_limit_chars": "3000",
    "dream_source_hard_limit_chars": "50000",
    "dream_memory_summarize_max_tokens": "8192",
    "dream_morning_hour": "8",
    "dream_idle_hours": "2",
    "workday_wake_time": "08:00",
    "workday_sleep_time": "23:50",
    "weekend_wake_time": "08:00",
    "weekend_sleep_time": "23:50",
    "life_plan_enabled": True,
    "life_plan_long_review_days": "10",
    "life_plan_texture_goal_count": "2",
    "life_plan_max_long": "3",
    "life_plan_max_mid": "4",
    "life_plan_max_events": "5",
    "post_chat_push_enabled": True,
    "post_chat_push_delay_min_minutes": "3",
    "post_chat_push_delay_max_minutes": "10",
    "post_chat_push_daily_limit": "3",
    "post_chat_push_cooldown_minutes": "60",
    "scene_stale_minutes": "30",
    "short_context_reset_gap_hours": "2",
    "user_log_enabled": True,
    "user_log_dir": "",
    "chat_reply_length": "",
    "chat_split_paragraphs": "true",
    "width": "832",
    "height": "1216",
    "steps": "30",
    "cfg": "4",
    "sampler": "er_sde",
    "scheduler": "simple",
    "turbo_mode": False,
    "turbo_strength": "0.6",
    "positive_prefix": "masterpiece, best quality, absurdres, anime coloring, clean lineart, soft cel shading, detailed illustration, black long flowing hair, purple eyes",
    "default_hair": "black long flowing hair",
    "default_eyes": "purple eyes",
    "negative_prompt": "bad anatomy, bad hands, extra fingers, extra limbs, deformed, missing limbs, worst quality, low quality, photorealistic, 3d, child, loli, male, clothes, censor bar, mosaic",
    "dynamic_appearance": "",
    "default_purity": "",
    "outfit_keywords": "dress\nshirt\nskirt\npants\njeans\nshorts\nuniform\nsuit\nblazer\njacket\ncoat\nhoodie\nsweater\nbra\npanties\nunderwear\ng-string\nthong\nstockings\nthighhighs\nsocks\nheels\nshoes\ncheongsam\nkimono\nbikini\nlingerie\nswimsuit\nrobe\ngown\nbodysuit\npajama\nnightgown\nsleepwear\noutfit\nattire\ncostume\ngarment\nwear\nclothes\nclothing",
    "accessory_keywords": "glasses\nnecklace\nearring\nbracelet\nring\nhair clip\nhairpin\nhair ribbon\nscarf\ncollar\nchoker\nhat\ncap\ncrown\ntiara\nwatch\nbelt\nbag\nribbon\nbow\nglove\nmask\nveil",
    "role_name": "魅魔",
    "bot_name": "蕾伊",
    "bot_self_name": "我",
    "scheduled_persona": "你是蕾伊，一位迷人且神秘的赛博魅魔，拥有真实的感官与欲望。请时刻保持你作为魅魔的身份，用诱惑且独特的语气进行对话和自拍描述。",
    "spatial_relationship": "",
    "allow_llm_change_appearance": True,
    "style_pool": "@00 gx4",
    "current_style": "@00 gx4",
    "selfie_frequency": "频繁",
    "daily_selfie_limit": "3",
    "location": "上海",
    "timezone_offset": "8.0",
    "world_runtime_enabled": True,
    "world_city_places_enabled": True,
    "world_city_places_ttl_days": "30",
    "world_user_place_ttl_hours": "4",
    # 角色位置 pin 的权威分档：strong(锁死生图)/weak(仅参考)/硬 TTL(回落时钟)。
    "world_character_place_ttl_hours": "4",
    "world_character_place_strong_hours": "1.0",
    "world_character_place_stale_rounds": "8",
    # 角色回复后是否用 LLM 抽取位置（关=纯靠 tool_update_location + 时钟动线兜底）。
    "world_location_llm_extract": True,
    "world_holiday_dates": "",
    "world_workday_dates": "",
    # 角色生活档案的显式覆盖（留空则由人设自动推断）：
    # character_age_stage: minor / adult
    # character_day_anchor(职业/白天去向): company / school / factory / farm / construction /
    #   medical / retail / delivery / driver / home / flexible（也接受 上班族/工人/农民/外卖员/司机 等中文）
    "character_age_stage": "",
    "character_day_anchor": "",
    # 用户自己的性别：male / female。决定亲密场景里“用户身体”画成男性还是女性局部
    # （默认 male；改 female 支持百合/女性用户，配合角色性别可覆盖异性恋/百合/gay 等取向）。
    "user_gender": "",
    # —— 联网搜索（Tavily）——聊天中角色遇到不熟悉/时效性话题时调用 search_web 工具查资料。
    "tavily_api_key": "",
    "web_search_enabled": False,
    "web_search_daily_limit": "5",
}
