# AGENTS.md — SucyuBot_neo

## 项目概述

SucyuBot_neo 是独立运行的 Telegram AI 角色扮演与动漫角色生图服务，包含 Telegram Bot、WebUI、角色卡、长期记忆、短期上下文、生活线、地点动线、天气时间以及 ComfyUI / AnimaFlow 生图规划。

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
├── model_thinking.py        # thinking 旧布尔值与 reasoning effort 的单字段规范化
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
├── animaflow_runtime.py     # AnimaFlow 工作流发现、schema/knowledge 与默认参数
├── appearance.py            # 外观、衣柜与标签处理
├── prompt_intake.py         # 自然语言外观/角色输入分类
├── memory.py                # 长期记忆 SQLite 存储
├── memory_policy.py         # 记忆提取与整理策略
├── scheduler_runtime.py     # 推送、dream、续场
├── world_runtime.py         # 地点、天气、城市 POI
├── encounter_runtime.py     # 跨会话角色邂逅编排器（一期：同世界观互访）
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
- thinking 可三处配置，优先级：用户级（`chat_thinking`/`fast_thinking`/`vision_thinking`，WebUI 全局/角色卡页面可改）> 全局配置默认（`chat_thinking_enabled`/`fast_thinking_enabled`/`vision_thinking_enabled`，配置文件可改）> profile 的 `disable_thinking`。同一字段兼容空值、旧 `true/false` 开关和 `none/minimal/low/medium/high/xhigh/max` effort；选择 effort 时下发 `reasoning_effort`，结构化任务显式关闭 thinking 时同时清除 effort。`thinking_fixed` 仅是 profile 默认标记，不再硬锁定用户设置。API 密钥对前端始终掩码，保存空值或 `********` 时保留旧值。
- `llm_sampling_params_enabled` 是模型采样细节总开关；关闭时所有 LLM 请求省略 temperature、top_p、frequency_penalty 和 presence_penalty，并保留配置值供再次开启，max_tokens、thinking 和工具参数不受影响。WebUI 根据该开关折叠或展开采样参数详情。
- OpenAI-compatible profile 的 `base_url` 同时接受 API Base 和完整 `/chat/completions` URL，运行时统一规范化。服务启动时仅对带密钥的全局端点并发拉取一次 `/models` 目录并保存在内存中，失败不阻止启动；目录只向管理员 WebUI 暴露，供全局 profile 选型与复用同端点密钥。
- 思考型模型仅在 content 里输出带 `<thinking>`/`<reasoning>`/`<analysis>` 标签的草稿时，`_call_llm` 才剥离或判为思考泄漏；不做关键词启发式判断。
- 聊天采样参数只用于真实聊天回复，不传给 checkpoint、dream、memory 等结构化任务。
- 结构化 LLM JSON 只可对明确位于相邻 token 之间的漏逗号做保守修复；其他损坏必须保持失败并走既有重试/回退。

## 聊天上下文

- 默认第一条 system 是跨角色稳定规则，第二条才是角色身份与人格，以提高跨角色前缀复用；`chat_persona_first=true` 可恢复人格优先的兼容顺序。
- 上下文按稳定度分层：全局规则、角色人格、低频记忆/历史、外观与世界半稳定槽、checkpoint、未折叠历史、精确时间与本轮输入动态尾部。
- 天气半稳定槽使用天气描述与温度区间；精确温度只放动态尾部，避免小幅温度变化破坏稳定前缀。
- 稳定前缀（system_static、stable_front）必须不含会话级插值（用户性别、空间关系、intimate 预判等），这些值统一放动态尾部；location-extract 等 prompt 的地点枚举从 `PLACE_TYPES` 运行时推导，不得硬编码两遍。
- chat system_static 只保留「何时必须调用工具」与行为约束；工具参数机制以 tools schema 为单一来源，避免双份描述。
- checkpoint 摘要 system/user 模板为模块级常量，chat/image 两分支共用同一文本仅 purpose 不同。
- 短期注意规则（切场景后提醒模型不要续旧上下文）在 checkpoint 落库后自动清除，不在稳定前缀永久驻留。
- `_chat_prompt_history()` 使用 checkpoint 之后的全量历史；裁剪后第一条必须为 `user`。
- checkpoint 按最旧完整轮次和实际字符预算正序分页；超长单轮分块全部成功后才推进其消息 ID。所有入口共享 `session_id + character` 锁，SQLite 提交使用版本 CAS 且边界只允许单调前进。
- 照片历史是精简后的历史 `system` 消息。dream 与长期记忆提取只消费真实 `user/assistant` 对话。
- `/新场景`、`/上下文重置` 先提炼旧窗口，再清空模型侧上下文；SQLite 原始聊天保留给 dream。切场景同时清理裸体与衣物部件状态。
- OpenAI 兼容端点返回在文本中的 DSML 工具调用时，要转换为内部工具调用并清理原始标记。

## 记忆与角色

- 长期记忆按 `session_id + character` 隔离；保存前先截断再按标准化文本去重。
- checkpoint 负责近期连续性，角色历史负责宏观关系阶段，长期记忆负责稳定事实、偏好、边界、纠正和未完事件，不要互相重复承担职责。
- 角色历史提要以软目标字数引导精炼，排除流水账、重复事实和不改变长期走向的细节，但不得为满足目标而牺牲关系阶段、未解事件、心理边界与后续演绎方向。另设宽松硬字符上限作为模型失控兜底；超限先进行价值压缩，最终机械截断必须兼顾开头关系背景和末尾扮演提示，并优先落在自然文本边界。
- 手动记忆不可被自动整理删除。自动提取可通过配置关闭，但角色历史总结与 dream 仍可独立工作。
- 增量整理允许只调整重要性；只有记忆显著超限才执行全量重写。重写失败时不得先删除旧记忆。
- dream 后整理按事件驱动：上次整理完成时的记忆最大 `updated_at` 记录在 `context_meta.last_memory_organize_watermark`，之后零写入则跳过；首次运行（无水位）不跳过。重要性 1-5 打分锚点在提取/增量整理/全量重写 prompt 中保持同一口径。
- `last_used_at` 只在记忆注入 prompt 时由 `touch_memories` 刷新，不改 `updated_at` 也不失效记忆读缓存；`list_memories` 同重要性时按 `COALESCE(last_used_at, updated_at)` 排序。
- `character_card.py` 是角色卡字段单一来源。角色切换必须保存并恢复该角色的上下文、衣柜、地点和照片历史。
- `/角色 clearup` 级联清空长期记忆、日记、检查点和检查点目录；删除角色统一走 `delete_character()`。WebUI 会话隐藏/彻底删除入口已废弃，不再为其保留前端静态回归测试；遗留会话清理代码如被内部调用，仍须通过 `delete_session()` 停稳作用域任务，再以单事务清库并清理检查点、头像和缓存。
- checkpoint、context_meta、长期记忆查询有会话级读缓存；写入操作（upsert_checkpoint、add_memory 等）失效对应缓存键。
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
- AnimaFlow 请求只发送规划器选出的 `aspect_ratio`，不发送 `width`/`height`；实际尺寸由 AnimaFlow 按服务端 `target_megapixels`（默认约 1MP）换算并对齐。WebUI 的宽高只属于原生 ComfyUI 后端，启用 AnimaFlow 时折叠。
- 任何场景都不自动追加性/裸露类反词（`no panties`、`bottomless`、`nsfw`、`nude` 等）；只有「公开场景且 purity>2」的护栏路径按最精简集追加防走光反词（`nude, topless, bottomless`，各表一种裸露程度、无同义重复），其余场景完全靠正向提示控制。
- AnimaFlow 源码不随 Bot 仓库分发；运行时以 `/anima/workflows` 为工作流单一来源，再按目录动态加载选中工作流的 schema、knowledge 与 generate 端点，禁止维护本地工作流清单。管理员开关默认关闭；每次开启都重新发现目录，切换工作流时用其动态默认值重置 cfg/steps。发现目录或工作流资源失败时，只通过外部 HTTP 接口回退到旧 `turbo_v1` 协议，不重新内置插件源码。cfg=1 时不构造任何负面字段，仅在 nsfw/explicit 的正面 tags 末尾追加 `no mosaic, uncensored`。

## 调度与世界状态

- dream 的每日执行独立于推送开关和推送次数限制；到角色起床时间后可单独运行。NTR/纯洁度自动覆盖（purity<0、超阈值、purity==0 随机）只作用于未显式指定 morning 的推送；显式 morning 保持 morning 语义（含 dream）。NTR 推送在今日尚未 dream 时也会在正文生成前补跑一次 dream（`_dream_done_today` 按 context_meta.last_dream_at 去重），保证非激活角色（无 scheduler daily-wake 兜底）的日记/角色背景不因被覆盖而停更。
- dream 在起床时间整理并归档前一天日记；每日推送次数是包含固定早安与固定晚安在内的总配额，1 次时仅发早安。晚间作息按下一自然日是否为休息日选择睡觉时间，固定晚安复用普通/NTR 推送并只注入临时晚安引导。
- dream 从最旧未处理消息开始按完整轮次和字符预算分页，日记、记忆链全部成功后只推进本页真实消息边界，剩余积压留给下一次继续。
- 业务后台协程统一通过 `_spawn_background()` 登记作用域与停机策略；完成回调必须消费异常并清理兼容 task map，停机在关闭 HTTP 前取消或排空。
- 同一会话的推送使用 `asyncio.Lock` 串行化，避免 morning/daily/continuity 并发重复发图。
- WebUI 手动测试推送使用 fail-fast：生活线或后续推送链路遇到上游异常时立即返回 JSON 错误并恢复临时角色上下文；后台定时推送仍按原有容错与模型回退策略运行。
- 普通定时推送只有在从未开始且确实错过窗口时才记 `missed-window`；已实际尝试失败的时间点使用指数退避跨窗口重试，成功后才扣除该时间点。dream 的角色历史提要失败必须让整页 dream 失败并保留游标，由 dream 退避机制重试。
- 多阶段 NTR/连续推送按阶段顺序 await；单阶段失败要隔离并记录，不阻塞后续调度循环。
- 同一会话角色互动推送默认关闭（每日上限 0）；用户在动线页选择参与角色并设置每日上限。它只作为普通每日推送的话题方向，当前活动角色必须在参与列表中，目标从已选择且清醒的非活动角色中随机抽取。
- 同会话角色互动抽中目标后，必须先按目标角色键建立其当日生活线/动线，禁止通过临时切换角色借用 live 上下文。编排结果在图片成功发送后才扣每日额度，并分别写入双方冻结/活动历史、SQLite 消息、长期记忆、生活线事件/NPC 与 encounters 关系史；生图仍保持单活动角色入画，另一角色位于画外。
- 场景结束和晚安判断只读取近期用户消息，不让 assistant 台词或照片 system 误触发。
- normal 推送先判断是否承接用户；不承接时从生活线与已有网络话题池中混选 1-3 条具体引导。当天第一次选择不承接后，必须先完成本次推送，再按角色兴趣搜索并补充角色维度的当日网络话题池；跨日整理可保留至多少量仍有时效性的旧话题。followup 默认承接用户，不调方向 LLM。
- 网络话题扩展的兴趣点、query 和整理结果都必须避开上一轮搜索、旧话题池与最近实际推送；同义改写按重复处理，最近已用条目不得作为历史话题保留。
- 网络话题列表整理属于结构化任务，必须关闭 thinking 并约束短 guide；首次拿到响应但 JSON 语法/根结构无效时使用独立 tag 低温重生成一次，请求失败不额外重试，第二次仍失败才从搜索摘要确定性兜底。不得为此放宽全局 JSON 保守修复边界。
- 聊天与推送侧 Tavily 搜索统一使用 `search_depth=basic`、`max_results=10`、`include_answer=advanced`；模型必须按用途显式选择 `general/news/finance` topic。
- 推送话题日志 `recent_push_topics` 跨 `/新场景` 保留（`reset_preserved=True`），切角色才清；专门堵 `/新场景` 后 `sent_photos_history` 被 `since=reset_time` 过滤导致避重失效的缺口。每条记录 ts/caption/scene/topic 签名/direction，保留最近 8 条；`_pushes_since_last_user_message` 据此统计用户上次发言后的推送间隔，间隔超过 1-2 次后 dialogue 方向应大幅减少。
- 推送 caption 优先展现角色自己的生活片段、看到想到的事或感兴趣的话题，避免写成对用户的询问式开场或催促回复；冷启动（用户长时间无互动）时非 dialogue 方向强制不带问句主旨。
- 主动推送 caption 必须以单段单行发送；即使模型返回多行，也要在发送前归一化为空格分隔的单行。
- 短英文关键词使用单词边界匹配，避免 `bed` 命中 `bedroom` 等子串。
- 天气缓存必须绑定城市；城市变化不能复用旧城市数据。外部天气请求复用统一代理配置。
- 地点匹配优先识别路线、街道等动线提示，再匹配普通地点标签。
- 用户位置（user_place）是会话级字段，不与角色地点一起随角色切换冻结/恢复；同处状态读取必须遵守 TTL。checkpoint 地点推断有内存缓存，切角色/更新位置时失效。

## 跨会话角色邂逅（一期）

- `cross_world_enabled`、`cross_world_pairs`、跨会话冷却与触发倾向只控制跨会话邂逅，不影响用户在动线页配置的同会话角色互动。配对即声明同世界观并授权编排，一期无逐次 consent。配对角色须为两侧会话当前活动角色，否则跳过。pairs 在配置文件里为对象列表，WebUI 设置页为文本格式（每行 `chat_id:角色名 = chat_id:角色名`），`_cross_world_pairs` 统一归一化。
- 邂逅由 `encounter_runtime.py` 编排：访客 A 当天旅行到地主 B 城（place box 的 `travel_override` 只覆盖 `_session_city` 读取层，不污染 `custom_location`；到期由 dream 结算 `_settle_travel_override` 清除），一次 LLM 调用编排整个场景。
- 触发决策与编排调用优先用对应会话的 fast profile、未配置时回退 image profile；编排用量记地主侧（`session_id=地主`）。JSON 走 `_parse_llm_json` 保守修复，summary/pov_a/pov_b 缺一即整体中止，不写半成品。
- 持双方 `character_operation_lock`（按 session_id 字典序取锁防死锁）内原子落库：encounters 表（关系史，供下次编排承接重逢）、双方各一条 system 历史事件（`_append_encounter_system_message`，措辞允许自然承接且不自我降权）、记忆建议过 `_is_long_memory_in_scope` 后落 kind=event/source=encounter:<id>、life_plan today.events + npcs[]。双人记忆互不共享。
- 一期不做：双人同框生图、实时接力对话、互发消息、专门的用户通知推送——角色在后续对话/推送中自然提起。调度入口在 scheduler_loop 每轮 `_maybe_schedule_encounters`：先检查冷却（encounters 表 max ts）与双方空闲/清醒，再由 LLM 结合双方人设、城市/地点、今日生活线、既往关系和低/中/高软倾向判断是否触发；不再使用随机概率门，否决与异常判定需节流，单对失败隔离不影响调度循环。

## Telegram 与 WebUI

- Telegram 用户图片、相册及显式回复/外部引用中的图片，在 chat 与 vision 解析到同一 `api_base + model` 时直接作为原生多模态 user content 进入 chat；两者不同时仍先由 vision 转为文本描述。bot 已发送图片继续只以照片历史文字摘要进入上下文；未配置 vision profile 时跳过图片理解。辅助模型复用消息时，若其与 vision 不是同一实际模型，必须在统一 LLM 请求出口移除图片 part、保留文字内容。
- Telegram update 必须先与确认 offset 一起写入 SQLite inbox，再进入按会话有序的有界 worker；跨会话受全局并发上限控制，停机先停止拉取并排空，超时待办由下次启动恢复。
- 同会话新消息可取消旧文字生成，但已进入生图/发图阶段的受保护任务不能被取消。
- Web API 错误优先返回 JSON；前端也必须兼容非 JSON 错误体、401 跳转与可读错误摘要。
- 日志分为 INFO 与 DEBUG：INFO 沿用 `data/logs/telegram_<chat_id>.log`、`errors.log` 及既有分片命名，长期保留完整用户输入、实际发送给用户的 Bot 文本/图片说明、业务行为、行动逻辑和判断依据；既有历史文件不迁移，均视为 INFO。DEBUG 使用 `llm_debug.jsonl` 保存完整 LLM 请求/返回，仅保留当前块和最新一个历史分片。新的 LLM 失败在 INFO 只写状态、用途、错误与短响应摘要，禁止再复制完整 prompt；管理员可在 WebUI 日志页切换 INFO/DEBUG。
- 普通用户不可查看系统日志项或其他用户的数据；管理员才能维护全局模型和运维配置。
- 管理员在 WebUI 设置页的独立面板维护全局模型 profile；角色页模型面板只管理当前用户私有 profile 和三类模型选择，避免混淆保存范围。
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
