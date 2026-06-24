const state = {
  auth: null,
  status: null,
  config: null,
  secretPresent: {},
  sessions: [],
  selectedSession: null,
  selectedCharacter: null,
  characterData: null,
  memoryDiaryTab: "memory",
  selectedWorldSession: null,
  worldPreview: null,
  logs: [],
  selectedLog: null,
  profiles: {},
};

const viewMeta = {
  overview: ["总览", "服务状态、连接测试和快捷入口"],
  settings: ["设置", "连接、模型、生图和推送参数"],
  characters: ["角色", "角色池、角色设定、长期记忆与日记"],
  world: ["动线", "按用户查看角色每日动线、城市地点和用户位置"],
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
  ["模型运行参数（模型 profile 在角色页按用户配置；生图后端/模型只读 YAML）", [
    ["default_chat_model_profile", "默认对话模型 profile", "model_select"],
    ["default_fast_model_profile", "默认快速模型 profile", "model_select"],
    ["chat_reply_length", "回复长度", "select:,简短,适中,详细"],
    ["chat_llm_temperature", "回复温度", "text"],
    ["image_llm_temperature_scene", "推送场景温度", "text"],
    ["image_llm_temperature_translate", "Tags 翻译温度", "text"],
    ["image_llm_temperature_classify", "角色分析温度", "text"],
    ["llm_temperature_scene", "默认场景温度", "text"],
    ["llm_temperature_translate", "默认翻译温度", "text"],
    ["llm_temperature_classify", "默认分析温度", "text"],
  ]],
  ["生图", [
    ["negative_prompt", "Negative Prompt", "textarea"],
    ["dynamic_appearance", "默认初始穿搭", "textarea"],
    ["style_pool", "画风池", "textarea"],
    ["current_style", "全局当前画风", "text"],
    ["width", "宽度", "number"],
    ["height", "高度", "number"],
    ["sampler", "Sampler", "text"],
    ["scheduler", "Scheduler", "text"],
    ["turbo_mode", "Turbo", "bool"],
    ["turbo_strength", "Turbo 强度", "text"],
  ]],
  ["推送与本地控制台", [
    ["selfie_frequency", "聊天生图频率", "select:极频繁,频繁,适度,偶尔,关闭"],
    ["daily_selfie_limit", "每日随机推送", "number"],
    ["location", "默认城市", "text"],
    ["timezone_offset", "时区偏移", "text"],
    ["character_age_stage", "默认年龄段", "select:,minor,adult"],
    ["character_day_anchor", "默认白天去向", "select:,company,school,factory,farm,construction,medical,retail,delivery,driver,home,flexible"],
    ["world_runtime_enabled", "启用自动动线", "bool"],
    ["world_city_places_enabled", "城市地点增强", "bool"],
    ["world_city_places_ttl_days", "城市地点缓存天数", "number"],
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
    ["short_context_reset_gap_hours", "短期场景超时小时", "text"],
  ]],
];

const characterFieldSections = [
  ["身份", [
    ["character", "角色名", "text"],
    ["series", "作品/系列", "text"],
    ["role_name", "角色类型", "text"],
    ["bot_name", "对话称呼", "text"],
    ["bot_self_name", "自称", "text"],
    ["visual_character", "生图角色 Tag", "text"],
    ["visual_series", "生图作品 Tag", "text"],
  ]],
  ["人格", [
    ["persona", "人格描述", "textarea"],
  ]],
  ["外貌", [
    ["count", "人数标签", "text"],
    ["appearance", "身体特征", "textarea"],
    ["outfit", "服装标签", "textarea"],
    ["style", "画风", "text"],
    ["allow_change_appearance", "自动换装", "tristate"],
  ]],
  ["关系与背景", [
    ["relationship", "空间关系", "textarea"],
    ["age_stage", "年龄段", "select:,minor,adult"],
    ["occupation", "职业", "text"],
    ["day_anchor", "白天去向", "select:,company,school,factory,farm,construction,medical,retail,delivery,driver,home,flexible"],
  ]],
  ["偏好", [
    ["scene_preference", "场景偏好", "textarea"],
    ["selfie_preference", "自拍偏好", "textarea"],
  ]],
  ["边界", [
    ["purity", "纯良度", "number"],
  ]],
];

const commands = ["初始化", "菜单", "帮助", "创建OC", "自拍", "天气", "天气设置", "画风", "角色", "外型", "人格", "纯良度", "新场景", "记忆", "记住", "忘记", "推送频率", "调度", "测试推送", "测试生图", "提示词", "生图状态", "管理", "turbo"];

const memoryKindMap = {
  manual: "手动",
  event: "事件",
  fact: "事实",
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
  "初始化": ["查看新用户上手向导。", ""],
  "菜单": ["打开快速菜单或某个详细分区。", "设置 / 角色 / 生图 / 记忆 / 推送 / 动线 / 上下文 / 调试 / 全部"],
  "帮助": ["等同于 /菜单。", ""],
  "创建OC": ["创建原创角色。名字只用于对话身份，不会作为生图角色标签写入提示词。", "名字：小雨\n角色类型：大学生\n年龄段：adult\n白天去向：school\n性格：温柔、慢热、说话简短，会认真回应用户的情绪\n外貌：黑色短发，蓝眼睛，身材纤细，浅色皮肤\n初始穿搭：白衬衫，深色百褶裙\n与你的关系：同城暧昧对象，周末经常一起出门\n所在城市：上海"],
  "自拍": ["按当前会话和聊天情境生成一张图。", ""],
  "天气": ["查看城市天气；留空时使用当前会话城市。", "上海"],
  "天气设置": ["设置当前会话的城市、时区和天气来源；也会用于每日动线和城市地点增强。", "上海"],
  "画风": ["查看、添加、删除或切换画风池。", "查看 / 添加 @artist / 删除 @artist / 切换 @artist"],
  "角色": ["设定角色，或管理角色档案。", "天童爱丽丝 / list / load 名称 / delete 名称 / clearup / reset"],
  "外型": ["查看或修改穿搭、物种特征、发型瞳色。", "black dress, glasses"],
  "人格": ["直接改角色性格、语气和习惯。", "温柔、黏人、说话简短一点"],
  "关系": ["设置你和角色的关系/空间设定，作为高级覆盖项，不替代自动动线。", "同城暧昧对象，周末经常一起出门"],
  "纯良度": ["查看或设置角色边界；数字越高越保守。", "0~10 / auto"],
  "新场景": ["开启新的短期场景，避免上一轮话题继续串进来。", ""],
  "回滚": ["回退最近 N 轮对话（删掉角色回复和对应的你的消息），默认 1 轮，方便测试。", "2"],
  "重答": ["删掉上一条角色回复，用同一句话重新生成。", ""],
  "记忆": ["查看、搜索、删除或清空当前角色长期记忆。", "查看 / 搜索 关键词 / 删除 ID / 清空 确认"],
  "记住": ["手动写入一条当前角色长期记忆。", "我喜欢你用温柔一点的语气"],
  "忘记": ["删除指定长期记忆，关键词会先列候选。", "ID / 关键词"],
  "推送频率": ["设置每天主动发图次数，0 为关闭。", "3"],
  "调度": ["查看今日主动推送计划。", ""],
  "测试推送": ["强制触发一次主动推送。", "normal / morning / ntr"],
  "测试生图": ["直接用文本测试 ComfyUI 生图链路。", "坐在窗边看雨"],
  "提示词": ["查看最终提示词拼接示例。", ""],
  "生图状态": ["查看 ComfyUI 连通性、模型和参数。", ""],
  "管理": ["打开管理入口。", "角色池 / 会话 / 位置"],
  "turbo": ["切换 Turbo 加速。", "on / off"],
};

function $(selector) { return document.querySelector(selector); }
function $all(selector) { return [...document.querySelectorAll(selector)]; }

async function api(path, options = {}) {
  const init = { ...options };
  if (init.body && typeof init.body !== "string") {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(path, init);
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}

function toast(message, kind = "info") {
  const el = $("#toast");
  el.hidden = false;
  el.textContent = message;
  el.style.borderColor = kind === "error" ? "#d9a197" : "#d9e0dc";
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => { el.hidden = true; }, 4200);
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
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

function delay(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms));
}

function switchView(name) {
  $all(".nav").forEach(btn => btn.classList.toggle("active", btn.dataset.view === name));
  $all(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  $("#view-title").textContent = viewMeta[name][0];
  $("#view-subtitle").textContent = viewMeta[name][1];
  if (name === "world") loadWorldSessions();
  if (name === "logs") loadLogs();
  if (name === "usage") loadUsage();
  if (name === "characters") loadCharacterPage();
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
  const tasks = [api("/api/status"), api("/api/sessions")];
  if (isAdmin) tasks.push(api("/api/config"));
  const results = await Promise.all(tasks);
  const status = results[0];
  const sessions = results[1];
  const config = results[2];
  state.status = status.status;
  if (config && config.config) {
    state.config = config.config.values;
    state.secretPresent = config.config.secret_present || {};
  } else {
    state.config = state.config || {};
    state.secretPresent = state.secretPresent || {};
  }
  state.sessions = (sessions && sessions.sessions) || [];
  // 普通用户固定为自己的会话；若后端尚无该会话，也占位显示
  if (!isAdmin && userId) {
    const fixedSid = `telegram:${userId}`;
    if (!state.sessions.some(s => s.session_id === fixedSid)) {
      state.sessions.unshift({ session_id: fixedSid, chat_id: userId, character: "", last_interaction: 0, last_interaction_ago: "无记录" });
    }
  }
  renderSessionSelector();
  // 获取模型 profile 列表供配置页下拉框使用
  try {
    const modelData = await api("/api/models");
    state.profiles = { ...(modelData.global_profiles || {}), ...(modelData.user_profiles || {}) };
  } catch (err) {
    state.profiles = state.profiles || {};
  }
  renderStatus();
  if (isAdmin) renderConfig();
  renderWorldSessionList();
  if (document.querySelector('.nav[data-view="characters"].active')) {
    loadCharacterPage();
  }
}

function renderSessionSelector() {
  const select = $("#session-select");
  const fixed = $("#session-select-fixed");
  const isAdmin = state.auth.role === "admin";
  if (!isAdmin) {
    select.hidden = true;
    fixed.hidden = false;
    const userId = state.auth.user_id || "";
    const fixedSid = userId ? `telegram:${userId}` : "";
    fixed.textContent = fixedSid ? `当前会话: ${fixedSid}` : "未登录";
    if (state.selectedSession !== fixedSid) {
      state.selectedSession = fixedSid;
    }
    return;
  }
  select.hidden = false;
  fixed.hidden = true;
  // 管理员默认选中第一个会话，避免角色页空白
  if (!state.selectedSession && state.sessions.length) {
    state.selectedSession = state.sessions[0].session_id;
  }
  const opts = ['<option value="">选择会话...</option>'];
  state.sessions.forEach(item => {
    const selected = item.session_id === state.selectedSession ? " selected" : "";
    const frozenTag = item.frozen ? " [已冻结]" : "";
    const label = item.character ? `${item.character}${frozenTag} · ${item.chat_id}` : `${String(item.chat_id || item.session_id)}${frozenTag}`;
    opts.push(`<option value="${escapeHtml(item.session_id)}"${selected}>${escapeHtml(label)}</option>`);
  });
  select.innerHTML = opts.join("");
  // 确保选中状态与 state 一致
  select.value = state.selectedSession || "";
}

async function selectSession(sessionId) {
  if (!sessionId) return;
  state.selectedSession = sessionId;
  renderSessionSelector();
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
}

function inputFor([key, label, type], values) {
  const fieldId = "field-" + key;
  const wrap = document.createElement("div");
  wrap.className = "field-wrap";
  const labelEl = document.createElement("label");
  labelEl.htmlFor = fieldId;
  labelEl.textContent = label;
  wrap.appendChild(labelEl);
  let input;
  const value = values[key];
  if (type === "textarea" || type === "list") {
    input = document.createElement("textarea");
    input.rows = type === "list" ? 3 : 5;
    input.value = Array.isArray(value) ? value.join("\n") : (value ?? "");
  } else if (type === "bool") {
    input = document.createElement("select");
    input.innerHTML = `<option value="true">开启</option><option value="false">关闭</option>`;
    input.value = value ? "true" : "false";
  } else if (type === "tristate") {
    input = document.createElement("select");
    input.innerHTML = `<option value="">跟随全局</option><option value="true">开启</option><option value="false">关闭</option>`;
    input.value = value === true || value === "true" ? "true" : (value === false || value === "false" ? "false" : "");
  } else if (type.startsWith("select:")) {
    input = document.createElement("select");
    const options = type.slice(7).split(",");
    input.innerHTML = options.map(opt => `<option value="${opt}">${opt || "默认"}</option>`).join("");
    input.value = value ?? "";
  } else if (type === "model_select") {
    input = document.createElement("select");
    const profileIds = Object.keys(state.profiles || {});
    const opts = ['<option value="">默认</option>'];
    profileIds.forEach(id => {
      const p = state.profiles[id];
      const lbl = `${escapeHtml(id)} · ${escapeHtml(p?.name || p?.model || "")}`;
      opts.push(`<option value="${escapeHtml(id)}">${lbl}</option>`);
    });
    input.innerHTML = opts.join("");
    input.value = value ?? "";
  } else {
    input = document.createElement("input");
    input.type = type === "secret" ? "password" : type;
    input.value = type === "secret" ? "" : (value ?? "");
    if (type === "secret" && state.secretPresent[key]) input.placeholder = "已保存；留空不修改";
  }
  input.id = fieldId;
  input.name = key;
  wrap.appendChild(input);
  return wrap;
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
    fields.forEach(field => grid.appendChild(inputFor(field, state.config || {})));
    fs.appendChild(grid);
    form.appendChild(fs);
  }
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

async function loadCharacterPage() {
  const pool = $("#character-pool");
  const form = $("#character-form");
  if (!state.selectedSession) {
    if (pool) pool.innerHTML = `<div class="empty-state">请从右上角选择会话。</div>`;
    if (form) form.innerHTML = `<div class="empty-state">请从右上角选择会话。</div>`;
    if ($("#memory-manager")) $("#memory-manager").innerHTML = `<div class="empty-state">请选择会话与角色。</div>`;
    if ($("#diary-manager")) $("#diary-manager").innerHTML = `<div class="empty-state">请选择会话与角色。</div>`;
    return;
  }
  await loadCharacters();
  await Promise.all([loadMemories(), loadDiaries(), loadModels()]);
}

function renderCharacterPool() {
  const pool = $("#character-pool");
  if (!pool) return;
  const data = state.characterData || {};
  const characters = data.characters || {};
  const activeId = data.active_id || "";
  const ids = Object.keys(characters);
  if (!ids.length) {
    pool.innerHTML = `<div class="empty-state">当前会话没有保存角色。点击“新建角色”创建。</div>`;
    return;
  }
  pool.innerHTML = ids.map(id => {
    const char = characters[id];
    const isActive = id === activeId;
    const isDefault = char.is_default === true;
    const activeBadge = isActive ? `<span class="active-badge">当前</span>` : "";
    const defaultBadge = isDefault ? `<span class="default-badge">默认</span>` : "";
    return `
      <button class="character-card ${state.selectedCharacter === id ? "selected" : ""}" data-character-id="${escapeHtml(id)}" type="button">
        <div class="character-card-title">${escapeHtml(char.character || id)}${activeBadge}${defaultBadge}</div>
        <div class="character-card-meta">${escapeHtml(char.series || "")} · ${escapeHtml(char.role_name || "未设定角色类型")}</div>
      </button>
    `;
  }).join("");
  pool.querySelectorAll(".character-card").forEach(btn => {
    btn.onclick = () => selectCharacter(btn.dataset.characterId);
  });
}

function selectCharacter(characterId) {
  state.selectedCharacter = characterId;
  renderCharacterPool();
  renderCharacterForm();
  loadMemories();
  loadDiaries();
}

function renderCharacterForm() {
  const title = $("#character-editor-title");
  const subtitle = $("#character-editor-subtitle");
  const form = $("#character-form");
  const activateBtn = $("#character-activate");
  if (!form) return;
  if (!state.selectedCharacter || !state.characterData) {
    title.textContent = "角色设定";
    subtitle.textContent = "从左侧角色池选择一个角色";
    form.innerHTML = `<div class="empty-state">请从左侧角色池选择一个角色。</div>`;
    if (activateBtn) activateBtn.disabled = true;
    return;
  }
  const characters = state.characterData.characters || {};
  const char = characters[state.selectedCharacter] || {};
  const isActive = state.characterData.active_id === state.selectedCharacter;
  const isDefault = char.is_default === true;
  title.textContent = isActive ? `${char.character || state.selectedCharacter}（当前）` : (char.character || state.selectedCharacter);
  if (isDefault) {
    subtitle.textContent = `会话: ${state.selectedSession} · 系统默认角色（不可删除）`;
  } else {
    subtitle.textContent = `会话: ${state.selectedSession}`;
  }
  if (activateBtn) {
    activateBtn.disabled = isActive;
    activateBtn.textContent = isActive ? "已是当前" : "设为当前";
  }

  form.innerHTML = "";
  characterFieldSections.forEach(([sectionTitle, fields], index) => {
    const section = document.createElement("section");
    section.className = "form-section character-section";
    const collapsed = index > 1;
    section.innerHTML = `<h3 class="section-toggle ${collapsed ? "collapsed" : ""}" type="button">${sectionTitle}</h3>`;
    const grid = document.createElement("div");
    grid.className = `field-grid ${collapsed ? "collapsed" : ""}`;
    fields.forEach(field => grid.appendChild(inputFor(field, char)));
    section.appendChild(grid);
    form.appendChild(section);
  });

  const histSection = document.createElement("section");
  histSection.className = "form-section character-section";
  histSection.innerHTML = `
    <h3 class="section-toggle" type="button">角色历史提要</h3>
    <div class="field-grid">
      <div class="field-wrap full-width">
        <label for="history-summary-editor">历史提要 <span class="muted">（dream 自动生成，可手动编辑）</span></label>
        <textarea id="history-summary-editor" name="history_summary" rows="6" placeholder="暂无历史提要，等待 dream 生成或手动输入。"></textarea>
        <div class="field-actions">
          <button type="button" id="history-summary-save" class="primary">保存提要</button>
        </div>
      </div>
    </div>
  `;
  form.appendChild(histSection);

  const histToggle = histSection.querySelector(".section-toggle");
  const histGrid = histSection.querySelector(".field-grid");
  histToggle.onclick = () => {
    histGrid.classList.toggle("collapsed");
    histToggle.classList.toggle("collapsed");
  };

  loadHistorySummary();

  const actions = document.createElement("div");
  actions.className = "form-actions";
  actions.innerHTML = `<button type="button" class="danger" id="delete-character" ${isDefault ? "disabled" : ""}>删除角色</button><button class="primary" type="submit">保存角色</button>`;
  form.appendChild(actions);

  form.querySelectorAll(".section-toggle").forEach(toggle => {
    toggle.onclick = () => {
      const grid = toggle.nextElementSibling;
      grid.classList.toggle("collapsed");
      toggle.classList.toggle("collapsed");
    };
  });

  if (!isDefault) {
    $("#delete-character").onclick = async () => {
      if (!window.confirm(`确定要删除角色 ${state.selectedCharacter} 吗？`)) return;
      const sid = encodeURIComponent(state.selectedSession);
      await api(`/api/sessions/${sid}/characters/${encodeURIComponent(state.selectedCharacter)}`, { method: "DELETE" });
      state.selectedCharacter = null;
      await loadCharacters();
      await loadAll();
      toast("角色已删除");
    };
  }

  form.onsubmit = async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true);
    try {
      const values = formValues(form);
      values.id = state.selectedCharacter;
      const sid = encodeURIComponent(state.selectedSession);
      await api(`/api/sessions/${sid}/characters`, { method: "POST", body: values });
      await loadCharacters();
      await loadAll();
      toast("角色已保存");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
}

async function loadHistorySummary() {
  const editor = $("#history-summary-editor");
  const saveBtn = $("#history-summary-save");
  if (!editor || !state.selectedSession) return;
  const sid = encodeURIComponent(state.selectedSession);
  const charKey = state.selectedCharacter || "";
  try {
    const data = await api(`/api/sessions/${sid}/history-summary?character_key=${encodeURIComponent(charKey)}`);
    editor.value = data.summary || "";
  } catch (_) {
    editor.value = "";
  }
  if (saveBtn) {
    saveBtn.onclick = async () => {
      setBusy(saveBtn, true);
      try {
        await api(`/api/sessions/${sid}/history-summary`, {
          method: "PUT",
          body: { character_key: charKey, summary: editor.value },
        });
        toast("历史提要已保存");
      } catch (err) {
        toast(err.message, "error");
      } finally {
        setBusy(saveBtn, false);
      }
    };
  }
}

async function loadCharacters() {
  const pool = $("#character-pool");
  if (!pool || !state.selectedSession) {
    if (pool) pool.innerHTML = `<div class="empty-state">请选择会话。</div>`;
    return;
  }
  const sid = encodeURIComponent(state.selectedSession);
  try {
    const data = await api(`/api/sessions/${sid}/characters`);
    state.characterData = data;
    renderCharacterPool();
    const characters = data.characters || {};
    const ids = Object.keys(characters);
    if (!state.selectedCharacter || !characters[state.selectedCharacter]) {
      state.selectedCharacter = data.active_id || ids[0] || "";
    }
    renderCharacterForm();
  } catch (err) {
    pool.innerHTML = `<div class="empty-state">${escapeHtml(err.message)}</div>`;
    toast(err.message, "error");
  }
}

function bindRangeInputs(container) {
  container.querySelectorAll('input[type="range"]').forEach(input => {
    const update = () => {
      const span = input.parentElement.querySelector(".range-value");
      if (span) span.textContent = input.value;
    };
    input.oninput = update;
    update();
  });
}

async function loadMemories() {
  const box = $("#memory-manager");
  if (!box || !state.selectedSession || !state.selectedCharacter) {
    if (box) box.innerHTML = `<div class="empty-state">请从左侧选择角色。</div>`;
    return;
  }
  const sid = encodeURIComponent(state.selectedSession);
  const charKey = encodeURIComponent(state.selectedCharacter);
  try {
    const data = await api(`/api/sessions/${sid}/memories?character_key=${charKey}`);
    const rows = (data.memories || []).map(mem => `
      <div class="manager-row memory-row">
        <textarea data-memory-summary="${mem.id}" rows="1" placeholder="记忆内容">${escapeHtml(mem.summary || "")}</textarea>
        <select data-memory-kind="${mem.id}" title="类型">${memoryKindOptions(mem.kind || "manual")}</select>
        <div class="range-wrap" title="重要度 1-5">
          <input type="range" data-memory-importance="${mem.id}" min="1" max="5" step="1" value="${escapeHtml(String(mem.importance ?? 3))}">
          <span class="range-value">${escapeHtml(String(mem.importance ?? 3))}</span>
        </div>
        <button data-memory-save="${mem.id}" type="button">保存</button>
        <button class="danger" data-memory-delete="${mem.id}" type="button">删除</button>
      </div>
    `).join("");
    box.innerHTML = `
      <form id="memory-add-form" class="inline-manager-form memory-add-form">
        <textarea name="summary" placeholder="新增一条手动记忆" rows="1"></textarea>
        <select name="kind">${memoryKindOptions("manual")}</select>
        <div class="range-wrap">
          <input type="range" name="importance" min="1" max="5" step="1" value="3">
          <span class="range-value">3</span>
        </div>
        <button class="primary" type="submit">新增记忆</button>
      </form>
      <div class="manager-list">${rows || `<div class="empty-state">暂无记忆。</div>`}</div>
    `;
    bindRangeInputs(box);
    $("#memory-add-form").onsubmit = async event => {
      event.preventDefault();
      await api(`/api/sessions/${sid}/memories?character_key=${charKey}`, { method: "POST", body: formValues(event.currentTarget) });
      await loadMemories();
      toast("记忆已新增");
    };
    box.querySelectorAll("[data-memory-save]").forEach(btn => {
      btn.onclick = async () => {
        const id = btn.dataset.memorySave;
        await api(`/api/sessions/${sid}/memories/${id}?character_key=${charKey}`, {
          method: "PATCH",
          body: {
            summary: box.querySelector(`[data-memory-summary="${id}"]`).value,
            kind: box.querySelector(`[data-memory-kind="${id}"]`).value,
            importance: box.querySelector(`[data-memory-importance="${id}"]`).value,
          },
        });
        await loadMemories();
        toast("记忆已保存");
      };
    });
    box.querySelectorAll("[data-memory-delete]").forEach(btn => {
      btn.onclick = async () => {
        const id = btn.dataset.memoryDelete;
        const summary = box.querySelector(`[data-memory-summary="${id}"]`).value.trim();
        if (!window.confirm(`确定删除这条记忆吗？\n\n${summary || "(空)"}`)) return;
        await api(`/api/sessions/${sid}/memories/${id}?character_key=${charKey}`, { method: "DELETE" });
        await loadMemories();
        toast("记忆已删除");
      };
    });
  } catch (err) {
    box.innerHTML = `<div class="empty-state">${escapeHtml(err.message)}</div>`;
    toast(err.message, "error");
  }
}

async function loadDiaries() {
  const box = $("#diary-manager");
  if (!box || !state.selectedSession || !state.selectedCharacter) {
    if (box) box.innerHTML = `<div class="empty-state">请从左侧选择角色。</div>`;
    return;
  }
  const sid = encodeURIComponent(state.selectedSession);
  const charKey = encodeURIComponent(state.selectedCharacter);
  try {
    const data = await api(`/api/sessions/${sid}/diaries?character_key=${charKey}&limit=30`);
    renderDiaries(data.diaries || [], sid, charKey);
  } catch (err) {
    box.innerHTML = `<div class="empty-state">${escapeHtml(err.message)}</div>`;
    toast(err.message, "error");
  }
}

function renderDiaries(diaries, sid, charKey) {
  const box = $("#diary-manager");
  const today = new Date().toISOString().slice(0, 10);
  const rows = diaries.map(diary => {
    const date = diary.diary_date;
    const updated = new Date(diary.updated_at * 1000).toLocaleString("zh-CN");
    return `
      <div class="manager-row diary-row">
        <div class="diary-header">
          <strong>${escapeHtml(date)}</strong>
          <span class="muted">${escapeHtml(updated)}</span>
        </div>
        <textarea data-diary-date="${escapeHtml(date)}" rows="3">${escapeHtml(diary.content || "")}</textarea>
        <div class="button-row">
          <button data-diary-save="${escapeHtml(date)}" type="button">保存</button>
          <button class="danger" data-diary-delete="${escapeHtml(date)}" type="button">删除</button>
        </div>
      </div>
    `;
  }).join("");
  box.innerHTML = `
    <form id="diary-add-form" class="inline-manager-form">
      <input type="date" name="diary_date" value="${today}">
      <textarea name="content" placeholder="新增或覆盖一条日记" rows="3"></textarea>
      <button class="primary" type="submit">新增日记</button>
    </form>
    <div class="manager-list">${rows || `<div class="empty-state">暂无日记。</div>`}</div>
  `;
  $("#diary-add-form").onsubmit = async event => {
    event.preventDefault();
    const values = formValues(event.currentTarget);
    if (!values.content || !values.diary_date) return;
    await api(`/api/sessions/${sid}/diaries/${encodeURIComponent(values.diary_date)}?character_key=${charKey}`, { method: "POST", body: { content: values.content } });
    await loadDiaries();
    toast("日记已保存");
  };
  box.querySelectorAll("[data-diary-save]").forEach(btn => {
    btn.onclick = async () => {
      const date = btn.dataset.diarySave;
      const content = box.querySelector(`[data-diary-date="${date}"]`).value;
      await api(`/api/sessions/${sid}/diaries/${encodeURIComponent(date)}?character_key=${charKey}`, { method: "POST", body: { content } });
      await loadDiaries();
      toast("日记已保存");
    };
  });
  box.querySelectorAll("[data-diary-delete]").forEach(btn => {
    btn.onclick = async () => {
      const date = btn.dataset.diaryDelete;
      if (!window.confirm(`确定删除 ${date} 的日记吗？`)) return;
      await api(`/api/sessions/${sid}/diaries/${encodeURIComponent(date)}?character_key=${charKey}`, { method: "DELETE" });
      await loadDiaries();
      toast("日记已删除");
    };
  });
}

function switchMemoryDiaryTab(tab) {
  state.memoryDiaryTab = tab;
  $all("#view-characters .tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  $all("#view-characters .tab-panel").forEach(p => p.classList.toggle("active", p.id === `${tab}-tab`));
}

async function activateSelectedCharacter() {
  if (!state.selectedCharacter || !state.selectedSession) return;
  const sid = encodeURIComponent(state.selectedSession);
  const cid = encodeURIComponent(state.selectedCharacter);
  await api(`/api/sessions/${sid}/characters/${cid}/activate`, { method: "POST" });
  await loadCharacters();
  await loadAll();
  toast("已切换到该角色");
}

function addNewCharacter() {
  if (!state.selectedSession) return;
  const baseName = "新角色";
  let id = baseName;
  const chars = state.characterData?.characters || {};
  let n = 1;
  while (chars[id]) id = `${baseName} ${n++}`;
  const payload = { id, character: id };
  const sid = encodeURIComponent(state.selectedSession);
  api(`/api/sessions/${sid}/characters`, { method: "POST", body: payload }).then(() => {
    loadCharacters().then(() => {
      selectCharacter(id);
      toast("新角色已创建");
    });
  }).catch(err => toast(err.message, "error"));
}

function importCharacter() {
  if (!state.selectedSession) return;
  const raw = window.prompt("粘贴角色 JSON：");
  if (!raw) return;
  try {
    const payload = JSON.parse(raw);
    const sid = encodeURIComponent(state.selectedSession);
    api(`/api/sessions/${sid}/characters`, { method: "POST", body: payload }).then(() => {
      loadCharacters().then(() => {
        const id = payload.id || payload.character || payload.bot_name;
        if (id) selectCharacter(id);
        toast("角色已导入");
      });
    }).catch(err => toast(err.message, "error"));
  } catch (err) {
    toast("JSON 无效：" + err.message, "error");
  }
}

async function loadModels() {
  const box = $("#model-manager");
  if (!box) return;
  const data = await api("/api/models");
  const allProfiles = { ...(data.global_profiles || {}), ...(data.user_profiles || {}) };
  const ids = Object.keys(allProfiles);
  const settings = data.settings || {};
  const chatProfileId = settings.chat_profile_id || data.default_chat_model_profile || "";
  const fastProfileId = settings.fast_profile_id || data.default_fast_model_profile || "";
  const chatProfile = allProfiles[chatProfileId] || {};
  const fastProfile = allProfiles[fastProfileId] || {};
  const options = ids.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(allProfiles[id]?.name || allProfiles[id]?.model || "")}</option>`).join("");
  const thinkingLabel = profile => {
    if (!profile || !profile.thinking_fixed) return "";
    const disabled = profile.disable_thinking === true || profile.disable_thinking === "true" || profile.disable_thinking === 1;
    return `（固定${disabled ? "关闭" : "开启"}）`;
  };
  const chatNote = thinkingLabel(chatProfile);
  const fastNote = thinkingLabel(fastProfile);
  const chatDisabled = chatNote ? "disabled" : "";
  const fastDisabled = fastNote ? "disabled" : "";
  box.innerHTML = `
    <form id="model-settings-form" class="model-settings-form">
      <label>对话模型<select name="chat_profile_id"><option value="">默认</option>${options}</select></label>
      <label>快速模型<select name="fast_profile_id"><option value="">默认</option>${options}</select></label>
      <label>对话思考<select name="chat_thinking" ${chatDisabled}><option value="">默认</option><option value="true">开启</option><option value="false">关闭</option></select> <span class="fixed-note">${chatNote}</span></label>
      <label>快速思考<select name="fast_thinking" ${fastDisabled}><option value="">默认</option><option value="true">开启</option><option value="false">关闭</option></select> <span class="fixed-note">${fastNote}</span></label>
      <button class="primary" type="submit">保存模型选择</button>
    </form>
    <form id="model-profile-form" class="inline-manager-form">
      <input name="profile_id" placeholder="自定义 profile id">
      <textarea name="json" placeholder='{"name":"DeepSeek","base_url":"...","api_key":"...","model_no_think":"deepseek-chat"}'></textarea>
      <button type="submit">保存自定义模型</button>
    </form>
  `;
  box.querySelector("[name=chat_profile_id]").value = settings.chat_profile_id || "";
  box.querySelector("[name=fast_profile_id]").value = settings.fast_profile_id || "";
  box.querySelector("[name=chat_thinking]").value = settings.chat_thinking === true ? "true" : settings.chat_thinking === false ? "false" : "";
  box.querySelector("[name=fast_thinking]").value = settings.fast_thinking === true ? "true" : settings.fast_thinking === false ? "false" : "";
  $("#model-settings-form").onsubmit = async event => {
    event.preventDefault();
    await api("/api/models/settings", { method: "PATCH", body: formValues(event.currentTarget) });
    await loadModels();
    toast("模型设置已保存");
  };
  $("#model-profile-form").onsubmit = async event => {
    event.preventDefault();
    const values = formValues(event.currentTarget);
    await api(`/api/models/${encodeURIComponent(values.profile_id)}`, { method: "POST", body: JSON.parse(values.json) });
    await loadModels();
    toast("自定义模型已保存");
  };
}

function worldSessionTitle(item) {
  return item.character ? `${item.character} · ${item.chat_id}` : String(item.chat_id || item.session_id || "");
}

function renderWorldSessionList() {
  const list = $("#world-session-list");
  if (!list) return;
  list.innerHTML = "";
  if (!state.sessions.length) {
    list.innerHTML = `<div class="empty-state">暂无用户。机器人收到 Telegram 消息后会自动出现在这里。</div>`;
    return;
  }
  state.sessions.forEach(item => {
    const btn = document.createElement("button");
    btn.className = "session-item" + (item.frozen ? " frozen" : "");
    btn.dataset.sid = item.session_id;
    const frozenBadge = item.frozen ? ' <span class="frozen-badge">已冻结</span>' : "";
    btn.innerHTML = `<div class="session-title">${escapeHtml(item.character || item.chat_id)}${frozenBadge}</div><div class="session-meta">${escapeHtml(item.location || "未设置城市")} · UTC${escapeHtml(item.timezone || "-")} · 推送 ${escapeHtml(item.daily_push || "-")}</div>`;
    btn.onclick = () => selectWorldSession(item.session_id);
    list.appendChild(btn);
  });
  if (state.selectedWorldSession) {
    $all("#world-session-list .session-item").forEach(btn => btn.classList.toggle("active", btn.dataset.sid === state.selectedWorldSession));
  }
}

async function loadWorldSessions() {
  try {
    const data = await api("/api/sessions");
    state.sessions = data.sessions || [];
    renderWorldSessionList();
    if (!state.sessions.length) {
      state.selectedWorldSession = null;
      state.worldPreview = null;
      $("#world-title").textContent = "每日动线";
      $("#world-subtitle").textContent = "暂无用户";
      $("#world-content").innerHTML = `<div class="empty-state">暂无用户。机器人收到 Telegram 消息后会自动出现在这里。</div>`;
      return;
    }
    const exists = state.sessions.some(item => item.session_id === state.selectedWorldSession);
    if (!state.selectedWorldSession || !exists) state.selectedWorldSession = state.sessions[0].session_id;
    await loadWorldRoute();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function selectWorldSession(sessionId) {
  state.selectedWorldSession = sessionId;
  renderWorldSessionList();
  await loadWorldRoute();
}

async function loadWorldRoute({ refreshPlaces = false } = {}) {
  if (!state.selectedWorldSession) return;
  const box = $("#world-content");
  box.innerHTML = `<div class="empty-state">正在读取动线...</div>`;
  try {
    const sid = encodeURIComponent(state.selectedWorldSession);
    const data = await api(refreshPlaces ? `/api/world/${sid}/places/refresh` : `/api/world/${sid}`, refreshPlaces ? { method: "POST" } : {});
    state.worldPreview = data.world;
    renderWorldRoute(data.world);
  } catch (err) {
    box.innerHTML = `<div class="empty-state">${escapeHtml(err.message)}</div>`;
    toast(err.message, "error");
  } finally {
    renderWorldSessionList();
  }
}

function placeText(place) {
  if (!place) return "未知";
  const name = place.name ? ` · ${place.name}` : "";
  return `${place.label || place.key || "未知"}${name}`;
}

function lifeProfileText(profile = {}) {
  const age = { minor: "未成年", adult: "成年", unknown: "年龄未知" }[profile.age_stage] || "年龄未知";
  const anchor = {
    company: "上班族",
    school: "在校/学校",
    factory: "工厂工人",
    farm: "务农",
    construction: "建筑工人",
    medical: "医护",
    retail: "店员/服务员",
    delivery: "外卖/快递",
    driver: "司机",
    home: "无固定职场",
    flexible: "时间自由",
    unknown: "去向未知",
  }[profile.day_anchor] || "去向未知";
  return `${age} · ${anchor}`;
}

function placeTags(place) {
  if (!place) return "";
  const tags = [place.indoor ? "室内" : "室外", place.public ? "公开场合" : "私密场合"];
  if (place.views?.length) tags.push(`视角 ${place.views.join(" / ")}`);
  return tags.map(tag => `<span>${escapeHtml(tag)}</span>`).join("");
}

function renderCandidateChips(items = []) {
  if (!items.length) return `<span class="muted">无候选地点</span>`;
  return items.map(item => `<span class="place-chip">${escapeHtml(placeText(item))}<small>${Number(item.score || 0).toFixed(1)}</small></span>`).join("");
}

function renderCatalog(catalog = {}) {
  if (!catalog.enabled) return `<div class="empty-state">城市地点增强已关闭，当前使用基础场所目录。</div>`;
  if (!catalog.has_catalog) return `<div class="empty-state">还没有城市地点目录。可以点右上角“刷新城市地点”，或通过 /天气设置 城市 生成。</div>`;
  const rows = (catalog.items || []).map(item => `
    <div class="catalog-row">
      <strong>${escapeHtml(item.label || item.key)}</strong>
      <span>${escapeHtml((item.places || []).join("、"))}</span>
    </div>
  `).join("");
  const updated = catalog.updated_ago ? ` · ${escapeHtml(catalog.updated_ago)}` : "";
  return `<div class="catalog-list"><div class="catalog-head">城市地点目录${updated}</div>${rows}</div>`;
}

function renderTimeline(timeline = []) {
  if (!timeline.length) return `<div class="empty-state">没有可显示的动线。</div>`;
  return `<div class="timeline-list">${timeline.map(item => {
    const place = item.character_place || {};
    const classes = item.is_current_slot ? "timeline-item now" : "timeline-item";
    const light = item.time_context || {};
    const lightText = light.light_phase ? `${light.season || ""} · ${light.light_phase}${light.sunrise && light.sunset ? ` · 日出 ${light.sunrise} / 日落 ${light.sunset}` : ""}` : "";
    return `
      <article class="${classes}">
        <time>${escapeHtml(item.slot_label || "")}</time>
        <div>
          <strong>${escapeHtml(placeText(place))}</strong>
          <p>${escapeHtml(item.time_period || "")} · ${escapeHtml(item.day_type || "")} · ${escapeHtml(item.weather || "天气未知")}</p>
          ${lightText ? `<p>${escapeHtml(lightText)}</p>` : ""}
          <div class="tag-row">${placeTags(place)}</div>
        </div>
      </article>
    `;
  }).join("")}</div>`;
}

function renderWorldRoute(world) {
  const box = $("#world-content");
  if (!world) {
    box.innerHTML = `<div class="empty-state">没有动线数据。</div>`;
    return;
  }
  const session = world.session || {};
  $("#world-title").textContent = worldSessionTitle(session) || "每日动线";
  $("#world-subtitle").textContent = `${world.city || "未设置城市"} · UTC${world.timezone || "-"} · ${world.weather || "天气未知"}`;
  if (!world.enabled) {
    box.innerHTML = `<div class="empty-state">自动动线已关闭。可在“设置 → 推送与本地控制台 → 启用自动动线”打开。</div>`;
    return;
  }
  const current = world.current || {};
  const currentPlace = current.character_place || {};
  const nextPlace = current.next_place || null;
  const light = current.time_context || {};
  const lightText = light.light_phase ? `${light.season || ""} · ${light.light_phase}${light.sunrise && light.sunset ? ` · 日出 ${light.sunrise} / 日落 ${light.sunset}` : ""}` : "-";
  const nextText = nextPlace ? `${current.next_time_period || "接下来"} · ${placeText(nextPlace)}` : "未知";
  const up = current.user_place;
  const userPlace = up ? `${up.co_located ? "🤝 同处 · " : ""}${up.label}${up.text ? ` · ${up.text}` : ""}${up.updated_ago ? ` · ${up.updated_ago}` : ""}` : "未知";
  const constraints = (current.constraints || []).map(item => `<li>${escapeHtml(item)}</li>`).join("");
  const override = current.spatial_override ? `<div class="note-line"><strong>额外空间关系</strong><span>${escapeHtml(current.spatial_override)}</span></div>` : "";

  box.innerHTML = `
    <div class="world-summary">
      <div><span>角色当前</span><strong>${escapeHtml(placeText(currentPlace))}</strong><div class="tag-row">${placeTags(currentPlace)}</div></div>
      <div><span>角色身份</span><strong>${escapeHtml(lifeProfileText(current.life_profile || {}))}</strong></div>
      <div><span>接下来</span><strong>${escapeHtml(nextText)}</strong></div>
      <div><span>用户位置</span><strong>${escapeHtml(userPlace)}</strong></div>
      <div><span>地点来源</span><strong>${escapeHtml(current.catalog_source || "-")}</strong></div>
      <div><span>天气</span><strong>${escapeHtml(current.weather || world.weather || "未知")}</strong></div>
      <div><span>自然光</span><strong>${escapeHtml(lightText)}</strong></div>
    </div>
    <section class="world-block">
      <h4>空间判断</h4>
      <p>${escapeHtml(current.relation || "暂无判断")}</p>
      ${override}
    </section>
    <section class="world-block">
      <h4>候选地点</h4>
      <div class="chip-row">${renderCandidateChips(current.character_candidates || [])}</div>
    </section>
    <section class="world-block">
      <h4>场景约束</h4>
      <ul class="constraint-list">${constraints || "<li>暂无额外约束</li>"}</ul>
    </section>
    <section class="world-block">
      <h4>今日预览</h4>
      ${renderTimeline(world.timeline || [])}
    </section>
    <section class="world-block">
      <h4>城市地点</h4>
      ${renderCatalog(world.catalog || {})}
    </section>
  `;
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function loadLogs() {
  try {
    const data = await api("/api/logs");
    state.logs = data.logs || [];
    renderLogList(data);
  } catch (err) {
    toast(err.message, "error");
  }
}

function formatNumber(n) {
  return Number(n || 0).toLocaleString("zh-CN");
}

function formatRate(n) {
  if (!n && n !== 0) return "-";
  return `${(Number(n) * 100).toFixed(1)}%`;
}

async function loadUsage() {
  if (state.auth.role !== "admin") {
    $("#usage-table-body").innerHTML = `<tr><td colspan="10" class="empty-cell">需要管理员权限查看用量</td></tr>`;
    return;
  }
  const range = $("#usage-range");
  const seconds = range ? Number(range.value || "86400") : 86400;
  const now = Math.floor(Date.now() / 1000);
  const after = seconds > 0 ? now - seconds : 0;
  try {
    const data = await api(`/api/admin/llm-usage?after=${after}&before=${now}&group_by=profile_id,model,purpose,tag`);
    state.usage = data;
    populateUsageFilters(data.groups || []);
    renderUsage(data);
  } catch (err) {
    toast(err.message, "error");
  }
}

function populateUsageFilters(rows) {
  const map = [
    ["profile_id", "profile", "Profile"],
    ["model", "model", "Model"],
    ["purpose", "purpose", "Purpose"],
    ["tag", "tag", "Tag"],
  ];
  map.forEach(([field, id, label]) => {
    const sel = $(`#usage-filter-${id}`);
    if (!sel) return;
    const current = sel.value;
    const values = new Set(rows.map(r => r[field] || "").filter(Boolean));
    sel.innerHTML = `<option value="">全部 ${label}</option>` +
      Array.from(values).sort().map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
    if (values.has(current)) sel.value = current;
  });
}

function renderUsage(data) {
  const summary = data.summary || {};
  const summaryBox = $("#usage-summary");
  if (summaryBox) {
    summaryBox.innerHTML = `
      <div class="metric"><span>请求数</span><strong>${formatNumber(summary.requests)}</strong></div>
      <div class="metric"><span>Prompt Tokens</span><strong>${formatNumber(summary.prompt_tokens)}</strong></div>
      <div class="metric"><span>Completion Tokens</span><strong>${formatNumber(summary.completion_tokens)}</strong></div>
      <div class="metric"><span>缓存命中</span><strong>${formatNumber(summary.cached_tokens)}</strong></div>
      <div class="metric"><span>缓存命中率</span><strong>${formatRate(summary.cache_hit_rate)}</strong></div>
      <div class="metric"><span>Total Tokens</span><strong>${formatNumber(summary.total_tokens)}</strong></div>
    `;
  }
  const tbody = $("#usage-table-body");
  let rows = (data.groups || []).map(row => ({ ...row, hitRate: row.prompt_tokens ? (row.cached_tokens / row.prompt_tokens) : 0 }));
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-cell">该时间范围内暂无 LLM 调用记录</td></tr>`;
    return;
  }

  const textFilter = (($("#usage-filter-text")?.value || "").toLowerCase().trim());
  const profileFilter = $("#usage-filter-profile")?.value || "";
  const modelFilter = $("#usage-filter-model")?.value || "";
  const purposeFilter = $("#usage-filter-purpose")?.value || "";
  const tagFilter = $("#usage-filter-tag")?.value || "";

  if (textFilter) {
    rows = rows.filter(row =>
      [row.profile_id, row.model, row.purpose, row.tag].some(v => (v || "").toLowerCase().includes(textFilter))
    );
  }
  if (profileFilter) rows = rows.filter(row => row.profile_id === profileFilter);
  if (modelFilter) rows = rows.filter(row => row.model === modelFilter);
  if (purposeFilter) rows = rows.filter(row => row.purpose === purposeFilter);
  if (tagFilter) rows = rows.filter(row => row.tag === tagFilter);

  const sort = $("#usage-sort")?.value || "last_used-desc";
  const [sortKey, sortDir] = sort.split("-");
  rows.sort((a, b) => {
    let va, vb;
    if (sortKey === "cache_hit_rate") {
      va = a.hitRate;
      vb = b.hitRate;
    } else if (sortKey === "last_used" || sortKey === "first_used") {
      va = a[sortKey] || 0;
      vb = b[sortKey] || 0;
    } else {
      va = a[sortKey] ?? 0;
      vb = b[sortKey] ?? 0;
    }
    return sortDir === "asc" ? va - vb : vb - va;
  });

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-cell">没有符合筛选条件的记录</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(row => `
    <tr>
      <td>${escapeHtml(row.profile_id || "-")}</td>
      <td>${escapeHtml(row.model || "-")}</td>
      <td>${escapeHtml(row.purpose || "-")}</td>
      <td>${escapeHtml(row.tag || "-")}</td>
      <td>${formatNumber(row.requests)}</td>
      <td>${formatNumber(row.prompt_tokens)}</td>
      <td>${formatNumber(row.completion_tokens)}</td>
      <td>${formatNumber(row.cached_tokens)}</td>
      <td>${formatRate(row.hitRate)}</td>
      <td>${formatNumber(row.total_tokens)}</td>
    </tr>
  `).join("");
}

function renderLogList(meta) {
  const list = $("#log-list");
  list.innerHTML = "";
  if (meta && meta.enabled === false) {
    list.innerHTML = `<div class="empty-state">分用户日志已关闭，可在「设置 → 推送与本地控制台」开启。</div>`;
    return;
  }
  if (!state.logs.length) {
    list.innerHTML = `<div class="empty-state">暂无日志。用户与机器人交互后会自动生成。</div>`;
    return;
  }
  state.logs.forEach(item => {
    const btn = document.createElement("button");
    btn.className = "session-item";
    btn.dataset.chat = item.chat_id;
    btn.innerHTML = `<div class="session-title">${item.character || item.chat_id}</div><div class="session-meta">${item.chat_id} · ${item.mtime_ago} · ${formatBytes(item.size)}</div>`;
    btn.onclick = () => selectLog(item.chat_id);
    list.appendChild(btn);
  });
  if (state.selectedLog) {
    $all("#log-list .session-item").forEach(btn => btn.classList.toggle("active", btn.dataset.chat === state.selectedLog));
  }
}

async function selectLog(chatId) {
  state.selectedLog = chatId;
  $("#log-title").textContent = `日志 · ${chatId}`;
  $all("#log-list .session-item").forEach(btn => btn.classList.toggle("active", btn.dataset.chat === chatId));
  try {
    const data = await api(`/api/logs/${encodeURIComponent(chatId)}?tail=1000`);
    const box = $("#log-content");
    box.textContent = data.content || "（空）";
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    $("#log-content").textContent = err.message;
  }
}

function fillCommandSelect() {
  const sel = document.querySelector("#command-form select[name=command]");
  sel.innerHTML = commands.map(cmd => `<option value="${cmd}">/${cmd}</option>`).join("");
  sel.onchange = updateCommandHelp;
  updateCommandHelp();
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
  $("#reload-logs").onclick = () => loadLogs().then(() => toast("日志列表已刷新"));
  $("#log-refresh").onclick = () => { if (state.selectedLog) selectLog(state.selectedLog); };
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
      if (state.memoryDiaryTab === "memory") await loadMemories();
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
      const charKey = state.selectedCharacter || "";
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
      const mode = btn.dataset.mode;
      const chatId = state.selectedSession.replace("telegram:", "");
      if (!window.confirm(`确定触发 ${mode} 模式测试推送吗？`)) return;
      setBusy(btn, true);
      try {
        await api("/api/actions/run-command", {
          method: "POST",
          body: { chat_id: chatId, command: "测试推送", arg: mode },
        });
        toast(`${mode} 推送已触发`);
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
  $all(".panel-head.collapsible").forEach(head => {
    head.onclick = () => {
      const body = head.nextElementSibling;
      if (body) body.classList.toggle("collapsed");
      head.classList.toggle("collapsed");
    };
  });

  $("#prompt-cleanup-preview").onclick = (event) => runPromptCleanup(false, event.currentTarget);
  $("#prompt-cleanup-apply").onclick = (event) => runPromptCleanup(true, event.currentTarget);

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
    out.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    out.textContent = err.message;
  }
}

fillCommandSelect();
initEvents();
loadAll().catch(err => toast(err.message, "error"));
