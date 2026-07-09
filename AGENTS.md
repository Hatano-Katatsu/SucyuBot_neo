# AGENTS.md — SucyuBot_neo

## 项目概述

Telegram 机器人，结合 AI 角色扮演（DeepSeek 等 OpenAI 兼容 API）与 ComfyUI / AnimaTool 生成动漫角色自拍。项目已从 AstrBot 插件重构为独立服务，包含 Telegram Bot、WebUI 管理台、角色卡、长期记忆、短期上下文、地点动线、天气时间和生图规划。

## 技术栈

- **语言**: Python 3.11+
- **依赖**: `aiohttp>=3.9` 为主，测试环境可选 `aiohttp_socks`
- **数据库**: SQLite（长期记忆、会话状态、城市地点目录、聊天日志、模型 profile、Web 凭据）
- **配置**: YAML 优先（`data/config.yml`），不存在时回退 JSON（`data/config.json`）
- **前端**: Vanilla HTML/CSS/JS（aiohttp SPA，Web 控制台）

## 运行命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行服务（YAML 配置优先，不存在则回退 JSON）
py -3 -m telegram_comfyui_selfie --config data/config.yml

# 或一行启动
./run.cmd

# 运行测试
py -3 -m unittest tests.test_core -v

# 单独测试某个测试类/方法
py -3 -m unittest tests.test_core.ServiceTestCase.test_parse_command_with_bot_mention -v
```

> 本机注意：Git Bash 里裸 `python` 可能解析到 Windows Store 占位 stub，会以 exit 49 静默失败。一律用 `py -3`，或绝对路径 `C:\Users\17122\AppData\Local\Programs\Python\Python311\python.exe`。

## 项目结构

```text
telegram_comfyui_selfie/
├── __main__.py          # CLI 入口
├── service.py           # 核心服务类，组合所有 mixin
├── defaults.py          # 默认配置、菜单、场景
├── commands.py          # /command 处理
├── chat_context.py      # 聊天消息构建、上下文分层、checkpoint
├── generation.py        # ComfyUI/AnimaTool 生图与 PromptSlots
├── image_planning.py    # LLM 画面规划器
├── appearance.py        # 外观标签解析/合并/注入
├── prompt_intake.py     # 自然语言角色/外观输入分类
├── time_context.py      # 时间/季节/光线阶段
├── memory.py            # SQLite 长期记忆存储
├── memory_policy.py     # 记忆提取/整理策略
├── scheduler_runtime.py # 定时推送、dream、续场
├── world_runtime.py     # 地点动线、天气、城市 POI
├── telegram_io.py       # Telegram Bot API 收发、文件下载、输入增强
├── webui.py             # aiohttp Web 控制台与 REST API
├── app_store.py         # SQLite 应用状态库
├── session_schema.py    # 会话状态 schema 与分盒访问器
├── character_card.py    # 角色卡字段单一来源
├── character_checkpoint.py # 角色 JSON 检查点导出/导入/清理
└── static/              # Web 前端

根目录:
├── Start-SucyuBot.cmd / Start-SucyuBot.ps1
├── run.cmd
├── scripts/compare_llm_chat_prompts.py
├── config.example.yml / config.example.json
├── requirements.txt
├── AGENTS.md
└── README.md
```

## 代码风格

- 使用 `from __future__ import annotations`
- 类通过 mixin 多重继承组织：`TelegramComfyUIService(ProcessRestartMixin, TelegramIOMixin, CharacterCheckpointMixin, CommandHandlersMixin, ChatContextMixin, MemoryPolicyMixin, SchedulerRuntimeMixin, WorldRuntimeMixin, GitUpdateMixin)`
- Mixin 不定义 `__init__`；初始化集中在 `TelegramComfyUIService.__init__`
- 所有 I/O 方法使用 `async def`
- 配置通过 `self.config.get(key, default)` 访问；会话级覆盖通过 `self._get_session_cfg(session_id, key, default)`
- 日志统一用 `logger = logging.getLogger(__name__)`
- 注释用中文，docstring 用中文，代码标识符用英文
- 每轮功能完成后更新本文件：同步当前框架状态、已完成事项、测试结果和下一阶段目标，避免后续接手重新考古

## 测试

- 测试文件：`tests/test_core.py`
- 基于 `unittest.TestCase` + `unittest.mock.AsyncMock`
- 每个异步测试方法内部用 `asyncio.run(run())`
- `make_service()` 创建临时目录 + 最小配置
- 添加新功能时必须写测试；核心路径已覆盖命令解析、提示词构建、记忆 CRUD、角色切换、外观合并、Web 序列化、上下文缓存、模型 profile、Telegram 图片/引用输入
- 测试临时目录为 `.tmp/tests`，测试进程首次创建时会自动清理上次残留
- 测试进程设置 `SUCYUBOT_TEST_FAST_SQLITE=1`，仅测试环境关闭 SQLite 同步/使用内存 journal，生产默认不受影响
- 真实前缀缓存请求测试默认跳过；需要额外设置 `$env:SUCYUBOT_TEST_LIVE_CACHE_PROBE='1'` 后单独运行 `py -3 -m unittest tests.test_core.ServiceTestCase.test_live_chat_context_cache_probe_uses_current_config_when_available -v`

## 当前架构状态（2026-06-24）

### 配置与存储

- 服务默认读取 `data/config.yml`，不存在时回退 `data/config.json`。
- `state.json` 已弃用；首次启动时若 SQLite 无数据但旧 `state.json` 存在，会自动迁移到 SQLite 并备份旧文件。
- `app_store.py` 负责应用状态库：`session_state`、`city_catalogs`、`chat_messages`、`checkpoints`、`diaries`、`context_meta`、`web_credentials`、`model_profiles`、`user_model_settings`、`llm_usage`。
- `session_schema.py` 是会话状态字段单一来源；`character`、`clothing`、`place`、`context`、`session` 等盒子已接入懒迁移和访问器。为兼容旧代码，部分扁平键仍会双写，后续清理时不要直接删除。

### 模型配置

- 模型配置统一走 profile：全局 `global_model_profiles` + 用户私有 `model_profiles`。全局 profile 可由 YAML 或管理员 WebUI 模型面板维护；用户 profile 仅该 Telegram 用户可见可用。
- 默认全局 profile 仅保留 `deepseek-pro`（默认思考开）、`deepseek-flash`（默认思考关）、`glm`（默认思考关），kimi 系列已移除。
- 思考状态只由模型 profile 的 `disable_thinking` / `model_think` / `model_no_think` 决定，不再按 chat/fast/vision 任务或用户单独切换。
- `default_chat_model_profile` 用于聊天模型，`default_fast_model_profile` 用于生图辅助模型，`default_vision_model_profile` 用于用户图片/引用图片理解模型。
- 视觉模型默认留空；留空时不处理图片输入和引用图片。
- WebUI/API 返回模型 `api_key` / `api_key_no_think` 时显示为 `********`；保存空值或 `********` 会保留旧密钥。
- WebUI 模型 profile 编辑器只暴露常用字段（profile id、名称、base_url、api_key、model、max_tokens、timeout），不要求用户填写 JSON，也不暴露 thinking / fixed thinking 等内部兼容字段。
- 聊天回复链路可通过 `chat_llm_top_p` / `chat_llm_frequency_penalty` / `chat_llm_presence_penalty` 配置采样参数；这些参数只在真实用户回复请求（`chat` / `chat-final`）中显式下发，不影响 checkpoint、dream、memory 等结构化低温任务。

### 聊天上下文

聊天 prompt 按变化频率分层，目标是减少互相冗余并提升 DeepSeek/OpenAI 兼容接口的 prefix cache 命中率：

1. 静态 system：身份、人设、关系、工具规则、照片历史规则、固定发图节奏规则。
2. 天级/低频稳定层：低频对话控制、角色历史提要、按重要性选取的长期记忆。
3. 半稳定状态快照：当前可见外型、衣橱、当前附加外貌；低频世界规则（角色身份、地点来源、空间覆盖、位置优先级/不瞬移段落）固定在 checkpoint/历史之前。
4. 世界当前条件半稳定槽：城市/日期、天气、季节/自然光、自然光硬规则；按城市/日期/天气/自然光阶段签名缓存，只在这些字段变化时更新，不随每分钟滚动。
5. checkpoint 会话连续性：近期已折叠对话摘要。
6. 未折叠历史：checkpoint 之后的真实 `user/assistant/system` 历史。
7. 动态尾部：精确当前时间、按需本轮用户位置/空间关系判断、overdue 发图提醒、场景断档提醒、当前用户输入。

关键约束：

- `_chat_prompt_history(state)` 使用 checkpoint 之后的全量未折叠历史，checkpoint 之间只追加不滑动。
- checkpoint 裁剪后第一条必须是 `user`；多余的孤立 `assistant` / `system` 会进入 checkpoint 摘要。
- dream 和 dream 记忆整理只读取实际 `user/assistant` 对话，不消费照片历史 system。
- 照片历史是真正的历史 `system` 消息，位于 checkpoint 锚定的未折叠历史段中，保留到被正常历史裁剪为止，并参与 checkpoint 摘要；内容只保留最终生图 `nltag/tags` 与瘦身后的短意图/情绪/必须包含，不再写入完整原始草案、外观槽位或全部 PromptSlots，避免照片记录污染后续前缀缓存。
- 主动推送和对话后续场推送的生图 planner 复用 `_build_chat_messages()` 生成的正式聊天上下文前缀，去掉占位 user 后再追加 planner 稳定 system、本次动态 system 和当前时段 user；照片历史 system 也在这个正式前缀内，推送路径不再额外拼一份“最近照片摘要”或重复的短期连续性。
- 外观/衣柜、低频世界规则、世界当前条件或动态尾部结构变化时，如果未折叠历史达到 `context_window_message_limit / 2`，会异步强制 checkpoint 一次，近似恢复后续前缀稳定；动态签名不包含精确分钟，避免时钟滚动触发无意义 checkpoint。
- 普通聊天保留固定位置的世界规则槽和世界当前条件槽；只有本轮出现地点/天气/时间/发图等信号时，才展开本轮用户位置/动线/空间关系动态尾部，避免每句普通对话都跑高频世界判断。
- `/新场景` / `/上下文重置` 会先对切换前未折叠上下文跑一次 checkpoint 摘要和记忆提取，再清空当前模型侧 `chat_history` 和 checkpoint 摘要，并把 checkpoint 边界推进到当前最新消息；SQLite `chat_messages` 不删除，后续 dream 仍会读取真实 `user/assistant` 对话。
- 兼容部分 OpenAI 兼容端点把工具调用以 DSML 文本写进 `message.content` 的情况；聊天链路会提取 `<...DSML...invoke>` 为内部工具调用并清理残留标记，避免原始工具 XML/DSML 泄漏给用户。

### 记忆与角色历史

- 长期记忆按 `session_id + character` 隔离。
- 长期记忆注入时直接按重要性选取前 N 条，不维护也不使用 `hit_count`；重要性由 checkpoint/dream 的记忆整理阶段审视。
- checkpoint 只负责近期已折叠对话连续性。
- 角色历史提要只负责宏观关系/剧情阶段。
- 长期记忆只负责高重要度稳定事实、偏好、边界和纠正。
- 用户明确提到的未来/待完成时间节点在 checkpoint 记忆提取阶段通过提示词软约束保存为 `event`；dream 整理过时时间节点时，只有事件已解决/取消/被替代，或已从近期日记、checkpoint 和当前窗口完全淡出，才删除或合并。
- 手动记忆（`kind=manual`）不被自动整理删除。
- 角色生活线保存在 SQLite `life_plans(session_id, character_key)`，结构化长线/中期线/今日片段只给 dream、WebUI 与推送 planner 后台使用；聊天 prompt 只注入渲染后的“生活底色”自然语言，不泄漏目标/计划/任务式结构。长期目标带 `dimension`，生成时要求从生活、理想、事业、爱好、身份、关系等不同维度拆开，不把同一条关系目标改写成多条。dream 后更新，首次聊天、手动推送或新建角色缺当天生活线时会后台/同步生成；当老角色已有当天生活线但缺少长期/中期目标时，也会刷新目标，并要求模型从角色视角自行根据原始人设、历史、记忆、近期上下文和日记推断核心驱动力。WebUI 支持长线/中期线增删改、按用户指示重生成；Telegram 支持 `/生活主线` 查看/生成和 `/生活主线 目标指示 <要求>`。按用户指示重生成目标时会把原长期/中期目标作为草案注入，并要求 LLM 一次性输出完整 `long_goals` + `mid_goals` 替换旧版本，不走零散 ops。
- dream 日记、checkpoint、长期记忆提取、dream 记忆整理和角色历史提要都必须显式遵守视角映射：`User`/用户是人类用户，`Assistant`/角色是当前 bot 角色；日记第一人称“我”是 bot 角色，不得把用户和角色的动作、承诺、情绪或身体状态互换。checkpoint/current window 形式的多轮聊天不能被包成一整段“用户发言”交给记忆提取。

### 角色系统

- `character_card.py` 是角色卡字段单一来源；导出、快照、导入/写回共用同一字段表。
- `custom_scheduled_persona` 只存纯人格描述，禁止把身份、角色类型、关系、职业焊进人格文本。
- 身份、角色类型、关系、职业等信息各有独立字段，读取侧实时组装：`_get_effective_persona()`、`_build_chat_messages()`、生图/推送身份行各自拼接。
- 画风 `style` 跟随角色卡保存；`/画风 <画风名>` 不要求画风已在池中，会直接写入当前角色卡字段。空画风是有效状态，表示该角色不注入画风/画师，不回退全局默认。
- 当前角色切换会保存离开角色的可变状态，切回时恢复该角色的上下文、衣柜、位置和照片历史。
- `/角色 reset` 是硬重置入口；轻量上下文切场景使用 `/新场景` 或相关别名。

### 世界状态与地点动线

- 世界状态结合现实时间、星期/节假日、天气、城市、角色年龄阶段、职业/白天去向和用户位置。
- 角色位置与用户位置都有 TTL；显式工具声明优先级最高，LLM/正则抽取作为兜底。
- 对话进行中不钉死时钟地点；只给背景倾向，当前位置以对话为准。
- 推送/生图仍会使用持久位置和结构化地点约束，避免场景瞬移。
- 城市地点目录优先真实 POI：中国用高德，海外用 Google Places；失败时回落到 LLM 生成和内置示例。
- WebUI 动线按钟点预测整天时不会把"此刻持久 pin"套到每个时间段。

### 生图与 PromptSlots

- `PromptSlots` 是最终正向提示词来源；日志中会记录 `PROMPT_SLOTS`，实际 ComfyUI prompt 也保留旧版 `PROMPT` 日志方便对比。
- 核心槽位顺序：`quality -> count -> identity -> style_artist -> effective_appearance -> style_general -> safety -> scene -> one_shot_appearance`。
- `safety` 槽承载 `safe/nsfw` 等随纯良度和时段变化的评级词；不要再塞进最前面的 `quality` 槽，避免把高变动 token 放在提示词开头。AnimaTool Turbo 的 `quality_meta_year_safe` 字段仍在提交前临时组合 `quality + safety`，以兼容其 schema。
- `scene` 只描述镜头、地点、动作、光线、道具和氛围，不重复稳定外貌。
- `one_shot_appearance` 是本轮临时补充，不持久化。
- `clothing_off` 对衣物/裸体默认仍是“仅本图生效”；但当它明确命中当前已穿戴的可持久配饰（如眼镜、项链、耳环、发夹）时，生图成功后会把该配饰从当前穿搭中移除，避免下一张图被稳定外貌重新加回去。
- OC 不把中文名、昵称或作品名塞进视觉 identity；只有已知公开角色才注入角色/作品 tag。
- 亲密场景默认走 POV，只允许用户/伴侣身体局部入画；除非用户明确要求拍照、录像或对镜，才允许设备入画。
- `/配图`（同义词 `/画图`、`/绘图` 等）按当前聊天场景生成图片，不强制自拍或看镜头；命令后的参数作为最高优先级场景/视角/机位/远近/局部特写要求，但规划器只消费瘦身后的短期连续性、最近已发图片的最终 `nltag/tags` 与短意图、世界状态和记忆，不再直接吞完整聊天流水、原始草案或整段外观快照。
- `roleplay-image-plan` 在瘦身连续性之外保留“空间/身体关系硬约束”旁路：坐/站/躺/跪、脚边、腿上、身后、怀里、肩膀、背向、俯身等站位线索会单独进入 planner，并在 planner 漏写时追回到最终 `scene`，避免关键身体关系被每条 140 字截断吃掉。
- `partner_in_frame` 区分日常局部同框和真正亲密/性爱场景；日常帮吹头发、坐脚边、靠肩等只去掉 `solo` 冲突并允许必要的手/脚/肩局部，不再自动追加 `male torso` / `intimate close-up`。
- POV 正向开场只声明“用户视角看向角色”，不再默认强塞 `eye contact` / `solo`；翻译层允许保留 `she/the character` 动作主语，确保姿态和身体关系归属清楚。
- 画幅只允许 2:3（竖版）和 3:2（横版），模拟真实相机画幅；负向提示词包含 `split screen, grid, multiple panels, collage` 防止四宫格/分格出图。
- AnimaTool Turbo 路径不再提交 `neg` / `negative` 字段；自然语言 `nltag` / `tags` 尾部统一追加 `no text, no logo, no ui, no mosaic, uncensored`。

### Telegram 输入增强

- Telegram 当前图片、`reply_to_message` 图片、`external_reply` 图片只进入视觉模型描述任务。
- 单独发送无配文图片时，如果视觉模型可用，会按 `photo_caption_wait_seconds`（默认 30 秒，设为 0 关闭）等待用户下一条纯文本；等到则把该文本作为图片配文/附近上下文一起送入视觉模型，超时则按旧的纯图片逻辑处理。
- 视觉模型可参考最近两轮实际 `user/assistant` 对话和当前文字/引用线索。
- chat 模型最终只收到纯文本：引用内容、图片描述、用户当前输入。
- 引用文本支持 `quote.text`、`reply_to_message.text/caption`、`external_reply.text/caption`。
- Telegram 文件下载走 `getFile`，遵守 Bot API 20MB 文件下载限制。
- 同一会话新用户消息会取消旧的可中断文字生成/分段发送/等待配文任务，并立即处理新输入；取消前已收到的旧用户输入和 Telegram 已确认发送的 assistant 片段会写回正式上下文。生图链路使用受保护 task，聊天工具生图和 `/自拍`、`/配图` 已进入生成/发图阶段时不会被新消息取消，完成后仍会发图并记录照片历史。

### WebUI 与运维

- WebUI 支持管理员与普通用户登录；普通用户只看自己的会话与私有模型。
- 管理员可以查看用量、维护全局模型 profile、执行 Git 更新、重启服务、从当前配置文件热载运行态配置、冻结不活跃用户。
- 基础设施/运维配置如 Web host/port、日志路径、数据库路径仍只允许 YAML 修改，不通过通用 Web 配置表单写入。
- WebUI 角色面板不展示场景偏好/自拍偏好栏；这些字段保留为内部数据和兼容字段。
- WebUI 角色面板支持导出 dream 前自动生成的角色 JSON 检查点、导出当前状态，以及粘贴/上传 JSON 导入；导入模式分为“只导入基本字段”“导入长期记忆（不替换 checkpoint/上下文）”“完全覆盖”。
- WebUI 总览页包含反馈板：反馈运行时读写项目根目录 `TODO.md`，按 `## 角色名` + `<!-- session_id: ... -->` 分段；普通用户只看到自己的反馈，管理员可见全部。`TODO.md` 是运行时文件，已加入 `.gitignore`，不要提交。

## 关键行为规则

- `view=selfie` 是前摄自拍：角色看向镜头、伸手自拍，但画面中不得出现手机本体、手机 UI、消息界面、倒计时界面。正向提示词不要写 `off-frame front-facing phone camera` 这类容易诱发手机 UI 的措辞。
- `view=portrait` 是别人帮角色拍的照片：角色看向镜头、摆姿势，拍摄者在画面外，画面里只有角色。
- `/配图` / `/画图` 是自由配图：允许用户覆盖视角、机位、距离、构图和部位特写，不套用 `/自拍` 的前摄自拍硬设定。
- 只有 `view=mirror` 才允许同时出现镜子和手机。
- 非 mirror 场景负面提示词要压制 `holding phone`、`visible phone`、`phone in hand`、`mirror selfie` 等。
- 用户性别由全局 `user_gender` 或会话 `custom_user_gender` 控制，影响亲密场景中用户身体局部的描述。
- 自然语言角色/外观输入应交给 `prompt_intake.py` 分类，不要求用户手写 tag。
- 明确摘掉并继续不戴的配饰（如眼镜、项链、耳环、发夹）属于当前可见外观变更，不应只停留在单张图的 scene 叙事里；聊天链路应调用 `change_appearance`，生图链路也会对明确的 `clothing_off` 配饰移除做持久化兜底。
- 长期记忆不写临时服装、上一轮场景台词、一次性道具；这些属于短期上下文、衣柜或照片历史。
- fire-and-forget `asyncio.create_task` 内异常可能被静默吞掉；排查生图/推送失败优先看 service log。
- `_get_llm_value("chat", "temperature")` 的 legacy 回退会落到 `llm_temperature_scene`，除非 `chat_llm_temperature` 显式设置。

## 今日变更（2026-07-09）

1. **拉取远端更新**：本轮先从 `origin/main` 快进到 `07c4bd0`，在最新代码上修复主动推送和续场推送容易复读上一句话的问题。
2. **续场推送时机与延迟**：`handle_chat()` 改为等 bot 回复发送完成后再安排续场推送，默认延迟从 5-15 分钟改为 3-10 分钟；期间用户继续发言仍会取消旧任务并按最新对话重排。
3. **推送前 checkpoint 整理**：`_sched_fire()` 发送任何推送前会执行专用 checkpoint 检查，把未折叠上下文裁到“最近一条用户消息及其之后”，旧内容仍按 checkpoint 路径摘要和抽取长期记忆；已经满足该形态时不做无意义裁剪。
4. **推送 planner 前缀稳定化**：推送 planner 复用正式聊天 prompt 前缀时会保留 checkpoint 后的最近一轮对话与照片历史；推送前 checkpoint 负责把窗口收敛到最近用户消息及之后。这样用户继续对话、连续 followup/主动推送时，最近轮上下文和前次推送照片记录都能作为共同前缀命中。
5. **followup 节拍推进修正**：followup 不再跳过 `_push_scene_transition_decision()`，会接收同一套场景节拍推进/必要转场提示；轻量续场规则改为优先承接最近轮，但出现节拍推进时要自然推进一拍，不停在上一句话附近。
6. **普通主动推送改用生活片段**：normal/morning/ntr 推送的动态 system 只放照片避重、空间摘要、世界动线和今日生活片段候选；最近对话原文只作为 checkpoint 后历史进入正式前缀，不再额外复制到动态块。`_life_plan_push_context()` 会把当前时间段内的所有 planned 今日片段都给 planner，并明确这些片段只是参考，可选择、混合或按天气/地点自然发散，不写成日程播报。
7. **推送避重改为提示词约束**：推送 caption 可写 1-3 句、30-120 个中文字符以增加生活气息；planner 会在提示词中看到最近图片 forbidden caption 和最近 scheduled/followup/manual push 的 caption、scene、nltag 与意图作为避重材料，但不再在返回后做 exact/语义重复二次判断或 retry。
8. **本轮验证**：新增/更新测试覆盖回复发送后再排续场、推送前 checkpoint 裁剪、续场 planner checkpoint 后历史进入前缀、followup 节拍推进、普通推送动态块不重复注入最近原句、今日片段候选注入、推送避重提示词注入且无二次 retry、早安转场清理临时裸体但保留照片避重。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 395 tests in 7.841s`，`OK (skipped=1)`。

## 今日变更（2026-07-09）

1. **dream 固定早安触发**：普通/续场/手动以外的非 morning 推送不再检查 `_should_run_dream_before_push()`，只有 `mode=morning` 的早安推送会同步执行 dream；避免随机推送前额外整理记忆打断主动推送节奏。
2. **角色作息接入调度**：角色卡新增 `workday_wake_time`、`workday_sleep_time`、`weekend_wake_time`、`weekend_sleep_time`，默认 08:00 / 23:50。工作日/周末按角色作息生成当天随机推送窗口：起床后 30 分钟开始，睡觉时间作为最晚自动推送时间；早安推送和 dream 时间点使用起床时间，并继续尊重 `world_workday_dates` / `world_holiday_dates`。
3. **创建角色可设置作息**：`/创建OC` 模板和自然结构字段支持工作日/周末起床睡觉时间；初始化向导新增作息步骤，可回复“默认”跳过。默认角色卡也会把作息字段写回全局 config。
4. **WebUI 角色页与衣橱移动端优化**：角色表单合并“关系/背景/边界”为“生活与边界”，加入四个作息 time 输入；角色概览显示作息摘要。衣橱在移动端改为单列布局，分槽收藏横向浏览，槽内按钮和编辑表单适配窄屏。
5. **本轮回归验证**：新增/更新测试覆盖普通推送不触发 dream、morning 推送触发 dream、角色作息生成随机推送窗口和早安窗口、创建 OC 写入作息、默认角色作息写回 config、WebUI 字段合并与移动端衣橱样式。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 398 tests in 14.645s`，`OK (skipped=1)`。

## 今日变更（2026-07-06）

1. **拉取远端更新**：本轮先从 `origin/main` 快进到 `22a3fbf`，在最新代码上继续处理 Telegram 图片输入、续场推送、life plan 和 dream 摘要链路。
2. **多图输入统一识别**：Telegram 相册 `media_group_id` 会先短暂聚合，再作为一组图片交给视觉模型统一描述；无配文相册会继续进入“图片后等待文字”窗口。连续发送多张单图时也复用同一个等待窗口，用户随后发来的纯文本会作为这组图的配文/附近上下文；窗口内最多保留前 5 张，超出的图片丢弃。聊天模型仍只收到纯文本图片描述，不直接接收多模态 payload。
3. **续场/生图 image profile 作用域修复**：续场推送调度、推送/生图 planner、Anima slots、场景 tag 翻译等 image LLM 判断改为带 `session_id`，避免用户私有 image profile 配好但全局 image 配置为空时误判不可用；不改 `_schedule_post_chat_push()` / `_fire_post_chat_push()` 的 skip reason 结构。
4. **生活线长期/中期节奏**：`life_plan_long_review_days` 默认不变；长期目标现在有代码层硬门控，只有首次补齐、手动重写或 review 到期时才允许替换/新增/更新/完成长期目标。中期目标仍允许每天根据长期目标、前一天日记状态和近期材料重排；即使模型误输出 long ops，未到期也会忽略。
5. **dream 摘要链路分层**：dream 日记落库后先抽取长期记忆，再整理长期记忆，再用长期记忆、checkpoint 和当前窗口生成角色历史提要，最后以最新长期/历史依据更新 checkpoint。提示词明确长期记忆负责稳定事实/偏好/边界，角色历史负责重大事件台账、个人轨迹和扮演计划，checkpoint 只承接近期连续性，并丢弃已过期、解决或被替代的短期事实，降低三层互相重叠和事实断裂。
6. **本轮回归验证**：新增测试覆盖 Telegram 相册统一识别、连续单图等待配文合并且最多 5 张、多图无配文兜底文本、续场推送使用 session 级 image 配置、`_translate_to_tags()` 传递 session、长期目标未到 review 时忽略 long 更新、角色历史读取长期记忆/checkpoint/current window、dream 摘要链路顺序。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 370 tests in 9.753s`，`OK (skipped=1)`。
7. **WebUI 角色头像布局修复**：角色详情顶部概览中，头像生成/重新生成按钮移到头像下方，头像和按钮组成独立窄列，右侧只保留角色文本与标签，避免窄宽度或长人设文本时按钮挤压正文列。验证 `node --check telegram_comfyui_selfie\static\app.js` 通过。
8. **撤回带提示重答**：`/撤回` 无参数和数字参数仍走原回滚逻辑；`/撤回 <扮演提示>` 会撤掉上一条角色回复，用上一条用户消息重新生成，并把提示作为仅本次生效的 system 指令注入，不写入 `chat_history`。`/重答 [扮演提示]` 复用同一重答链路。顺手固定 `clothing_off` 单测的私有地点，避免白天公开动线触发公开穿搭 guard 干扰该用例。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 372 tests in 9.604s`，`OK (skipped=1)`。

## 今日变更（2026-07-03）

1. **对话后续场推送**：新增 `post_chat_push_enabled`、`post_chat_push_delay_min_minutes`、`post_chat_push_delay_max_minutes`、`post_chat_push_daily_limit`、`post_chat_push_cooldown_minutes` 配置，并接入 WebUI/示例配置/会话 schema。普通聊天收到用户消息后会安排一次 5-15 分钟随机 followup 推送；若用户期间继续说话，会取消旧任务并按最新消息重排；发送前仍检查 frozen、goodnight inhibition、active push、每日上限和冷却。
2. **主动推送上下文链路重构**：`plan_roleplay_image()` 的推送路径改用 `_build_chat_context_messages_for_push()` 复用正式聊天 prompt 前缀，再追加 planner system、动态 system 和当前时段 user。followup 不跑硬转场判定，不主动切场景；推送临时规则、避重规则、空间硬约束和生活线侧面提示都放在动态 system，user 段不再重复塞短期连续性/长期记忆，提升 prefix cache 命中并降低语义复读。
3. **推送图片写回正式上下文**：`_record_sent_photo()` 新增 `source_kind`，聊天配图、漏图兜底、定时推送、手动推送、followup 推送分别标记为 `chat_image` / `auto_chat_image` / `scheduled_push` / `manual_push` / `followup_push`。照片历史 system 统一写入正式 `chat_history`，内容拼接 `source_kind`、`view`、最终 `nltag`、瘦身后的 `source_intent` 和 caption；不主动清理图片上下文，只随 checkpoint 和历史溢出统一折叠/裁剪。
4. **生活线目标补齐**：life plan 增加空长期/中期目标 bootstrap 判定，老角色当天已有生活线但没有长期/中期目标时也会刷新。LLM 提示词只给核心驱动力判断规则，不再由代码预提取候选；要求模型私下从角色视角根据原始人设、历史、记忆和日记自行推断角色想追求、逃避、证明、修复、保护或成为的东西，并禁止默认生成“维系感情”这类空泛关系目标。
5. **本轮回归验证**：新增/更新测试覆盖 followup 调度替换、静默发送与计数、推送图片 `source_kind` 写回 system 历史、followup planner 复用正式聊天上下文且不重复注入短期连续性/长期记忆、空 life plan bootstrap、生活线核心驱动力规则提示词、推送硬转场保留照片 system 历史但清理临时裸体态，以及 schema 新字段单一来源。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 345 tests in 8.399s`，`OK (skipped=1)`。
6. **纯图片输入等待配文**：Telegram 输入层新增 `photo_caption_wait_seconds`，无配文图片会先挂起等待后续纯文本；收到文本时消费为该图片的配文/附近上下文，不再单独触发一轮聊天，超时或配置为 0 时沿用旧的纯图片理解流程。该配置已接入默认配置、示例配置、YAML 分组和 WebUI 设置。
7. **用户新消息抢占旧任务**：同一 session 的新消息会取消旧的可中断处理 task，包括 LLM 文本生成、1 秒分段发送间隔和待配文图片处理；`handle_chat()` 在取消时补写旧用户输入，且把已入库但未完全发出的 assistant 回复裁剪到 Telegram 已确认发送的部分。聊天工具生图和 `/自拍`、`/配图` 使用受保护生图 task，外层命令/聊天被取消后图片仍会继续生成、发送并记录照片历史。
8. **本轮追加验证**：新增测试覆盖图片等待后续配文、等待超时旧逻辑、等待秒数 0 立即旧逻辑、新消息取消旧聊天并保留旧用户输入、分段发送被取消只保留已发送 assistant 文本、聊天工具生图不被取消、`/配图` 命令生图不被取消。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 352 tests in 20.542s`，`OK (skipped=1)`。
9. **生活主线目标维度化**：长期目标新增 `dimension` 字段并在归一化时补齐旧数据；生成提示词要求模型从角色自身的不同维度拆分目标（如生活、理想、事业、爱好、身份、关系等，但不固定这些枚举），禁止把同一条陪伴/亲密关系诉求改写成多条。生成材料新增近期真实 `user/assistant` 上下文，并支持用户额外目标指示参与重生成。
10. **生活主线编辑入口**：WebUI 动线页支持长线/中期线新增、编辑、删除和带指示重生成；Web API 新增 `/api/world/{session_id}/life-plan/goals` 的 create/update/delete 路径。Telegram 新增 `/生活主线` 查看/生成，以及 `/生活主线 目标指示 <要求>`，会把要求交给 LLM 辅助刷新目标。
11. **新角色生活主线展示**：自然语言创建新角色卡后会同步生成当天生活主线，并把长期线/中期线摘要追加在 bot 返回消息里，用户可以直接看到角色的理想、追求或生活底色，不需要进 WebUI 才发现。
12. **本轮生活线验证**：新增测试覆盖目标维度去重与父子关联、Web 目标 CRUD 与指示重生成、新角色创建展示生活主线、`/生活主线 目标指示` 命令，以及生活线提示词读取近期上下文和用户目标指示。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 356 tests in 7.956s`，`OK (skipped=1)`。
13. **目标指示全量重写**：`regenerate_life_plan_goals()` 进入手动目标重写模式，会把原始长期/中期目标单独作为草案注入，并要求 LLM 同一次返回完整 `long_goals` 与 `mid_goals`；解析层以 `replace_goals` 忽略增量 goal ops，原子替换全部长期/中期目标。WebUI 空指示“重生成目标”也走该模式，不再退回只刷新今日生活线。
14. **日记与摘要视角归属**：dream 日记 prompt 强化“第一人称=当前 bot 角色”；checkpoint prompt 注入 `User = human user; Assistant = current bot roleplay character` 映射；长期记忆提取修复 checkpoint 多轮对话被整体包成“用户”输入的问题；角色历史提要和 dream 记忆整理也明确日记/聊天的用户与角色归属，降低摘要里 bot/用户视角混淆。
15. **本轮视角与目标重写验证**：新增测试覆盖目标指示旧版本草案注入、长期/中期目标一次性替换且忽略增量 ops、Web 空指示目标重生成、dream 日记视角规则、checkpoint 角色映射注入、checkpoint 对话进入长期记忆提取时不被包成用户发言。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 359 tests in 9.026s`，`OK (skipped=1)`。

## 今日变更（2026-07-07）

1. **远端同步与本地回退**：本轮先将本地未推送提交 `5517763 Fix life plan cold start silently failing to create goals` 备份到 `codex/backup-unpushed-life-plan-5517763`，随后把 `main` 硬重置到 `origin/main` 的 `7218c26 Add prompted rollback regeneration`；未跟踪设计文档移到 `.tmp/rollback-backup/20260707/life_plan_design.md`，同步后工作树保持干净再继续修复。
2. **裸体/脱衣不再误删角色特征**：`clothing_off` 处理前先用外观分类器把可脱项限定为 `outfit/accessory`，全裸或局部脱衣只从本图渲染外观里剥离衣物/配饰并把刚脱掉的衣物压入负向；发型、发色、瞳色、兽耳等角色特征继续留在正向，也不会再被写入负向，避免“黑色中长卷发”在裸体图后漂成白色短发。
3. **收紧 planner 一次性外观入口**：`roleplay-image-plan` 返回的 `new_appearance_tags` 只有在用户本轮意图/必须包含/原始提示明确要求穿、换、戴、脱、发型发色等视觉变更时才会进入最终生图；普通推送、早安/日常配图、续场图里 planner 自行发散出的白衬衫、短裤、睡衣、临时发色等会被丢弃，最终仍以当前可见外貌和衣柜为准。
4. **照片历史补充裸露状态但不污染外观槽**：发送图片后记录 `visual_state`，从最终 `nltag/scene` 检测 `nude/topless/bottomless/no underwear` 等“未好好穿衣”状态，并注入照片历史 system 消息及后续 planner 的最近图片摘要；普通穿着只标记为 `clothed`，不把完整衣柜、发型或 PromptSlots 外观重新塞回聊天前缀。
5. **本轮验证**：新增/更新测试覆盖全裸保留发型特征、未请求 one-shot 服装被丢弃、用户明确要求 one-shot 服装仍保留、照片历史携带裸露状态且不回灌完整外观。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 376 tests in 7.899s`，`OK (skipped=1)`。
6. **照片回传补足可见穿搭**：排查“语言模型能不能完整理解自己发出的图”后，照片历史 `visual_state` 从只标记 `clothed/nude` 改为记录最终可见衣物/配饰的短摘要（如 `visible outfit: black dress, white cotton knit cardigan`）；来源优先读取刚刚实际生成的 `PromptSlots.effective_appearance + one_shot_appearance`，再回退调用方传入 appearance 和当前可见外貌，避免 public guard / clothing_off 改写后仍把旧衣柜当成照片内容。该摘要只保留 outfit/accessory，不回灌发色、瞳色、体型、兽耳或完整 PromptSlots。
7. **追加验证**：新增 `test_record_sent_photo_captures_visible_outfit_without_full_appearance` 与 `test_record_sent_photo_prefers_last_prompt_slots_appearance`，覆盖照片历史能告诉聊天模型具体穿搭，同时不泄漏发色/瞳色，也能优先使用最终生图槽位而不是旧衣柜；定向验证通过。
8. **公开场景外出兜底改为独立覆盖**：公开场景 guard 若移除了 `slip dress` / `nightgown` 等私密主体衣物后只剩开衫、外套或配饰，会补 `plain white crew-neck t-shirt, dark blue jeans`，但不再写回当前 `wardrobe` / `dynamic_appearance`；兜底单独保存在 `clothing.public_fallback_outfit`，只在公开场景构建 `PromptSlots.effective_appearance` 时覆盖，回到家里或私密场景仍按当前衣柜显示原本的睡衣/吊带裙/开衫。兜底单品仍加入 closet，换装 reset、角色 reset/switch/delete 会清掉该临时兜底状态，不影响海边泳装、角色 base 暴露造型或明确 public play 例外。
9. **WebUI 衣柜展示修正**：角色卡详情页不再展示原始 `outfit` 的“服装标签” textarea，避免把兼容字段误当成当前衣柜；当前 active 角色会展示“当前衣柜”运行时面板，读取 `current_clothing` 中的当前穿搭标签、分槽 `wardrobe`、公开场合兜底、closet 收藏和 nudity 状态。保存角色卡时仍通过隐藏字段保留旧 `outfit` 兼容，不破坏导入导出和旧 API。
10. **本轮公开穿搭与 WebUI 验证**：新增/更新测试覆盖 `black silk slip dress + white cotton knit cardigan` 在 izakaya/cafe 场景只在公开 prompt 中叠加完整外出兜底、保留开衫且不污染私密场景当前穿搭；覆盖公开睡衣兜底、wardrobe reset 清理公开兜底但保留 closet，以及 WebUI 序列化/前端隐藏原始服装标签并展示当前衣柜。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 379 tests in 10.123s`，`OK (skipped=1)`。
11. **WebUI 衣柜面板可操作化**：新增 `POST /api/sessions/{session_id}/wardrobe`，支持 WebUI 自然语言更新穿搭、整套替换、清空、按槽位脱下、从衣橱收藏一键穿上，以及把旧状态中混进当前穿搭的公开兜底移回 `public_fallback_outfit`。前端“当前衣柜”改成明确三块：身上穿着（当前真源，可脱下单槽）、公开兜底（只说明外出/公开场景临时叠加）、衣橱收藏（普通收藏可一键复穿）；系统自动生成的 `public fallback top/bottom` 不再混进普通收藏展示。新增 `test_webui_wardrobe_actions_edit_current_clothing` 并更新 WebUI 衣柜展示测试；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 380 tests in 10.157s`，`OK (skipped=1)`。
12. **WebUI 衣橱收藏选择器重排**：衣橱收藏不再按“名称 / 英文生图标签 / 按钮”的表格展示，改为左侧衣服类型、右侧具体服装按钮的选择器；主显示使用中文收藏名或由常见英文 tag 本地化出的中文名（如 `white cotton knit cardigan` 显示为“白色棉质针织开衫”），英文 tags 只保留在 hover title 里供调试，不占主界面。当前穿搭摘要和输入示例也改为中文可读文案，不改变底层 prompt tags。更新 WebUI 测试锁定 `closet-picker` / `closet-choice` 结构和不再直接渲染 `entry.tags`；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 380 tests in 10.822s`，`OK (skipped=1)`。
13. **WebUI 当前穿着保留可读衣名**：当前穿着的 prompt 真源仍是英文 tags，但 `/api/sessions/{session_id}/characters` 会用 `slot + tags` 从普通衣橱收藏反查中文短名，新增 `current_clothing.wardrobe_display` 给前端“身上穿着”和“当前摘要”优先显示；例如当前 top tag 只有 `white`，但衣橱里同槽同 tag 收藏名是“露脐白衬衫”，WebUI 会显示“露脐白衬衫”而不是“白色”。新增 `test_webui_current_wardrobe_prefers_closet_display_names`，验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 381 tests in 9.058s`，`OK (skipped=1)`。
14. **衣橱新增保留用户输入名**：`_wardrobe_closet_display_name()` 统一处理衣橱收藏显示名；当 LLM/兜底分槽只返回 `white shirt`、`white` 等英文 prompt tags 而没有可靠中文 `names` 时，单件中文输入会用用户原始短语作为衣橱名，避免点击“存进衣橱（暂不换）”或“直接换上”自动收藏后又显示成英文/泛化标签。新增回归覆盖 WebUI `save-closet` 和 `_apply_wardrobe()` 自动收藏两条入口；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 385 tests in 11.415s`，`OK (skipped=1)`。
15. **推送配文复读收敛**：正式照片历史仍保留 `caption` 供聊天连续性使用，但 `roleplay-image-plan` 的推送分支会在 planner 消息副本中移除照片历史的 `caption:` 行，并把最近图片配文放进“禁止原样复用”动态规则；若 planner 仍返回与最近配文完全相同的 caption，则只重试一次，不做近似判重、不清空配文。推送用的空间/身体关系硬约束也从原始对话文本改为英文站位摘要，避免把上一轮台词/拟声词整段高权重追回进 scene。新增测试覆盖 followup planner 隐藏历史 caption 行但保留 forbidden list、完全重复配文触发一次 retry、推送空间硬约束不复述原对话；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 389 tests in 9.567s`，`OK (skipped=1)`。

## 今日变更（2026-07-02）

1. **文爱/性爱聊天语言规则**：聊天静态前缀新增 `CHAT_INTIMATE_LANGUAGE_RULES`，仅在明确进入文爱、性爱、插入、抽插、高潮或同等性行为描写时启用；普通调情、拥抱、亲吻和日常亲密不套用。规则覆盖台词字数上限、兴奋度 1-6 对应的语言破碎度、拟声词优先、失语优先、回复结构不规则化，以及禁止评论员口吻、完整逻辑推演和“不是……而是……”句式。该段接在“回复格式规则”之后、“对话推进规则”之前，仍位于 `messages[0]` 静态槽，避免随穿搭、世界状态或动态尾部变化破坏 checkpoint/history 前缀形状。
2. **本轮回归验证**：新增 `test_chat_system_static_has_intimate_language_rules`，锁定启用边界、阶段密度、拟声词要求、禁用句式和静态前缀内顺序；验证 `py -3 -m unittest tests.test_core.ServiceTestCase.test_chat_system_static_has_intimate_language_rules -v`、`py -3 -m compileall -q telegram_comfyui_selfie tests`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 330 tests in 6.615s`，`OK (skipped=1)`。
3. **拉取远端更新**：本轮先从 `origin/main` 快进到 `cd08a7c`，包含 `Add intimate chat language rules`、主动推送节奏、生图规划缓存前缀和公开穿搭 guard 等更新。
4. **地点工具自然语言归一**：`tool_update_location()` / `tool_update_user_location()` 新增统一 `_match_place_key()`，保留模型给出的原始地点文本作为显示名，但用 `PLACE_TEXT_ALIASES` 与“路上/途中/前往”等转场提示归一到固定 `PLACE_TYPES` key；`前往私立中学的路上` 会落到 `street/大街`，`私立中学` 会落到 `school/学校`，`街道` 不再因内置 label 是“大街”而被拒绝。
5. **最终回复工具粘连恢复**：当 `chat-final` 因兼容端点在无 tools 请求中仍返回空 `tool_calls` 时，先按原逻辑追加禁止工具的 `chat-final-retry`；如果 retry 仍只有空工具调用，新增 `chat-final-recovery`，去掉 `role=tool` 和 `assistant.tool_calls` 协议消息，只保留普通上下文与工具结果摘要，要求模型直接输出自然语言回复，避免地点修正循环导致“回复生成失败”。
6. **本轮追加验证**：新增测试覆盖自然语言地点别名/路上归一，以及 final+retry 连续返回 `update_location` 空工具调用时的 text-only recovery；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 332 tests in 7.880s`，`OK (skipped=1)`。
7. **早安/硬转场临时脱衣边界修复**：`roleplay-image-plan` 在主动推送判定为硬转场时（早安、明确结束/离开/改约信号、超过 `push_continuity_hours`）不再把上一幕 `短期连续性` 和 `最近已发图片摘要` 注入本轮 planner，避免上一场性爱、临时脱衣或照片 tags 被当作跨天当前穿搭继续使用；同时在硬转场边界清除临时裸体态。保留同一场景内的持久裸体 TTL 续态逻辑不变，`scene_stale_minutes` 的软节拍推进也仍保留连续性，不影响昨天公共场合睡衣/内衣兜底。
8. **本轮综合验证**：新增 `test_morning_push_hard_transition_drops_temporary_undress_context`，覆盖上一幕性爱/只披针织衫/裸体态在早安推送中不进入 planner user prompt 且不续 `clothing_off`；同步回归公共场合穿搭兜底、同场裸体续态、scene stale 软推进。综合远端地点归一与 final recovery 更新后，验证 `py -3 -m unittest tests.test_core.ServiceTestCase.test_morning_push_hard_transition_drops_temporary_undress_context tests.test_core.ServiceTestCase.test_persistent_nudity_continues_until_dressed_or_new_scene tests.test_core.ServiceTestCase.test_build_prompt_public_context_replaces_private_sleepwear_outfit tests.test_core.ServiceTestCase.test_planner_warns_private_sleepwear_in_public_world_context tests.test_core.ServiceTestCase.test_scheduled_push_stale_gap_alone_keeps_continuity tests.test_core.ServiceTestCase.test_scheduled_push_stale_gap_advances_beat_without_dropping_place -v`、`py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 333 tests in 6.301s`，`OK (skipped=1)`。
9. **生图翻译失败不再丢场景细节**：排查 12:32 日常同空间配图发现 `IMAGE scene` 已正确包含“背对用户卷头发、尾巴绕脚踝、雨天客厅”，但 `_translate_to_tags()` 在翻译模型空返或原样回显中文 scene 时只返回 `view_opener`，导致 `PROMPT_SLOTS.scene` 退化成泛化 POV，AnimaTool slots planner 再把画面补成正面对用户。现在固定视角 fallback 会保留原始 scene 细节（`view_opener + 原始 scene`），无固定视角时也会在空返时回退原始 scene，避免最终 prompt/payload 把已规划正确的动作和站位吞掉。
10. **本轮回归验证**：新增 `test_translate_to_tags_fallback_preserves_scene_details_with_fixed_view`，覆盖翻译模型原样回显和空返两种失败形态下，固定 POV 仍保留“背对、卷头发、尾巴绕脚踝”等关键 scene 细节；验证 `py -3 -m unittest tests.test_core.ServiceTestCase.test_translate_to_tags_fallback_preserves_scene_details_with_fixed_view tests.test_core.ServiceTestCase.test_translate_to_tags_uses_anima_mixed_prompt_with_fixed_view tests.test_core.ServiceTestCase.test_translate_to_tags_injects_current_weather tests.test_core.ServiceTestCase.test_build_prompt_partner_flag_routes_to_everyday_partner_path -v`、`py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 334 tests in 6.165s`，`OK (skipped=1)`。
11. **基础外观变更即时进入生图规划**：`roleplay-image-plan` 的动态 system 槽新增 `当前可见外貌`，内容来自最终可见外观标签 `_effective_visual_prompt_tags()`，并放在 `当前附加外貌` 之前；稳定规则同步声明当前可见外貌优先，旧短期连续性或最近照片摘要里的发色、瞳色、体貌等冲突细节视为过期参考，避免改基础外观后必须 `/新场景` 清掉旧上下文才生效。新增 `test_roleplay_image_planner_prioritizes_current_visible_appearance`，覆盖旧连续性/旧照片仍写 `black hair` 时 planner system 仍优先携带当前 `silver hair`、`blue eyes` 和当前穿搭。
12. **角色生活线功能**：新增 `LifePlanMixin` 与 SQLite `life_plans` 表，按 `session_id + character_key` 存储长线目标、中期目标和今日片段；dream 后用 op-list 更新并渲染成低目的性的“生活底色”，首次聊天或推送缺当天生活线时异步/按需懒生成。聊天 durable 层只注入通过禁词过滤的自然语言底色，不把结构化目标、计划或任务泄漏给 chat；主动推送 planner 只收到当前事件的一句侧面状态，用于地点/情绪/余韵，不写进度汇报。角色 checkpoint 完整导出/导入支持 life_plan，`/角色 clearup` 和删除角色会清理对应生活线。
13. **WebUI 与验证**：动线页新增“生成生活线”按钮和生活线预览，展示角色、底色、长线/中期线和今日片段关联；设置页新增 life plan 开关、复盘天数和容量上限。新增测试覆盖底色-only 注入、目的词重试、op-list 上限/未知 ID、动线页序列化、推送侧面提示和 checkpoint full 恢复；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 339 tests in 9.488s`，`OK (skipped=1)`。

## 今日变更（2026-07-01）

1. **拉取远端更新**：本轮先从 `origin/main` 快进到 `c907f36`，包含 `591a15b Improve roleplay image planner cache ordering`、dream 记忆压缩输出预算和 chat 默认输出预算上调等更新。
2. **09:05 `LLM_FULL_LOG` 报错定位与修复**：附件里的 `chat-final returned tool_calls without content` 不是网络失败，而是兼容层在最终文本请求阶段（请求体已不携带 `tools`）仍返回 `finish_reason=tool_calls`，并给出 `generate_roleplay_image` 工具调用且 `content=""`；现在检测到这种情况会追加禁止工具的 text-only system 提示重试一次 `chat-final-retry`，成功则写 `WARN` 并发自然语言回复，只有重试仍为空或异常时才写 `ERROR LLM_FULL_LOG`。前一轮工具调用已成功记录“用户在公司 / 角色在家”。
3. **`591a15b` 行为说明**：该提交把 `roleplay-image-plan` 的 scene boundary、模式、世界/地点、空间关系、亲密同框、单帧构图、视角和 JSON 契约等稳定规则集中到 planner system 最前段；角色身份、外貌、天气、地点 pin、用户位置和短期上下文保留在后段，减少动态角色/天气/穿搭打断 provider prefix cache。
4. **照片历史上下文瘦身**：发送图片成功后缓存并持久化最终生图 `nltag/tags`（普通后端取 `PromptSlots.scene`，AnimaTool 取实际 payload 的 `nltag/nl_tag/nl_tags/tags`）；照片历史 system 消息和 `format_sent_photo_context()` / `format_recent_photo_dedup_context()` 均优先使用该字段，只附加从 `source_description` 提取的短意图/情绪/必须包含，明确排除 `原始草案/上下文`、外观快照和全部 PromptSlots。
5. **照片历史位置稳定性**：照片历史仍作为真实 `chat_history` 里的 `system` 消息写入 checkpoint 锚定的未折叠历史段，位于动态时间/天气尾部之前；checkpoint 之间只追加不滑动，不再每轮动态注入。新增测试锁定照片历史不会进入 `system_dynamic`，短意图保留但长上下文和外观槽不进入 prompt。
6. **动态 system 门控与 checkpoint 收敛**：`_build_chat_messages()` 先轻量判定本轮是否明确发图，再复用 `_should_include_chat_world_context(..., explicit_image=...)` 控制本轮位置/动线/空间关系动态尾部；`_track_dynamic_context_change()` 只跟踪发图提醒、场景断档和世界动态文本，不把精确分钟纳入签名。动态结构变化、半稳定状态变化或世界当前条件变化时，统一经 `_queue_checkpoint_if_pending_half()` 在未折叠历史过半后排一次 forced checkpoint。
7. **世界条件/自然光降频**：新增 `_chat_world_conditions_context()`，把城市/日期、天气、季节/自然光和自然光硬规则放在 semistable 之后、checkpoint/历史之前的独立半稳定 system 槽；签名包含城市、日期、星期/节假日、天气、季节、`light_phase`、日出日落和自然光硬规则。同一自然光阶段内如 12:00→12:10 内容字节稳定，跨阶段如白天→夜晚才更新并按需触发 checkpoint。
8. **专用错误日志**：所有 `_ulog(..., "ERROR", ...)` 会继续写入用户活动日志，同时额外镜像到全局 `errors.log`（独立 `error_log_enabled`，默认开启；即使关闭 `user_log_enabled` 仍会记录错误）；`errors.log` 使用同一套完整行轮转机制，历史块进入 `logs/chunks/errors.<timestamp>.log`。`/api/logs/system-errors` 只读取 `errors.log` 及其历史分块，不再扫描 `telegram_*.log`；结构化返回 `session_id`、错误类型、原始行，且会解析 `LLM_FULL_LOG` / `MEMORY_OP_FAILED` 的 JSON payload，把完整 `request` / `response` 展示到 WebUI。
9. **回归验证**：新增/更新测试覆盖照片历史 nltag 写入、短意图保留、原始草案/外观槽排除、planner 图片摘要使用 nltag、照片历史在未折叠历史中的稳定位置、`chat-final` 空工具调用 text-only 重试、世界当前条件同阶段稳定/跨阶段更新、条件变化过半窗口触发 checkpoint、专用错误日志写入/轮转、错误页只读 `errors.log` 不扫描用户日志、错误 payload 展开 LLM 请求/返回；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core`，结果 `Ran 313 tests in 6.027s`，`OK (skipped=1)`。
10. **WebUI 角色卡与日记改版**：角色池卡片改为头像 + 身份摘要 + chips 的紧凑布局；角色编辑区顶部增加资料条，未生成头像时留空白占位。日记页改为双栏便签式网格，每篇日记有日期/标题头和更高的正文编辑区，提高连续阅读性。
11. **角色头像生成**：角色页新增“生成头像”按钮，按当前选中角色卡构造头像场景，临时把生图上下文切到该角色后调用 `_do_generate()`，图片保存到 `data/avatars/<session>/<character>.png`，再次点击会覆盖旧头像；角色卡保存 `avatar_path/avatar_updated_at`，快照 `_snapshot_character()` 会保留头像字段，避免切角色或推送后头像丢失。
12. **角色页操作强制按选中角色隔离**：WebUI 的长期记忆、日记、角色历史提要和手动记忆整理接口不再回落到当前 active 角色，必须携带 `character_key`；历史提要只有请求的是 active 角色时才允许读取 session state 兜底，防止选中 B 时显示 A 的角色信息。
13. **WebUI 手动推送按选中角色执行**：角色页按钮文案从“测试推送”改为“手动推送”，前端改走 `POST /api/sessions/{session_id}/test-push` 并传 `character_key`；后端会临时切到选中角色执行 `_sched_fire(..., skip_active_check=True)`，推送产生的照片历史和上下文保存进选中角色的 `character_contexts`，最后恢复原 active 角色。
14. **命令兼容**：Telegram 命令处理器仍保留规范命令 `测试推送` 与旧别名 `推送测试`，新增别名 `手动推送`；WebUI 命令提示只展示“手动推送”。
15. **本轮回归验证**：新增测试覆盖头像生成覆盖写入、WebUI 角色页记忆/日记/历史提要不回落 active、手动推送期间临时切到选中角色并在结束后恢复原 active，且照片历史落入选中角色上下文；验证 `node --check telegram_comfyui_selfie\static\app.js`、`py -3 -m compileall -q telegram_comfyui_selfie tests`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 318 tests in 7.589s`，`OK (skipped=1)`。
16. **角色页布局二次收敛**：左侧角色池从多行信息卡改为固定高度列表行，仅展示头像、名称、来源/类型和一行摘要，避免窄列中内容高度不稳和视觉重叠；右侧角色编辑表单增加字段布局元数据，`persona/appearance/outfit/relationship` 等长文本字段横跨整行，`persona` 使用更高 textarea，身份短字段保持二列/三列网格。右侧简介栏已有头像时可点击查看大图，支持点击背景、关闭按钮或 Esc 退出预览。
17. **角色操作并发串味修复**：WebUI 头像生成和手动推送都会临时把同一会话切到目标角色后执行长耗时任务；此前同一会话连续触发 A/B 头像或头像+手动推送时，两个请求可能交错恢复 `service.sessions[session_id]`，导致 active 角色被恢复成另一个临时角色，甚至把照片历史写入错误上下文。新增 session 级 `_web_character_operation_locks`，让这些会临时切角色的操作按会话串行执行；新增并发测试覆盖 A/B 头像同时生成后 active/chat_history 不变，以及头像生成与手动推送混跑时只会按序进入目标角色上下文。
18. **聊天回复格式统一**：聊天静态 system 新增回复格式规则，要求角色说出口的语言单独放入中文直角引号 `「」`，动作、神态、姿态、心理、环境和状态描写单独放入全角括号 `（）`；台词段和状态段用空行分成独立自然段，继续沿用 `chat_split_paragraphs=true` 的空行分段发送机制。新增测试锁定该规则存在于静态前缀。
19. **`chat-final` DSML-only 空回复修复**：兼容 OpenAI 兼容端在最终文字阶段返回 `finish_reason=stop` 但把 DSML 工具调用文本塞进 `message.content` 的情况；如果剥离 DSML 后没有自然语言，会和结构化 `tool_calls` 空回复一样追加 text-only system 提示并重试 `chat-final-retry`，不执行最终阶段泄漏出的工具调用。新增测试覆盖 DSML-only `generate_roleplay_image` 触发重试且不写 `LLM_FULL_LOG`。
20. **AnimaTool 自拍手机旁路修复**：AnimaTool Turbo 的 slots planner 不再把未清洗的原始 `scene_desc` 作为 `用户意图` 重喂给 LLM，避免 raw scene 中的 `holds her phone` 绕过 `PromptSlots.scene` 清洗；`plan_animatool_slots()` 在最终 `tags/nltag` 入 payload 前会按 `view=selfie/portrait/pov` 再跑一次设备/手机/UI 清理。新增测试覆盖 raw scene 不再作为 intent 传入，以及 LLM 泄漏手机词时 Turbo nltag 被清洗。
21. **聊天话题转换柔化**：聊天静态 system 的对话推进规则新增两条硬约束：当用户本轮话题与前文、旧场景或旧动作明显无关时，直接接续用户的新话题，不为了显得连续而强行呼应上一场景；用户一句话里同时包含寒暄、解释、问题和转折时，先抓核心意图与最需要被接住的情绪自然推进，不逐句逐点机械回应。更新 `test_chat_system_static_has_interpretation_rules` 锁定规则；验证 `py -3 -m unittest tests.test_core.ServiceTestCase.test_chat_system_static_has_interpretation_rules -v`、`py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 321 tests in 7.480s`，`OK (skipped=1)`。
22. **公共场合睡衣/内衣穿搭兜底**：排查 18:03 主动推送发现世界动线已判定角色在学校，但当前持久穿搭 `black lace camisole nightgown` 仍被直接注入 `effective_appearance/one_shot_appearance`，导致校园/大学房间提示词着装暴露。`generation.py` 新增公开场合外观兜底：当 scene 或世界地点属于学校、公司、商场、街道、咖啡店等公开场合时，从本图 `PromptSlots` 中移除 nightgown/lingerie/lace camisole/slip dress 等睡衣内衣类项，若没有其他衣物则补 `modest casual clothes`，并把被移除项追加到 negative；不改持久衣柜，私密卧室/家中场景仍保留睡衣。`roleplay-image-plan` system 同步提示当前公开场合不能直接使用这些暴露项，优先生成得体替代穿搭；按用户要求未改 `user_location=unknown → 与角色同处` 的迟滞判定。新增测试覆盖公共校园 prompt 清洗、私密卧室不误伤、planner 公开场合穿搭约束注入；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 324 tests in 7.400s`，`OK (skipped=1)`。
23. **公共穿搭兜底边界收窄**：复查发现第 22 条如果按所有公开场合全量扫 `effective_appearance`，可能误伤角色 base 里的标志性暴露造型、海边泳装或用户明确要求的公开 play。现改为只针对“当前衣柜/本次 one-shot 外观”里的私密睡衣/内衣类冲突项，不扫描角色 base；泳装在海边/泳池/游泳/温泉等上下文保留，`bikini armor/costume` 等角色造型保留，`is_intimate`、显式 `clothing_off` 裸露和“公开play/露出/羞耻play”等明确意图跳过兜底。`roleplay-image-plan` 的公开穿搭提示也会读取用户原始 intent/mood/must_include/prompt，避免海边或 play 意图被错误提示。新增测试覆盖角色 base 暴露造型不被删、海边泳装保留、明确公开 play 保留睡衣，以及原校园睡衣兜底仍生效；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 327 tests in 7.666s`，`OK (skipped=1)`。
24. **生图 planner 缓存命中回落排查**：本轮检查 `data/logs/llm_debug.json`、SQLite `llm_usage` 与 `scripts/compare_llm_chat_prompts.py` 报告后确认：普通 `chat:chat` 最近样本仍可到 90%+，7/1 总体回落主要来自 `image:roleplay-image-plan`，当天 16 次约 `27648/69816`（39.6%）。相邻 prompt diff 显示主动推送 planner 的 system 前缀常在“推送场景转换判定: 距离上次互动约 N 分钟”处断开，N 每次滚动导致稳定角色/人设/外观内容被挡在 provider prefix cache 之后。现将转场原因改为不含精确分钟的“距离上次互动已超过场景断档阈值”，并把转场提示从推送角色身份块后移到当前外观/偏好/性观念等较稳定信息之后；转场判断和旧地点降级逻辑不变。更新 `test_scheduled_push_transition_does_not_lock_stale_previous_scene_place` 锁定不再注入 `45 分钟` 且转场提示位于更靠后的动态区；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 327 tests in 6.931s`，`OK (skipped=1)`。
25. **主动推送场景节拍推进**：复查用户贴出的 20:03、21:29、22:15 日志后确认，`scene_stale_minutes=30` 不应被理解为“每过半小时硬切到新场景”，但也不能让一杯茶、同一姿势或同一短动作持续 45-90 分钟。现在主动推送分为两层：超过 `scene_stale_minutes` 且未超过 `push_continuity_hours` 时注入“推送场景节拍推进”，保留大地点、同处关系、情绪和未完成约定，但要求消耗品/短动作自然推进为喝完、放下、换姿势或相邻动作；只有早安、明确结束/离开/改约信号或超过 `push_continuity_hours` 才进入硬转场并降级旧地点权威。新增测试覆盖 45-60 分钟 gap 只软推进不丢地点、planner prompt 禁止原样冻结同一杯茶，以及咖啡店告别 45 分钟后仍能硬转场；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 329 tests in 6.333s`，`OK (skipped=1)`。

## 今日变更（2026-06-30）

1. **记忆整理可观测性修复**：`_organize_memories_after_dream()` 不再因没有近期日记直接静默返回，仍会基于 checkpoint/current window 尝试整理；返回结构化 `status/mode/applied/failed`，WebUI 手动整理会显示 skipped/no-op/failed/partial_failed 的真实状态。
2. **记忆失败完整入 ERROR**：长期记忆提取、dream 增量整理、全量压缩的 LLM 解析失败、空结果、非法 ops、单条 add/update/delete 执行失败都会写入用户日志 `ERROR MEMORY_OP_FAILED`，包含本次请求 prompt/op 与执行结果；`_call_llm()` 200 但正文为空也会写 `ERROR LLM_FULL_LOG`。
3. **dream 日记防丢内容**：dream 源聊天不再整段 tail 截断，而是按最近 user/assistant 对话组倒序装入上限，尽量保留 5w 字以内最多最近完整对话对；同日已有日记时 prompt 强化旧事实保全，并在模型漏写旧日记片段时追加“补记”兜底，避免覆盖当天日记时丢失旧信息。
4. **会话日志补充输出**：dream 写日记会记录 source/message/diary 字数与日记摘要，记忆整理会记录每个成功 op 和最终结果，角色历史提要日志包含输出摘要；dream/history 后台异常写入 `ERROR`。
5. **日志分块与 WebUI 最新块**：用户日志和 `llm_debug.json` 写入前达到 `user_log_rotate_bytes`（默认 6MB）会把旧块以时间后缀保存到 `logs/chunks`（如 `telegram_123.20260630_153000.log` / `llm_debug.20260630_153000.json`），原始文件名继续作为当前最新块写入；单条日志不硬拆，超大最后一条允许撑大当前块；WebUI 日志详情默认读取最新块，也可在块选择器切换历史块，清空日志会同时清理历史块，并修复 `/api/logs/llm-debug` / `/api/logs/system-errors` 被 `{chat_id}` 路由吞掉的问题。
6. **创建角色人格兜底**：`/初始化` / `/创建角色` / `/创建OC` 在角色设定复合字段中会本地拆分 role/persona/occupation 等槽位；若用户跳过角色设定或只填身份职业，会写入纯人格默认描述，避免新角色卡 `persona` 为空并回退串味。
7. **回归测试**：新增测试覆盖 dream 源文本完整对话组裁剪、旧日记保全、无日记仍整理并返回 no-op、失败 op 写入 ERROR 请求/结果、日志完整条目分块、创建角色人格兜底；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 294 tests in 6.377s`，`OK (skipped=1)`。
8. **高频生图规划归因修复**：`plan_roleplay_image()` 调用 `roleplay-image-plan` 时传递 `session_id`，让用量、调试日志和用户级 fast profile 归到真实会话；不改变 prompt 内容和缓存结构。
9. **聊天反幻觉与记忆提取边界**：聊天静态前缀新增事实来源优先级（本轮输入 > 最近真实对话 > checkpoint > 长期记忆 > 世界/动线背景），低优先级背景不能覆盖高优先级事实；旧的 `_queue_long_memory_extraction()` 改为保护性 no-op，普通聊天不再即时抽取长期记忆，长期记忆仍只在 checkpoint 折叠阶段从溢出对话异步提取；`_queue_checkpoint_if_needed()` 明确只排后台任务，不阻塞本轮回复。
10. **dream/历史提要防幻觉结构化**：保留 dream 日记、记忆整理、角色历史提要全量流程；日记 prompt 把旧日记视为存档、新对话视为证据，压缩重复动作但保留事实、承诺、未解张力和情绪转折；dream 记忆整理只允许基于日记/checkpoint/current window/已有记忆证据更新；角色历史提要建议输出「关系/剧情惯性」「角色心理与心情界定」「未解事件」「新一天演绎提示」，新一天提示必须尊重剧情逻辑惯性，聚焦心理和心情边界，不写死台词、地点、日程或剧情分支。
11. **本轮回归验证**：新增/更新测试覆盖普通聊天不触发即时记忆提取、checkpoint 后台异步不阻塞聊天、`roleplay-image-plan` 会话归因、聊天事实来源优先级、dream/历史提要 grounding 与新一天心理提示；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`py -3 -m unittest tests.test_core -v`、`py -3 -m py_compile scripts\compare_llm_chat_prompts.py` 与 prompt 比对脚本，结果 `Ran 296 tests in 8.729s`，`OK (skipped=1)`，prompt 比对 `entries=10 sessions=2 pairs=8`。
12. **WebUI 配置热载入口**：操作页“Git 更新与重启”区域新增“重新载入配置文件”按钮，调用 `POST /api/service/reload-config` 从当前 `config_path` 重新读取 YAML/JSON 并替换运行态 `service.config`，同时清理依赖配置的穿搭/配饰关键词缓存；该接口不调用 `save_config()`、不重启服务。重启入口保持原行为，仍会先保存当前运行态配置再准备进程重启。新增测试覆盖热载不写回配置文件、重启仍保存配置。
13. **记忆压缩模型回落与空 JSON 保护**：dream/手动记忆整理的 JSON LLM 调用抽出 `_call_memory_json_llm()`，先用 chat profile，请求失败或 JSON 解析失败时回落到 fast/flash profile（内部 `purpose="image"`）；fast 回落不强制 `disable_thinking=True`，沿用模型 profile。全量压缩输入改为有字符预算的紧凑记忆列表，超预算未传入的低优先级记忆保持不动；空代码块/空 JSON 会返回明确错误并写入 `MEMORY_OP_FAILED`，不会删除旧记忆。新增测试覆盖 chat→fast 回落成功、chat/fast 都返回空 JSON 时不改库。
14. **角色 JSON 检查点**：新增 `character_checkpoint.py`，每次 `_dream_once()` 写 dream 日记前按角色和日期生成本地 JSON 检查点（默认 `data/character_checkpoints/<session>/<character>/<YYYY-MM-DD>.json`），内容包含角色卡、当前/冻结短期状态、SQLite checkpoint、角色历史提要、近 7 条日记、长期记忆和当天聊天记录；每个角色只保留最近 7 天，过期文件自动清理。
15. **检查点 Web 导入导出**：角色页新增“角色检查点”区，可导出某日检查点或当前状态；角色池导入支持粘贴 JSON 和上传 `.json` 文件，导入模式包括 basic（只保存/写入角色基础字段）、memory（合并长期记忆和日记但不替换 checkpoint/上下文）、full（覆盖当前上下文、角色历史提要和 SQLite checkpoint）。新增测试覆盖当天聊天过滤、7 天保留、dream 前写检查点、三种导入模式边界；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests`、`node --check telegram_comfyui_selfie\static\app.js` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 302 tests in 5.811s`，`OK (skipped=1)`。
16. **`roleplay-image-plan` 稳定规则前置**：`plan_roleplay_image()` 将 scene boundary、推送模式通用规则、世界/地点优先级、自然光硬规则、空间/身体关系、亲密/同框/用户身体归属、单帧构图、视角和 JSON 字段契约集中到 dynamic system 最前段；角色身份、人设、当前外貌、天气/世界状态、地点 pin、用户位置状态和短期上下文保留在后段，避免稳定规则被角色穿搭、天气、地点和推送状态提前打断 provider prefix cache。新增 `test_roleplay_image_planner_orders_stable_rules_before_dynamic_context` 锁定稳定规则位于 `当前附加外貌` 前且不重复。

17. **dream 记忆压缩输出预算与截断观测**：`dream-memory-summarize` 不再继承聊天 profile 的低 `max_tokens`，而是通过 `dream_memory_summarize_max_tokens` 单独覆盖输出预算，默认 `8192`，可在配置中调到 `12000`；chat 失败回落 fast/flash 时同样沿用该预算。`llm_debug.json` 每条记录顶层新增 `finish_reason`、`completion_tokens` 和请求 `max_tokens`，`ERROR LLM_FULL_LOG` 也补同样摘要，后续看到 `finish_reason="length"` 即可直接确认是否输出截断。新增测试覆盖请求体预算覆盖、debug/error 日志截断字段、默认 8192 和配置 12000。

18. **chat 思考模型输出预算上调**：排查当前 `data/config.json`、SQLite `user_model_settings` / `model_profiles` 与 `llm_debug.json` 后确认所有 chat profile 当前都没有 profile 级 `max_tokens`，实际请求统一继承 `chat_llm_max_tokens=8192`；为避免思考内容被输出预算截断，默认 `chat_llm_max_tokens` 上调为 `12000`，示例配置、WebUI 管理页字段和本机运行配置同步更新。新增 `test_default_chat_max_tokens_is_high_enough_for_thinking` 锁定默认 chat 解析预算为 `12000`。

## 今日变更（2026-06-28）

1. **自动推送完成标记修复**：随机推送点不再在进入触发窗口时提前写入 `daily_triggered_times`；现在只有 `_sched_fire()` 实际成功发送照片后才标记完成。错过 5 分钟窗口或晚安抑制仍会显式写入处理原因，避免“无声吃掉当天推送点”。
2. **推送任务异常兜底**：`_sched_fire()` 改为返回 `bool`，并捕获规划、翻译、生图、Telegram `send_photo` 等环节异常，写入用户日志 `PUSH` 与 service log；后台推送任务使用统一包装器，失败时窗口内可继续重试。
3. **测试推送链路统一**：`/测试推送` 改走同一个安全后台包装器；`morning` 测试不再先额外跑一次 dream，避免与 `_sched_fire(mode=morning)` 重复整理。
4. **自动配图后台异常记录**：聊天 judge 自动补图改为调用 `_run_background_roleplay_image()`，后台任务异常会写入 `ERROR` 用户日志和 service log，便于排查“文字回复正常但图片没出来”的情况。
5. **回归测试**：新增测试覆盖 Telegram 发图异常不穿透后台任务、推送点只在成功后标记、后台自动配图异常入日志；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 272 tests in 6.596s`，`OK (skipped=1)`。
6. **高频 LLM 规划缓存止血**：排查 `data/logs/llm_debug.json` 与 SQLite `llm_usage` 后确认低命中主要集中在 `image:roleplay-image-plan` 和 `image:translate`；`_call_llm()` 为这两个简单两段式高频任务追加稳定的首条 system cache anchor，原动态 system/user 原文保留在后续消息，避免角色人格、穿搭、天气、地点和推送模式在请求开头把 provider prefix cache 直接打断。
7. **缓存结构回归测试**：新增 `test_call_llm_adds_cache_anchor_for_hot_simple_tasks`，验证 `roleplay-image-plan` / `translate` 会生成 `system + system + user` 三段请求，普通 `_call_llm()` 仍保持原来的 `system + user` 两段结构；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 273 tests in 6.635s`，`OK (skipped=1)`。

8. **17:08 空回复排查修复**：确认 `telegram:6430033168` 在 `chat-final` 阶段收到供应商返回的 `finish_reason=tool_calls` 且 `message.content=null`，原链路因此得到空正文并触发“回复生成失败，请稍后重试。”；现在工具执行后的最终文本请求不再继续携带 `tools` / `tool_choice=none`，避免 OpenAI 兼容端忽略 `none` 后再次返回工具调用。
9. **LLM 错误完整日志**：新增 `_record_llm_error_log()`，LLM 非 200、初始/最终请求异常、最终 200 但正文为空或继续返回 tool_calls 时，会向用户日志写入 `ERROR LLM_FULL_LOG`，包含不带 Authorization/API key 的完整请求体与响应体；新增 `test_chat_final_omits_tools_and_logs_empty_tool_call_response`，验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 274 tests in 5.955s`，`OK (skipped=1)`。
10. **角色切换衣柜串味修复**：定位到 `character_contexts` 冻结/解冻短期态时未深拷贝，`state["clothing"]` 与已冻结角色上下文共享同一个 dict，导致切到新角色后修改衣柜会同步污染离开角色；现在冻结与恢复都 `deepcopy`，WebUI 保存并激活角色也统一走切换流程，目标角色无冻结衣柜时只用自己的角色卡 `outfit` 初始化，绝不继承上一角色衣柜。新增测试覆盖 `/角色 load` 与 WebUI `activate=true`，验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 276 tests in 5.794s`，`OK (skipped=1)`。

11. **chat 世界 semistable 固定槽位**：`_build_chat_messages()` 重新把 `_format_world_semistable_context()` 与自然光规则常驻到 checkpoint/历史之前的半稳定 system 槽位，避免普通寒暄和地点/天气/发图触发轮之间插入/移除 system 段，把后面的 checkpoint 与未折叠历史挡在 prefix cache 之外；本轮位置、动线和空间关系仍只在地点/天气/发图等相关输入时追加到 dynamic tail。
12. **semistable checkpoint 签名恢复完整槽位**：`_track_semistable_context_change()` 跟踪完整 semistable 内容（外观、衣柜、世界低频模板、自然光规则），世界/光线低频变化只在未折叠历史足够长时异步触发 checkpoint，符合“常驻固定位置、低频变化再收敛”的缓存策略。
13. **LLM 小任务门控**：`image-judge` 新增轻量视觉/动作触发词，纯寒暄和普通问答不再调用小模型；`location-extract` 在聊天后台任务和 `world_runtime.py` 请求入口都增加地点/移动信号门控，避免“嗯嗯”“我在想”等无地点回复每轮消耗 token。
14. **回归测试**：新增/更新测试覆盖普通寒暄保留世界 semistable 固定槽位但不展开本轮动线动态、地点相关输入只改变 dynamic tail、无地点回复不调用 `location-extract`、无画面信号不调用 `image-judge`，并保留原有同空间 judge 视角清理与明确自拍视角测试；验证 `py -3 -m unittest tests.test_core -v`，结果 `Ran 281 tests in 6.040s`，`OK (skipped=1)`。
15. **缓存命中统计字段兼容**：排查发现切到 mimo/Xiaomi 兼容层后，provider 原始响应把缓存命中放在 `usage.prompt_tokens_details.cached_tokens`，而 SQLite 记录层只读取 DeepSeek 的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 与部分旧别名，导致 WebUI 显示 0。新增 `_cached_tokens_from_usage()` 统一解析 `prompt_cache_hit_tokens`、`prompt_cached_tokens`、`cached_tokens`、`prompt_tokens_details.cached_tokens` 和 miss 推算，确保后续切回 DeepSeek 也继续正常显示。
16. **本地用量回填**：基于 `data/logs/llm_debug.json` 原始响应精确匹配 `llm_usage` 行，备份 `data/memory.sqlite3` 后回填 55 行错写的 `cached_tokens`；最近 30 分钟 SQLite 看板口径恢复为 `27392/42041`（65.16%），今日口径恢复为 26.71%。

17. **常驻前缀槽计测（Tier 3）**：`_build_chat_messages()` 末尾新增 `_log_prefix_slot_signatures()`，每轮把 `static / durable / semistable / checkpoint` 各常驻 system 槽的稳定短哈希与未折叠历史条数写入用户日志 `CACHE prefix …`。前缀缓存在第一个变化的槽处断开，这条日志可直接和 `USAGE` 的 `cached` 命中对照，定位某轮命中率下降是哪个槽变了，而不再靠体感。
18. **世界揮发字段移出常驻槽（Tier 1）**：排查发现 semistable 虽已位置常驻，但 `_format_world_semistable_context()` 内仍嵌着 `城市/日期`（含 `time_period`）、`天气`、`季节/自然光`，且 `_format_light_guard()` 随 `light_phase`（日间/黄昏/暮色/入夜）日内漂移——这些每滚动一次就把后面的 checkpoint+历史踢出前缀缓存，还会经 `_track_semistable_context_change()` 触发多余的强制 checkpoint（摘要 LLM）。现在 `_format_world_semistable_context()` 只保留 session 内不变的世界规则（角色身份、地点来源、空间覆盖、位置优先级/不瞬移段落），新增 `_format_world_conditions_context()` 把城市/日期/天气/季节自然光与自然光硬规则一并放到非缓存动态尾部 `system_dynamic`，模型每轮仍能看到当前天气/光线（展示内容不变，只换位置）。实测同一 session 在 12:00/19:00/23:00 三个 time_period 下常驻世界槽哈希恒为 `822a3662`，尾部哈希各不相同。新增 `test_world_conditions_move_to_tail_keep_resident_slot_stable` 回归锁定「time_period 滚动时常驻槽字节不变、揮发条件落尾部」；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 282 tests in 7.099s`，`OK (skipped=1)`。
19. **主动推送场景转场判定**：新增 `_push_scene_transition_decision()` / `_format_push_scene_transition_context()`，主动推送会识别“结束、离开、改约、晚上等着、不和…扯了”等信号，以及超过 `scene_stale_minutes` 的断档；旧对话/照片此时只作为情绪、约定和避免重复参考，不再默认把上一幕地点、姿势和话题续写为此刻仍在发生。
20. **推送旧地点强锁降级**：`plan_roleplay_image(mode=normal/morning/ntr)` 在判定应转场时，临时忽略本轮旧 `character_place` 的 strong pin，并让 `_format_world_context(..., apply_persisted_place=False)` 按当前时间/天气/动线给出转场后的落点；旧地点仍以 weak 参考出现，不清空状态，规划器若输出新的 `character_location` 会回写刷新。
21. **回归测试**：新增 `test_scheduled_push_transition_does_not_lock_stale_previous_scene_place`，覆盖咖啡店告别/晚上约定后 45 分钟推送不再出现 `地点锁定（最高优先）`，并允许角色地点从 `cafe` 刷新为 `transit`；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 283 tests in 6.097s`，`OK (skipped=1)`。
22. **内裤/细服装标签误分类修复**：排查用户反馈“角色不穿内裤”时发现两类根因：`black g-string` 因 `ring` 子串被粗分到配饰/随身物，且只换上 `dress/nightgown` 时若衣柜没有 `panties` 槽，最终 prompt 没有内裤护栏。`appearance.py` 现在先按细服装词识别 bra/panties/g-string/stockings/shoes 等，再走配饰关键词；默认与示例 `outfit_keywords` 同步补齐内衣、袜鞋关键词。
23. **普通穿着出图防误裸护栏**：`build_prompt()` 默认在 negative 追加 `no panties / no underwear / bottomless / crotchless`，防止短裙、睡裙或低纯良度场景被模型自由发挥成未穿内裤；当 `clothing_off` 明确是全裸、bottomless、panties/g-string/thong/underwear 等下身脱衣意图时，`_apply_clothing_off()` 会移除这些护栏，不阻断用户明确要求的单图脱衣。
24. **回归测试**：新增测试覆盖 `g-string` 归入穿搭而非配饰、聊天可见外型显示不再把 `black g-string` 放进配饰、普通短裙 prompt 压制误判未穿内裤、明确 `clothing_off="panties"` 时放开对应 negative；验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 287 tests in 6.792s`，`OK (skipped=1)`。
25. **生图 safety 槽后移**：`PromptSlots` 新增独立 `safety` 槽，标准正向提示词不再把 `safe/nsfw` 追加到最前面的 `quality`；渲染顺序改为稳定画质/人数/身份/外观/画风在前，随纯良度和时段变化的 `safe/nsfw` 放到 scene 前，减少高变动评级词破坏前缀稳定。`PROMPT_SLOTS` 与 `/查看提示词` 会单独显示 `safety`，便于排查。
26. **AnimaTool 兼容**：因 Turbo schema 字段名是 `quality_meta_year_safe`，提交 payload 时用 `PromptSlots.quality_for_schema()` 临时组合 `quality + safety`，内部槽位和标准 ComfyUI prompt 仍保持拆分；新增测试锁定标准 prompt 中 `nsfw` 不在 `quality`，且 Turbo payload 仍输出 `quality_meta_year_safe="masterpiece, nsfw"`。验证 `py -3 -m compileall -q telegram_comfyui_selfie tests` 与 `py -3 -m unittest tests.test_core -v`，结果 `Ran 288 tests in 6.335s`，`OK (skipped=1)`。

## 今日变更（2026-06-27）

1. **checkpoint 时间节点软约束**：`_summarize_checkpoint()` 追加提示词规则，要求保留用户明确提到的日期、几点、期限、倒计时、约定时间和相对时间节点；`_extract_long_term_memories()` 在 checkpoint 来源下允许把跨场景仍有影响的时间节点保存为 `event`，不新增独立管线。
2. **dream 过时时间节点清理策略**：`_incremental_organize_memories()` 与 `_summarize_all_memories()` 的整理 prompt 约束为“过时不等于立刻删除”；只有事件已解决、取消、被替代，或从近期日记/checkpoint/当前窗口完全淡出时，才更新、合并或删除相关时间节点记忆。
3. **新场景前置 checkpoint**：`/新场景` 调用 `_checkpoint_current_context_before_reset()`，先处理切换前未折叠上下文与记忆提取，再清空当前 `chat_history`、checkpoint 摘要和短期场景状态；原始 SQLite `chat_messages` 仍保留给 dream。
4. **AnimaTool Turbo 去 neg**：`plan_animatool_slots()`、`_build_animatool_turbo_payload()` 和 `_post_animatool()` 均会剔除 `neg` / `negative` 字段，并把 `no text, no logo, no ui, no mosaic, uncensored` 追加到自然语言 `nltag` / `tags` 尾部；同时兼容 `nltag`、`nl_tag`、`nl_tags`、`tags` 字段名。
5. **回归测试**：新增/更新测试覆盖新场景前置 checkpoint、checkpoint 时间节点提示词、dream 时间节点淡出清理提示词、AnimaTool nltag 尾部与去 neg 行为。
6. **聊天采样参数**：新增 `chat_llm_top_p`、`chat_llm_frequency_penalty`、`chat_llm_presence_penalty` 默认配置、WebUI 字段与示例配置；代码层用显式 `sampling=True` 限定只在真实聊天回复请求中下发，避免污染 checkpoint/dream/memory 等结构化任务。
7. **生图站位精度修复**：`format_planning_spatial_context()` 从本轮意图/画面草案和最近真实 `user/assistant` 对话中单独抽取身体站位线索，作为“空间/身体关系硬约束”注入 `roleplay-image-plan`；`_merge_spatial_constraint_into_scene()` 在 planner 漏写时把关键站位追回到 `scene`。
8. **POV 与翻译层去冲突**：`view_opener("pov")` 改为中性的用户视角，不再固定 `eye contact` / `solo`；`_translate_to_tags()` 不再禁止动作主语，允许保留 `she/the character` 来锁定坐、站、躺、跪、脚边、腿上、身后等动作归属。
9. **日常同框与亲密同框拆分**：`build_prompt()` 不再把所有 `partner_in_frame=True` 都当作性爱/亲密路径；日常同框只移除 `solo` 并加入必要的手/脚/肩局部提示，真正 `is_intimate` 或性关键词场景才追加 `partial male body visible` / `intimate close-up` 等亲密兜底。

## 今日变更（2026-06-26）

1. **LLM prompt 比对脚本**：新增 `scripts/compare_llm_chat_prompts.py`，可读取 `llm_debug.json` / `llm_debug - 副本.json` 中指定 `entries_by_type`（默认 `chat:chat`），按 `session_id` 分组并对相邻请求做规范化 JSON prompt 比对。
2. **prompt 缓存排查报告**：脚本同时输出 provider 记录的 `cached_tokens` / `prompt_tokens`、本地精确公共前缀字符数与消息段数、前缀之后仍相同的消息块、消息 diff opcode、tools/tool_choice 与非 prompt 请求参数是否稳定；默认只输出 hash/长度，不展开 prompt 原文，避免人工扫日志误判。
3. **脚本回归测试**：新增 `LlmPromptCompareScriptTestCase`，覆盖“前缀相同、首个差异之后仍有相同消息段”的核心判断，防止后续修改脚本时把非前缀相同块漏报。
4. **本次日志验证结果**：对 `data/logs/llm_debug - 副本.json` 运行脚本，`chat:chat` 共 10 条、2 个会话、8 个相邻 pair；所有 pair 的 `tools` / `tool_choice` 和非 prompt 请求参数均稳定，变化集中在 `messages`；早期 provider cache 命中低，最后一个同会话 pair 达到 `7552/7658`（98.62%）。
5. **工具 schema 与请求体缓存优化**：压缩 `_chat_tools_schema()` 文案但保留视角、换装分层、临时裸体禁用、位置持久化和用户位置推断等语义；`_call_llm_messages()` 构造请求体时把 `tools` / `tool_choice` 放到 `messages` 前，减少工具定义落在动态尾部后的重复 miss 风险。
6. **用户当前输入标记清理**：Telegram 输入增强仍会在本轮 prompt 中保留 `【用户当前输入】` 方便模型理解，但写入 `chat_history` / SQLite `chat_messages` 前会移除该标题；读取旧历史、checkpoint 摘要和记忆提取格式化时也会兼容清理旧的 `【用户当前输入】` 标记，避免它继续污染历史前缀。
7. **dream 日记提示词约束**：日记生成 prompt 改为角色第一人称私密总结，并在提示词里要求首行使用 `# 日期 星期几 标题`；日记不再生成“新一天演绎提示”，也不做保存前标题强改或元信息剔除。若同一天已有日记，prompt 会明确本次输出将覆盖旧日记而不是追加续写。角色历史提要仍可生成“新一天演绎提示”。

## 今日变更（2026-06-25）

1. **聊天 dynamic system 拆分**：`_build_chat_messages()` 将低频世界模板和自然光硬规则并入 semistable；dynamic tail 只保留精确当前时间、本轮用户位置/空间关系、发图提醒和场景断档提醒。`world_runtime.py` 新增 `_format_world_semistable_context()` / `_format_world_dynamic_context()`，原 `_format_world_context()` 保持给生图/推送链路使用。
2. **semistable checkpoint 收敛复用**：新增世界模板参与 semistable 签名；天气、光线阶段、城市、角色身份或动线模板变化时，继续复用 `_track_semistable_context_change()`，在 checkpoint 后 pending 达到 `context_window_message_limit / 2` 时 force checkpoint。
3. **真实缓存复测**：使用 `deepseek-pro` / `deepseek-v4-pro` 和 `llm_debug - 副本.json` 的真实 entry 3/4/5 请求体，验证 tools 字段 JSON 位置不影响缓存；热缓存后原结构与拆分后结构连续请求均达到 99%+ 命中。拆分后模拟请求 dynamic tail 从约 642 字符降到 63 字符；新 semistable 首轮有冷缓存成本，第二轮恢复 99%+。
4. **角色扮演 prompt 精简**：静态聊天提示去掉重复发图节奏描述，补强“优先回应用户本轮话题、避免连续类似回复、不要因重要记忆主动跳题”的规则；checkpoint 摘要进一步限定为短期连续性，不承载长期记忆/角色弧线职责。
5. **dream 新一天指导**：角色历史提要可生成“新一天演绎提示”，只给基于事件与角色情绪的灵活方向，不写死台词、地点、日程或剧情；日记侧已在 2026-06-26 改为纯第一人称总结。
6. **自由配图命令**：新增 `/配图`，并加入 `/画图`、`/绘图`、`/生图` 等同义词；该命令复用完整聊天上下文，但允许用户通过参数优先覆盖场景、视角、机位、远近和局部特写，不再套用 `/自拍` 的硬设定。
7. **新场景上下文硬切换**：`/新场景` 不再只移动 `short_context_start`，而是清空模型侧未折叠对话历史和 checkpoint 摘要，同时在 `app_store.checkpoints` 中把边界推进到当前最新消息；旧聊天仍保留在 `chat_messages`，不影响后续 dream。
8. **角色画风字段化**：`/画风 <画风名>` 可直接写入当前角色卡 `style`，不再要求命中画风池；`/画风 清空` 会把角色画风字段置空且生图不回退全局画风。dream 会把当前角色的非空画风补入全局画风池，WebUI 角色设定面板提供画风池下拉与手动输入。
9. **WebUI 模型 profile 表单字段化**：模型 profile 编辑不再要求填写 JSON，改为 profile id、名称、base_url、api_key、model、max_tokens、timeout 等显式字段；thinking / fixed thinking 控制保持为内部兼容配置，不在 WebUI 中让用户填写。
10. **WebUI 反馈板**：总览页底部新增反馈板，异步读取 `/api/feedback`，不阻塞页面主状态加载；提交内容写入项目根目录 `TODO.md`，按当前会话激活角色名分 `##` 段并带 session 标记防冲突。普通用户只读写自己的段落，管理员可查看全部反馈。
11. **DSML 工具调用兼容**：聊天模型如果把工具调用以 DSML 文本返回到 `content`（例如 `update_location`），会转换成正常工具执行流程并在最终回复中清理 DSML 残留，避免 Telegram 直接收到原始 `<...tool_calls>` 标记。
12. **生图规划与 AnimaTool 适配层隔离**：`plan_roleplay_image()` 不再把 AnimaTool Turbo schema/knowledge 拼进业务图片规划 prompt，避免业务 schema 的 `scene` 被后端 schema 的 `tags` 污染；新增图片计划 scene 归一化，兼容旧 `tags` 返回并在 strong 地点锁定时给泛化 scene 补地点锚点，防止餐厅/家/商场等场景在最终生图 prompt 中丢失。
13. **生图天气贯通**：`plan_roleplay_image()` 优先使用调用方传入的 `weather_data`，避免推送链路重复拉取后前后不一致；`_translate_to_tags()` 与 `plan_animatool_slots()` 都显式注入当前天气文本，要求雨、雪、雾、风、冷热等可见天气在最终英文 tags 中通过窗外、地面、伞、湿痕、空气质感和光线体现。
14. **同空间视角终裁**：`image_planning.py` 新增 `_resolve_roleplay_view()`，对 LLM 给出的 `requested_view/planned_view` 做最终业务校正：同空间且无明确自拍/对镜/录像信号时，不再允许普通配图落到 `selfie/mirror`；普通同空间单人场景改压到 `third`，近距离互动改压到 `pov`，明确“帮忙拍一张”改为 `portrait`，并在 `user_location` 缺失时回退使用持久 `co_located` 状态。新增 3 条回归测试锁定这三种分支。
15. **自动配图 judge 视角收敛**：定位到 `chat:image-judge` 仍会把“凑近镜头确认论文”这类同空间日常陪伴场景错误写成 `view=selfie`，而线上服务进程启动时间早于 `b4d75ac`，当天日志仍在跑旧代码。`chat_context.py` 新增 `_sanitize_judge_view_hint()`：自动配图判断器只有在文本里出现明确自拍/对镜/拿手机拍/帮忙拍照等硬相机约束时才保留 `view`，其余普通日常场景一律清空，交给后续 `plan_roleplay_image()` 再按同空间规则判成 `pov/third/portrait`。同时把 `JUDGE` 日志补充为可打印 `view=...` 方便排查，并新增 3 条测试覆盖“论文场景误自拍清空 / 明确自拍保留 / judge 误传 selfie 时 planner 仍强制改成 pov”。
16. **摘配饰持久化兜底**：排查出“用户让角色摘掉眼镜，下一张图又戴回去”是因为首张图只通过 `clothing_off` 临时剥离了 `effective_appearance`，却没有改写当前衣柜。`service.py` 新增图片后处理：当 `tool_generate_image()` 的 `clothing_off` 明确命中当前已穿戴的可持久配饰（眼镜/项链/耳环/发夹等）时，生图成功后自动从 `wardrobe.accessory` / `dynamic_appearance` 移除，并记录 `WARDROBE` 日志；`chat_context.py` 同步强化 `change_appearance` 提示，明确“摘掉并继续不戴”的配饰也必须视作持久外观变更。新增 2 条回归测试覆盖“摘眼镜后下一张图不再戴回 / 临时脱开衫仍只影响单图”。
17. **`roleplay-image-plan` 输入瘦身**：`image_planning.py` 将角色扮演生图规划器从“完整对话上下文 + 详细照片历史”收敛为“短期连续性 + 最近已发图片摘要”。新的 `format_planning_continuity_context()` 只读取最近 `user/assistant` 消息、过滤 `system` 照片历史并按条截断；新的 `format_recent_photo_dedup_context()` 只保留最近图片的时间/视角/scene 摘要，不再重复注入原始描述和整段外貌快照。这样避免照片历史先混进对话块、再在图片块重复一次，同时让长期记忆检索 query 也随之稳定下来。新增回归测试覆盖“照片历史 system 不混进连续性 / planner user prompt 不再携带原始描述与长尾文本”。

## 今日变更（2026-06-24）

1. **初始化向导改为角色卡创建入口**：`/创建角色`、`/初始化`、无参数 `/创建OC` 都进入逐题状态机，普通文本回复优先被向导消费；流程压缩为 8 步：角色卡主键、出处/原名、外貌和穿搭、角色设定、关系和称呼、城市、纯良度、推送频率。
2. **初始化字段综合归档**：初始化收尾不再只依赖结构化行解析，会强制走一次 prompt intake，把外貌/穿搭、人格/类型、关系/称呼等非数值/固定格式字段交给 LLM 综合判断后合并。现有作品角色的 `original_name` 要求英文或姓氏在前罗马音，`series` 要求英文作品名，`visual_character` / `visual_series` 要求 Danbooru 风格标签；原创角色默认不写作品和视觉 tag。
3. **角色称呼字段修正**：新增 `custom_user_address` / `user_address`，表示"角色对用户的称呼"，与角色名和角色自称分离，并注入聊天静态前缀。
4. **WebUI 角色面板精简**：隐藏场景偏好/自拍偏好栏，保留内部兼容字段；快捷菜单包含 `/角色 list` 与 `/角色 load <名称>`。
5. **缓存命中与上下文分层修复**：聊天历史窗口改为 checkpoint 锚定，不再逐轮滑动；照片历史改为真实历史 `system`；checkpoint 裁剪保证第一条为 `user`；dream 只读 `user/assistant`；动态 system 拆出天级稳定层和半稳定状态快照层。
6. **长期记忆注入策略调整**：长期记忆直接按重要性取前 N 条注入，不维护 `hit_count`；checkpoint、角色历史、长期记忆三者职责重新分工。
7. **视觉模型与图片/引用输入**：新增用户级 `vision_profile_id` 和全局 `default_vision_model_profile`；视觉模型默认留空，留空时跳过图片处理。配置后，用户图片和引用图片先转成中文描述，再作为纯文本注入 chat 输入。
8. **模型管理重构**：WebUI 去掉 chat/fast 思考开关，思考状态完全绑定模型 profile；管理员可维护全局 profile，用户可维护私有 profile；模型 API key 返回掩码并支持保留旧密钥。
9. **Telegram 引用增强**：支持 `quote.text`、`reply_to_message`、`external_reply` 的文本注入；引用中包含图片时交给视觉模型描述。
10. **命令别名集中维护**：新增 `command_aliases.py`，命令别名按规范命令分组列表维护，并自动派生完整别名表与裸词快捷别名表；别名覆盖 `/创建角色`、`/新建角色`、`/角色创建`、`/menu`、`/拍照`、`/推送测试` 等正序/倒装写法。
11. **文档整理**：压缩旧时间线，把本次会话前的历史变更合并进当前架构状态；"今日变更"只保留 2026-06-24 新内容。
12. **画幅限制与反四宫格**：默认 width/height 从 1024x1024 改为 832x1216（2:3 竖版）；`_aspect_ratio_from_dimensions` 只返回 `2:3` 或 `3:2`，模拟真实相机画幅。负向提示词追加 `split screen, grid, multiple panels, collage`；画面规划器和 AnimaTool turbo slots 规划器都加入单帧构图硬规则（scene 只描写单一冻结瞬间，严禁分格/分镜/拼贴/多面板）。
13. **聊天 prompt 质量**：system_static 追加语言理解规则（日常表述默认不是表白或调情，只有明确使用恋爱/亲密词汇时才理解为亲密信号）+ 对话自然度规则（不要反复提及同一个具体物件/食物/配饰，保持话题新鲜感）。
14. **多层反幻觉约束**：checkpoint 摘要 prompt 追加 grounding 约束（只保留对话中明确出现的规则/承诺/事件，不确定时省略而非编造）；记忆提取 prompt 强化来源约束（只从对话原文提取，不推断/联想/编造，附反例）；角色历史提要 prompt 追加反编造约束（只基于日记原文）。
15. **角色切换修复**：`/角色 <名称>` 或 `/切换角色 <名称>` 匹配已保存角色卡时直接加载该角色，不再经过 LLM 分类创建新角色。

## 最新验证

- `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3 -m compileall -q telegram_comfyui_selfie tests`
- `node --check telegram_comfyui_selfie\static\app.js`
- `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3 -m py_compile scripts\compare_llm_chat_prompts.py`
- `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3 -m unittest tests.test_core -v`
- 最新结果：`Ran 389 tests in 9.567s`，`OK (skipped=1)`；默认跳过真实前缀缓存请求测试
- 工具 schema 当前紧凑 JSON 长度：`1898` 字符；chat 回复请求体 key 顺序为 `model, max_tokens, temperature, top_p, frequency_penalty, tools, tool_choice, messages`（`presence_penalty` 留空时不下发；checkpoint/dream/memory 等内部任务不下发采样参数）
- 本轮未改 prompt 比对脚本逻辑，未重新生成 `.tmp\llm_chat_prompt_compare_current.md`。
- 真实 API 缓存探针沿用上一轮结论：拆分后 entry 3/4/5 改写请求首轮为冷缓存，第二轮分别命中 `7040/7099`、`7168/7198`、`7552/7562`。
- 额外真实 API 缓存探针 `test_live_chat_context_cache_probe_uses_current_config_when_available`：默认跳过；设置 `SUCYUBOT_TEST_LIVE_CACHE_PROBE=1` 后才使用当前配置文件中的模型连接信息，通过真实 `handle_chat()` 链路连续回答三轮预设问题并输出缓存命中率。运行态 state / SQLite / 用户日志均隔离在测试临时目录；模型临时未返回可用回复时跳过。
- `git diff --check` 通过；Windows 下仅可能出现 LF/CRLF 提示

## 已知限制

- `selfie` 取景与互动动作仍可能存在轻度矛盾；当前已通过空间/身体关系硬约束与日常/亲密同框拆分降低站位错配，但尚未拆出完整 `pose/action/forbidden` 结构化字段。
- 会话状态处于"盒子 + 部分旧扁平键双写"的兼容期；后续彻底删除扁平键前必须先清点所有访问点和迁移测试。

## 下一阶段目标

1. 继续场景结构化：在已有 `location` 基础上拆出 `props`、`forbidden`、`pose/action/light`，减少手机、镜子、多手、互斥姿态等问题。
2. 收敛会话状态双写：逐步移除旧扁平键读写，只保留 `session_schema` 访问器和盒子结构。
3. 对视觉模型输入链路做真实 Telegram 文件回放测试，确认不同客户端的 `quote` / `external_reply` / caption 组合都能按预期注入。
4. 在 WebUI 模型面板增加更友好的 profile 编辑器，减少直接编辑 JSON 的误操作。
