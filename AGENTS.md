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
- 类通过 mixin 多重继承组织：`TelegramComfyUIService(ProcessRestartMixin, TelegramIOMixin, CommandHandlersMixin, ChatContextMixin, MemoryPolicyMixin, SchedulerRuntimeMixin, WorldRuntimeMixin, GitUpdateMixin)`
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

### 聊天上下文

聊天 prompt 按变化频率分层，目标是减少互相冗余并提升 DeepSeek/OpenAI 兼容接口的 prefix cache 命中率：

1. 静态 system：身份、人设、关系、工具规则、照片历史规则、固定发图节奏规则。
2. 天级/低频稳定层：低频对话控制、角色历史提要、按重要性选取的长期记忆。
3. 半稳定状态快照：当前可见外型、衣橱、当前附加外貌、低频世界模板（天气/光线阶段/角色身份/日常动线背景/场景约束）。
4. checkpoint 会话连续性：近期已折叠对话摘要。
5. 未折叠历史：checkpoint 之后的真实 `user/assistant/system` 历史。
6. 动态尾部：精确当前时间、本轮用户位置/空间关系判断、overdue 发图提醒、场景断档提醒、当前用户输入。

关键约束：

- `_chat_prompt_history(state)` 使用 checkpoint 之后的全量未折叠历史，checkpoint 之间只追加不滑动。
- checkpoint 裁剪后第一条必须是 `user`；多余的孤立 `assistant` / `system` 会进入 checkpoint 摘要。
- dream 和 dream 记忆整理只读取实际 `user/assistant` 对话，不消费照片历史 system。
- 照片历史是真正的历史 `system` 消息，保留到被正常历史裁剪为止，并参与 checkpoint 摘要。
- 半稳定层变化时，如果未折叠历史达到 `context_window_message_limit / 2`，会异步强制 checkpoint 一次，近似恢复后续前缀稳定。
- 天气、光线阶段、城市、角色身份和动线背景已从每轮 dynamic system 拆入 semistable；本轮用户位置仍保留在 dynamic tail，避免把用户当前位置缓存成旧状态。
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
- 核心槽位顺序：`quality -> count -> identity -> style_artist -> effective_appearance -> style_general -> scene -> one_shot_appearance`。
- `scene` 只描述镜头、地点、动作、光线、道具和氛围，不重复稳定外貌。
- `one_shot_appearance` 是本轮临时补充，不持久化。
- `clothing_off` 对衣物/裸体默认仍是“仅本图生效”；但当它明确命中当前已穿戴的可持久配饰（如眼镜、项链、耳环、发夹）时，生图成功后会把该配饰从当前穿搭中移除，避免下一张图被稳定外貌重新加回去。
- OC 不把中文名、昵称或作品名塞进视觉 identity；只有已知公开角色才注入角色/作品 tag。
- 亲密场景默认走 POV，只允许用户/伴侣身体局部入画；除非用户明确要求拍照、录像或对镜，才允许设备入画。
- `/配图`（同义词 `/画图`、`/绘图` 等）按当前聊天场景生成图片，不强制自拍或看镜头；命令后的参数作为最高优先级场景/视角/机位/远近/局部特写要求，但规划器只消费瘦身后的短期连续性、最近已发图片摘要、世界状态和记忆，不再直接吞完整聊天流水。
- 画幅只允许 2:3（竖版）和 3:2（横版），模拟真实相机画幅；负向提示词包含 `split screen, grid, multiple panels, collage` 防止四宫格/分格出图。
- AnimaTool Turbo 路径不再提交 `neg` / `negative` 字段；自然语言 `nltag` / `tags` 尾部统一追加 `no text, no logo, no ui, no mosaic, uncensored`。

### Telegram 输入增强

- Telegram 当前图片、`reply_to_message` 图片、`external_reply` 图片只进入视觉模型描述任务。
- 视觉模型可参考最近两轮实际 `user/assistant` 对话和当前文字/引用线索。
- chat 模型最终只收到纯文本：引用内容、图片描述、用户当前输入。
- 引用文本支持 `quote.text`、`reply_to_message.text/caption`、`external_reply.text/caption`。
- Telegram 文件下载走 `getFile`，遵守 Bot API 20MB 文件下载限制。

### WebUI 与运维

- WebUI 支持管理员与普通用户登录；普通用户只看自己的会话与私有模型。
- 管理员可以查看用量、维护全局模型 profile、执行 Git 更新、重启服务、冻结不活跃用户。
- 基础设施/运维配置如 Web host/port、日志路径、数据库路径仍只允许 YAML 修改，不通过通用 Web 配置表单写入。
- WebUI 角色面板不展示场景偏好/自拍偏好栏；这些字段保留为内部数据和兼容字段。
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

## 今日变更（2026-06-27）

1. **checkpoint 时间节点软约束**：`_summarize_checkpoint()` 追加提示词规则，要求保留用户明确提到的日期、几点、期限、倒计时、约定时间和相对时间节点；`_extract_long_term_memories()` 在 checkpoint 来源下允许把跨场景仍有影响的时间节点保存为 `event`，不新增独立管线。
2. **dream 过时时间节点清理策略**：`_incremental_organize_memories()` 与 `_summarize_all_memories()` 的整理 prompt 约束为“过时不等于立刻删除”；只有事件已解决、取消、被替代，或从近期日记/checkpoint/当前窗口完全淡出时，才更新、合并或删除相关时间节点记忆。
3. **新场景前置 checkpoint**：`/新场景` 调用 `_checkpoint_current_context_before_reset()`，先处理切换前未折叠上下文与记忆提取，再清空当前 `chat_history`、checkpoint 摘要和短期场景状态；原始 SQLite `chat_messages` 仍保留给 dream。
4. **AnimaTool Turbo 去 neg**：`plan_animatool_slots()`、`_build_animatool_turbo_payload()` 和 `_post_animatool()` 均会剔除 `neg` / `negative` 字段，并把 `no text, no logo, no ui, no mosaic, uncensored` 追加到自然语言 `nltag` / `tags` 尾部；同时兼容 `nltag`、`nl_tag`、`nl_tags`、`tags` 字段名。
5. **回归测试**：新增/更新测试覆盖新场景前置 checkpoint、checkpoint 时间节点提示词、dream 时间节点淡出清理提示词、AnimaTool nltag 尾部与去 neg 行为。

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

- `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3 -m compileall -q telegram_comfyui_selfie`
- `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3 -m unittest tests.test_core -v`
- `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3 -m py_compile scripts\compare_llm_chat_prompts.py`
- `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3 scripts\compare_llm_chat_prompts.py --log "data\logs\llm_debug.json" --output .tmp\llm_chat_prompt_compare_current.md`
- 最新结果：`Ran 267 tests in 6.991s`，`OK (skipped=1)`；默认跳过真实前缀缓存请求测试
- 工具 schema 当前紧凑 JSON 长度：`1898` 字符；chat 请求体 key 顺序为 `model, max_tokens, temperature, tools, tool_choice, messages`
- 当前 `data/logs/llm_debug.json` 复跑 prompt 比对：报告生成 `.tmp\llm_chat_prompt_compare_current.md`，`entries=10 sessions=2 pairs=8`；会话 pair 的 `tools` / `tool_choice` / 非 prompt 请求参数均稳定，变化集中在 `messages`。
- 真实 API 缓存探针：拆分后 entry 3/4/5 改写请求首轮为冷缓存，第二轮分别命中 `7040/7099`、`7168/7198`、`7552/7562`。
- 额外真实 API 缓存探针 `test_live_chat_context_cache_probe_uses_current_config_when_available`：默认跳过；设置 `SUCYUBOT_TEST_LIVE_CACHE_PROBE=1` 后才使用当前配置文件中的模型连接信息，通过真实 `handle_chat()` 链路连续回答三轮预设问题并输出缓存命中率。运行态 state / SQLite / 用户日志均隔离在测试临时目录；模型临时未返回可用回复时跳过。
- `git diff --check` 通过；Windows 下仅可能出现 LF/CRLF 提示

## 已知限制

- `selfie` 取景与互动动作有时存在轻度矛盾；当前依靠视角和负面提示词约束，尚未拆出完整 `pose/action/forbidden` 结构化字段。
- 会话状态处于"盒子 + 部分旧扁平键双写"的兼容期；后续彻底删除扁平键前必须先清点所有访问点和迁移测试。

## 下一阶段目标

1. 继续场景结构化：在已有 `location` 基础上拆出 `props`、`forbidden`、`pose/action/light`，减少手机、镜子、多手、互斥姿态等问题。
2. 收敛会话状态双写：逐步移除旧扁平键读写，只保留 `session_schema` 访问器和盒子结构。
3. 对视觉模型输入链路做真实 Telegram 文件回放测试，确认不同客户端的 `quote` / `external_reply` / caption 组合都能按预期注入。
4. 在 WebUI 模型面板增加更友好的 profile 编辑器，减少直接编辑 JSON 的误操作。
