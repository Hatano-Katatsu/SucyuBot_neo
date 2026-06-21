# AGENTS.md — SucyuBot_neo

## 项目概述

Telegram 机器人，结合 AI 角色扮演（DeepSeek 等 OpenAI 兼容 API）与 ComfyUI（Anima3 模型）生成动漫角色自拍。

## 技术栈

- **语言**: Python 3.11+
- **依赖**: `aiohttp>=3.9` (无其他第三方依赖)
- **数据库**: SQLite（长期记忆）
- **存储**: JSON 文件（配置 `data/config.json`，状态 `data/state.json`）
- **前端**: Vanilla HTML/CSS/JS（aiohttp SPA，Web 控制台）

## 运行命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行服务
python -m telegram_comfyui_selfie --config data/config.json

# 运行测试
python -m unittest tests.test_core -v

# 单独测试某个测试类/方法
python -m unittest tests.test_core.ServiceTestCase.test_parse_command_with_bot_mention -v
```

## 项目结构

```
telegram_comfyui_selfie/
├── __init__.py          # 导出 TelegramComfyUIService
├── __main__.py          # CLI 入口
├── service.py           # 核心服务类（组合所有 mixin）
├── defaults.py          # 默认配置、菜单、场景
├── commands.py          # 所有 /command 处理
├── chat_context.py      # 聊天管道：系统提示构建 + 工具调用
├── generation.py        # ComfyUI 生图：PromptSlots + 提示词构建 + 工作流 + 生成
├── image_planning.py    # LLM 画面规划器
├── appearance.py        # 外观标签解析/合并/注入
├── prompt_intake.py     # 自然语言输入分类（OC 创建、外观归档）
├── time_context.py      # 时间/季节/日出日落/光线阶段计算
├── memory.py            # SQLite 长期记忆存储
├── memory_policy.py     # 记忆自动提取 + 过滤规则
├── scheduler_runtime.py # 定时推送 + 天气 + NTR 冷落惩罚 + 场景连续性
├── world_runtime.py     # 世界状态：地点动线 + 生活档案 + 天气 + 城市
├── telegram_io.py       # Telegram Bot API 通信
├── process_restart.py   # 进程自重启
├── webui.py             # aiohttp Web 控制台 + REST API + Prompt 槽位编辑
└── static/              # Web 前端 (index.html, app.js, styles.css)

根目录:
├── Start-SucyuBot.cmd   # Windows 一键启动脚本（调用 ps1）
├── Start-SucyuBot.ps1   # PowerShell 启动脚本（检测端口/自动打开 WebUI）
├── config.example.json  # 配置模板
├── requirements.txt     # aiohttp>=3.9
├── AGENTS.md            # 本文件
└── README.md            # 用户文档
```

## 代码风格

- 使用 `from __future__ import annotations` 延迟求值类型注解
- 类通过 **mixin 多重继承** 组织：`TelegramComfyUIService(ProcessRestartMixin, TelegramIOMixin, CommandHandlersMixin, ChatContextMixin, MemoryPolicyMixin, SchedulerRuntimeMixin, WorldRuntimeMixin)`
- Mixin 方法通过 `self` 访问其他 mixin 的方法，使用 `hasattr` 做防御性检查
- 所有 I/O 方法都是异步 (`async def`)
- 配置通过 `self.config.get(key, default)` 访问，用户会话级覆盖通过 `self._get_session_cfg(session_id, key, default)`
- 日志统一用 `logger = logging.getLogger(__name__)`
- 注释用中文，docstring 用中文，代码标识符用英文

## 测试

- 测试文件：`tests/test_core.py`
- 基于 `unittest.TestCase` + `unittest.mock.AsyncMock`
- 每个测试方法内部用 `asyncio.run(run())` 执行异步代码
- `make_service()` 辅助方法创建临时目录 + 最小配置
- 添加新功能时必须写测试（核心路径已覆盖：命令解析、提示词构建、记忆 CRUD、角色切换、外观合并、Web 序列化）

## 注意事项

- **不要**在 mixin 类中定义 `__init__`；所有初始化在 `TelegramComfyUIService.__init__` 中完成
- **禁止**将 LLM API key 写入 `config.example.json`；通过 `masked_config()` 对 Web 端隐藏
- `session_id` 字符串格式固定为 `"telegram:{chat_id}"`，通过 `session_id_for_chat` / `chat_id_from_session` 转换
- 长期记忆以 `session_id + character` 为隔离维度：换角色即换记忆空间
- fire-and-forget `asyncio.create_task` 内的异常可能被静默吞掉，排查生图/推送失败时优先查看 service.log
- `_get_llm_value("chat", "temperature")` 的 legacy 回退会落到 `llm_temperature_scene` 而非专门的聊天温度——如果 `chat_llm_temperature` 被显式设为空字符串
- 每轮功能完成后更新本文件：同步当前框架状态、已完成事项、测试结果和下一阶段目标，避免后续接手时重新考古。

## 当前框架状态（2026-06-21）

本项目已经从 AstrBot 插件重构为独立 Telegram Bot 服务。当前核心能力包括：Telegram 原生 Bot API 收发消息、OpenAI 兼容聊天模型、独立生图辅助模型、ComfyUI 工作流调用、WebUI 管理台、定时推送、长期记忆、短期上下文、角色系统、OC 创建、自然语言外观输入、地点动线、天气与时间场景。

服务入口仍是 `python -m telegram_comfyui_selfie --config data/config.json`。Windows 用户可使用根目录的 `Start-SucyuBot.cmd` 或 `Start-SucyuBot.ps1` 一键启动：如果 `127.0.0.1:8787` 已经有 WebUI，则直接打开；否则启动服务后打开 WebUI。WebUI 顶部也有启动/状态相关入口。

`service.py` 现在只负责组合 mixin，不再承载主要业务。新增或调整功能时优先放到对应模块：命令放 `commands.py`，聊天上下文放 `chat_context.py`，生图与提示词放 `generation.py`，画面规划放 `image_planning.py`，外观合并放 `appearance.py`，自然语言输入放 `prompt_intake.py`，时间光照放 `time_context.py`，长期记忆放 `memory.py` / `memory_policy.py`，动线与天气放 `world_runtime.py` / `scheduler_runtime.py`。

## 当前 Prompt 框架

生图提示词已经改为由 `PromptSlots` 渲染最终正向提示词。`generation.py` 中的 `PromptSlots` 用来记录并输出各个槽位，日志中会出现 `PROMPT_SLOTS`，最终 ComfyUI 调用仍会保留旧版 `PROMPT` 日志，方便对比。

当前核心槽位：

- `quality`：稳定质量词，例如 `masterpiece, best quality, absurdres, score_9`
- `count`：人数与主体数量，例如 `1girl, solo`
- `identity`：既有公开角色的角色名/作品名；OC 不应把姓名当视觉 tag
- `base_appearance`：用户设定的稳定外观，例如发色、瞳色、体型、种族特征
- `effective_appearance`：基础外观叠加默认发色/眼色等后的有效外观
- `style_artist`：画师、模型偏好的风格 tag，例如 `@00 gx4`、`artist:...`
- `style_general`：一般画风描述
- `scene`：镜头、地点、动作、光线、道具、氛围
- `one_shot_appearance`：画面规划器本轮临时提出的外观补充，不应持久化
- `negative`：负面提示词
- `positive_final`：最终送入 ComfyUI 的正向提示词

旧数据里可能把质量词、人数词、画师 tag 混进了 `positive_prefix`。当前运行时会自动拆分清理：质量词归 `quality`，`1girl/solo` 归 `count`，画师 tag 归 `style_artist`，剩余稳定外观归 `base_appearance`。此外已有安全清理工具 `cleanup_prompt_prefix_slots()`，WebUI「操作 -> 维护清理」可以先预览再备份执行，把质量词移出存储、把风格词合并进画风字段、把人数词迁移到 `custom_count` 字段。

最终正向提示词的稳定顺序是：`quality -> count -> identity -> style_artist -> effective_appearance -> style_general -> scene -> one_shot_appearance`。`one_shot_appearance` 即使没有被调用方手动拼进 scene，也会由槽位渲染器放到最终 positive 末尾；如果 scene 中已有完全相同的 tag，会自动去重。

WebUI 会话页已经有专门的「Prompt 槽位编辑」面板。它按用户编辑 `custom_count`、`custom_positive_prefix`、`custom_default_hair`、`custom_default_eyes`、`custom_current_style`、`dynamic_appearance`、`custom_scene_preference`、`custom_selfie_preference`，并通过 `/api/prompt-slots/{session_id}` 显示实际生效值和 PromptSlots 预览。

## 关键行为规则

- 前置自拍不能在画面中出现手机；只有对镜自拍允许同时出现镜子和手机。
- 如果场景不是对镜自拍，负面提示词要压制 `holding phone`、`visible phone`、`phone in hand`、`mirror selfie` 等。
- 亲密/伴侣同框场景默认改用 POV，并只允许用户/伴侣身体局部入画；只有用户明确要求拍照、录像或对镜时，`device_in_frame=true` 才允许保留 selfie/mirror 视角和手机/镜子。
- 用户自己的性别由全局 `user_gender` 或会话 `custom_user_gender` 控制，决定亲密场景里用户身体局部按 male 还是 female 处理；命令侧可用「用户性别/我的性别」设置。
- 场景槽位应描述镜头、地点、动作、光线、道具和氛围，不应重复塞入稳定外貌。
- 角色稳定外观由用户设定和角色配置提供，画面规划器只能提出本轮临时补充。
- 既有角色设定必须把“对话身份”和“生图识别”分开：`custom_character/custom_series` 是用户可读角色身份，`custom_visual_character/custom_visual_series` 是 Anima/danbooru 识别 tag。聊天、推送和图片规划都必须通过 `_session_role_identity()` 获取当前角色名，角色态不能回落到全局默认 `bot_name/role_name`。
- **角色字段单一来源 + 读时组装（重要架构不变量）**：`custom_scheduled_persona` 只存**纯人格描述**（性格/语气/习惯），**禁止**把身份、角色类型、关系、职业焊进这个串。这些信息各有独立字段（`custom_bot_name`/`custom_role_name`/`custom_spatial_relationship`/`custom_character_occupation`…），由读取侧实时拼：`_get_effective_persona()` 补“你是X”身份前缀，`_build_chat_messages` 补角色类型行与 `rel_line` 关系行，生图/推送各自的身份行补角色类型。新增“需要进聊天/生图的角色信息”时，加字段 + 在读取侧拼，**不要**回到写时焊接（那是历史漂移根源，已迁移修复，见 `_migrate_legacy_personas`）。
- 关系 `custom_spatial_relationship` 全局默认为空，只有显式设置才注入聊天/生图/推送，`/关系` 是专用命令。职业 `custom_character_occupation` 是用户面向自由文本，白天去向枚举 `custom_character_day_anchor` 由职业后台派生（`_normalize_day_anchor` 别名表 + LLM）。`/个性设置` 可调项由单一来源 `PERSONALIZE_FIELDS` 派生（别名映射与展示列表同源）。
- OC 角色不要把中文名、昵称或作品名塞进正向 tag；只有已知公开角色才使用角色/作品 identity。
- 视觉身份系统：`service.py` 中 `VISUAL_IDENTITY_OVERRIDES` 是 (角色名, 作品名) → (英文视觉tag, 英文作品名) 的硬编码映射表，`_infer_visual_identity` 优先查表、其次从外观字段中识别已有 danbooru tag，最后交给 LLM 推断。OC 角色（系列名为空或 "原创/OC"）跳过身份注入。
- 自然语言输入应交给 `prompt_intake.py` 分类，不要求用户手写 tag。
- 长期记忆存储稳定偏好、人物关系和重要事实；短期上下文负责当前话题、当前地点、当前事件。不要把临时服装、上一轮场景台词、一次性道具写入长期记忆。
- 定时推送应尽量接上最后上下文和世界动线，避免上一轮还在某地，下一轮无解释地跳到不相关场景。
- 时间系统需要同时考虑小时、星期、季节、天气、城市和日出日落，避免夏季下午被错误写成黄昏夕阳。

## 近期已完成

- 独立服务化：脱离原 AstrBot 插件，以 Telegram 原生 API 运行。
- 双模型分工：聊天模型负责角色回复和工具调用，生图辅助模型负责画面规划。
- 长期记忆：SQLite 存储，按 `session_id + character` 隔离。
- 短期注意力约束：降低上一轮场景、旧话题、旧动作污染下一轮的概率。
- 世界动线：支持基础地点、用户城市、天气、现实时间和定时推送场景。
- 季节光照：根据日期和城市估算日出日落，修正清晨、黄昏、夜晚等描述。
- Prompt 槽位日志：`PROMPT_SLOTS` 可观察每个槽位的来源和最终拼接结果。
- Prompt 槽位渲染：`PromptSlots.render_positive()` 已成为最终 positive 的来源，避免日志槽位和实际 ComfyUI prompt 分叉。
- Prompt 老数据清理：新增 dry-run/apply 双模式清理工具，覆盖全局 `positive_prefix`、会话 `custom_positive_prefix` 和保存角色 `appearance`；执行前会备份配置/状态文件，并展示 before/after。
- 真实数据清理：已清理全局 `positive_prefix` 中的质量词和 `artist:wlop`，备份为 `data\config.prompt-prefix-backup-1781877912.json`；清理后再次预览无剩余变更。
- Prompt 槽位编辑 WebUI：会话页新增按用户编辑面板，支持基础外观、默认发型/瞳色、画风、临时穿搭/配饰、场景偏好和自拍偏好，并显示最终槽位预览。
- 场景/自拍偏好注入：新增 `custom_scene_preference`、`custom_selfie_preference`，OC 创建和自然外型输入会自动归档；生图规划和主动推送场景都会读取这些偏好。
- 自然语言 OC/外观输入：`prompt_intake.py` 自动把自然描述归入角色、外观、关系、城市、场景偏好等槽位。
- WebUI 改进：增加启动入口、用户维度查看、动线相关信息入口和提示词查看能力。
- 视觉身份标签：新增 `custom_visual_character` / `custom_visual_series`，既有作品角色自动映射到 danbooru 视觉标签；OC 留空。
- 角色身份防串味：`/角色 <角色名>` 现在会把 LLM 推断出的用户可读角色名写入 `custom_character` 和 `custom_bot_name`，并在人设缺少“你是 X”时自动补身份；旧状态即使缺 `custom_bot_name`，聊天/推送/图片规划也会从 `custom_character` 回退，避免东云绘名这类角色被全局“蕾伊”身份污染。
- 生活档案：LLM 自动推断角色的年龄阶段（成年/未成年）和白天去向（公司/学校/工厂等），影响世界动线地点选择。
- 场景连续性：推送场景规划器注入最近对话和照片上下文，让推送图承接上一轮场景而非瞬移。
- 手机屏幕 UI 去除：生图 prompt 自动清理 `phone screen`、`countdown`、`message interface` 等 UI 描述。
- 持久化 count 槽：新增 `custom_count` 字段存储人数标签（`1girl`/`1boy`）；OC 创建、角色设定、外观输入自动拆分人数到该字段；性别推断优先读取 `custom_count`，为空时回退到从旧式前缀提取；已有的 `cleanup_prompt_prefix_slots` 工具已将人数词迁移到 `custom_count` 并从外观字段中剥离。
- 亲密场景配图规则：`image_planning.py` 新增中文亲密关键字检测和 `is_intimate` 标志；图片规划器在亲密场景下固定 POV 视角、人物优先（表情/身体反应/用户身体局部）、环境精简、近景特写。`is_intimate` 标志通过 `tool_generate_image → _do_generate → do_generate → build_prompt` 管道透传，`build_prompt` 内与英文关键字 OR 作为 fallback。亲密 scene 下正向剥离 `solo`，并按 `user_gender/custom_user_gender` 把用户身体写成男性或女性局部；负向会移除对应阻挡项，另压制 `third-person perspective`。
- 规划器主判 + 正则兜底：`image_planning.py` 现在要求规划器输出 `is_intimate`、`partner_in_frame`、`device_in_frame` 三个布尔值，并用中文关键词兜底亲密和拍摄/录像意图；`generation.py` 只用无歧义英文拍摄词（`recording`、`filming`、`sex tape`、`on camera` 等）兜底设备入画，刻意不把 `holding a phone/smartphone` 当成拍摄意图，避免误泄漏手机被放行。
- 推送配文可见性修复：`_inject_photo_history_messages()` 现在将推送图片的 caption 注入聊天上下文（"你给这张图配的文字：{caption}"），聊天模型能感知推送时角色说了什么。
- `replying_to_selfie` 提示优化：从读取 `source_description` 改为读取 `sent_photos_history[-1]` 的 `scene` + `caption`；措辞从"用户这句话是在回应你"改为"你刚向用户发了一张图……用户现在说"，不替模型预判用户意图。
- PromptSlots 清理：移除纯展示字段 `dynamic_appearance`（实际内容已通过 `_explicit_appearance_override()` 注入 `effective_appearance`）。
- 人设串漂移消除：`custom_scheduled_persona` 只存纯人格描述。身份/角色类型/关系/职业全部字段单源、读时渲染。写入侧停焊——`cmd_create_oc` 和 `cmd_character` LLM 路径都不再把人称前缀焊进人设。老数据迁移用 `_migrate_legacy_personas` 正则自动剥离，幂等。读时 `_get_effective_persona` 统一组装。
- 快照漂移消除：`_snapshot_character(state)` 在切换角色时自动保存离开角色的最新可变状态（人格/外观/关系/画风/纯良度）。快照格式统一为 18 字段，删除无用的 `prompt_intake`。LLM 角色路径补全缺失的 `style`/`scene_preference`/`selfie_preference`。下次切回拿到的是离开时的最新值而非建角色时的过时快照。
- `/角色 reset` 轻量化：只清对话上下文和照片历史，保留角色设定和角色池。硬重置入口改为 `/角色 clearup`。
- 角色位置漂移：聊天侧 prompt 解耦（2026-06-21）。`_format_world_context` 新增 `pin_location` 参数：对话进行中（`_active_chat_history` 非空）时 `pin_location=False`，世界状态不再输出声明式的「角色当前所在: 具体地名」「接下来动线」「空间关系判断」，改为只给「日常此时多半在 X 一带（背景倾向，当前位置以对话为准）」，从源头消除“时钟地点”与“对话地点”的互斥指令；冷启动/无活跃对话仍 `pin_location=True` 钉死时钟地点供模型自然提及。推送/生图侧不受影响（仍钉地点）。
- 角色位置持久化（2026-06-21）。新增带 TTL 的 `character_place` 持久字段（`world_character_place_ttl_hours`，默认 4h），仿 `user_place`。两条写入路径：①工具 `update_location`（模型显式声明换地点，置信 0.95）；②自动抽取 `_update_character_place_from_text`（每条角色回复后复用 `_infer_user_place` 提取“说话者自述所在”，置信 0.8，作兜底基线）。`build_world_state` 在新鲜期内用持久位置覆盖时钟推断——推送/生图、`short_context` 重置后的冷启动都据此保持连续。换角色时 `_clear_conversation_context` 清空角色位置。至此位置漂移（prompt 互斥 + 跨上下文持久化）两块均闭环。
- 场景结构化·location 优先（2026-06-21）。把生图地点从「scene 自由散文」提升为受约束的结构化字段，闭合“位置持久化→生图”这条回路。配图规划器（`image_planning.py`）：新鲜期内（`_active_character_place`）在 system prompt 加“地点锁定（最高优先）”约束并钉死本次画面地点；JSON 输出新增 `character_location` 字段（取值同 `user_location`），冷启动时把规划器判断回写 `character_place`（置信 0.6），对称于已有的 `_apply_llm_user_location`。至此角色位置在聊天与生图两侧都受同一权威字段约束，不再各自发挥。location 之外的 `props/forbidden/pose` 等子字段属独立收益，留待后续。
- 测试用上下文命令（2026-06-21）。`/回滚 [N]`（`cmd_rollback`，别名 rollback/undo/回退/撤回）从聊天历史尾部删掉最近 N 轮（角色回复+对应用户消息），默认 1 轮，纯删上下文不调模型；`/重答`（`cmd_regenerate`，别名 regenerate/redo/重新生成）删掉上一条角色回复并用同一句话重跑聊天管线。方便手动测试对比同一输入下的输出。
- 默认服装串味修复（2026-06-21）。全局默认 `dynamic_appearance`（魅魔的临时穿搭）原本在 4 处消费点被当 `state.get(...) or config.get(...)` 回退值用，导致东云绘名等既有角色没自带穿搭时套上了魅魔的默认服装。新增 `_effective_dynamic_appearance(session_id)`：自己有穿搭用自己的，否则**仅默认角色态**（`not _is_character_set`）才回退全局默认，设了角色一律返回空、交画面规划器按场景决定。统一替换 `_get_effective_persona` / `_chat_visible_appearance_context` / `image_planning` / `scheduler_runtime` 四处（`generation.py` 本就只读会话级、无回退）。另外 `/角色` 切到新既有角色时清空 `dynamic_appearance`，不继承上一个角色的穿搭。
- 天气聊天刷新（2026-06-21）。天气只在生图/推送/手动查询时按需拉取（30 分钟缓存），纯文字聊天只读缓存、从不刷新，导致一整天聊天的天气停在早安推送那次。新增 `_schedule_weather_refresh(session_id)`，`handle_chat` 每轮在缓存过期（>30min）时 fire-and-forget 后台拉一次，不阻塞回复、下一轮即生效。
- 最近一次全量测试：`python -m unittest tests.test_core -v`，124 tests OK。

## 已解决：角色位置漂移（location drift）

**原问题**：角色“当前位置”是纯时钟计算（`build_world_state` → `_routine_scores`，按小时/星期/天气/职业算得分最高地点），无视对话、无持久化；聊天里同时出现声明式“角色当前所在: 商场”和对话“我在家”，互斥指令导致瞬移。

**三步修复（均见上方近期已完成）**：
1. **prompt 解耦**：对话进行中不再钉死时钟地点（`pin_location=False`），位置交给对话。
2. **持久化**：对话/工具确立的位置写进带 TTL 的 `character_place`，`build_world_state` 新鲜期内优先用它而非时钟，跨上下文重置也连续。
3. **生图侧绑定**：配图规划器把地点从自由散文提升为结构化 `character_location`，新鲜期钉死=`character_place`、冷启动回写，生图不再二次发挥。

**仍可继续打磨**（非阻塞）：自动抽取靠 `PLACE_PATTERNS` 正则，新颖地点/复杂表述会漏（此时回落时钟，不会更差）；如需更稳可让模型多调 `update_location`，或给自动抽取加 LLM 兜底。

## 下一阶段目标
1. 继续场景结构化（location 已完成，见上）。把 scene 余下部分继续拆成 `props`（道具，可去重/限数，治“三只手”）、`forbidden`（本次禁止元素，view=selfie 自动注入 phone/mirror，治手机镜子规则失效）、`pose/action/light` 等子字段，让规则用代码强制而非散文叮嘱；`outfit_once` 已由 `new_appearance_tags` 覆盖。
2. 加强测试不变量。重点覆盖：基础外观不含质量词/画师词/人数词；OC 不注入姓名 tag；一次性外观不持久化；正负提示词不冲突；自拍不露手机；对镜自拍才允许镜子和手机共存；亲密场景下 `is_intimate / partner_in_frame / device_in_frame` 标志正确透传。

## 接手建议

下一位 agent 接手时，先读 `tests/test_core.py` 中和 Prompt、外观、推送、记忆相关的测试，再改实现。这个项目的风险不在单个函数，而在多个模块共同拼出一次聊天或一次生图：聊天模型、画面规划器、短期上下文、长期记忆、世界动线、Prompt 槽位都会同时影响结果。

改 Prompt 或生图流程时，优先增加小而具体的测试，再改实现。每次改完至少运行：

```bash
python -m unittest tests.test_core -v
```

如果只改文档或 WebUI 文案，可以不跑完整测试，但要在最终回复里说明未运行测试的原因。
