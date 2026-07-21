# AGENTS.md — SucyuBot_neo

## 项目概述

SucyuBot_neo 是独立运行的 Telegram AI 角色扮演与动漫角色生图服务，包含 Telegram Bot、WebUI、角色卡、长期记忆、短期上下文、生活线、地点动线、天气时间以及 ComfyUI / AnimaTool 生图规划。

## 技术栈

- Python 3.11+，异步 I/O 以 `aiohttp` 为主。
- SQLite 保存应用状态、聊天、记忆、角色生活线、模型配置和 Web 凭据。
- 配置优先读取 `data/config.yml`，不存在时回退 `data/config.json`。
- WebUI 使用 aiohttp SPA + Vanilla HTML/CSS/JS。

## 常用命令

```powershell
pip install -r requirements.txt
py -3 -m telegram_comfyui_selfie --config data/config.yml
py -3 -m unittest tests.test_core -q
py -3 -m compileall -q telegram_comfyui_selfie tests
node --check telegram_comfyui_selfie\static\app.js
```

也可运行 `run.cmd`。本机不要依赖裸 `python`，统一使用 `py -3`，避免命中 Windows Store 占位程序。

真实前缀缓存探测默认跳过；需要时设置 `SUCYUBOT_TEST_LIVE_CACHE_PROBE=1` 后单独运行对应测试。

## 目录职责

```text
telegram_comfyui_selfie/
├── service.py               # 服务初始化与 mixin 组合
├── llm_runtime.py           # 模型 profile、LLM HTTP 调用、用量与调试日志
├── state_runtime.py         # 配置、状态迁移、会话访问与活动日志
├── task_runtime.py          # 后台任务 registry、作用域取消、停机排空与失败退避
├── deletion_runtime.py      # 角色/会话统一删除事务、文件隔离回滚与缓存清理
├── character_artifacts.py   # 角色头像等文件路径的安全单一来源
├── appearance_runtime.py    # 画风、稳定外观、衣柜状态与换装工具
├── defaults.py              # 默认配置
├── commands.py              # Telegram 命令处理
├── command_aliases.py       # 命令及别名的单一来源
├── chat_context.py          # 聊天上下文、checkpoint、工具调用
├── generation.py            # PromptSlots 与生图后端
├── image_planning.py        # LLM 画面规划
├── appearance.py            # 外观、衣柜与标签处理
├── prompt_intake.py         # 自然语言外观/角色输入分类
├── memory.py                # 长期记忆 SQLite 存储
├── memory_policy.py         # 记忆提取与整理策略
├── scheduler_runtime.py     # 推送、dream、续场
├── world_runtime.py         # 地点、天气、城市 POI
├── telegram_io.py           # Telegram 收发与图片输入
├── webui.py                 # WebUI 与 REST API
├── app_store.py             # 应用状态数据库
├── session_schema.py        # 会话状态 schema 与访问器
├── character_card.py        # 角色卡字段单一来源
├── character_checkpoint.py  # 角色检查点导入导出
└── static/                  # Web 前端
```

## 开发约定

- 使用 `from __future__ import annotations`。
- Mixin 不定义 `__init__`；初始化集中在 `TelegramComfyUIService.__init__`。
- I/O 路径使用 `async def`，不要在事件循环中执行阻塞网络或磁盘操作。
- 全局配置使用 `self.config.get(key, default)`；会话覆盖使用 `_get_session_cfg(session_id, key, default)`。
- 日志统一使用模块级 `logger = logging.getLogger(__name__)`。
- 注释与 docstring 使用中文，代码标识符使用英文。
- 新行为必须补回归测试；测试采用 `unittest.TestCase`、`AsyncMock` 与测试方法内 `asyncio.run()`。
- 运行时文件 `TODO.md` 不提交。
- 更新本文件时只记录长期有效的架构、约束和命令，不维护提交记录、日期化变更流水或具体测试次数。

## 配置、存储与模型

- `state.json` 已弃用；旧数据仅在 SQLite 为空时迁移并备份。
- `app_store.py` 管理 session、城市目录、聊天、checkpoint、日记、上下文元数据、生活线、Web 凭据、模型 profile 和用量。
- `session_schema.py` 是会话字段单一来源。当前仍处于盒子结构与少量旧扁平键双写的兼容期，删除兼容键前必须清点所有读写点并补迁移测试。
- 模型配置统一走全局/用户 profile；chat、fast、vision 分别选择 profile。视觉 profile 留空时跳过图片理解。
- thinking 状态由 profile 决定。API 密钥对前端始终掩码，保存空值或 `********` 时保留旧值。
- 聊天采样参数只用于真实聊天回复，不传给 checkpoint、dream、memory 等结构化任务。
- 结构化 LLM JSON 只可对明确位于相邻 token 之间的漏逗号做保守修复；其他损坏必须保持失败并走既有重试/回退。

## 聊天上下文

- 默认第一条 system 是跨角色稳定规则，第二条才是角色身份与人格，以提高跨角色前缀复用；`chat_persona_first=true` 可恢复人格优先的兼容顺序。
- 上下文按稳定度分层：全局规则、角色人格、低频记忆/历史、外观与世界半稳定槽、checkpoint、未折叠历史、精确时间与本轮输入动态尾部。
- 天气半稳定槽使用天气描述与温度区间；精确温度只放动态尾部，避免小幅温度变化破坏稳定前缀。
- `_chat_prompt_history()` 使用 checkpoint 之后的全量历史；裁剪后第一条必须为 `user`。
- checkpoint 按最旧完整轮次和实际字符预算正序分页；超长单轮分块全部成功后才推进其消息 ID。所有入口共享 `session_id + character` 锁，SQLite 提交使用版本 CAS 且边界只允许单调前进。
- 照片历史是精简后的历史 `system` 消息。dream 与长期记忆提取只消费真实 `user/assistant` 对话。
- `/新场景`、`/上下文重置` 先提炼旧窗口，再清空模型侧上下文；SQLite 原始聊天保留给 dream。切场景同时清理裸体与衣物部件状态。
- OpenAI 兼容端点返回在文本中的 DSML 工具调用时，要转换为内部工具调用并清理原始标记。

## 记忆与角色

- 长期记忆按 `session_id + character` 隔离；保存前先截断再按标准化文本去重。
- checkpoint 负责近期连续性，角色历史负责宏观关系阶段，长期记忆负责稳定事实、偏好、边界、纠正和未完事件，不要互相重复承担职责。
- 手动记忆不可被自动整理删除。自动提取可通过配置关闭，但角色历史总结与 dream 仍可独立工作。
- 增量整理允许只调整重要性；只有记忆显著超限才执行全量重写。重写失败时不得先删除旧记忆。
- `character_card.py` 是角色卡字段单一来源。角色切换必须保存并恢复该角色的上下文、衣柜、地点和照片历史。
- Telegram 与 Web 删除角色必须共用 `delete_character()`；彻底删除会话必须共用 `delete_session()`，先停稳作用域任务，再以单事务清库并清理检查点、头像和缓存。Web“隐藏”不得删除业务数据，彻底删除必须校验完整 session_id 二次确认。
- dream、checkpoint、记忆与角色历史必须保持视角：User 是人类用户，Assistant/第一人称“我”是 bot 角色。

## 外观、衣柜与纯良度

- 衣柜异步分类返回后必须基于最新状态合并，不能用 await 前的旧快照覆盖并发修改。
- 结构化工具同一次调用中可先换装再设置新衣物的部件状态；存在性校验应在换装合并后执行。
- 衣物部件状态只渲染一次。读取/序列化衣柜不得顺带修改状态。
- 明确摘掉并持续不戴的配饰属于持久外观变化；单图 `clothing_off` 命中当前配饰时，生图成功后也要做持久化兜底。
- 衣柜 reset、WebUI 清除部件状态、新场景切换都必须同时清理 nudity 状态。
- `purity > 2` 时，公开场合对裸体、乳头/下体暴露及明显私密穿着执行护栏。
- **`purity <= 2` 时，不对公开场合下的任何暴露情况施加限制。** 该规则优先于世界上下文中的“得体/克制”建议，确定性 prompt 清洗与 LLM planner 都必须放行用户要求。

## 生图与 PromptSlots

- `PromptSlots` 是最终正向提示词来源，顺序为 `quality -> count -> identity -> style_artist -> effective_appearance -> style_general -> safety -> scene -> one_shot_appearance`。
- `scene` 只描述镜头、地点、动作、光线、道具和氛围，不重复稳定外貌与穿搭。
- `view=selfie` 是前摄自拍但画面不得出现手机本体/UI；`portrait` 是画外人拍摄且画面只有角色；只有 `mirror` 可同时出现镜子和手机。
- `/配图` 是自由配图，用户参数对视角、机位、距离和局部特写具有最高优先级，不套用自拍规则。
- 异地且无伴侣入画时，非用户显式要求的 POV 必须降级为第三人称；同处状态读取必须遵守 TTL。
- 日常局部同框与性爱伴侣场景分开处理。性爱场景保留 `your <body>` 归属，并在明确提及时补充相应视觉 tag。
- 场景衣物冲突采用精确删除，不生成 `the current outfit` 等不可渲染占位语，也不能误删人物动作。
- 画幅只允许 2:3 或 3:2；负向提示词压制 split screen、grid、multiple panels、collage。
- AnimaTool 工作流与 schema 由 `ANIMATOOL_WORKFLOWS` 管理。quality、neg、count 必须按实时 schema 构造，不能直接复制项目内部槽位全文。

## 调度与世界状态

- dream 的每日执行独立于推送开关和推送次数限制；到角色起床时间后可单独运行。
- dream 从最旧未处理消息开始按完整轮次和字符预算分页，日记、记忆链全部成功后只推进本页真实消息边界，剩余积压留给下一次继续。
- 业务后台协程统一通过 `_spawn_background()` 登记作用域与停机策略；完成回调必须消费异常并清理兼容 task map，停机在关闭 HTTP 前取消或排空。
- 同一会话的推送使用 `asyncio.Lock` 串行化，避免 morning/daily/continuity 并发重复发图。
- 多阶段 NTR/连续推送按阶段顺序 await；单阶段失败要隔离并记录，不阻塞后续调度循环。
- 场景结束和晚安判断只读取近期用户消息，不让 assistant 台词或照片 system 误触发。
- normal 推送先判断是否承接用户；不承接时从生活线与已有网络话题池中混选 1-3 条具体引导。当天第一次选择不承接后，必须先完成本次推送，再按角色兴趣搜索并补充角色维度的当日网络话题池；跨日整理可保留至多少量仍有时效性的旧话题。followup 默认承接用户，不调方向 LLM。
- 网络话题扩展的兴趣点、query 和整理结果都必须避开上一轮搜索、旧话题池与最近实际推送；同义改写按重复处理，最近已用条目不得作为历史话题保留。
- 聊天与推送侧 Tavily 搜索统一使用 `search_depth=basic`、`max_results=10`、`include_answer=advanced`；模型必须按用途显式选择 `general/news/finance` topic。
- 推送话题日志 `recent_push_topics` 跨 `/新场景` 保留（`reset_preserved=True`），切角色才清；专门堵 `/新场景` 后 `sent_photos_history` 被 `since=reset_time` 过滤导致避重失效的缺口。每条记录 ts/caption/scene/topic 签名/direction，保留最近 8 条；`_pushes_since_last_user_message` 据此统计用户上次发言后的推送间隔，间隔超过 1-2 次后 dialogue 方向应大幅减少。
- 推送 caption 优先展现角色自己的生活片段、看到想到的事或感兴趣的话题，避免写成对用户的询问式开场或催促回复；冷启动（用户长时间无互动）时非 dialogue 方向强制不带问句主旨。
- 短英文关键词使用单词边界匹配，避免 `bed` 命中 `bedroom` 等子串。
- 天气缓存必须绑定城市；城市变化不能复用旧城市数据。外部天气请求复用统一代理配置。
- 地点匹配优先识别路线、街道等动线提示，再匹配普通地点标签。

## Telegram 与 WebUI

- Telegram 图片、引用图片先由 vision 模型转换为文本描述；chat 模型只接收文本。未配置 vision profile 时跳过图片理解。
- Telegram update 必须先与确认 offset 一起写入 SQLite inbox，再进入按会话有序的有界 worker；跨会话受全局并发上限控制，停机先停止拉取并排空，超时待办由下次启动恢复。
- 同会话新消息可取消旧文字生成，但已进入生图/发图阶段的受保护任务不能被取消。
- Web API 错误优先返回 JSON；前端也必须兼容非 JSON 错误体、401 跳转与可读错误摘要。
- 普通用户不可查看系统日志项或其他用户的数据；管理员才能维护全局模型和运维配置。
- WebUI 命令下拉从 `/api/commands` 动态读取 `COMMAND_ALIAS_GROUPS`，避免前后端各维护一份命令列表。
- 数值配置在前后端都校验为有限数；移动端输入字号至少 16px，主要触控目标至少 40px。

## 验证要求

功能完成后按改动范围执行：

```powershell
py -3 -m unittest tests.test_core -q
py -3 -m compileall -q telegram_comfyui_selfie tests
node --check telegram_comfyui_selfie\static\app.js
py -3 -m json.tool config.example.json > $null
git diff --check
```

测试日志中的预期 mock 异常不等于失败，以进程退出码和 unittest 最终状态为准。不要把某一次验证的测试数量、耗时或日期写回本文件。

## 已知兼容边界

- 会话状态仍有盒子字段与旧扁平键双写，迁移未完成前不要直接删除兼容字段。
- 自拍取景与复杂互动姿态仍依赖 planner 与规则终裁共同约束；继续拆分结构化 pose/action/forbidden 时必须保留旧输入兼容。
