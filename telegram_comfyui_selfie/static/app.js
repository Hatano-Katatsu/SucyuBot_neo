const state = {
  auth: null,
  status: null,
  config: null,
  secretPresent: {},
  sessions: [],
  selectedSession: null,
  selectedCharacter: null,
  characterData: null,
  memoryDiaryTab: "wardrobe",
  selectedWorldSession: null,
  worldPreview: null,
  logs: [],
  selectedLog: null,
  selectedLogType: null,
  logLevel: "info",
  selectedLogChunk: "",
  logRawContent: "",
  logTail: 1000,
  llmDebugCursorStack: [null],
  profiles: {},
  animaflow: null,
};

const frontendCore = window.SucyuFrontendCore;
if (!frontendCore) throw new Error("frontend_core.js 未在 app.js 前加载");

const viewMeta = {
  overview: ["总览", "服务状态、连接测试和快捷入口"],
  settings: ["设置", "连接、模型、生图和推送参数"],
  characters: ["角色", "角色池、角色设定、长期记忆与日记"],
  world: ["动线", "按用户查看角色实时位置、后续去向、城市地点和用户位置"],
  logs: ["日志", "按用户查看活动日志"],
  usage: ["用量", "LLM 模型按任务维度的 token 消耗与缓存命中率"],
  actions: ["操作", "向指定 Chat ID 发送命令或文字"],
};

const configSections = [
  ["连接", [
    ["telegram_bot_token", "Telegram Bot Token", "secret"],
    ["allowed_chat_ids", "允许的 Chat ID", "list"],
    ["telegram_proxy_enabled", "启用 Telegram 代理", "bool"],
    ["telegram_proxy_url", "Telegram 代理地址", "text"],
  ]],
  ["模型运行参数（模型 profile 在角色页按用户配置）", [
    ["default_chat_model_profile", "默认对话模型 profile", "model_select"],
    ["default_fast_model_profile", "默认快速模型 profile", "model_select"],
    ["default_vision_model_profile", "默认视觉模型 profile", "model_select"],
    ["photo_caption_wait_seconds", "纯图片等待配文秒数", "number"],
    ["chat_reply_length", "回复长度", "select:,简短,适中,详细"],
    ["chat_llm_max_tokens", "回复 max_tokens（含思考输出上限）", "text"],
    ["llm_sampling_params_enabled", "自定义模型采样参数", "bool"],
    ["chat_llm_temperature", "回复温度", "text", "sampling-detail"],
    ["chat_llm_top_p", "回复 top_p（核采样，砍胡话尾巴，留空不下发）", "text", "sampling-detail"],
    ["chat_llm_frequency_penalty", "回复频率惩罚（抗复读/车轱辘话，留空不下发）", "text", "sampling-detail"],
    ["chat_llm_presence_penalty", "回复存在惩罚（推话题发散，默认空=关）", "text", "sampling-detail"],
    ["image_llm_temperature_scene", "推送场景温度", "text", "sampling-detail"],
    ["image_llm_temperature_translate", "Tags 翻译温度", "text", "sampling-detail"],
    ["image_llm_temperature_classify", "角色分析温度", "text", "sampling-detail"],
    ["llm_temperature_scene", "默认场景温度", "text", "sampling-detail"],
    ["llm_temperature_translate", "默认翻译温度", "text", "sampling-detail"],
    ["llm_temperature_classify", "默认分析温度", "text", "sampling-detail"],
  ]],
  ["生图", [
    ["animaflow_enabled", "启用 AnimaFlow", "bool"],
    ["animaflow_workflow", "AnimaFlow 工作流", "animaflow_select", "animaflow-detail"],
    ["animaflow_cfg", "AnimaFlow CFG", "number", "animaflow-detail"],
    ["animaflow_steps", "AnimaFlow 采样步数", "number", "animaflow-detail"],
    ["animaflow_filename_prefix", "AnimaFlow 文件名前缀", "text", "animaflow-detail"],
    ["negative_prompt", "Negative Prompt", "textarea"],
    ["dynamic_appearance", "默认初始穿搭", "textarea"],
    ["style_pool", "画风池", "textarea"],
    ["current_style", "全局当前画风", "text"],
    ["width", "宽度", "number", "native-image-detail"],
    ["height", "高度", "number", "native-image-detail"],
    ["sampler", "Sampler", "text", "native-image-detail"],
    ["scheduler", "Scheduler", "text", "native-image-detail"],
    ["turbo_mode", "Turbo", "bool", "native-image-detail"],
    ["turbo_strength", "Turbo 强度", "text", "native-image-detail"],
  ]],
  ["联网搜索", [
    ["tavily_api_key", "Tavily API Key", "secret"],
    ["web_search_enabled", "启用聊天联网搜索", "bool"],
    ["web_search_daily_limit", "每会话每日搜索上限", "number"],
    ["push_topic_search_daily_limit", "推送话题搜索每日上限", "number"],
  ]],
  ["推送与本地控制台", [
    ["selfie_frequency", "聊天生图频率", "select:极频繁,频繁,适度,偶尔,关闭"],
    ["daily_selfie_limit", "每日主动推送总次数", "number"],
    ["post_chat_push_enabled", "对话后续场推送", "bool"],
    ["post_chat_push_delay_min_minutes", "续场最短延迟(分钟)", "number"],
    ["post_chat_push_delay_max_minutes", "续场最长延迟(分钟)", "number"],
    ["post_chat_push_daily_limit", "每日续场推送上限", "number"],
    ["post_chat_push_cooldown_minutes", "续场冷却(分钟)", "number"],
    ["workday_wake_time", "默认工作日起床", "time"],
    ["workday_sleep_time", "默认工作日睡觉", "time"],
    ["weekend_wake_time", "默认周末起床", "time"],
    ["weekend_sleep_time", "默认周末睡觉", "time"],
    ["location", "默认城市", "text"],
    ["timezone_offset", "时区偏移", "text"],
    ["character_age_stage", "默认年龄段", "select:,minor,adult"],
    ["character_day_anchor", "默认白天去向", "select:,company,school,factory,farm,construction,medical,retail,delivery,driver,home,flexible"],
    ["world_runtime_enabled", "启用自动动线", "bool"],
    ["world_city_places_enabled", "城市地点增强", "bool"],
    ["world_city_places_ttl_days", "城市地点缓存天数", "number"],
    ["life_plan_enabled", "启用角色生活线", "bool"],
    ["life_plan_long_review_days", "长期线复盘天数", "number"],
    ["life_plan_texture_goal_count", "底色参考中期线数", "number"],
    ["life_plan_max_long", "生活线长期上限", "number"],
    ["life_plan_max_mid", "生活线中期上限", "number"],
    ["life_plan_max_events", "每日片段上限", "number"],
    ["amap_api_key", "高德 POI API Key", "secret"],
    ["amap_poi_enabled", "启用高德真实POI", "bool"],
    ["amap_poi_per_type", "每类POI数量", "number"],
    ["google_places_api_key", "谷歌 Places API Key(海外)", "secret"],
    ["google_places_enabled", "启用谷歌POI(海外)", "bool"],
    ["google_places_language", "谷歌POI语言(如ja/en，留空默认)", "text"],
    ["world_user_place_ttl_hours", "用户地点记忆小时", "number"],
    ["world_holiday_dates", "节假日日期 YYYY-MM-DD", "textarea"],
    ["world_workday_dates", "调休工作日 YYYY-MM-DD", "textarea"],
    ["default_purity", "默认纯良度", "text"],
    ["allow_llm_change_appearance", "允许模型改外型", "bool"],
    ["long_memory_enabled", "启用长期记忆注入", "bool"],
    ["long_memory_extract_enabled", "自动提取长期记忆", "bool"],
    ["long_memory_context_limit", "长期记忆注入条数", "number"],
    ["character_history_summary_target_chars", "历史提要目标字数", "number"],
    ["character_history_summary_max_chars", "历史提要硬上限", "number"],
    ["short_context_reset_gap_hours", "短期场景超时小时", "text"],
    ["scene_stale_minutes", "场景断档感知分钟", "number"],
    ["cross_world_enabled", "启用跨会话角色邂逅（仅跨会话）", "bool"],
    ["cross_world_pairs", "跨会话邂逅配对(每行: chatID:角色名=chatID:角色名)", "textarea", "cross-world-detail"],
    ["cross_world_encounter_cooldown_days", "跨会话邂逅冷却天数", "number", "cross-world-detail"],
    ["cross_world_encounter_trigger_strength", "跨会话邂逅触发倾向", "encounter_strength", "cross-world-detail"],
  ]],
];

const characterFieldSections = [
  ["身份", [
    ["character", "角色名", "text", "half"],
    ["series", "作品/系列", "text", "half"],
    ["role_name", "角色类型", "text", "half"],
    ["bot_name", "角色对话名", "text", "half"],
    ["bot_self_name", "自称", "text", "half"],
    ["user_address", "对用户称呼", "text", "half"],
    ["visual_character", "生图角色 Tag", "text", "half"],
    ["visual_series", "生图作品 Tag", "text", "half"],
  ]],
  ["人格", [
    ["persona", "人格描述", "textarea", "wide tall"],
  ]],
  ["外貌", [
    ["count", "人数标签", "text", "half"],
    ["style", "画风", "style_combo", "half"],
    ["appearance", "身体特征", "textarea", "wide"],
    ["allow_change_appearance", "自动换装", "tristate", "half"],
  ]],
  ["生活与边界", [
    ["relationship", "空间关系", "textarea", "wide"],
    ["age_stage", "年龄段", "select:,minor,adult", "third"],
    ["occupation", "职业", "text", "third"],
    ["day_anchor", "白天去向", "select:,company,school,factory,farm,construction,medical,retail,delivery,driver,home,flexible", "third"],
    ["workday_wake_time", "工作日起床", "time", "quarter"],
    ["workday_sleep_time", "工作日睡觉", "time", "quarter"],
    ["weekend_wake_time", "周末起床", "time", "quarter"],
    ["weekend_sleep_time", "周末睡觉", "time", "quarter"],
    ["purity", "纯良度", "number", "third"],
  ]],
];

let commands = ["创建角色", "菜单", "自拍", "天气", "角色", "新场景", "记忆", "手动推送"];

const memoryKindMap = {
  manual: "手动",
  event: "事件",
  fact: "事实",
  user_profile: "用户画像",
  profile: "资料",
  preference: "偏好",
  relationship: "关系",
  setting: "设定",
  boundary: "边界",
  visual: "外貌",
  correction: "纠正",
  diary: "日记",
  location: "地点",
  schedule: "日程",
  tag: "标签",
  self: "自我",
  system: "系统",
  auto: "自动",
};

const memoryKindOptions = (selected = "") => {
  return Object.entries(memoryKindMap).map(([value, label]) => {
    const sel = value === selected ? " selected" : "";
    return `<option value="${escapeHtml(value)}"${sel}>${escapeHtml(label)}</option>`;
  }).join("");
};

const commandHelp = {
  "创建角色": ["逐步创建新的角色卡。", ""],
  "初始化": ["等同于 /创建角色。", ""],
  "菜单": ["打开快速菜单或某个详细分区。", "设置 / 角色 / 生图 / 记忆 / 推送 / 动线 / 上下文 / 调试 / 全部"],
  "帮助": ["等同于 /菜单。", ""],
  "创建OC": ["无参数时等同于 /创建角色；带完整内容时一次性导入角色卡。", "名字：小雨\n角色出处与原名：原创\n外貌和穿搭：黑色短发，蓝眼睛，白衬衫，深色百褶裙\n角色设定：大学生，温柔、慢热、说话简短\n关系和称呼：同城暧昧对象，称呼我主人\n所在城市：上海"],
  "新建角色": ["等同于 /创建角色。", ""],
  "自拍": ["按当前会话和聊天情境生成一张图。", ""],
  "拍照": ["等同于 /自拍。", ""],
  "天气": ["查看城市天气；留空时使用当前会话城市。", "上海"],
  "天气设置": ["设置当前会话的城市、时区和天气来源；也会用于动线和城市地点增强。", "上海"],
  "画风": ["设置当前角色画风；可直接输入未在池中的画风，dream 后会补入画风池。", "@artist / 清空 / 添加 @artist / 删除 @artist"],
  "角色": ["设定角色，或管理角色档案。", "天童爱丽丝 / list / load 名称 / delete 名称 / clearup / reset"],
  "外型": ["查看或修改穿搭、物种特征、发型瞳色。", "black dress, glasses"],
  "人格": ["直接改角色性格、语气和习惯。", "温柔、黏人、说话简短一点"],
  "关系": ["设置你和角色的关系/空间设定，作为高级覆盖项，不替代自动动线。", "同城暧昧对象，周末经常一起出门"],
  "纯良度": ["查看或设置角色边界；数字越高越保守。", "0~10 / auto"],
  "新场景": ["开启新的短期场景，避免上一轮话题继续串进来。", ""],
  "回滚": ["回退最近 N 轮对话；输入非数字时按扮演提示撤回并重生成上一轮回复。", "2 / 语气更冷一点"],
  "重答": ["删掉上一条角色回复，用同一句话重新生成；可附加本次扮演提示。", "更主动一点"],
  "记忆": ["查看、搜索、删除或清空当前角色长期记忆。", "查看 / 搜索 关键词 / 删除 ID / 清空 确认"],
  "记住": ["手动写入一条当前角色长期记忆。", "我喜欢你用温柔一点的语气"],
  "忘记": ["删除指定长期记忆，关键词会先列候选。", "ID / 关键词"],
  "推送频率": ["设置每天主动发图次数，0 为关闭。", "3"],
  "调度": ["查看今日主动推送计划。", ""],
  "手动推送": ["强制触发一次主动推送。", "normal / morning / ntr"],
  "测试生图": ["直接用文本测试 ComfyUI 生图链路。", "坐在窗边看雨"],
  "提示词": ["查看最终提示词拼接示例。", ""],
  "生图状态": ["查看 ComfyUI 连通性、模型和参数。", ""],
  "管理": ["打开管理入口。", "角色池 / 会话 / 位置"],
  "turbo": ["切换 Turbo 加速。", "on / off"],
};

function $(selector) { return document.querySelector(selector); }
function $all(selector) { return [...document.querySelectorAll(selector)]; }

async function api(path, options = {}) {
  const res = await fetch(path, frontendCore.buildRequestOptions(options));
  const raw = await res.text();
  try {
    return frontendCore.parseApiResponse(res, raw);
  } catch (err) {
    if (err?.authExpired) window.location.href = "/";
    throw err;
  }
}

function toast(message, kind = "info") {
  const el = $("#toast");
  el.hidden = false;
  el.dataset.kind = kind;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  el.style.borderColor = kind === "error" ? "#d9a197" : "#d9e0dc";
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => { el.hidden = true; }, 4200);
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  // 只改写按钮内的直接文本节点，避免 textContent 覆写销毁 SVG 图标等子元素
  const textNode = [...button.childNodes].find(node => node.nodeType === 3 && node.textContent.trim());
  if (busy) {
    if (!button.dataset.originalText) button.dataset.originalText = (textNode ? textNode.textContent : button.textContent).trim();
    if (textNode) textNode.textContent = button.dataset.originalText + "…";
    if (!button.querySelector(".spinner")) {
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      button.prepend(spinner);
    }
    button.setAttribute("aria-busy", "true");
  } else {
    if (textNode) textNode.textContent = button.dataset.originalText || textNode.textContent;
    button.querySelector(".spinner")?.remove();
    button.removeAttribute("aria-busy");
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

// 角色卡、衣橱、记忆与日记页面实现在 character_ui.js。

function delay(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms));
}

function switchView(name) {
  _stopOverviewPolling();
  $all(".nav").forEach(btn => btn.classList.toggle("active", btn.dataset.view === name));
  $all(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  $("#view-title").textContent = viewMeta[name][0];
  $("#view-subtitle").textContent = viewMeta[name][1];
  if (name === "overview") { loadFeedbackBoard(); _startOverviewPolling(); }
  if (name === "world") loadWorldSessions();
  if (name === "logs") loadLogs();
  if (name === "usage") loadUsage();
  if (name === "characters") loadCharacterPage();
  if (name === "settings" && state.auth?.role === "admin") loadGlobalModels();
}

async function loadAll() {
  // 先获取身份，决定是否加载管理员专属数据
  try {
    const me = await api("/api/auth/me");
    state.auth = me.auth || {};
  } catch (err) {
    state.auth = {};
  }
  const isAdmin = state.auth.role === "admin";
  const userId = state.auth.user_id || "";
  // 非 admin 隐藏管理员面板和导航入口
  document.querySelectorAll(".admin-panel").forEach(el => {
    el.style.display = isAdmin ? "" : "none";
  });
  document.querySelectorAll('.nav[data-view="usage"]').forEach(el => {
    el.style.display = isAdmin ? "" : "none";
  });

  // 管理员才请求 /api/config（非 admin 调用会 403）
  // 模型 profile 列表与上面三个请求并行发起，避免串行等待拖慢首屏渲染。
  const tasks = [api("/api/status"), api("/api/sessions"), api(modelApiUrl())];
  if (isAdmin) tasks.push(api("/api/config"));
  const results = await Promise.all(tasks);
  const status = results[0];
  const sessions = results[1];
  const modelData = results[2];
  const config = isAdmin ? results[3] : null;
  state.status = status.status;
  if (config && config.config) {
    state.config = config.config.values;
    state.secretPresent = config.config.secret_present || {};
    state.animaflow = config.animaflow || null;
  } else {
    state.config = state.config || {};
    state.secretPresent = state.secretPresent || {};
  }
  if (modelData) {
    state.globalProfiles = modelData.global_profiles || {};
    state.userProfiles = modelData.user_profiles || {};
    state.profiles = { ...(modelData.global_profiles || {}), ...(modelData.user_profiles || {}) };
  }
  state.sessions = (sessions && sessions.sessions) || [];
  // 普通用户固定为自己的会话；若后端尚无该会话，也占位显示
  if (!isAdmin && userId) {
    const fixedSid = `telegram:${userId}`;
    if (!state.sessions.some(s => s.session_id === fixedSid)) {
      state.sessions.unshift({ session_id: fixedSid, chat_id: userId, character: "", last_interaction: 0, last_interaction_ago: "无记录" });
    }
  }
  renderChatIdOptions();
  renderSessionSelector();
  loadFeedbackBoard();
  renderStatus();
  if (isAdmin) {
    renderConfig();
    await loadGlobalModels({ data: modelData });
  }
  renderWorldSessionList();
  if (document.querySelector('.nav[data-view="characters"].active')) {
    loadCharacterPage();
  }
  // 初始加载默认停在 overview，需在此启动轮询；switchView 只负责切换视图时的启停
  if (document.querySelector('.nav[data-view="overview"].active')) {
    _startOverviewPolling();
  }
}

function renderSessionSelector() {
  const select = $("#session-select");
  const fixed = $("#session-select-fixed");
  const isAdmin = state.auth.role === "admin";
  state.selectedSession = frontendCore.resolveSelectedSession(
    state.sessions,
    state.selectedSession,
    state.auth,
  ) || null;
  if (!isAdmin) {
    select.hidden = true;
    fixed.hidden = false;
    const userId = state.auth.user_id || "";
    const fixedSid = userId ? `telegram:${userId}` : "";
    fixed.textContent = fixedSid ? `当前会话: ${fixedSid}` : "未登录";
    return;
  }
  select.hidden = false;
  fixed.hidden = true;
  const opts = ['<option value="">选择会话...</option>'];
  state.sessions.forEach(item => {
    const selected = item.session_id === state.selectedSession ? " selected" : "";
    const frozenTag = item.frozen ? " [已冻结]" : "";
    const hiddenTag = item.hidden ? " [已隐藏]" : "";
    const label = item.character
      ? `${item.character}${frozenTag}${hiddenTag} · ${item.chat_id}`
      : `${String(item.chat_id || item.session_id)}${frozenTag}${hiddenTag}`;
    opts.push(`<option value="${escapeHtml(item.session_id)}"${selected}>${escapeHtml(label)}</option>`);
  });
  select.innerHTML = opts.join("");
  // 确保选中状态与 state 一致
  select.value = state.selectedSession || "";
}

async function selectSession(sessionId) {
  state.selectedSession = sessionId || null;
  renderSessionSelector();
  if (!sessionId) return;
  loadFeedbackBoard();
  if (document.querySelector('.nav[data-view="characters"].active')) {
    await loadCharacterPage();
  }
}

function sessionLabel(sessionId) {
  const item = state.sessions.find(s => s.session_id === sessionId);
  return item ? (item.character || item.chat_id || sessionId) : sessionId;
}

function renderStatus() {
  const s = state.status;
  const botCard = $("#metric-bot-card");
  const genCard = $("#metric-generate-card");
  const llmCard = $("#metric-llm-card");
  botCard.className = "metric " + (s.bot_running ? "status-on" : "status-off");
  $("#metric-bot").textContent = s.bot_running ? "运行中" : "未启动";
  $("#metric-sessions").textContent = String(s.sessions_count);
  genCard.className = "metric " + (s.generating ? "status-busy" : "status-on");
  $("#metric-generate").textContent = s.generating ? "生成中" : "空闲";
  const llmReady = Number(Boolean(s.chat_llm_configured)) + Number(Boolean(s.image_llm_configured));
  const llmClass = llmReady === 2 ? "status-on" : llmReady === 0 ? "status-off" : "status-partial";
  llmCard.className = "metric " + llmClass;
  $("#metric-llm").textContent = `${llmReady}/2 已配置`;
  $("#bot-name").textContent = s.bot_username ? `@${s.bot_username}` : (s.token_configured ? "Token 已填写" : "Token 未填写");
  $("#status-web-url").textContent = s.web_url;
  $("#status-config-path").textContent = s.config_path;
  $("#status-state-path").textContent = s.state_db_path || "-";
  $("#status-process").textContent = s.process_id ? `PID ${s.process_id}` : "-";
  $("#status-launch-script").textContent = s.launch_script || "-";
  $("#status-comfyui").textContent = s.comfyui_url || "-";
  $("#status-chat-llm-model").textContent = s.chat_llm_model ? `${s.chat_llm_model} @ ${s.chat_llm_api_base}` : "-";
  $("#status-image-llm-model").textContent = s.image_llm_model ? `${s.image_llm_model} @ ${s.image_llm_api_base}` : "-";
  $("#status-vision-llm-model").textContent = s.vision_llm_model ? `${s.vision_llm_model} @ ${s.vision_llm_api_base}` : "未配置";
  const startBtn = $("#bot-start-btn");
  const stopBtn = $("#bot-stop-btn");
  if (startBtn && stopBtn) {
    startBtn.style.display = s.bot_running ? "none" : "";
    stopBtn.style.display = s.bot_running ? "" : "none";
  }
}

let _overviewPollTimer = null;
function _startOverviewPolling() {
  if (_overviewPollTimer) return;
  _overviewPollTimer = window.setInterval(async () => {
    try {
      const data = await api(`/api/status?t=${Date.now()}`);
      state.status = data.status;
      renderStatus();
    } catch (_) { /* 轮询失败静默忽略 */ }
  }, 15000);
}

function _stopOverviewPolling() {
  if (_overviewPollTimer) {
    window.clearInterval(_overviewPollTimer);
    _overviewPollTimer = null;
  }
}

function updateFeedbackCharCount() {
  const ta = document.querySelector("#feedback-form textarea[name=content]");
  const counter = document.querySelector("#feedback-form .feedback-char-count");
  if (!ta || !counter) return;
  const remaining = 2000 - String(ta.value || "").length;
  counter.textContent = `剩余 ${remaining} 字符`;
  counter.style.color = remaining < 100 ? "var(--warn)" : "var(--muted)";
}

function feedbackApiPath() {
  const sid = state.selectedSession || "";
  return sid ? `/api/feedback?session_id=${encodeURIComponent(sid)}` : "/api/feedback";
}

function renderFeedback(data) {
  const list = $("#feedback-list");
  const scope = $("#feedback-scope");
  if (!list || !scope) return;
  const currentName = data.current_user_name || sessionLabel(data.current_session_id || state.selectedSession || "") || "当前用户";
  scope.textContent = data.is_admin
    ? `管理员视图：显示全部反馈；当前提交身份为 ${currentName}`
    : `当前身份：${currentName}`;
  const sections = data.sections || [];
  if (!sections.length) {
    list.innerHTML = `<div class="empty-state">暂无反馈。</div>`;
    return;
  }
  list.innerHTML = sections.map(item => `
    <article class="feedback-item">
      <div class="feedback-item-head">
        <strong>${escapeHtml(item.user_name || item.session_id || "用户")}</strong>
        <span>${escapeHtml(item.session_id || "")}</span>
      </div>
      <div class="feedback-content">${escapeHtml(item.content || "（空）")}</div>
    </article>
  `).join("");
}

async function loadFeedbackBoard() {
  const list = $("#feedback-list");
  if (!list || !state.auth?.role) return;
  try {
    const data = await api(feedbackApiPath());
    renderFeedback(data);
  } catch (err) {
    list.innerHTML = `<div class="empty-state">反馈加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function configTextareaValue(value) {
  return frontendCore.configTextareaValue(value);
}

function inputFor([key, label, type, layout], values) {
  const fieldId = "field-" + key;
  const wrap = document.createElement("div");
  const layoutClasses = String(layout || "").split(/\s+/).filter(Boolean).map(item => `field-${item}`);
  wrap.className = ["field-wrap", ...layoutClasses].join(" ");
  const labelEl = document.createElement("label");
  labelEl.htmlFor = fieldId;
  labelEl.textContent = label;
  wrap.appendChild(labelEl);
  let input;
  const extraNodes = [];
  const value = values[key];
  if (type === "textarea" || type === "list") {
    input = document.createElement("textarea");
    input.rows = layoutClasses.includes("field-tall") ? 8 : (type === "list" ? 3 : 4);
    input.value = configTextareaValue(value);
  } else if (type === "bool") {
    input = document.createElement("select");
    input.innerHTML = `<option value="true">开启</option><option value="false">关闭</option>`;
    input.value = value ? "true" : "false";
  } else if (type === "tristate") {
    input = document.createElement("select");
    input.innerHTML = `<option value="">跟随全局</option><option value="true">开启</option><option value="false">关闭</option>`;
    input.value = value === true || value === "true" ? "true" : (value === false || value === "false" ? "false" : "");
  } else if (type === "encounter_strength") {
    input = document.createElement("select");
    input.innerHTML = `<option value="low">低</option><option value="medium">中</option><option value="high">高</option>`;
    const text = String(value ?? "").trim().toLowerCase();
    const aliases = { low: "low", "低": "low", medium: "medium", mid: "medium", "中": "medium", high: "high", "高": "high" };
    let normalized = aliases[text];
    if (!normalized && text !== "") {
      const legacyChance = Number(text);
      if (Number.isFinite(legacyChance)) normalized = legacyChance <= (1 / 3) ? "low" : (legacyChance >= (2 / 3) ? "high" : "medium");
    }
    input.value = normalized || "medium";
    const hint = document.createElement("p");
    hint.className = "field-hint";
    hint.textContent = "仅作为模型判断的软倾向，不对应固定概率；模型仍会结合双方动线、关系和既往邂逅决定。";
    extraNodes.push(hint);
  } else if (type.startsWith("select:")) {
    input = document.createElement("select");
    const options = type.slice(7).split(",");
    input.innerHTML = options.map(opt => `<option value="${opt}">${opt || "默认"}</option>`).join("");
    input.value = value ?? "";
  } else if (type === "style_combo") {
    input = document.createElement("input");
    input.type = "text";
    input.value = value ?? "";
    input.placeholder = "留空则不注入画风/画师";
    const listId = `${fieldId}-options`;
    input.setAttribute("list", listId);
    const datalist = document.createElement("datalist");
    datalist.id = listId;
    const styles = state.characterData?.style_pool || [];
    datalist.innerHTML = styles.map(style => `<option value="${escapeHtml(style)}"></option>`).join("");
    extraNodes.push(datalist);
    const hint = document.createElement("p");
    hint.className = "field-hint";
    hint.textContent = "可从画风池选择，也可手动输入；留空表示本角色不注入画风。";
    extraNodes.push(hint);
  } else if (type === "model_select") {
    input = document.createElement("select");
    const profiles = state.globalProfiles || state.profiles || {};
    const profileIds = Object.keys(profiles);
    const opts = ['<option value="">默认</option>'];
    profileIds.forEach(id => {
      const p = profiles[id];
      const lbl = `${escapeHtml(id)} · ${escapeHtml(p?.name || p?.model || "")}`;
      opts.push(`<option value="${escapeHtml(id)}">${lbl}</option>`);
    });
    input.innerHTML = opts.join("");
    input.value = value ?? "";
  } else if (type === "animaflow_select") {
    input = document.createElement("select");
    const workflows = Array.isArray(state.animaflow?.workflows)
      ? state.animaflow.workflows.filter(item => !item.deprecated)
      : [];
    const opts = workflows.map(item => (
      `<option value="${escapeHtml(item.name || "")}">${escapeHtml(item.name || "")}</option>`
    ));
    if (value && !workflows.some(item => item.name === value)) {
      opts.unshift(`<option value="${escapeHtml(value)}">${escapeHtml(value)} · 尚未检测</option>`);
    }
    input.innerHTML = opts.join("");
    input.value = value ?? "";
    const hint = document.createElement("p");
    hint.className = "field-hint animaflow-workflow-hint";
    hint.textContent = state.animaflow?.error
      ? `检测失败：${state.animaflow.error}`
      : (state.animaflow?.description || "开启后从 /anima/workflows 动态加载。弃用工作流不会列出。");
    extraNodes.push(hint);
  } else {
    input = document.createElement("input");
    input.type = type === "secret" ? "password" : type;
    if (type === "number") {
      input.inputMode = "decimal";
      input.step = key === "animaflow_cfg" ? "any" : "1";
    }
    input.value = type === "secret" ? "" : (value ?? "");
    if (type === "secret" && state.secretPresent[key]) input.placeholder = "已保存；留空不修改";
  }
  input.id = fieldId;
  input.name = key;
  wrap.appendChild(input);
  extraNodes.forEach(node => wrap.appendChild(node));
  return wrap;
}

function applyAnimaflowStateToForm(form, data, { resetDefaults = false } = {}) {
  if (!form || !data) return;
  state.animaflow = data;
  const select = form.elements.namedItem("animaflow_workflow");
  const workflows = Array.isArray(data.workflows) ? data.workflows.filter(item => !item.deprecated) : [];
  if (select) {
    select.innerHTML = workflows.map(item => (
      `<option value="${escapeHtml(item.name || "")}">${escapeHtml(item.name || "")}</option>`
    )).join("");
    select.value = data.selected || select.value;
  }
  const cfg = form.elements.namedItem("animaflow_cfg");
  const steps = form.elements.namedItem("animaflow_steps");
  if (resetDefaults) {
    if (cfg) cfg.value = data.defaults?.cfg ?? "";
    if (steps) steps.value = data.defaults?.steps ?? "";
  }
  for (const [input, bounds] of [[cfg, data.cfg_bounds], [steps, data.steps_bounds]]) {
    if (!input) continue;
    if (bounds?.minimum !== null && bounds?.minimum !== undefined) input.min = bounds.minimum;
    else input.removeAttribute("min");
    if (bounds?.maximum !== null && bounds?.maximum !== undefined) input.max = bounds.maximum;
    else input.removeAttribute("max");
  }
  const hint = form.querySelector(".animaflow-workflow-hint");
  if (hint) {
    const fallbackText = data.legacy_fallback
      ? `AnimaFlow 发现失败，已自动回退 turbo_v1 兼容接口：${data.fallback_reason || "未知原因"}。`
      : "";
    const negText = data.supports_negative ? "该工作流会使用负面提示词。" : "该工作流不构造负面提示词。";
    const defaultsText = data.defaults?.cfg == null || data.defaults?.steps == null
      ? "未公开数值的参数将留空并使用 AnimaFlow 服务端默认。" : "";
    hint.textContent = `${fallbackText} ${data.description || "已加载工作流"} ${negText} ${defaultsText}`.trim();
  }
}

async function discoverAnimaflow(form, workflow = "") {
  const data = await api("/api/admin/animaflow/discover", {
    method: "POST",
    body: { workflow },
  });
  applyAnimaflowStateToForm(form, data.animaflow, { resetDefaults: true });
  return data.animaflow;
}

function wireAnimaflowConfig(form) {
  const toggle = form.elements.namedItem("animaflow_enabled");
  const workflow = form.elements.namedItem("animaflow_workflow");
  const detailWraps = [...form.querySelectorAll(".field-animaflow-detail")];
  const nativeDetailWraps = [...form.querySelectorAll(".field-native-image-detail")];
  const sync = () => {
    const enabled = toggle?.value === "true";
    detailWraps.forEach(wrap => {
      wrap.hidden = !enabled;
      const control = wrap.querySelector("input, select, textarea");
      if (control) control.disabled = !enabled;
    });
    nativeDetailWraps.forEach(wrap => {
      wrap.hidden = enabled;
      const control = wrap.querySelector("input, select, textarea");
      if (control) control.disabled = enabled;
    });
    if (toggle) toggle.setAttribute("aria-expanded", enabled ? "true" : "false");
  };
  if (state.animaflow && !state.animaflow.error) {
    applyAnimaflowStateToForm(form, state.animaflow, { resetDefaults: false });
  }
  if (toggle) {
    toggle.setAttribute("aria-controls", "field-animaflow_workflow");
    toggle.addEventListener("change", async () => {
      sync();
      if (toggle.value !== "true") return;
      toggle.disabled = true;
      try {
        const detected = await discoverAnimaflow(form, workflow?.value || "");
        toast(detected.legacy_fallback ? "AnimaFlow 检测失败，已回退 turbo_v1" : "已检测 AnimaFlow 工作流");
      } catch (err) {
        toggle.value = "false";
        sync();
        toast(err.message, "error");
      } finally {
        toggle.disabled = false;
      }
    });
  }
  if (workflow) {
    workflow.addEventListener("change", async () => {
      workflow.disabled = true;
      try {
        const detected = await discoverAnimaflow(form, workflow.value);
        toast(detected.legacy_fallback ? "工作流检测失败，已回退 turbo_v1 默认值" : "已载入工作流默认 CFG 与步数");
      } catch (err) {
        toast(err.message, "error");
      } finally {
        workflow.disabled = false;
      }
    });
  }
  sync();
}

function wireCrossWorldConfig(form) {
  const toggle = form.elements.namedItem("cross_world_enabled");
  const detailWraps = [...form.querySelectorAll(".field-cross-world-detail")];
  const sync = () => {
    const enabled = toggle?.value === "true";
    detailWraps.forEach(wrap => {
      wrap.hidden = !enabled;
      const control = wrap.querySelector("input, select, textarea");
      if (control) control.disabled = !enabled;
    });
    if (toggle) toggle.setAttribute("aria-expanded", enabled ? "true" : "false");
  };
  if (toggle) {
    toggle.setAttribute("aria-controls", detailWraps.map(wrap => wrap.querySelector("input, select, textarea")?.id).filter(Boolean).join(" "));
    toggle.addEventListener("change", sync);
  }
  sync();
}

function renderConfig() {
  const form = $("#config-form");
  form.innerHTML = "";
  for (const [title, fields] of configSections) {
    const fs = document.createElement("fieldset");
    fs.className = "form-section";
    fs.innerHTML = `<legend>${title}</legend>`;
    const grid = document.createElement("div");
    grid.className = "field-grid";
    const samplingGrid = document.createElement("div");
    samplingGrid.className = "field-grid sampling-detail-grid";
    fields.forEach(field => {
      const layouts = String(field[3] || "").split(/\s+/).filter(Boolean);
      const target = layouts.includes("sampling-detail") ? samplingGrid : grid;
      target.appendChild(inputFor(field, state.config || {}));
    });
    fs.appendChild(grid);
    if (samplingGrid.children.length) {
      const panel = document.createElement("div");
      panel.id = "model-sampling-details";
      panel.className = "sampling-detail-panel";
      panel.dataset.configDetails = "sampling";
      panel.innerHTML = `
        <div class="sampling-detail-head">
          <strong>采样参数详情</strong>
          <span>关闭总开关时保留当前配置值，但请求模型时不会下发这些参数。</span>
        </div>`;
      panel.appendChild(samplingGrid);
      fs.appendChild(panel);
    }
    form.appendChild(fs);
  }
  const samplingToggle = form.elements.namedItem("llm_sampling_params_enabled");
  const samplingPanel = form.querySelector('[data-config-details="sampling"]');
  const syncSamplingDetails = () => {
    const expanded = samplingToggle?.value === "true";
    if (samplingPanel) samplingPanel.hidden = !expanded;
    if (samplingToggle) samplingToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  };
  if (samplingToggle) {
    samplingToggle.setAttribute("aria-controls", "model-sampling-details");
    samplingToggle.addEventListener("change", syncSamplingDetails);
  }
  syncSamplingDetails();
  wireAnimaflowConfig(form);
  wireCrossWorldConfig(form);
  const actions = document.createElement("div");
  actions.className = "form-actions";
  actions.innerHTML = `<button type="button" id="reload-config">撤销未保存</button><button class="primary" type="submit">保存设置</button>`;
  form.appendChild(actions);
  $("#reload-config").onclick = () => loadAll().then(() => toast("已重新载入配置"));
}

function formValues(form) {
  const values = {};
  new FormData(form).forEach((value, key) => { values[key] = value; });
  return values;
}

function renderChatIdOptions() {
  const list = $("#chat-id-options");
  if (!list) return;
  list.innerHTML = state.sessions.map(item => {
    const value = item.chat_id || String(item.session_id || "").replace(/^telegram:/, "");
    return `<option value="${escapeHtml(value)}">${escapeHtml(item.character || "")}</option>`;
  }).join("");
}

function selectedModelUserId() {
  if (state.auth?.role === "admin") {
    return (state.selectedSession || "").startsWith("telegram:") ? state.selectedSession.replace("telegram:", "") : "";
  }
  return state.auth?.user_id || "";
}

function modelApiUrl(path = "/api/models") {
  const userId = selectedModelUserId();
  if (state.auth?.role === "admin" && userId) {
    const sep = path.includes("?") ? "&" : "?";
    return `${path}${sep}user_id=${encodeURIComponent(userId)}`;
  }
  return path;
}

// 角色管理异步操作实现在 character_ui.js。

function syncConfigGlobalProfileSelects() {
  const profiles = state.globalProfiles || {};
  const options = Object.keys(profiles).sort((a, b) => a.localeCompare(b)).map(id => {
    const profile = profiles[id] || {};
    return `<option value="${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(profile.name || profile.model || "")}</option>`;
  }).join("");
  for (const key of ["default_chat_model_profile", "default_fast_model_profile", "default_vision_model_profile"]) {
    const select = document.querySelector(`#config-form select[name="${key}"]`);
    if (!select) continue;
    const currentValue = select.value;
    const missingOption = currentValue && !profiles[currentValue]
      ? `<option value="${escapeHtml(currentValue)}">${escapeHtml(currentValue)} · 已不存在</option>`
      : "";
    select.innerHTML = `<option value="">默认</option>${options}${missingOption}`;
    select.value = currentValue;
  }
}

async function loadGlobalModels({ selectedProfileId = "", data = null } = {}) {
  const box = $("#global-model-manager");
  if (!box || state.auth?.role !== "admin") return;
  const modelData = data || await api("/api/models");
  const globalProfiles = modelData.global_profiles || {};
  const availableGlobalModels = Array.isArray(modelData.available_global_models)
    ? modelData.available_global_models
    : [];
  state.globalProfiles = globalProfiles;
  state.profiles = { ...globalProfiles, ...(state.userProfiles || {}) };
  syncConfigGlobalProfileSelects();

  const profileOptions = Object.keys(globalProfiles).sort((a, b) => a.localeCompare(b)).map(id => {
    const profile = globalProfiles[id] || {};
    return `<option value="${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(profile.name || profile.model || "")}</option>`;
  }).join("");
  const modelCatalogId = "available-global-model-list";
  const modelCatalogOptions = availableGlobalModels.map(item => {
    const source = item.source_profile_id ? `来源 ${item.source_profile_id}` : "启动时发现";
    return `<option value="${escapeHtml(item.id || "")}" label="${escapeHtml(`${source} · ${item.api_base || ""}`)}"></option>`;
  }).join("");

  box.innerHTML = `
    <div class="global-model-toolbar">
      <label>选择已有全局 profile
        <select id="global-model-profile-select">
          <option value="">新建全局 profile</option>
          ${profileOptions}
        </select>
      </label>
      <button id="global-model-new" type="button">新建全局 profile</button>
      <span class="muted">已配置 ${Object.keys(globalProfiles).length} 个；启动时发现 ${availableGlobalModels.length} 个可用模型</span>
    </div>
    <form id="global-model-profile-form" class="global-model-form">
      <div class="global-model-form-grid">
        <label>Profile ID<input name="profile_id" placeholder="opencode-kimi" autocomplete="off" required></label>
        <label>显示名称<input name="name" placeholder="OpenCode Kimi" autocomplete="off"></label>
        <label>Base URL<input name="base_url" placeholder="https://api.example.com/v1 或完整 /chat/completions URL" autocomplete="off" required></label>
        <label>API Key<input name="api_key" type="password" placeholder="填写新密钥；编辑时留空保留" autocomplete="new-password"></label>
        <label>模型名<input name="model" list="${modelCatalogId}" placeholder="kimi-k2.5" autocomplete="off" required></label>
        <label>最大 tokens<input name="max_tokens" type="number" min="1" step="1" inputmode="numeric" placeholder="可选"></label>
        <label>超时秒数<input name="timeout" type="number" min="1" step="1" inputmode="numeric" placeholder="可选"></label>
        <label>默认思考 Effort<select name="thinking_effort">
          <option value="">不指定（沿用模型开关）</option>
          <option value="none">none</option>
          <option value="minimal">minimal</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
          <option value="xhigh">xhigh</option>
          <option value="max">max</option>
        </select></label>
      </div>
      <datalist id="${modelCatalogId}">${modelCatalogOptions}</datalist>
      <div class="form-actions global-model-actions">
        <button id="global-model-save" class="primary" type="submit">添加全局模型</button>
        <button id="global-model-delete" class="danger" type="button" disabled>删除全局模型</button>
      </div>
      <p class="muted">从“模型名”的候选列表选择启动时发现的模型，可自动带入来源 Base URL，并安全复用同端点已有全局 profile 的密钥。默认思考 Effort 会随该 profile 生效，但用户级思考设置仍有更高优先级。API Key 永远不会回显；编辑时留空即可保留。</p>
    </form>
  `;

  const form = $("#global-model-profile-form");
  const profileSelect = $("#global-model-profile-select");
  const profileIdInput = form.elements.profile_id;
  const modelInput = form.elements.model;
  const saveButton = $("#global-model-save");
  const deleteButton = $("#global-model-delete");

  const showProfile = profileId => {
    form.reset();
    delete form.dataset.catalogSourceProfileId;
    delete form.dataset.editingProfileId;
    profileIdInput.readOnly = false;
    form.elements.api_key.placeholder = "填写新密钥；同端点候选可复用已有密钥";
    saveButton.textContent = "添加全局模型";
    deleteButton.disabled = true;
    const profile = globalProfiles[profileId];
    if (!profile) return;
    form.dataset.editingProfileId = profileId;
    profileIdInput.value = profileId;
    profileIdInput.readOnly = true;
    for (const key of ["name", "base_url", "model", "max_tokens", "timeout", "thinking_effort"]) {
      form.elements[key].value = profile[key] ?? "";
    }
    form.elements.api_key.value = "";
    form.elements.api_key.placeholder = profile.api_key
      ? "已配置；留空保留原密钥"
      : "尚未配置 API Key";
    saveButton.textContent = "保存全局模型修改";
    deleteButton.disabled = false;
  };

  profileSelect.onchange = () => showProfile(profileSelect.value);
  $("#global-model-new").onclick = () => {
    profileSelect.value = "";
    showProfile("");
    profileIdInput.focus();
  };
  modelInput.onchange = () => {
    const entry = availableGlobalModels.find(item => item.id === modelInput.value.trim());
    if (!entry) {
      delete form.dataset.catalogSourceProfileId;
      return;
    }
    form.dataset.catalogSourceProfileId = entry.source_profile_id || "";
    form.elements.base_url.value = entry.api_base || form.elements.base_url.value;
    if (!form.dataset.editingProfileId) {
      if (!profileIdInput.value.trim()) profileIdInput.value = entry.id || "";
      if (!form.elements.name.value.trim()) form.elements.name.value = entry.id || "";
    }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const values = formValues(form);
    const profileId = (values.profile_id || "").trim();
    if (!profileId) return toast("请填写全局 Profile ID", "error");
    const wasEditing = Boolean(form.dataset.editingProfileId);
    if (!wasEditing && globalProfiles[profileId]) {
      return toast("该全局 Profile ID 已存在，请先从上方下拉框选择后再修改", "error");
    }
    const body = { _scope: "global" };
    for (const key of ["name", "base_url", "model", "max_tokens", "timeout", "thinking_effort"]) {
      body[key] = (values[key] || "").trim();
    }
    const apiKey = (values.api_key || "").trim();
    if (apiKey) body.api_key = apiKey;
    if (form.dataset.catalogSourceProfileId) {
      body._catalog_source_profile_id = form.dataset.catalogSourceProfileId;
    }
    setBusy(saveButton, true);
    try {
      await api(`/api/models/${encodeURIComponent(profileId)}`, { method: "POST", body });
      await loadGlobalModels({ selectedProfileId: profileId });
      toast(wasEditing ? "全局模型配置已更新" : "全局模型配置已添加");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(saveButton, false);
    }
  };
  deleteButton.onclick = async () => {
    const profileId = form.dataset.editingProfileId || "";
    if (!profileId) return;
    if (!window.confirm(`确认删除全局 profile「${profileId}」？所有用户将无法再选择它。`)) return;
    setBusy(deleteButton, true);
    try {
      await api(`/api/models/${encodeURIComponent(profileId)}?scope=global`, { method: "DELETE" });
      await loadGlobalModels();
      toast("全局模型配置已删除");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(deleteButton, false);
    }
  };

  if (selectedProfileId && globalProfiles[selectedProfileId]) {
    profileSelect.value = selectedProfileId;
    showProfile(selectedProfileId);
  } else {
    showProfile("");
  }
}

async function loadModels() {
  const box = $("#model-manager");
  if (!box) return;
  const data = await api(modelApiUrl());
  const globalProfiles = data.global_profiles || {};
  const userProfiles = data.user_profiles || {};
  const allProfiles = { ...globalProfiles, ...userProfiles };
  const ids = Object.keys(allProfiles);
  const settings = data.settings || {};
  const chatProfileId = settings.chat_profile_id || data.default_chat_model_profile || "";
  const fastProfileId = settings.fast_profile_id || data.default_fast_model_profile || "";
  const visionProfileId = settings.vision_profile_id || data.default_vision_model_profile || "";
  const options = ids.map(id => {
    const p = allProfiles[id] || {};
    const scope = userProfiles[id] ? "私有" : "全局";
    return `<option value="${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(p.name || p.model || "")} · ${scope}</option>`;
  }).join("");
  const resolved = data.resolved || {};
  const resolvedText = key => {
    const item = resolved[key] || {};
    const configured = item.configured ? "已配置" : "未配置";
    const profile = item.profile_id || "默认";
    const model = item.model || "-";
    const thinking = item.thinking_effort
      ? ` · effort=${item.thinking_effort}`
      : (item.thinking === undefined ? "" : (item.thinking ? " · 思考开" : " · 思考关"));
    return `${profile} / ${model} / ${configured}${thinking}`;
  };
  const thinkingSelect = (name, label) => `
    <label>${label}<select name="${name}">
      <option value="">跟随模型</option>
      <option value="true">开启</option>
      <option value="false">关闭</option>
      <option value="none">Effort · none</option>
      <option value="minimal">Effort · minimal</option>
      <option value="low">Effort · low</option>
      <option value="medium">Effort · medium</option>
      <option value="high">Effort · high</option>
      <option value="xhigh">Effort · xhigh</option>
      <option value="max">Effort · max</option>
    </select></label>`;
  box.innerHTML = `
    <form id="model-settings-form" class="model-settings-form">
      <label>对话模型<select name="chat_profile_id"><option value="">默认</option>${options}</select></label>
      <label>快速模型<select name="fast_profile_id"><option value="">默认</option>${options}</select></label>
      <label>视觉模型<select name="vision_profile_id"><option value="">关闭</option>${options}</select></label>
      ${thinkingSelect("chat_thinking", "对话思考")}
      ${thinkingSelect("fast_thinking", "快速思考")}
      ${thinkingSelect("vision_thinking", "视觉思考")}
      <button class="primary" type="submit">保存模型选择</button>
    </form>
    <div class="model-current">
      <div>当前用户: ${escapeHtml(data.user_id || selectedModelUserId() || "-")}</div>
      <div>对话: ${escapeHtml(resolvedText("chat"))}</div>
      <div>快速: ${escapeHtml(resolvedText("image"))}</div>
      <div>视觉: ${escapeHtml(resolvedText("vision"))}</div>
    </div>
    <form id="model-profile-form" class="inline-manager-form">
      <label>私有 Profile ID<input name="profile_id" placeholder="my-chat-model" autocomplete="off"></label>
      <label>显示名称<input name="name" placeholder="DeepSeek V4 Pro" autocomplete="off"></label>
      <label>Base URL<input name="base_url" placeholder="https://api.example.com/v1" autocomplete="off"></label>
      <label>API Key<input name="api_key" type="password" placeholder="留空保留旧密钥" autocomplete="new-password"></label>
      <label>模型名<input name="model" placeholder="deepseek-chat" autocomplete="off"></label>
      <label>最大 tokens<input name="max_tokens" type="number" min="1" step="1" inputmode="decimal" placeholder="可选"></label>
      <label>超时秒数<input name="timeout" type="number" min="1" step="1" inputmode="decimal" placeholder="可选"></label>
      <button type="submit">保存私有模型 profile</button>
      <button id="delete-model-profile" class="danger" type="button">删除私有 profile</button>
      <p class="muted">这里只管理当前用户的私有 profile；管理员请到“设置 → 全局模型配置”维护全局 profile。API Key 保存空值或 ******** 时会保留旧密钥。对话与视觉选择解析到同一 API Base 和模型名时，用户发送或显式引用的图片会直接进入对话模型；否则先由视觉模型转成文字描述。</p>
    </form>
  `;
  box.querySelector("[name=chat_profile_id]").value = settings.chat_profile_id || "";
  box.querySelector("[name=fast_profile_id]").value = settings.fast_profile_id || "";
  box.querySelector("[name=vision_profile_id]").value = settings.vision_profile_id || "";
  ["chat_thinking", "fast_thinking", "vision_thinking"].forEach(key => {
    const el = box.querySelector(`[name=${key}]`);
    if (el && settings[key] !== undefined && settings[key] !== null) el.value = String(settings[key]);
  });
  $("#model-settings-form").onsubmit = async event => {
    event.preventDefault();
    await api(modelApiUrl("/api/models/settings"), { method: "PATCH", body: formValues(event.currentTarget) });
    await loadModels();
    toast("模型设置已保存");
  };
  $("#model-profile-form").onsubmit = async event => {
    event.preventDefault();
    const values = formValues(event.currentTarget);
    const profileId = (values.profile_id || "").trim();
    if (!profileId) {
      toast("请填写 profile id");
      return;
    }
    const body = {};
    ["name", "base_url", "api_key", "model", "max_tokens", "timeout"].forEach(key => {
      const value = (values[key] || "").trim();
      if (value) body[key] = value;
    });
    await api(modelApiUrl(`/api/models/${encodeURIComponent(profileId)}`), { method: "POST", body });
    await loadModels();
    toast("模型 profile 已保存");
  };
  $("#delete-model-profile").onclick = async event => {
    const form = event.currentTarget.closest("form");
    const profileId = (form.elements.profile_id.value || "").trim();
    if (!profileId) return toast("请先填写要删除的 profile id", "error");
    if (!window.confirm(`确认删除私有 profile「${profileId}」？`)) return;
    await api(modelApiUrl(`/api/models/${encodeURIComponent(profileId)}`), { method: "DELETE" });
    await loadModels();
    toast("模型 profile 已删除");
  };
}

// 实时动线与生活线页面实现在 world_ui.js。

// 日志与 LLM 用量页面实现在 admin_logs.js，保持此文件聚焦主界面编排。

function fillCommandSelect() {
  const sel = document.querySelector("#command-form select[name=command]");
  sel.innerHTML = commands.map(cmd => `<option value="${cmd}">/${cmd}</option>`).join("");
  sel.onchange = updateCommandHelp;
  updateCommandHelp();
}

async function loadCommandSelect() {
  const fallbackCommands = commands;
  try {
    const data = await api("/api/commands");
    commands = frontendCore.resolveCommands(data.commands, fallbackCommands);
  } catch (err) {
    console.warn("加载命令列表失败，使用内置兜底", err);
    commands = frontendCore.resolveCommands([], fallbackCommands);
  }
  fillCommandSelect();
}

function updateCommandHelp() {
  const sel = document.querySelector("#command-form select[name=command]");
  const help = $("#command-help");
  const arg = document.querySelector("#command-form textarea[name=arg]");
  const [text, placeholder] = commandHelp[sel.value] || ["", ""];
  if (help) help.textContent = text;
  if (arg) arg.placeholder = placeholder || "";
}

function formatPromptCleanup(cleanup) {
  const changes = cleanup.changes || [];
  const lines = [
    cleanup.applied ? "执行结果" : "预览结果",
    `配置更新: ${cleanup.config_updated || 0}`,
    `会话更新: ${cleanup.sessions_updated || 0}`,
    `角色档案更新: ${cleanup.saved_characters_updated || 0}`,
    `待处理条目: ${changes.length}`,
  ];
  if (cleanup.backup_paths && cleanup.backup_paths.length) {
    lines.push(`备份: ${cleanup.backup_paths.join(" | ")}`);
  }
  if (cleanup.note) lines.push(`说明: ${cleanup.note}`);
  if (!changes.length) {
    lines.push("", "没有发现需要清理的 positive_prefix 污染。");
    return lines.join("\n");
  }
  lines.push("", "变更预览:");
  changes.slice(0, 40).forEach((item, index) => {
    lines.push(`${index + 1}. ${item.label}${item.character ? ` (${item.character})` : ""}`);
    if (item.removed_quality) lines.push(`   移除质量词: ${item.removed_quality}`);
    if (item.moved_style) lines.push(`   移动风格词: ${item.moved_style}`);
    if (item.style_before !== item.style_after) lines.push(`   ${item.style_field}: ${item.style_before || "（空）"} -> ${item.style_after || "（空）"}`);
    lines.push(`   before: ${item.before}`);
    lines.push(`   after : ${item.after || "（空）"}`);
  });
  if (changes.length > 40) lines.push(`... 还有 ${changes.length - 40} 条未显示`);
  return lines.join("\n");
}

async function runPromptCleanup(applyChanges, button) {
  if (applyChanges && !window.confirm("这会先备份 config/state，再改写老 Prompt 数据。继续吗？")) return;
  setBusy(button, true);
  try {
    const data = await api("/api/admin/cleanup-prompt-prefix", { method: "POST", body: { apply: applyChanges } });
    $("#prompt-cleanup-output").textContent = formatPromptCleanup(data.cleanup || {});
    if (applyChanges) await loadAll();
    toast(applyChanges ? "Prompt 清理已执行" : "Prompt 清理预览已生成");
  } catch (err) {
    $("#prompt-cleanup-output").textContent = err.message;
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function initEvents() {
  document.addEventListener("click", handleLifePlanAction);
  document.addEventListener("click", handleCharacterInteractionAction);
  $all(".nav").forEach(btn => btn.onclick = () => switchView(btn.dataset.view));
  $("#refresh-btn").onclick = () => loadAll().then(() => toast("已刷新"));
  $("#restart-btn").onclick = async () => {
    if (!confirm("确认重启服务？\n\n服务将短暂中断后自动恢复。")) return;
    const btn = $("#restart-btn");
    setBusy(btn, true);
    try {
      await api("/api/service/restart", { method: "POST" });
      toast("重启指令已发送，服务将在几秒后恢复");
    } catch (e) {
      toast("重启失败: " + e.message, true);
    } finally {
      setBusy(btn, false);
    }
  };
  $("#bot-start-btn").onclick = async (event) => {
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      await api("/api/bot/start", { method: "POST" });
      await loadAll();
      toast("机器人已启动");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $("#bot-stop-btn").onclick = async (event) => {
    if (!confirm("确定停止机器人吗？停止后用户将无法收到消息。")) return;
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      await api("/api/bot/stop", { method: "POST" });
      await loadAll();
      toast("机器人已停止");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $("#reload-world-sessions").onclick = () => loadWorldSessions().then(() => toast("动线用户已刷新"));
  $("#world-refresh").onclick = () => loadWorldRoute().then(() => toast("动线已刷新"));
  $("#world-refresh-places").onclick = async (event) => {
    if (!state.selectedWorldSession) return;
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      await loadWorldRoute({ refreshPlaces: true });
      toast("城市地点已刷新");
    } finally {
      setBusy(btn, false);
    }
  };
  $("#world-generate-life").onclick = async (event) => {
    if (!state.selectedWorldSession) return;
    const instruction = window.prompt("目标指示（可留空）：", "");
    if (instruction === null) return;
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      const data = await api(`/api/world/${encodeURIComponent(state.selectedWorldSession)}/life-plan`, {
        method: "POST",
        body: { instruction, regenerate_goals: true },
      });
      await applyLifePlanResponse(data);
      toast("生活线已生成");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $("#reload-logs").onclick = async () => {
    await loadLogs();
    if (state.selectedLog) await selectLog(state.selectedLog);
    else if (state.selectedLogType) await selectSystemLog(state.selectedLogType);
    toast("日志已刷新");
  };
  $("#log-filter").oninput = () => renderFilteredLog(state.logRawContent);
  $("#log-level").onchange = async event => {
    state.logLevel = event.currentTarget.value === "debug" ? "debug" : "info";
    state.selectedLog = null;
    state.selectedLogType = null;
    hideLogChunkSelector();
    hideLogPageControls();
    await loadLogs();
    if (state.logLevel === "debug" && state.auth?.role === "admin") {
      await selectSystemLog("llm-debug");
    } else if (state.logLevel === "debug") {
      $("#log-title").textContent = "DEBUG 日志";
      renderFilteredLog("需要管理员权限查看 DEBUG 日志。");
      $("#log-clear").disabled = true;
    } else {
      $("#log-title").textContent = "INFO 日志内容";
      renderFilteredLog("选择左侧会话或错误日志查看内容。");
      $("#log-clear").disabled = true;
    }
  };
  $("#log-tail").onchange = event => {
    state.logTail = Number(event.currentTarget.value) || 1000;
    if (state.selectedLog) selectLog(state.selectedLog);
    else if (state.selectedLogType) selectSystemLog(state.selectedLogType);
  };
  $("#feedback-refresh").onclick = () => loadFeedbackBoard().then(() => toast("反馈已刷新"));
  $("#feedback-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const btn = event.submitter;
    const content = String(new FormData(form).get("content") || "").trim();
    if (!content) {
      toast("反馈内容不能为空", "error");
      return;
    }
    if (content.length > 2000) {
      toast("反馈内容不能超过 2000 字符", "error");
      return;
    }
    setBusy(btn, true);
    try {
      await api("/api/feedback", {
        method: "POST",
        body: { session_id: state.selectedSession || "", content },
      });
      form.reset();
      updateFeedbackCharCount();
      await loadFeedbackBoard();
      toast("反馈已提交");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  const feedbackTa = document.querySelector("#feedback-form textarea[name=content]");
  if (feedbackTa) {
    feedbackTa.oninput = updateFeedbackCharCount;
    updateFeedbackCharCount();
  }
  $("#log-refresh").onclick = () => {
    if (state.selectedLog) selectLog(state.selectedLog);
    else if (state.selectedLogType) selectSystemLog(state.selectedLogType);
  };
  $("#usage-refresh").onclick = (event) => {
    const btn = event.currentTarget;
    setBusy(btn, true);
    loadUsage().then(() => toast("用量已刷新")).finally(() => setBusy(btn, false));
  };
  $("#usage-range").onchange = () => loadUsage();
  ["usage-filter-text", "usage-filter-profile", "usage-filter-model", "usage-filter-purpose", "usage-filter-tag", "usage-sort"].forEach(id => {
    const el = $(`#${id}`);
    if (el) el.oninput = el.onchange = () => { if (state.usage) renderUsage(state.usage); };
  });
  $("#log-clear").onclick = async () => {
    if (!state.selectedLog) return;
    if (!window.confirm(`确定清空 ${state.selectedLog} 的日志吗？`)) return;
    try {
      await api(`/api/logs/${encodeURIComponent(state.selectedLog)}`, { method: "DELETE" });
      $("#log-content").textContent = "（已清空）";
      toast("日志已清空");
      await loadLogs();
    } catch (err) {
      toast(err.message, "error");
    }
  };

  $("#config-form").onsubmit = async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true);
    try {
      const invalid = frontendCore.firstInvalidNumberField(
        event.currentTarget.querySelectorAll('input[type="number"]'),
      );
      if (invalid) {
        invalid.focus();
        throw new Error("字段「" + (invalid.closest("label")?.textContent?.trim() || invalid.name) + "」必须是有效数字");
      }
      await api("/api/config", { method: "POST", body: { values: formValues(event.currentTarget) } });
      await loadAll();
      toast("设置已保存");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };

  document.querySelector("[data-action=test-comfyui]").onclick = () => runTest("/api/actions/test-comfyui");
  document.querySelector("[data-action=test-chat-llm]").onclick = () => runTest("/api/actions/test-llm", { purpose: "chat" });
  document.querySelector("[data-action=test-image-llm]").onclick = () => runTest("/api/actions/test-llm", { purpose: "image" });
  document.querySelector("[data-action=test-vision-llm]").onclick = () => runTest("/api/actions/test-llm", { purpose: "vision" });

  $("#command-form").onsubmit = async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true);
    try {
      await api("/api/actions/run-command", { method: "POST", body: formValues(event.currentTarget) });
      toast("命令已发送");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };

  $("#message-form").onsubmit = async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true);
    try {
      await api("/api/actions/send-message", { method: "POST", body: formValues(event.currentTarget) });
      toast("消息已发送");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };

  $("#session-select").onchange = (event) => selectSession(event.currentTarget.value);

  $("#character-refresh").onclick = async (event) => {
    if (!state.selectedSession) return;
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      await loadCharacters();
      toast("角色池已刷新");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $("#character-add").onclick = () => addNewCharacter();
  $("#character-import").onclick = () => importCharacter();
  $("#character-import-file").onclick = () => importCharacterFile();
  $("#character-import-file-input").onchange = handleCharacterImportFile;
  $("#character-activate").onclick = async (event) => {
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      await activateSelectedCharacter();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $all("#view-characters .tab").forEach(tab => {
    tab.onclick = () => switchMemoryDiaryTab(tab.dataset.tab);
  });
  $("#memory-diary-refresh").onclick = async (event) => {
    if (!state.selectedSession || !state.selectedCharacter) return;
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      if (state.memoryDiaryTab === "wardrobe") await loadCharacters();
      else if (state.memoryDiaryTab === "memory") await loadMemories();
      else await loadDiaries();
      toast("已刷新");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $("#memory-organize-btn").onclick = async (event) => {
    if (!state.selectedSession || !state.selectedCharacter) return;
    const btn = event.currentTarget;
    if (!window.confirm("这会让 AI 根据最近日记和对话自动整理非手动记忆（增删改）。手动记忆不会被修改。继续吗？")) return;
    setBusy(btn, true);
    try {
      const charKey = characterApiKey(state.selectedCharacter || "");
      const data = await api(`/api/sessions/${state.selectedSession}/organize-memories?character_key=${encodeURIComponent(charKey)}`, { method: "POST" });
      toast(data.message || "记忆整理完成");
      await loadMemories();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  const pushMenu = $("#test-push-menu");
  $("#test-push-btn").onclick = (event) => {
    event.stopPropagation();
    pushMenu.classList.toggle("open");
  };
  pushMenu.querySelectorAll("[data-mode]").forEach(btn => {
    btn.onclick = async () => {
      pushMenu.classList.remove("open");
      if (!state.selectedSession) { toast("请先选择会话", "error"); return; }
      if (!state.selectedCharacter) { toast("请先选择角色", "error"); return; }
      const mode = btn.dataset.mode;
      const charLabel = state.characterData?.characters?.[state.selectedCharacter]?.character || state.selectedCharacter;
      if (!window.confirm(`确定为 ${charLabel} 触发 ${mode} 模式手动推送吗？`)) return;
      setBusy(btn, true);
      try {
        const data = await api(`/api/sessions/${encodeURIComponent(state.selectedSession)}/test-push`, {
          method: "POST",
          body: { character_key: characterApiKey(state.selectedCharacter), mode },
        });
        toast(data.message || `${mode} 手动推送已触发`);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        setBusy(btn, false);
      }
    };
  });
  document.addEventListener("click", () => pushMenu.classList.remove("open"));
  $("#model-refresh").onclick = async (event) => {
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      await loadModels();
      toast("模型配置已刷新");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $("#global-model-refresh").onclick = async (event) => {
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      await loadGlobalModels();
      toast("全局模型配置已刷新");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
  $all(".panel-head.collapsible").forEach(head => {
    head.onclick = () => {
      const body = head.nextElementSibling;
      if (body) body.classList.toggle("collapsed");
      head.classList.toggle("collapsed");
      if (head.tagName === "BUTTON") {
        head.setAttribute("aria-expanded", head.classList.contains("collapsed") ? "false" : "true");
      }
    };
  });

  $("#prompt-cleanup-preview").onclick = (event) => runPromptCleanup(false, event.currentTarget);
  $("#prompt-cleanup-apply").onclick = (event) => runPromptCleanup(true, event.currentTarget);

  $("#reload-config-file-btn").onclick = async (event) => {
    if (!window.confirm("这会从当前配置文件重新载入运行态配置。\n\n不会保存设置，也不会重启服务。继续吗？")) return;
    const btn = event.currentTarget;
    const out = $("#git-update-output");
    setBusy(btn, true);
    out.textContent = "正在重新载入配置文件...";
    try {
      const data = await api("/api/service/reload-config", { method: "POST" });
      await loadAll();
      out.textContent = `已重新载入配置文件:\n${data.config_path}\n配置项: ${data.loaded_keys}`;
      toast("配置文件已重新载入");
    } catch (err) {
      out.textContent = err.message;
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };

  $("#git-update-btn").onclick = async (event) => {
    if (!window.confirm("这会从远端 Git 拉取最新代码并自动重启服务。继续吗？")) return;
    const btn = event.currentTarget;
    const out = $("#git-update-output");
    setBusy(btn, true);
    out.textContent = "正在执行 Git 更新...";
    try {
      const data = await api("/api/admin/git-update", { method: "POST" });
      out.textContent = data.report || JSON.stringify(data, null, 2);
      if (data.result && data.result.pulled) {
        toast("已拉取更新，服务正在重启，控制台会自动重新连接...");
        await waitForServiceRestart(data.restart?.old_pid || state.status?.process_id);
      } else {
        toast("Git 更新完成（无新提交或拉取失败）");
      }
    } catch (err) {
      out.textContent = err.message;
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };

  $("#freeze-inactive-btn").onclick = async (event) => {
    if (!window.confirm("这会冻结所有 7 天以上未主动发消息的用户。被冻结用户发一条消息即可自动解冻。继续吗？")) return;
    const btn = event.currentTarget;
    const out = $("#freeze-output");
    setBusy(btn, true);
    out.textContent = "正在扫描并冻结不活跃用户...";
    try {
      const data = await api("/api/admin/freeze-inactive", { method: "POST" });
      if (data.frozen_count === 0) {
        out.textContent = "没有需要冻结的用户（所有用户均在 7 天内活跃过）。";
        toast("没有需要冻结的用户");
      } else {
        const lines = data.frozen.map(s => `  ${s.character || s.chat_id} (${s.session_id}) — 最后活跃: ${s.last_interaction_ago}`);
        out.textContent = `已冻结 ${data.frozen_count} 个用户:\n${lines.join("\n")}`;
        toast(`已冻结 ${data.frozen_count} 个不活跃用户`);
        await loadAll();
      }
    } catch (err) {
      out.textContent = err.message;
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };

  document.addEventListener("keydown", (event) => {
    const tag = document.activeElement?.tagName;
    const editable = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || document.activeElement?.isContentEditable;
    if ((event.ctrlKey || event.metaKey) && event.key === "s") {
      event.preventDefault();
      const form = document.querySelector(".view.active form");
      if (form && form.requestSubmit) { form.requestSubmit(); toast("已保存"); }
      return;
    }
    if (editable || event.ctrlKey || event.metaKey || event.altKey) return;
    const key = Number(event.key);
    if (key >= 1 && key <= 5) {
      const views = Object.keys(viewMeta);
      if (key <= views.length) switchView(views[key - 1]);
    }
  });
}

async function waitForServiceRestart(oldPid) {
  const deadline = Date.now() + 35000;
  while (Date.now() < deadline) {
    await delay(1200);
    try {
      const data = await api(`/api/status?t=${Date.now()}`);
      const pid = data.status?.process_id;
      if (!oldPid || (pid && pid !== oldPid)) {
        await loadAll();
        toast(pid ? `服务已重启，新进程 PID ${pid}` : "服务已重启");
        return;
      }
    } catch (err) {
      // During restart the control port is expected to disappear briefly.
    }
  }
  toast("重启请求已发出，但控制台暂时还没连回。稍后手动刷新页面即可。", "error");
}

async function runTest(path, body = undefined) {
  const out = $("#test-output");
  out.textContent = "Running...";
  try {
    const data = await api(path, { method: "POST", body });
    if (Object.prototype.hasOwnProperty.call(data, "thinking_length") && Object.prototype.hasOwnProperty.call(data, "reply_length")) {
      const effort = data.thinking_effort ? ` · effort=${data.thinking_effort}` : "";
      out.textContent = [
        `Profile: ${data.profile_id || "-"}`,
        `模型: ${data.model || "-"}`,
        `思考配置: ${data.thinking ? "开启" : "关闭"}${effort}`,
        `返回思考长度: ${data.thinking_length} 字符`,
        `返回回复长度: ${data.reply_length} 字符`,
        `finish_reason: ${data.finish_reason ?? "-"}`,
        `回复:\n${data.reply || "（空）"}`,
      ].join("\n");
    } else {
      out.textContent = JSON.stringify(data, null, 2);
    }
  } catch (err) {
    out.textContent = err.message;
  }
}

loadCommandSelect();
initEvents();
loadAll().catch(err => toast(err.message, "error"));
