# AGENTS.md — SucyuBot_neo

## 项目概述

Telegram 机器人，结合 AI 角色扮演（DeepSeek 等 OpenAI 兼容 API）与 ComfyUI（Anima3 模型）生成动漫角色自拍。

## 技术栈

- **语言**: Python 3.11+
- **依赖**: `aiohttp>=3.9` (无其他第三方依赖)
- **数据库**: SQLite（长期记忆 + 会话状态 + 城市目录）
- **存储**: YAML 文件（配置 `data/config.yml`，回退 `data/config.json`）；`data/state.json` 已弃用，首次启动自动迁移到 SQLite
- **前端**: Vanilla HTML/CSS/JS（aiohttp SPA，Web 控制台）

## 运行命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行服务（YAML 配置优先，不存在则回退 JSON）
py -3 -m telegram_comfyui_selfie --config data/config.yml

# 或一行启动（调用 run.cmd）
./run.cmd

# 运行测试
py -3 -m unittest tests.test_core -v

# 单独测试某个测试类/方法
py -3 -m unittest tests.test_core.ServiceTestCase.test_parse_command_with_bot_mention -v
```

> 本机注意：Git Bash 里裸 `python` 解析到 Windows Store 占位 stub，会以 exit 49 静默失败（无输出）。一律用 `py -3`，或绝对路径 `C:\Users\17122\AppData\Local\Programs\Python\Python311\python.exe`。

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
├── run.cmd              # 一行命令启动（优先使用 data/config.yml）
├── config.example.json  # 旧版 JSON 配置模板（兼容）
├── config.example.yml   # 新版 YAML 配置模板
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

## 当前框架状态（2026-06-24）

本项目已经从 AstrBot 插件重构为独立 Telegram Bot 服务。当前核心能力包括：Telegram 原生 Bot API 收发消息、OpenAI 兼容聊天模型、独立生图辅助模型、ComfyUI 工作流调用、WebUI 管理台、定时推送、长期记忆、短期上下文、角色系统、OC 创建、自然语言外观输入、地点动线、天气与时间场景。

服务入口默认使用 `data/config.yml`（不存在时回退 `data/config.json`）。Windows 用户可直接双击 `run.cmd` 或根目录的 `Start-SucyuBot.cmd` / `Start-SucyuBot.ps1` 一键启动：如果 `127.0.0.1:8787` 已经有 WebUI，则直接打开；否则启动服务后打开 WebUI。WebUI 顶部也有启动/状态相关入口。

模型配置统一走 `global_model_profiles`（YAML 中嵌套定义），支持每个 profile 固定思考开关。当前默认 profile 仅保留 `deepseek-pro`（思考开）、`deepseek-flash`（思考关）、`glm`（思考关），kimi 系列已移除。`default_chat_model_profile` 对应聊天模型，`default_fast_model_profile` 对应生图辅助模型。ComfyUI 本地 socket 端口由 `comfyui_local_socket_port` 控制，默认 `7789`。生图后端通过 `image_backend`（`native`/`animatool`）切换，AnimaTool 方式下从 `/anima/schema_turbo` 和 `/anima/knowledge_turbo` 动态获取字段和规则。

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

- `view=selfie` 是前摄自拍（角色伸手举手机自拍、看向镜头），但**手机本体和手机 UI 都不能出现在画面里**。正向提示词只写 `A selfie of a ..., looking at viewer, one arm extended toward the viewer`，**绝不写 `off-frame front-facing phone camera` 这类措辞——那正是手机 UI 框的来源**；手机/UI 的抑制全部交给负向提示词。
- `view=portrait` 是独立的新视角：「别人（用户或他人）在画面外帮角色拍的照片」，角色看向镜头、为镜头摆姿势，画面里只有角色。正向写 `A photo of a ..., looking at viewer, posing for the camera, taken by someone else just out of frame`。规划器只在两种情况选它：①用户与角色**同处一地**且角色明确请用户/他人帮忙拍照；②NTR 场景（他人给角色拍照）。同样不出现手机/相机。
- 只有对镜自拍（`view=mirror`）才允许同时出现镜子和手机。
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
- 角色位置 pin 三类错误修复（2026-06-21）。自动抽取加职业身份门：主妇/自由职业等无固定职场角色随口提“上班/公司”不再被钉到锚定职场（显式 `tool_update_location` 不受限）；低置信锚定 pin 仅在该时段时钟评分>0 时才覆盖时钟，避免傍晚提一句上班深夜仍卡公司；`build_world_state` 新增 `apply_persisted_place` 开关，WebUI 按钟点预测一整天动线时跳过“此刻”的持久 pin，避免整天被同一 pin 拉平。
- LLM 抽取角色位置 + 权威分档 + 历史轨迹（2026-06-22）。把角色位置抽取从纯正则升级为 LLM 兜底，置信度按来源分档（工具声明 > LLM 抽取 > 正则），并保留历史轨迹，进一步闭合位置漂移回路。
- 地点分类扩充 + 真实 POI（2026-06-22）。`PLACE_TYPES` 新增 13 类（博物馆/景点/寺庙神社/图书馆/动物园水族馆/游乐园/酒吧/KTV/体育馆/超市/书店/海边/美容美发），对照高德大类补齐；城市地点目录接入真实 POI（高德 `/v3/place/text` 用于中国、谷歌 Places `searchText` 用于海外），来源链 真实POI → LLM生成 → 内置示例 逐级回落；中国/海外判定改用 `_classify_city_region`（LLM 按城市缓存）替代脆弱的高德 geocode level 启发式（海外只用谷歌，杜绝同名中国地点污染）；角色位置保留完整地名（`tool_update_location` 与 LLM 抽取存具体 `place_name`，钉位时优先于目录示例）。新增 `amap_api_key` / `google_places_api_key` 配置（掩码）+ 控制台字段。
- 控制台品牌化（2026-06-22）：像素风魅魔 favicon，标题改为 Sucyubot Console。
- `/角色 delete` 删当前角色后被快照复活修复（2026-06-22）。delete 分支此前只从 `saved_characters` 删 key、未清当前角色态，后续 `_snapshot_character`（load/切换/创建OC/设定角色触发）会用 `custom_character` 把刚删的角色重新写回 `saved_characters`，表现为“删了又出现”，角色池只剩一个时最明显。修复：删的若是**当前角色**，一并清空 `custom_*` / `dynamic_appearance` / `wardrobe` / `persona_user_set` / `purity` 并清对话上下文、回退全局默认；删非当前角色保持原行为（仅删存档）。新增两个测试覆盖“删当前不复活”“删非当前不影响当前”。
- YAML 配置默认化（2026-06-22）。`__main__.py` 默认优先读取 `data/config.yml`，不存在时回退 `data/config.json`；新增 `run.cmd` 一行命令启动；`config.example.yml` 成为新版配置模板，旧 `config.example.json` 保留兼容。
- 配置项清理（2026-06-22）。移除已废弃字段：`skill_md_path`（代码无引用）、legacy LLM 字段（`llm_api_*`、`chat_llm_api_*`、`image_llm_api_*`、`*_disable_thinking`），统一由 `global_model_profiles` 描述模型。同步清理 `defaults.py`、`config_store.py` 的 `CONFIG_GROUPS`、WebUI 前端和示例文件。
- 外部 POI 代理透传（2026-06-22）。`world_runtime.py` 新增 `_external_http_proxy()`，把 `telegram_proxy_enabled`/`telegram_proxy_url` 复用到高德和谷歌 Places 请求；HTTP(S) 代理直接传 `aiohttp`，SOCKS 代理使用 `aiohttp_socks.ProxyConnector`；无代理时保持 `trust_env=True` 读取环境变量兜底。新增 `ExternalProxyTestCase` 覆盖三种代理场景。
- 模型 profile 固定思考开关（2026-06-22）。`global_model_profiles` 中 `deepseek-pro` 固定开启思考、`deepseek-flash` 固定关闭、`glm` 固定关闭；kimi 系列已移除。新增 `thinking_fixed` 字段，`service._resolve_llm_profile` 识别该字段后忽略用户侧 `chat_thinking`/`fast_thinking` 覆盖；WebUI 模型页显示“固定”状态；`/think`/`/fastthink` 命令对固定模型给出提示。
- 本地 socket 端口配置（2026-06-22）。新增 `comfyui_local_socket_port: 7789`（位于 `comfyui` 段），用于 ComfyUI 本地 Unix/TCP socket 通信，与 Telegram 代理端口一致时复用同一本地通道。
- YAML 解析器升级（2026-06-22）。`config_store.load_simple_yaml` 从二级解析器升级为递归解析器，支持任意层级的嵌套字典和 literal block，使 `global_model_profiles` 可以保持人类可读的嵌套 YAML 格式；`dump_simple_yaml` 也支持嵌套渲染，保证配置 load/dump 往返一致。
- state.json 弃用，迁移到 SQLite（2026-06-22）。`app_store.py` 新增 `session_state` 表（每会话一行 JSON blob）和 `city_catalogs` 表；`service._load_state` / `_write_state` / `_flush_sessions` / `_save_session_state` 全部改为读写 SQLite；首次启动时若 SQLite 无数据但 `state.json` 存在，自动迁移旧数据并写入 SQLite。`_write_state` 不再全量写 JSON 文件，改为按脏会话逐条 UPSERT。`world_runtime._store_city_catalog` 直接写 SQLite。WebUI 删会话调 `app_store.delete_session_state`。cleanup 工具备份目标从 `state.json` 改为 SQLite 数据库文件。
- 上下文缓存优化（2026-06-22）。`_build_chat_messages` 中 system prompt 按变化频率分层：静态前缀（人设/身份/关系/工具说明/持久化说明）和动态后缀（时间/光线/频率/外型/世界状态/记忆）拆成两个独立 system message。`messages[0]` 为静态前缀，history 紧随其后（前缀稳定，定期 checkpoint 裁剪），动态 system 放在 history 之后、user 之前。DeepSeek 服务端 prefix cache 可命中 `static + history前缀` 这一大段，不再被每请求变化的时间/光线冲掉。
- 上下文三层化（2026-06-23）。prompt 结构从两层（静态+动态）升级为三层：静态 system（人设/身份/关系）→ 稳定上下文（checkpoint/角色历史提要/长期记忆，仅 checkpoint/dream 时更新）→ 动态上下文（时间/光线/外貌/世界状态，每轮变化）。稳定上下文放在照片注入之后、动态 system 之前，使 `[静态+历史+照片+稳定] 在两次 checkpoint/dream 之间几乎不动，最大化前缀缓存命中。`_build_scene_system_prompt()` 复用同一三层结构供推送/自拍场景生成使用。
- checkpoint 字符数触发（2026-06-22）。`_queue_checkpoint_if_needed` 新增 30k 字符触发条件：pending 消息总字符超 30000 时立即触发 checkpoint 裁剪，防止 history 过长冲掉缓存。`_run_context_checkpoint` 入口同步检查。
- 聊天回复分段发送（2026-06-22）。`telegram_io.py` 的 `send_message` 新增 `split_paragraphs` 参数，按 `\n\n` 拆分后每段间隔 1 秒发送，每段内部仍走 3900 字符切分。仅 LLM 聊天回复开启（`chat_context.py` 和 `cmd_regenerate`），菜单/命令回复不受影响。通过 `chat_split_paragraphs` 配置控制（默认 `true`，设为 `false` 关闭）。
- 照片历史注入下移（2026-06-23）。`_inject_photo_history_messages` 从 `messages[1]`（静态 system 与 history 之间）移到 history 之后。照片块每隔几轮就变（发图/12h 过期/去重翻转），放在原位置会作废其后**整段 history** 的前缀缓存；下移后它的变化只影响尾部（照片+动态 system+user），两次 checkpoint 之间累积的 history 前缀不再被周期性清零。拼接顺序定为 `[静态 system] + [history] + [照片注入] + [动态 system] + [user]`。
- 聊天历史窗口 checkpoint 锚定（2026-06-23）。`_build_chat_messages` 的 prompt 历史窗口从 `_active_chat_history`（`history[start:][-N:]`，每轮滑动）改为新增的 `_chat_prompt_history`（`history[start:]`，全量未折叠消息）——不再逐轮 `[-N:]` 滑动，长度由 checkpoint 折叠 + 存储兜底共同约束，使前缀在两次 checkpoint 之间**只增不移**，仅 checkpoint 落地那一刻发生一次归位。配套三处：①第 185 行每轮硬裁（裁到 `context_window_message_limit`）改为 `_apply_history_trim(state, _history_storage_cap())`（=阈值×3 的存储兜底，正常运行不触及，仅 checkpoint 长期失联时防 `chat_history` 无限膨胀）；②新增 `_apply_history_trim` 在从头部删消息时同步下移 `short_context_start`，修复原本前缀裁剪后短期场景切片错位（甚至取到空窗口）的隐患；③`_run_context_checkpoint` 的 keep 裁剪也改走该 helper。`_active_chat_history` 保留给配图判断器（`[-6]`）和推送续场（`[-10]`），各自独立开窗、语义不变。dream 机制走 SQLite 全量日志、不碰 prompt 前缀，与本次缓存改动互不干扰。
- 短期位置重置·对称化 + 连续化（2026-06-23）。原先两条重置路径各管一个、各漏一个：`_reset_short_context`（SR，`/新场景`/关键词/隔 6h）清 `user_place` 不清 `character_place`；`_clear_conversation_context`（CC，换角色/clearup）清 `character_place` 不清 `user_place`——换话题角色位置不动、换角色用户位置渗进新角色。修复：(1) CC 补上 `user_place*`（含 `user_co_located`），两路径对称硬清；(2) SR 改为**不硬清位置**——`user_place` 交给 4h TTL 自然老化（B 方案，换话题≠用户物理移动），`character_place` 经新增 `_demote_character_place` **降级为 weak**（把 `character_place_updated_at` 后移到 strong 边界外、带 -1s 余量防 Windows 时钟分辨率 flaky；confidence/TTL 不动），新场景不再钉死生图、仍作背景，等新位置声明覆盖或 TTL 过期——连续过渡而非瞬移/失忆。`_reset_short_context` 由 staticmethod 改实例方法。新增 3 个测试钉住语义。
- 时效判定薄原语 `_within`（2026-06-23）。新增 `_within(updated_at, ttl_seconds=None, *, since=0)`（`world_runtime.py`，挂 WorldRuntimeMixin，`self.`/`service.` 均可调），统一“算年龄/在不在窗口”这一步：`ttl_seconds=None` 只按 `since` 切点过滤、`since>0` 要求晚于该时刻。**刻意只收年龄计算、不上收策略**（pin 的 strong/weak 分档、过滤条数上限等仍留各调用点）。收敛 4 处散点：replying 窗口（12h）、照片注入（12h+短期重置边界）、近 3h 用户发言、生图照片过滤（重置边界），外加 `_active_user_place`/`_active_character_place` 的 TTL 判定。scheduler 续场那处**故意不收**——它用注入的 `now` 做整天预测，换成 `time.time()` 会破坏 WebUI 日预览。
- WebUI 配置项隐藏（2026-06-23）。从 `app.js` 的 `configSections` 移除 6 个基础设施/运维配置项：`long_memory_db_path`（长期记忆数据库路径）、`user_log_enabled`（分用户活动日志开关）、`user_log_dir`（日志目录）、`web_enabled`（启用控制台）、`web_host`（控制台 Host）、`web_port`（控制台 Port）；同时加入 `webui.py` 的 `YAML_ONLY_CONFIG_KEYS` 使后端 API 也拒绝写入，防止通过 API 绕过前端隐藏。
- Git 更新编码修复（2026-06-23）。`git_update.py` 的 `_git_run()` 添加 `encoding="utf-8"` + `errors="replace"`，修复中文 Windows 下系统默认 GBK 解码 git UTF-8 输出导致的 `UnicodeDecodeError`（`0xad` 等多字节序列在 GBK 中非法）。
- 冻结不活跃用户（2026-06-23）。新增会话状态字段 `frozen`（布尔）和 `frozen_at`（冻结时间戳）；WebUI 操作页新增"冻结 7 天不活跃用户"按钮（`POST /api/admin/freeze-inactive`，扫描 `last_interaction` 超过 7 天的会话批量冻结）；`telegram_io.py` 收到消息时自动解冻（`frozen=False`）并记录日志；`scheduler_runtime.py` 跳过冻结会话的推送调度；会话选择器和动线用户列表显示"已冻结"标签（虚线边框 + 红色 badge）；支持单个会话冻结/解冻 API（`POST /api/sessions/{sid}/freeze` / `unfreeze`）。
- 前端删除二次确认（2026-06-23）。所有删除操作统一添加 `window.confirm` 确认对话框：记忆删除（新增，显示记忆内容预览）、日志清空（新增）；角色删除和日记删除已有确认（无需改动）。
- 手动整理记忆（2026-06-23）。WebUI 角色标签页记忆列表新增"手动整理记忆"按钮，调用 `_organize_memories_after_dream` 让 AI 根据最近日记、checkpoint 和当前对话自动整理非手动记忆（增删改），手动记忆（`kind=manual`）不受影响。新增 API `POST /api/sessions/{sid}/organize-memories?character_key=...`。按钮带确认对话框和 loading 状态。
- 记忆提取时机统一（2026-06-23）。移除逐轮记忆提取（`run_roleplay_chat` 中的 `_queue_long_memory_extraction` 调用），记忆仅在 checkpoint 溢出时由 `_extract_long_term_memories_from_messages` 提取，dream 时由 `_organize_memories_after_dream` 整理。平时对话不再触发 LLM 记忆提取，降低 token 消耗和 API 调用频率。
- 记忆作为稳定上下文（2026-06-23）。`_build_chat_messages` 中记忆和 checkpoint 从 `system_dynamic`（每轮变化）移至新的**稳定上下文**区段，位于照片注入与动态 system 之间。prompt 结构变为：`[静态 system] + [历史] + [照片] + [稳定: checkpoint/历史提要/记忆] + [动态: 时间/光线/世界] + [用户]`。稳定上下文仅在 checkpoint/dream 时更新，最大化 DeepSeek 前缀缓存命中。
- dream 记忆整理阈值化（2026-06-23）。`_organize_memories_after_dream` 分两路：非手动记忆条数 ≤ `long_memory_context_limit / 2` 时走增删改（`_incremental_organize_memories`），prompt 中要求合并类似记忆、去掉过时记忆、保持总数在阈值内；条数超过 `long_memory_context_limit` 时走全量 LLM 重写（`_summarize_all_memories`），先删除全部非手动记忆再由 LLM 总结为 ≤ 阈值条数的新记忆。手动记忆（`kind=manual`）全程不受影响。
- 角色历史提要（2026-06-23）。`app_store.py` 的 `context_meta` 表新增 `character_history_summary` 列（含自动迁移）。dream 写完日记并整理记忆后，新增 `_generate_character_history_summary()` 根据上次历史提要 + 最近两天日记生成角色发展脉络摘要（字数上限同 checkpoint），存入 DB 和 session state。`_build_scene_system_prompt` 和 `_build_chat_messages` 均注入该提要作为稳定上下文的一部分。
- 推送/自拍复用完整聊天上下文（2026-06-23）。`_llm_write_scene` 不再从头构建 system prompt，改为调用新增的 `_build_scene_system_prompt()` 获取完整上下文（静态人设 + checkpoint + 历史提要 + 记忆 + 时间/光线/外貌/世界状态 + 场景连续性），只追加推送特定的模式指令（morning/normal/ntr 要求、画面主体规则、自拍物理规则等）。`cmd_selfie` 也通过同一管道受益。`chat_context.py` 新增 `_build_scene_system_prompt(session_id, weather, mode, now)` 方法。
- `/测试推送 morning` 触发 dream（2026-06-23）。`cmd_test_push` 在 `mode == "morning"` 时先 `_run_dream(force=True)` 再触发推送，与自动早安推送行为一致。
- 短期态字段单一来源（③，2026-06-23 已实现）。原先三份手写名单（`_clear_conversation_context` 清空 / `_reset_short_context` SR 清空 / `_conversation_context_payload` 快照）各列字段子集、互不同源，新增字段须三处同步、漏一处即 drift（默认服装串味/快照复活/`user_place`↔`character_place` 不对称/`short_context_start` 错位都是此模式；还查出 `wardrobe` 切角色串味的潜在 bug）。改为**按归属分类、黑名单反推**：state 键分三类——会话全局（`SESSION_GLOBAL_STATE_KEYS`：计时/调度/NTR/`frozen`/容器自身，绝不随角色走）、角色配置（`custom_*` 前缀 + `purity`/`*_user_set`，走 `saved_characters` 卡）、**角色短期态（其余一切）**。`_is_transient_state_key()` = 非全局且非配置，**新增字段默认跟角色走，漏配的失败方向是“正确隔离”而非串味**。快照 `_conversation_context_payload` 与全清 `_clear_transient_state` 同源遍历分类器，杜绝“冻的≠清的”。短期态里再分出 `RESET_PRESERVED_TRANSIENT_KEYS`（外型/穿搭/`life_profile`）：`/reset` 清对话但留外型（`keep_appearance=True`），切角色 `_restore_character_context` 全清（`keep_appearance=False`）后用目标角色存档覆盖。默认值集中到新 `_session_state_defaults()`（单一来源，清空按字段默认复位）。副带收益：**外型/穿搭现随角色往返**（切回拿回自己穿搭），`wardrobe` 串味修复；`create_oc` 的初始穿搭赋值移到 restore 之后避免被全清。删掉 load 路径中 restore 之后多余且有害的 `dynamic_appearance=""`。新增 2 个测试（A→B→A 全量往返 + 分类器分区）。**配置卡 `_character_export_payload`（WebUI/可移植）保持 schema 不动**；config↔transient 完全统一成单一 per-character 归档留作后续。
- 角色卡 schema 单一来源（2026-06-23）。「一个角色」此前在导出（`_character_export_payload`）、快照（`_snapshot_character`）、写回（`_apply_character_payload`）三处各手写一份 18+2 字段表，新增字段须三处同步、漏一处即 drift（「快照统一为 18 字段」即此负担的疤）。新增 `character_card.py` 作为唯一字段表：`CARD_STRING_FIELDS`（18 个 卡片键↔state 键，含 outfit↔`dynamic_appearance` 历史约定）+ 两个特殊字段（`allow_change_appearance` 三态、`purity` 带 clamp/`purity_user_set` 副作用）；导出/快照走 `card_from_state`，写回/导入走 `apply_card_to_state`，行为逐字段保持不变。默认卡 `_default_character_payload`（从全局 config 读、各字段有特殊默认值如 bot_name→蕾伊、role_name 空保留空）**未折叠其 body**（避免改动特殊默认值的风险），但其卡片字段→config 键的映射 `_DEFAULT_CARD_TO_CONFIG` 改为引用 `character_card.DEFAULT_CARD_TO_CONFIG`（读/写回共用），字段集由新增测试 `test_character_card_schema_single_source` 钉住（导出/快照/默认卡三者字段集 == `CARD_KEYS`，且写回→导出往返值一致）。四处方法签名不变，所有调用点（`commands.py`/`webui.py`）无需改。这是数据结构重构「阶段 0」（角色卡单源），state 字段 scope 表（阶段 1）与位置子对象（阶段 2）、对话上下文去重（阶段 3）留待后续。
- 前缀缓存毒化修复·穿搭混进静态前缀（2026-06-23）。**症状**：聊天前缀缓存命中率随换装暴跌。**根因**：`_get_effective_persona` 自初始提交起就把当前穿搭（`_effective_dynamic_appearance`）拼在人格末尾；后来缓存三层化（`3adc83f`/`3419aa2`/`40aa77e`）把 `_get_effective_persona()` 整个塞进 `system_static`（`messages[0]`），却没审计它内部装了什么——一个中频变化字段被焊进了「本应不变」的静态前缀，每次换装作废整条历史的服务端 prefix cache。穿搭其实在动态层已正确注入（聊天 `_chat_visible_appearance_context`、场景「当前附加外貌」），静态那份纯属冗余，且因输出始终正确、无指标监控、无测试守护而静默存在。**修复**：`_get_effective_persona(session_id, include_appearance=True)` 加开关；两个 prompt 构建器（`_build_chat_messages` / `_build_scene_system_prompt`）的静态前缀传 `include_appearance=False`，其余调用方（image_planning/scheduler/world_runtime/命令展示）不变。新增 `test_static_prefix_stable_across_outfit_change` 钉死不变量：只换穿搭时 `messages[0]` 必须逐字不变、新穿搭仍出现在动态 system。**教训**：这是数据病根（身份/配置概念里混入短期态字段）在 prompt 层的现身——名为 `persona` 却暗藏可变 `outfit`，任何假设「persona 稳定」的代码都会被背刺。治本即重构阶段 1（state 字段按身份/配置 vs 短期态分盒子），边界清晰后此类混入在源头写不出来。
- 数据结构重构·阶段 1（会话 state 字段单一来源，2026-06-23）。会话 state 的「默认值表」（service `_session_state_defaults` 70 行字面量）与「归属分类」（commands 三个手写 frozenset）此前散在两文件、各列一份，新增字段须两处同步、漏一处即 drift。新增 `session_schema.py` 的唯一 `STATE_SCHEMA`：每字段声明一次（归属 G/C/T + 默认值 + reset 保留）。`_session_state_defaults()` / `SESSION_GLOBAL_STATE_KEYS` / `CHARACTER_CONFIG_EXTRA_KEYS` / `RESET_PRESERVED_TRANSIENT_KEYS` / `is_character_config_key` / `is_transient_state_key` 全部从它派生。**刻意不改扁平命名空间**（`state["custom_bot_name"]` 读写点全不变，零迁移、零调用点改动）；分类器保留「custom_ 前缀 ⇒ 配置」「其余 ⇒ 短期态」兜底，未登记新字段也能正确归类。`commands.py` 再导出旧名保持 import 路径不变。`last_interaction` 用 `factory=time.time` 保留每会话取当前时间。新增 `test_state_schema_single_source_derives_sets` 逐字段锁死派生集合 == 重构前手写值 + 三类互斥全覆盖。阶段 2（位置子对象）、阶段 3（对话上下文去重）留待后续。
- 脱不掉衣服修复·clothing_off 裸体兜底（2026-06-23）。**症状**：性爱/沐浴后角色应裸体，但生图仍把持久穿搭画出来。**根因**：`clothing_off`（逐图脱衣/裸露指令）是**唯一没有确定性兜底**的判定项——`is_intimate` 有 `_detect_intimate_context`、device 有 `_detect_device_context`，唯独裸体全靠规划器 LLM 自觉填；规划器一漏填，持久 `dynamic_appearance` 就原样流进 `effective_appearance` 画回去，且规划器还会把脱下的衣服写进 scene（「湿裙子贴着胸口」）二次强化。**修复**：①`image_planning.py` 新增 `NUDITY_CONTEXT_ZH`（只收强信号：明确性行为 + 明确脱光/裸体词，刻意不收 沐浴/事后/余韵 等可能已重新着装的暧昧词）+ `_detect_nudity_context`；规划器 `clothing_off` 留空但对话/意图命中裸体信号时，三个返回路径统一兜底 `completely nude`（不覆盖规划器显式值）。②规划器 system prompt 要求性爱/裸体必填裸露词、且填后 scene 不再把已脱衣服写成穿着。③`generation.py` 的 `_apply_clothing_off` 全裸分支把刚脱掉的衣物压进**负向**，抵消 scene 残留着装描述。新增 `_detect_nudity_context`/兜底/负向三组测试。**注意取向**：兜底刻意保守（宁可漏判不可误脱），bath/事后 等暧昧场景不强制裸体——力度不够时往 `NUDITY_CONTEXT_ZH` 加词即可。治本（区分"拥有的衣柜"vs"此刻穿着含裸体"的持久态）仍属数据结构后续阶段。
- 数据结构重构·阶段 1b（嵌套分盒**骨架**，2026-06-23）。阶段 1（scope 表）只是给扁平 state 贴归属标签，字段仍挤在一个平面——clothing 这类「穿搭/衣柜/裸体混在一起没有模型」的 bug 根没动。决定做**真正的嵌套分盒**（`state["clothing"]["outfit"]` 这种），但 state 访问点有 **555 个**（`grep state[/state.get`，commands 192 / service 80 / webui 71…），一次性机械改几乎必然改崩。故按「先搭骨架、逐 box 切换」推进。**本步只搭骨架，不碰任何访问点**：`session_schema.py` 新增 5 个 box（`session`/`character`/`clothing`/`place`/`context`）+ 扁平↔嵌套双向迁移（`migrate_flat_to_nested`/`migrate_nested_to_flat`，无损、幂等、含 `_schema_version` 标记）+ `box_for`（box 由 scope 派生，短期态按域细分 clothing/place，其余 context；未登记字段也有确定去向）。新增 `test_state_box_migration_roundtrip` 钉死往返恒等 + 幂等 + 每字段归合法 box。**迁移尚未接入 load/save**（运行码仍读扁平），下一步起逐 box 把访问点切到嵌套并接迁移，**首个 box = clothing**（顺带把"脱衣 bug"从根上用 outfit/wardrobe/closet/nudity 的清晰模型解决，替代当前的关键词兜底补丁）。阶段 1 的 scope 表未浪费——它现在是 box 派生的来源。
- 数据结构重构·阶段 1c（clothing box 切换 + 持久裸体态根治脱衣 bug，2026-06-23）。第一个真正切换的盒：把 `dynamic_appearance`/`wardrobe`/`wardrobe_closet` 从扁平顶层收进 `state["clothing"]`，并新增**持久裸体态** `nudity`/`nudity_at`。`session_schema` 把 1b 的通用 `migrate_flat_to_nested` 换成 per-box 的 `ensure_clothing_box`（懒迁移旧扁平→盒 + 补齐子键，幂等）+ 访问器 `get_outfit/set_outfit/get_wardrobe/set_wardrobe/get_closet/set_closet/get_nudity/set_nudity/clear_nudity`；`STATE_SCHEMA` 删除 3 个旧字段、加 `clothing`（transient, reset_preserved，整盒作为一个短期态单元冻结/解冻/reset 保留）。约 40 处真·state 访问点（service/commands/generation/webui/memory_policy/image_planning/appearance）全改走访问器；`character_card` 的 outfit 从 `CARD_STRING_FIELDS` 拆出经访问器读写（CARD_KEYS 仍含 outfit）；`_get_session_state` 每次 `ensure_clothing_box` 自动迁移旧数据，零手动迁移。`config["dynamic_appearance"]`（全局默认穿搭）是另一命名空间，未动。**持久裸体态（脱衣 bug 根治）**：`plan_roleplay_image` 收口——本图全裸即 `set_nudity(now)`；后续图规划器没判脱衣但新鲜期内（TTL `NUDITY_PERSIST_TTL_SECONDS=3h`）自动续上 `clothing_off=completely nude`；换装（`_wardrobe_apply_to_state` 渲染出非空穿搭）、`/新场景`（`_reset_short_context`）、超 TTL 三者任一解除。替代了纯每图关键词兜底（兜底仍在，作首图触发）。新增测试：clothing box 迁移/访问器、持久裸体续接→换装/新场景解除；阶段1 测试更新（RESET_PRESERVED={clothing,life_profile}）。注意：旧 1b 的 `migrate_flat_to_nested`/`is_nested`/`SCHEMA_VERSION` 已移除（per-box ensure 取代全量迁移），box 常量/`box_for`/`BOX_OF` 保留供后续盒（character/place/context/session）复用同一模式。
- 穿搭串归一·确定性剥衣（2026-06-23）。线上日志显示持久裸体态已激活、但生图仍把裙子画出来。**根因**：`appearance.remove_tag` 是裸 `text.replace(tag, "")`，而存储的 `dynamic_appearance` 带**双空格**（`bias cut  liquid`）和**重复标签**，与渲染串（单空格、`normalize_appearance_tag` 折叠过）对不上 → 裙子标签删不掉，剥衣概率性失败。**修复**：`session_schema.normalize_outfit_string`（折叠内部空格 + 大小写不敏感去重，保序），在 `set_outfit`（写入）和 `ensure_clothing_box`（读取，懒清理历史脏数据）两处生效；纯字符串、不引入 appearance 依赖避免循环导入。**刻意不剔除发色/瞳色**：它们与临时换发功能（wardrobe hair 槽渲染进 dynamic）绑定，全裸时由 worn 一并删除 + base 兜底，不需在此动。真实脏会话验证：归一后裙子标签可被 `remove_tag` 全局删除（含渲染重复两遍的情况）。新增 `test_outfit_normalized_so_nude_strips_deterministically`。
- 最近一次全量测试：`py -3 -m unittest tests.test_core`，仅 `ExternalProxyTestCase::test_external_http_proxy_socks` 一例失败——**预先存在**的环境问题（`aiohttp` SOCKS 连接器需运行中事件循环，`git stash` 验证改动前同样失败），与本次改动无关，跳过。（注：测试类多继承自 `ServiceTestCase`，同名用例会在各子类各跑一遍，故 `Ran N` 计数远大于源码用例数；本机 Bash 里的 `python` 指向 Windows Store 占位 stub，会以 exit 49 静默失败；跑测试用 `py -3` 或 `C:\Users\17122\AppData\Local\Programs\Python\Python311\python.exe`。）
- OC 创建时穿搭过滤 + 去重（2026-06-24）。**修 #2（新 OC 发瞳污染 dynamic_appearance）+ #4（outfit 写入重复）**。在 `cmd_create_oc` 中 `outfit_tags` 计算完毕后：①调用 `session_schema.normalize_outfit_string` 折空格+大小写不敏感去重（治写入重复）；②用 `appearance_rules.parse_appearance` 分槽，只保留 outfit/accessory/other 三槽剔除 hair/eyes（治发瞳污染）。`dynamic_appearance` 仅存服装/配饰，稳定外貌走 `custom_positive_prefix`。新增 3 个测试：`test_create_oc_strips_hair_eyes_and_dedup_from_outfit`、`test_create_oc_empty_outfit_unchanged_after_filter`、`test_webui_outfit_save_reflected_in_build_prompt`。全量 1323 tests OK（预存 1 skip）。
- 数据结构重构·阶段 2（place 盒切换，2026-06-24）。第二个真正切换的盒：把 `user_place` / `character_place` / `user_co_located` / `character_place_history` / `rounds_since_location` 等 14 个扁平 transient 字段收进 `state["place"]`。`session_schema` 新增 `ensure_place_box`（懒迁移旧扁平→盒，幂等）+ `_PLACE_DEFAULT` + `_LEGACY_PLACE_FLAT_KEYS` + 全套访问器（`get_user_place/set_user_place/get_character_place/set_character_place/get_user_co_located/set_user_co_located/get_character_place_history/append_character_place_history/increment_rounds_since_location` 等 ~25 个）。`STATE_SCHEMA` 删除 14 个旧字段、加 `place`（T, reset_preserved=False）。`service._get_session_state` 挂接 `ensure_place_box`；`world_runtime.py` ~39 个访问点全部迁移走访问器；`chat_context.py` 的 `rounds_since_location` 递增和 `image_planning.py` 的历史轨迹读取同步迁移。所有地方不再直写 `state["user_place"]` / `state["character_place"]`。`_clear_transient_state` 因 `place` 在 STATE_SCHEMA 为 scope T，自动以盒粒度冻结/解冻/清空——换角色清位置、换话题只降级不硬清，语义不变。未登记的旧 `user_place_source` 补齐进盒默认值。新增 `test_place_box_migration_and_accessors` 覆盖迁移往返 + 访问器 + 幂等 + clear_transient 复位。全量 1339 tests OK（预存 1 skip）。

## 已解决：角色位置漂移（location drift）：角色“当前位置”是纯时钟计算（`build_world_state` → `_routine_scores`，按小时/星期/天气/职业算得分最高地点），无视对话、无持久化；聊天里同时出现声明式“角色当前所在: 商场”和对话“我在家”，互斥指令导致瞬移。

**三步修复（均见上方近期已完成）**：
1. **prompt 解耦**：对话进行中不再钉死时钟地点（`pin_location=False`），位置交给对话。
2. **持久化**：对话/工具确立的位置写进带 TTL 的 `character_place`，`build_world_state` 新鲜期内优先用它而非时钟，跨上下文重置也连续。
3. **生图侧绑定**：配图规划器把地点从自由散文提升为结构化 `character_location`，新鲜期钉死=`character_place`、冷启动回写，生图不再二次发挥。

**仍可继续打磨**（非阻塞）：自动抽取靠 `PLACE_PATTERNS` 正则，新颖地点/复杂表述会漏（此时回落时钟，不会更差）；如需更稳可让模型多调 `update_location`，或给自动抽取加 LLM 兜底。

## 交接任务：换装清空后角色卡在裸体 + 基础特征串被污染（2026-06-23，待修）

**现象**：会话生图提示词里**完全没有任何服装标签**，角色只靠场景里的 `blanket/pillow` 遮挡，等于没穿。复现样本（会话 `telegram:6430033168`，床戏后的一张 selfie）：正向从 `…purple eyes, pink vertical pupils,` 直接接 `A selfie of a woman, …, she freezes on the couch wrapped in a blanket…`，中间没有任何 outfit tag。

**已确认的根因（查了 SQLite `session_state`，非生图组装时丢失，是真没有）**：
```
dynamic_appearance : ""    # 服装标签：空
wardrobe           : {}    # 衣柜：空
wardrobe_closet    : {}    # 衣橱收藏：空（连能穿回的旧衣服都没了）
custom_allow_llm_change_appearance : true   # 自动换装开着
```
- 基础特征 `positive_prefix` 按设计**不含衣服**，衣服全靠 `dynamic_appearance` 提供；它一空，整条 prompt 就没服装。
- 项目有**两套脱衣机制**：①`clothing_off`（逐图临时脱，`dynamic_appearance` 保留、事后自动复原）；②`change_appearance`（永久换装，reset/replace/`reset_all` 会**真清空** `dynamic_appearance`+`wardrobe`）。床戏脱光走了 ②，把衣柜永久清空，事后没有剧情让角色穿回来——auto-换装开着也不会自补（它只在剧情出现换装时才触发）。
- `wardrobe_closet` 也空，导致连"穿回之前那套"的素材都没有。

**疑似副作用（需单独定位，源头未证实）**：`custom_positive_prefix` 开头混进了 `bangs, swept bangs, slit pupils, bedroom eyes`——更像某次 `change_appearance` 的 hair/eyes 槽**漏写进了基础特征串**（用户样本里那个孤立的 `swept` tag 即来自此）。基础串不该带这些临时项。

**已就位的诊断埋点（本次已加，历史日志没有，复现后即可坐实清空发生在哪一轮）**：
- `LOC` 行：[`_apply_llm_user_location`](telegram_comfyui_selfie/world_runtime.py:897)，记录 `co_located` / `user_location` 判定结果。
- `WARDROBE` 行：[`tool_change_appearance`](telegram_comfyui_selfie/service.py:1959)（入口 allow/desc/mode）+ [`_wardrobe_apply_to_state`](telegram_comfyui_selfie/service.py:1914)（分槽结果，含 `reset_all`）。复现时让角色换次衣服 / 走一次床戏脱衣，看 `WARDROBE … → 分槽=` 行。

**建议修复（按优先级，需确认后动手）**：
1. **床戏脱衣只走临时 `clothing_off`，不让模型用 `change_appearance` 永久清空衣柜**；或退一步：`change_appearance` 的 reset/replace **保留 `wardrobe_closet`**，使角色随时能"穿回上一套"。
2. **`dynamic_appearance` 为空时回落到"上一次非空穿搭"快照**，避免角色永久卡在裸体态（自定义角色不继承全局 `dynamic_appearance` 是有意的，见 `test_set_character_does_not_inherit_default_outfit`，所以要的是 per-session 的"上次穿搭"快照而非全局默认）。
3. **定位并堵住 `positive_prefix` 被 hair/eyes 临时项污染的写入路径**（`change_appearance` 的 hair/eyes 槽只应进 `dynamic_appearance`，绝不能并进 `custom_positive_prefix`）。这是纯定位+加守卫，不改判断逻辑，可先做。

**相关代码**：换装链路 [`tool_change_appearance`→`_apply_wardrobe`→`_wardrobe_apply_to_state`→`_classify_wardrobe_change`](telegram_comfyui_selfie/service.py:1887)；clothing_off 逐图脱衣在生图侧（`image_planning.py` / `generation.py` 的 `clothing_off` 分支）；外观开关三态见 [`_allow_llm_change_appearance`](telegram_comfyui_selfie/service.py:786)。先加针对性测试（脱光后 `dynamic_appearance`/`closet` 的预期、`positive_prefix` 不被临时项污染）再改实现。

## 交接任务（2026-06-24）：脱衣/外观污染 + 分盒重构进行中

**当前进度（数据结构分盒重构）**：阶段 1（scope 表单一来源）✅、`clothing` 盒切换 ✅、持久裸体态 ✅、穿搭串归一（去重+空格）✅、OC 创建发瞳过滤 ✅、`place` 盒切换 ✅。下一步按路线图砌 `context`→`session`→`character` 三盒。

**⚠️ 重启线上 bot**：place box 懒迁移依赖 `_get_session_state` → `ensure_place_box`，重启后所有老数据的 14 个扁平位置字段自动收敛到 `state["place"]` 盒。与 clothing box 同理——重启即生效，无需手动清理。

**线上日志（2026-06-24 00:11–00:14，新角色林翩翩）暴露的待修问题**：

1. **「衣服脱了」不触发裸体（P1，最严重）**。用户「…衣服脱了」、角色「寝衣滑落」，但自拍仍全套汉服。两因叠加：①`image_planning.NUDITY_CONTEXT_ZH` 缺倒装/含蓄词（有「脱掉衣服/脱下衣服/衣服都脱」，**没有「衣服脱了/脱了衣服/宽衣解带/敞开/滑落/褪下」**）；②规划器只写「半脱」（nightgown slips to elbows）、`clothing_off` 留空。方向：补关键词表 + 让明确「半脱/敞胸」也按程度填 `clothing_off`（topless 等）。

2. ~~**新角色被默认魅魔发/瞳污染（P1，根因）**~~ ✅ 已修复（2026-06-24）。`cmd_create_oc` 的 `outfit_tags` 写入前经 `parse_appearance` 分槽剔除 hair/eyes，只保留 outfit/accessory/other。穿搭去重同步修复。

3. **scene 英文被换装去冲突逻辑损坏（P2）**。`generation._strip_conflicting_scene_outfit` 把 `moon-white nightgown…` 替换成 `moon-wearing the current outfit`——颜色词正则 `\b(white|…)` 命中 `moon-white` 的 `white`，把 `moon-` 拦腰截断。方向：替换前要求颜色词左边是空白/句首边界，别截断连字符复合词。

4. ~~**outfit 写入时就重复（P2）**~~ ✅ 已修复（2026-06-24）。`cmd_create_oc` 写入前调用 `session_schema.normalize_outfit_string` 折空格+去重，`set_outfit` 内部二次归一，展示与存储两端干净。

## 已知限制（暂不修，等模型能力）

- **时序叙事 / 同一部位多位置导致崩图**：规划器有时会把 scene 写成带时间推进的小叙事（`Suddenly … / still cling / doesn't bother wiping`），并给同一部位分配互斥位置（例：尾巴同时“tracing circles on the armrest”又“taps the back of the hand”）、给脸两个表情态。diffusion 没有时间轴，会把整段当成同一帧的约束全集去满足 → 叠加态（双尾/扭曲尾、糊脸）。本质是画图模型的语义解析能力问题，**当前不在 prompt 端硬堵**（硬堵会把生动场景描述削干，收益有限）。若日后要治本，方向是在 `image_planning.py` 的 “Scene boundary” 指令里加约束：scene 只描述单一冻结瞬间、禁时间推进词、同一身体部位只给一个位置/动作、表情只给终态。`_normalize_*` 那套下游正则只改人称/颜色/光照，碰不到叙事结构，治不了这个。
- **selfie 取景 + 互动动作的轻度矛盾**：前置自拍框里混进“别人给她揉腿/尾巴拍手背”这类互动时逻辑不自洽，但因为人称视角已限定，最终图影响不大，归到同一类问题，一并暂不处理。

## 下一阶段目标
1. 继续场景结构化（location 已完成，见上）。把 scene 余下部分继续拆成 `props`（道具，可去重/限数，治“三只手”）、`forbidden`（本次禁止元素，view=selfie 自动注入 phone/mirror，治手机镜子规则失效）、`pose/action/light` 等子字段，让规则用代码强制而非散文叮嘱；`outfit_once` 已由 `new_appearance_tags` 覆盖。
2. 加强测试不变量。重点覆盖：基础外观不含质量词/画师词/人数词；OC 不注入姓名 tag；一次性外观不持久化；正负提示词不冲突；自拍不露手机；对镜自拍才允许镜子和手机共存；亲密场景下 `is_intimate / partner_in_frame / device_in_frame` 标志正确透传。

## 数据结构分盒重构·路线图（state 嵌套分盒）

会话 state 正在从「扁平 ~73 字段」迁到「按域嵌套 5 个盒」。**逐 box 切换**，每盒一个 commit：
`ensure_<box>_box`（懒迁移旧扁平→盒 + 补齐子键，幂等，挂进 `_get_session_state`）+ 访问器（`get_*/set_*`）
+ `STATE_SCHEMA` 把该盒的散字段换成一个盒字段 + 改它的访问点 + 迁移往返测试。

### 进度与各盒规模

| box | scope | 字段数 | 访问点(约) | 状态 | 主要消费方 |
|---|---|---|---|---|---|
| `clothing` | 短期态 | 3→盒(+nudity) | ~40 | ✅ 已切（见阶段 1c） | service 衣柜逻辑 / generation / webui / character_card |
| `place` | 短期态 | 14 | ~39 | ✅ 已切（见阶段 2） | world_runtime 为主（最集中） |
| `context` | 短期态 | 17 | ~61 | ⬜ 待切 | chat_context / image_planning / scheduler_runtime / commands(回滚/重答) |
| `session` | 全局 | 13 | ~62 | ⬜ 待切 | scheduler_runtime / telegram_io(frozen) / commands / service |
| `character` | **配置** | 28 | **~254** | ⬜ 待切（**最后做**） | 几乎所有模块 + `_get_session_cfg` 动态 `custom_{key}` + character_card + 切角色机器 |

### 建议顺序与理由

1. ~~**`place`（先做）**~~ ✅ 已切（2026-06-24）。字段多但消费几乎全在 `world_runtime.py`，自洽、好测；位置子模型（user/character 各 value/label/text/updated_at/confidence）顺势可收成结构化子对象（阶段 2 位置子对象完成）。
2. **`context`**——hot 路径集中在 `_build_chat_messages`/`format_dialog_context`/`format_sent_photo_context`/续场，量中等。注意 `chat_history` 与 SQLite `chat_messages` 是双真相（病根 ③），切 context 盒时**不要**顺手改去重，去重留给独立的「阶段 3 对话上下文去重」。
3. **`session`**——注意 `saved_characters`/`character_contexts` 是**切角色机器自身的容器**（freeze/restore 读写它们），盒化时这两个键的读写在 commands 切角色链路里要一起改、先加测试再动。
4. **`character`（最后，最重）**——254 个访问点 + 两个深坑：①`_get_session_cfg` 用 `state.get(f"custom_{key}")` **动态键**访问，访问器要支持「按 custom_{key} 读盒」；②character_card 的 `CARD_STRING_FIELDS` 全是 custom_* state 键，切盒时它与卡的 17 个映射要一起走访问器。建议拆成多个小 commit（按消费方分批）。

### 横切规则（每砌一盒都遵守）

- **盒必须 scope 纯净**：一个盒只装一种 scope。**当前唯一不纯**：`life_profile`（短期态缓存）被 `box_for` 归进了 `character` 盒（该盒主要是配置）。切 `character`/`context` 盒时**把 `life_profile` 挪进纯短期态盒（归 context）**，使 `character` 盒 = 纯配置。否则「scope 退化成按盒」不成立。
- **分类器按盒名归类的暗坑**：freeze/clear/snapshot 现在遍历**顶层键**按 scope 分类。字段进盒后顶层键变成**盒名**。clothing 盒名恰好被分类成短期态（对，clothing 是短期态）属侥幸。切 `character` 盒时，盒名 `"character"` 不带 `custom_` 前缀 → 会被误判成短期态（实为配置，应走 saved_characters 卡而非 context 冻结）。**这正是「per-field scope 收敛成 per-box scope」的触发点**：切 character 盒时，把分类器改成认「盒名→scope」（session=全局 / character=配置 / clothing,place,context=短期态），同时 `_conversation_context_payload`/`_clear_transient_state`/`_snapshot_character` 改成按盒操作。
- **`config["xxx"]` 是另一命名空间**：`dynamic_appearance`/`scheduled_persona`/`positive_prefix` 等在 config(全局默认)里同名存在，切盒只动 `state[...]`，**绝不碰 config 那份**（默认角色卡读写它）。
- **老数据零手动迁移**：靠 `ensure_<box>_box` 在 `_get_session_state` 里懒迁移；不写一次性迁移脚本。

### 终局（全部砌完后）

- **scope 从按字段收敛成按盒**：`STATE_SCHEMA` 的 `scope` 字段可退役，只在 5 个盒上各留一个 scope 标签；`STATE_SCHEMA` 本身（默认值 + 盒归属注册表）保留。freeze/clear/snapshot 变成纯按盒：session 盒不动、character 盒走卡、其余盒整盒冻结/解冻。
- **阶段 2（位置子对象）**：随 `place` 盒一并完成。
- **阶段 3（对话去重）**：`context` 盒砌完后，单独做 `chat_history`(prompt 窗口) 与 SQLite `chat_messages`(checkpoint 源) 的去重，让 SQLite 成唯一真相。

## 接手建议

下一位 agent 接手时，先读 `tests/test_core.py` 中和 Prompt、外观、推送、记忆相关的测试，再改实现。这个项目的风险不在单个函数，而在多个模块共同拼出一次聊天或一次生图：聊天模型、画面规划器、短期上下文、长期记忆、世界动线、Prompt 槽位都会同时影响结果。

改 Prompt 或生图流程时，优先增加小而具体的测试，再改实现。每次改完至少运行：

```bash
python -m unittest tests.test_core -v
```

如果只改文档或 WebUI 文案，可以不跑完整测试，但要在最终回复里说明未运行测试的原因。

## 交接记录（2026-06-22，上下文/模型/WebUI 大改造未完成）

本轮按用户的新需求启动了大范围改造，但用户中途要求停止继续实现，改为交接。当前工作区包含较多未提交改动，**尚未完成测试，也尚未完成完整功能闭环**。下一位接手时不要直接认为当前代码已经可发布，应先读 `TODO.md`，再按模块编译/运行最小验证。

### 已经写入但未完全验证的改动

- 新增 `telegram_comfyui_selfie/app_store.py`：SQLite 状态库，包含 `chat_messages`、`checkpoints`、`diaries`、`context_meta`、`web_credentials`、`model_profiles`、`user_model_settings`、`llm_usage`、`session_state`、`city_catalogs`。`state.json` 已弃用，首次启动自动迁移到 SQLite。
- 新增 `telegram_comfyui_selfie/config_store.py`：简易 YAML 分组读写，用于把配置按字段类别拆成 yml。现阶段只支持简单结构，尚未替换完整配置生命周期。
- `defaults.py` 已加入 Telegram 代理、Web 管理员账号密码、上下文/dream 参数、AnimaTool 后端参数、全局模型 profile（来自 `ref/app.py`）等默认项；快速菜单和初始化引导已改为中文高频指令版本。
- Telegram 代理：`service.py` 增加 `telegram_proxy_enabled` / `telegram_proxy_url` 读取，SOCKS 代理依赖 `aiohttp_socks`，HTTP 代理走 aiohttp 请求参数；`telegram_io.py` 已开始透传代理。**requirements 还没补 `aiohttp_socks`，需要接手补齐。**
- LLM profile：`service.py` 增加用户级模型 profile 解析，全局 profile 从 yml/defaults 读，用户覆盖从 SQLite `model_profiles` 读；`/模型` 命令和 WebUI 模型接口已部分加入。用户要求“生图后端/模型只允许 yml 配置，不允许 Web/命令修改”，服务端 Web 保存已经忽略部分生图后端字段，但还需要全面复查前端和命令入口。
- 上下文管理：`chat_context.py` 已加入 SQLite 聊天消息落库、最近 50 句窗口、超过后异步 checkpoint、checkpoint 摘要合并、checkpoint 时记忆提取；当前窗口保留最近 10 句并尽量不从 assistant 半句开始。需要继续验证边界：只保留 10 句是否会切掉 tool 消息、是否需要按 user/assistant 成对裁剪。
- 三层记忆：当前对话窗口、checkpoint、长期记忆的骨架已加入。checkpoint 注入系统提示，长期记忆仍走原 `memory.py`。`memory.py` 新增自动整理保护 manual 记忆的方法，以及 WebUI 显式编辑 manual 记忆的方法。
- dream：`scheduler_runtime.py` 已加入 dream 框架。早安推送前会 `force=True` 等 dream 完成；普通自动推送在两小时无交互且上次 dream 后发生 checkpoint 时触发后台 dream。dream 写日记使用上次 dream 后的新聊天记录，日记日期在早安前归前一天；随后根据最近两天日记、当前窗口和 checkpoint 整理非 manual 长期记忆。仍需测试并补失败保护。
- 角色独立上下文：`commands.py` 开始加入 `character_contexts`，切换角色前保存窗口，切换后恢复对应角色窗口/SQLite checkpoint。此逻辑刚写入，尚未验证完整路径。
- 新命令：已部分加入 `/web密码 <密码>`、`/webui`、`/完整菜单`、`/修改角色 <自然语言>`、`/模型`，以及 `/角色 export/import` 的 JSON 导入导出。命令注册和中文别名需要接手再复查。
- WebUI：`webui.py` 已加入登录鉴权。管理员用 config 的 `web_admin_username` / `web_admin_password`；普通用户用 Telegram 数字 ID + `/web密码` 设置的密码，`/webui` 返回持久 token 链接。普通用户只能访问自己的 session，管理员可访问全局配置/运维接口。已加入记忆、角色、模型 profile 的后端 REST 接口。
- WebUI 前端：`static/index.html` 已加入记忆、角色池、模型配置面板；`static/app.js` 已开始接入对应接口。**前端刷新按钮事件补丁中途停止，可能未完全绑定；JS 未运行验证。**
- AnimaTool 风格生图：`generation.py` 已加入 `image_backend == "animatool"` 分支和 `submit_animatool_turbo()` 草案，使用固定 turbo 模式参数。接口路径和 schema 需要对照真实服务继续确认。

### 新增用户需求（尚未实现）

- ~~新增 bot 管理员指令和 WebUI 管理员按钮：从 Git 自动拉取最新更新，输出 git 更新信息，然后重启自身。~~ **已实现**（2026-06-22）：`git_update.py` GitUpdateMixin + `webui.py` api_admin_git_update + `app.js` 前端按钮。编码修复见 2026-06-23 条目。
- ~~拉 Git 时也使用 Telegram 代理配置。~~ **已实现**：`_git_proxy_env()` 把 `telegram_proxy_url` 转成 `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`。
- ~~该功能必须只允许管理员使用。~~ **已实现**：bot 命令侧 `_is_admin_chat()`（`admin_chat_ids` 优先，回退 `allowed_chat_ids`）；WebUI 侧 `_require_admin()`。

### 当前高风险点

- ~~未跑测试~~ 已跑全量测试：1203 tests OK（1 skipped），与本次改动无关。
- WebUI 前端可能有未绑定按钮或 JS 运行时错误。
- `requirements.txt` 尚未加入 `aiohttp_socks`。
- ~~config.example.json / config.example.yml 尚未更新~~ 已更新。
- ~~state.json 到 SQLite 的迁移只做了新增链路~~ 已完成全量迁移，state.json 已弃用。
- `/初始化` 还只是文本引导，并未实现多轮连续初始化状态机。
- `/修改角色` 已有模型 JSON patch 骨架，但提示词、字段白名单和 diff 展示还需要打磨。
- ~~Git 自动更新 + 自重启功能未开始实现。~~ 已实现。
