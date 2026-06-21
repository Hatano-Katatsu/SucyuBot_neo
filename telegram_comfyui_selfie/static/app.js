const state = {
  status: null,
  config: null,
  secretPresent: {},
  sessions: [],
  selectedSession: null,
  selectedWorldSession: null,
  worldPreview: null,
  promptSlots: null,
  logs: [],
  selectedLog: null,
};

const viewMeta = {
  overview: ["总览", "服务状态、连接测试和快捷入口"],
  settings: ["设置", "连接、模型、生图和推送参数"],
  sessions: ["会话", "每个 Telegram 会话的角色与推送状态"],
  world: ["动线", "按用户查看角色每日动线、城市地点和用户位置"],
  logs: ["日志", "按用户查看活动日志"],
  actions: ["操作", "向指定 Chat ID 发送命令或文字"],
};

const configSections = [
  ["连接", [
    ["telegram_bot_token", "Telegram Bot Token", "secret"],
    ["allowed_chat_ids", "允许的 Chat ID", "list"],
    ["comfyui_url", "ComfyUI 地址", "text"],
  ]],
  ["聊天与角色扮演模型（回复用户、保持人设、决定何时调用发图工具）", [
    ["chat_llm_api_base", "API Base", "text"],
    ["chat_llm_api_key", "API Key", "secret"],
    ["chat_llm_model", "模型名", "text"],
    ["chat_llm_temperature", "回复温度", "text"],
    ["chat_reply_length", "回复长度", "select:,简短,适中,详细"],
    ["chat_llm_max_tokens", "Max Tokens", "number"],
    ["chat_llm_disable_thinking", "关闭 Thinking", "bool"],
  ]],
  ["生图辅助模型（写推送场景、翻译 tags、分析角色和外型、识别时区）", [
    ["image_llm_api_base", "API Base", "text"],
    ["image_llm_api_key", "API Key", "secret"],
    ["image_llm_model", "模型名", "text"],
    ["image_llm_max_tokens", "Max Tokens", "number"],
    ["image_llm_disable_thinking", "关闭 Thinking", "bool"],
    ["image_llm_temperature_scene", "推送场景温度", "text"],
    ["image_llm_temperature_translate", "Tags 翻译温度", "text"],
    ["image_llm_temperature_classify", "角色分析温度", "text"],
  ]],
  ["通用模型兜底（可选；上面对应项目留空时使用）", [
    ["llm_api_base", "API Base", "text"],
    ["llm_api_key", "API Key", "secret"],
    ["llm_model", "模型名", "text"],
    ["llm_max_tokens", "Max Tokens", "number"],
    ["llm_disable_thinking", "关闭 Thinking", "bool"],
    ["llm_temperature_scene", "默认场景温度", "text"],
    ["llm_temperature_translate", "默认翻译温度", "text"],
    ["llm_temperature_classify", "默认分析温度", "text"],
  ]],
  ["角色", [
    ["role_name", "角色类型", "text"],
    ["bot_name", "角色名", "text"],
    ["bot_self_name", "自称", "text"],
    ["scheduled_persona", "基础人格", "textarea"],
    ["positive_prefix", "身体特征 Prompt", "textarea"],
    ["negative_prompt", "Negative Prompt", "textarea"],
    ["default_hair", "默认发色", "text"],
    ["default_eyes", "默认瞳色", "text"],
    ["dynamic_appearance", "全局附加外型", "textarea"],
    ["character_quirk_rule", "角色专属规则", "textarea"],
    ["spatial_relationship", "空间关系", "textarea"],
  ]],
  ["生图", [
    ["style_pool", "画风池", "textarea"],
    ["current_style", "全局当前画风", "text"],
    ["width", "宽度", "number"],
    ["height", "高度", "number"],
    ["steps", "步数", "number"],
    ["cfg", "CFG", "text"],
    ["sampler", "Sampler", "text"],
    ["scheduler", "Scheduler", "text"],
    ["turbo_mode", "Turbo", "bool"],
    ["turbo_strength", "Turbo 强度", "text"],
    ["unet_model", "UNet 模型", "text"],
    ["clip_model", "CLIP 模型", "text"],
    ["vae_model", "VAE 模型", "text"],
    ["turbo_lora_model", "Turbo LoRA", "text"],
    ["comfyui_workflow_file", "自定义工作流文件", "text"],
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
    ["world_user_place_ttl_hours", "用户地点记忆小时", "number"],
    ["world_holiday_dates", "节假日日期 YYYY-MM-DD", "textarea"],
    ["world_workday_dates", "调休工作日 YYYY-MM-DD", "textarea"],
    ["default_purity", "默认纯良度", "text"],
    ["allow_llm_change_appearance", "允许模型改外型", "bool"],
    ["long_memory_enabled", "启用长期记忆注入", "bool"],
    ["long_memory_extract_enabled", "自动提取长期记忆", "bool"],
    ["long_memory_context_limit", "长期记忆注入条数", "number"],
    ["long_memory_db_path", "长期记忆数据库", "text"],
    ["user_log_enabled", "分用户活动日志", "bool"],
    ["user_log_dir", "日志目录（留空=data/logs）", "text"],
    ["short_context_history_limit", "短期场景历史条数", "number"],
    ["short_context_reset_gap_hours", "短期场景超时小时", "text"],
    ["web_enabled", "启用控制台", "bool"],
    ["web_host", "控制台 Host", "text"],
    ["web_port", "控制台 Port", "number"],
  ]],
];

const sessionFields = [
  ["custom_character", "角色", "text"],
  ["custom_series", "作品/系列", "text"],
  ["custom_visual_character", "生图角色Tag", "text"],
  ["custom_visual_series", "生图作品Tag", "text"],
  ["custom_scheduled_persona", "会话人格", "textarea"],
  ["custom_count", "人数", "text"],
  ["custom_positive_prefix", "身体特征", "textarea"],
  ["dynamic_appearance", "临时外型", "textarea"],
  ["custom_scene_preference", "场景偏好", "textarea"],
  ["custom_selfie_preference", "自拍偏好", "textarea"],
  ["custom_role_name", "角色类型", "text"],
  ["custom_bot_name", "角色名", "text"],
  ["custom_bot_self_name", "自称", "text"],
  ["custom_spatial_relationship", "空间关系", "textarea"],
  ["custom_location", "城市", "text"],
  ["custom_timezone_offset", "时区偏移", "text"],
  ["custom_character_age_stage", "年龄段覆盖", "select:,minor,adult"],
  ["custom_character_day_anchor", "白天去向覆盖", "select:,company,school,factory,farm,construction,medical,retail,delivery,driver,home,flexible"],
  ["custom_current_style", "画风", "text"],
  ["custom_daily_selfie_limit", "每日推送覆盖", "text"],
  ["purity", "纯良度", "number"],
  ["custom_allow_llm_change_appearance", "模型改外型", "select:,true,false"],
];

const promptSlotFields = [
  ["custom_count", "人数 (1girl/1boy)", "text"],
  ["custom_positive_prefix", "基础外观", "textarea"],
  ["custom_default_hair", "默认发色/发型", "text"],
  ["custom_default_eyes", "默认瞳色", "text"],
  ["custom_current_style", "画风", "text"],
  ["dynamic_appearance", "临时穿搭/配饰", "textarea"],
  ["custom_scene_preference", "场景偏好", "textarea"],
  ["custom_selfie_preference", "自拍偏好", "textarea"],
];

const commands = ["初始化", "菜单", "帮助", "创建OC", "自拍", "天气", "天气设置", "画风", "角色", "外型", "人格", "纯良度", "新场景", "记忆", "记住", "忘记", "推送频率", "调度", "测试推送", "测试生图", "提示词", "生图状态", "管理", "turbo"];
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
  "纯良度": ["查看或设置角色边界；数字越高越保守。", "0~10 / auto"],
  "新场景": ["开启新的短期场景，避免上一轮话题继续串进来。", ""],
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
}

async function loadAll() {
  const [status, config, sessions] = await Promise.all([
    api("/api/status"),
    api("/api/config"),
    api("/api/sessions"),
  ]);
  state.status = status.status;
  state.config = config.config.values;
  state.secretPresent = config.config.secret_present || {};
  state.sessions = sessions.sessions || [];
  renderStatus();
  renderConfig();
  renderSessions();
  renderWorldSessionList();
}

function renderStatus() {
  const s = state.status;
  $("#metric-bot").textContent = s.bot_running ? "运行中" : "未启动";
  $("#metric-sessions").textContent = String(s.sessions_count);
  $("#metric-generate").textContent = s.generating ? "生成中" : "空闲";
  const llmReady = Number(Boolean(s.chat_llm_configured)) + Number(Boolean(s.image_llm_configured));
  $("#metric-llm").textContent = `${llmReady}/2 已配置`;
  $("#bot-toggle-btn").textContent = s.bot_running ? "停止机器人" : "启动机器人";
  $("#bot-toggle-btn").classList.toggle("danger", s.bot_running);
  $("#bot-toggle-btn").classList.toggle("primary", !s.bot_running);
  $("#service-start-btn").textContent = s.bot_running ? "服务已启动" : "一键启动";
  $("#service-start-btn").disabled = Boolean(s.bot_running);
  $("#bot-name").textContent = s.bot_username ? `@${s.bot_username}` : (s.token_configured ? "Token 已填写" : "Token 未填写");
  $("#status-web-url").textContent = s.web_url;
  $("#status-config-path").textContent = s.config_path;
  $("#status-state-path").textContent = s.state_path;
  $("#status-process").textContent = s.process_id ? `PID ${s.process_id}` : "-";
  $("#status-launch-script").textContent = s.launch_script || "-";
  $("#status-comfyui").textContent = s.comfyui_url || "-";
  $("#status-chat-llm-model").textContent = s.chat_llm_model ? `${s.chat_llm_model} @ ${s.chat_llm_api_base}` : "-";
  $("#status-image-llm-model").textContent = s.image_llm_model ? `${s.image_llm_model} @ ${s.image_llm_api_base}` : "-";
}

function inputFor([key, label, type], values) {
  const wrap = document.createElement("label");
  wrap.textContent = label;
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
  } else if (type.startsWith("select:")) {
    input = document.createElement("select");
    const options = type.slice(7).split(",");
    input.innerHTML = options.map(opt => `<option value="${opt}">${opt || "默认"}</option>`).join("");
    input.value = value ?? "";
  } else {
    input = document.createElement("input");
    input.type = type === "secret" ? "password" : type;
    input.value = type === "secret" ? "" : (value ?? "");
    if (type === "secret" && state.secretPresent[key]) input.placeholder = "已保存；留空不修改";
  }
  input.name = key;
  wrap.appendChild(input);
  return wrap;
}

function renderConfig() {
  const form = $("#config-form");
  form.innerHTML = "";
  for (const [title, fields] of configSections) {
    const section = document.createElement("section");
    section.className = "form-section";
    section.innerHTML = `<h3>${title}</h3>`;
    const grid = document.createElement("div");
    grid.className = "field-grid";
    fields.forEach(field => grid.appendChild(inputFor(field, state.config || {})));
    section.appendChild(grid);
    form.appendChild(section);
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

function renderSessions() {
  const list = $("#session-list");
  list.innerHTML = "";
  if (!state.sessions.length) {
    list.innerHTML = `<div class="empty-state">暂无会话。机器人收到 Telegram 消息后会自动出现。</div>`;
  } else {
    state.sessions.forEach(item => {
      const btn = document.createElement("button");
      btn.className = "session-item";
      btn.dataset.sid = item.session_id;
      btn.innerHTML = `<div class="session-title">${item.character || item.chat_id}</div><div class="session-meta">${item.last_interaction_ago} · 纯良度 ${item.purity}/10 · 推送 ${item.daily_push}</div>`;
      btn.onclick = () => selectSession(item.session_id);
      list.appendChild(btn);
    });
  }
  if (state.selectedSession) {
    $all(".session-item").forEach(btn => btn.classList.toggle("active", btn.dataset.sid === state.selectedSession));
  }
}

async function selectSession(sessionId) {
  state.selectedSession = sessionId;
  const data = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
  renderSessionForm(data.state, data.session);
  renderSessions();
  await loadPromptSlots();
}

function renderSessionForm(sessionState, summary) {
  $("#selected-session-label").textContent = summary.session_id;
  const form = $("#session-form");
  form.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "field-grid";
  const values = { ...sessionState, purity: sessionState.purity ?? "", custom_allow_llm_change_appearance: sessionState.custom_allow_llm_change_appearance ?? "" };
  sessionFields.forEach(field => grid.appendChild(inputFor(field, values)));
  form.appendChild(grid);
  const actions = document.createElement("div");
  actions.className = "form-actions";
  actions.innerHTML = `<button type="button" class="danger" id="delete-session">删除会话</button><button class="primary" type="submit">保存会话</button>`;
  form.appendChild(actions);
  $("#delete-session").onclick = async () => {
    await api(`/api/sessions/${encodeURIComponent(state.selectedSession)}`, { method: "DELETE" });
    state.selectedSession = null;
    $("#session-form").innerHTML = "";
    $("#selected-session-label").textContent = "未选择";
    await loadAll();
    toast("会话已删除");
  };
}

function renderPromptSlotEditor(prompt) {
  const box = $("#prompt-slot-content");
  if (!state.selectedSession) {
    box.innerHTML = `<div class="empty-state">请先从上方选择一个会话。</div>`;
    return;
  }
  const editable = prompt?.editable || {};
  const effective = prompt?.effective || {};
  const items = prompt?.items || [];
  const notes = (prompt?.notes || []).map(note => `<li>${escapeHtml(note)}</li>`).join("");
  const previewScene = prompt?.scene || "{场景描述}";
  const slotRows = items.map(item => `
    <div class="slot-row">
      <strong>${escapeHtml(item.key)}</strong>
      <span>${escapeHtml(item.value || "（空）")}</span>
    </div>
  `).join("");
  const WARDROBE_SLOT_LABELS = {
    hair: "发型/发色", eyes: "瞳色", dress: "连衣裙", top: "上衣", bottom: "下装",
    outerwear: "外套", bra: "胸衣", panties: "内裤", legwear: "袜", footwear: "鞋",
    accessory: "配饰", other: "其它",
  };
  const wardrobe = prompt?.wardrobe || {};
  const wardrobeRows = Object.keys(WARDROBE_SLOT_LABELS)
    .filter(slot => (wardrobe[slot] || "").trim())
    .map(slot => `
    <div class="slot-row">
      <strong>${escapeHtml(WARDROBE_SLOT_LABELS[slot])}</strong>
      <span>${escapeHtml(wardrobe[slot])}</span>
    </div>
  `).join("");
  const closet = prompt?.closet || {};
  const closetRows = Object.keys(closet).map(name => `
    <div class="slot-row">
      <strong>${escapeHtml(name)}</strong>
      <span>${escapeHtml(closet[name]?.slot || "")} · ${escapeHtml(closet[name]?.tags || "")}</span>
    </div>
  `).join("");

  box.innerHTML = `
    <form id="prompt-slot-form" class="prompt-slot-form">
      <div class="slot-edit-grid"></div>
      <label class="preview-scene">预览场景<input name="preview_scene" value="${escapeHtml(previewScene)}"></label>
      <div class="effective-grid">
        <div><span>实际基础外观</span><strong>${escapeHtml(effective.positive_prefix || "（空）")}</strong></div>
        <div><span>实际画风</span><strong>${escapeHtml(effective.current_style || "（空）")}</strong></div>
        <div><span>实际默认发型</span><strong>${escapeHtml(effective.default_hair || "（空）")}</strong></div>
        <div><span>实际默认瞳色</span><strong>${escapeHtml(effective.default_eyes || "（空）")}</strong></div>
        <div><span>场景偏好</span><strong>${escapeHtml(effective.scene_preference || "（空）")}</strong></div>
        <div><span>自拍偏好</span><strong>${escapeHtml(effective.selfie_preference || "（空）")}</strong></div>
      </div>
      <div class="form-actions">
        <button type="button" id="prompt-slot-preview">刷新槽位预览</button>
        <button class="primary" type="submit">保存槽位</button>
      </div>
    </form>
    <div class="prompt-slot-preview">
      <section>
        <h4>当前衣柜（按槽位 · 只读）</h4>
        <div class="slot-list">${wardrobeRows || `<div class="empty-state">衣柜为空（无附加外型）。</div>`}</div>
      </section>
      <section>
        <h4>衣橱收藏（穿过的衣服 · 只读）</h4>
        <div class="slot-list">${closetRows || `<div class="empty-state">衣橱为空。</div>`}</div>
      </section>
      <section>
        <h4>槽位预览</h4>
        <div class="slot-list">${slotRows || `<div class="empty-state">暂无槽位。</div>`}</div>
      </section>
      <section>
        <h4>编辑说明</h4>
        <ul class="constraint-list">${notes}</ul>
      </section>
    </div>
  `;
  const grid = box.querySelector(".slot-edit-grid");
  promptSlotFields.forEach(field => grid.appendChild(inputFor(field, editable)));
  $("#prompt-slot-preview").onclick = () => loadPromptSlots();
  $("#prompt-slot-form").onsubmit = async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true);
    try {
      const values = formValues(event.currentTarget);
      delete values.preview_scene;
      await api(`/api/sessions/${encodeURIComponent(state.selectedSession)}`, { method: "PATCH", body: values });
      await selectSession(state.selectedSession);
      await loadAll();
      toast("Prompt 槽位已保存");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
}

async function loadPromptSlots() {
  if (!state.selectedSession) {
    renderPromptSlotEditor(null);
    return;
  }
  const sceneInput = document.querySelector("#prompt-slot-form input[name=preview_scene]");
  const scene = sceneInput ? sceneInput.value : "{场景描述}";
  try {
    const sid = encodeURIComponent(state.selectedSession);
    const data = await api(`/api/prompt-slots/${sid}?scene=${encodeURIComponent(scene)}`);
    state.promptSlots = data.prompt;
    renderPromptSlotEditor(data.prompt);
  } catch (err) {
    $("#prompt-slot-content").innerHTML = `<div class="empty-state">${escapeHtml(err.message)}</div>`;
    toast(err.message, "error");
  }
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
    btn.className = "session-item";
    btn.dataset.sid = item.session_id;
    btn.innerHTML = `<div class="session-title">${escapeHtml(item.character || item.chat_id)}</div><div class="session-meta">${escapeHtml(item.location || "未设置城市")} · UTC${escapeHtml(item.timezone || "-")} · 推送 ${escapeHtml(item.daily_push || "-")}</div>`;
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
  $("#reload-sessions").onclick = () => loadAll().then(() => toast("会话已刷新"));
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
  $("#log-clear").onclick = async () => {
    if (!state.selectedLog) return;
    try {
      await api(`/api/logs/${encodeURIComponent(state.selectedLog)}`, { method: "DELETE" });
      $("#log-content").textContent = "（已清空）";
      toast("日志已清空");
      await loadLogs();
    } catch (err) {
      toast(err.message, "error");
    }
  };

  $("#service-start-btn").onclick = async (event) => {
    const btn = event.currentTarget;
    setBusy(btn, true);
    setBusy($("#bot-toggle-btn"), true);
    try {
      if (!state.status?.bot_running) {
        await api("/api/bot/start", { method: "POST" });
      }
      await loadAll();
      toast("服务已启动");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
      setBusy($("#bot-toggle-btn"), false);
      if (state.status?.bot_running) btn.disabled = true;
    }
  };

  $("#service-restart-btn").onclick = async (event) => {
    if (!window.confirm("这会重启整个 Python 服务进程，当前 Web 控制台会短暂断开。继续吗？")) return;
    const btn = event.currentTarget;
    const oldPid = state.status?.process_id;
    setBusy(btn, true);
    setBusy($("#bot-toggle-btn"), true);
    try {
      const data = await api("/api/service/restart", { method: "POST" });
      const restart = data.restart || {};
      toast("正在重启服务，控制台会自动重新连接...");
      await waitForServiceRestart(restart.old_pid || oldPid);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
      setBusy($("#bot-toggle-btn"), false);
    }
  };

  $("#bot-toggle-btn").onclick = async (event) => {
    const btn = event.currentTarget;
    setBusy(btn, true);
    try {
      if (state.status?.bot_running) {
        await api("/api/bot/stop", { method: "POST" });
        toast("机器人已停止");
      } else {
        await api("/api/bot/start", { method: "POST" });
        toast("机器人已启动");
      }
      await loadAll();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
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

  $("#session-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!state.selectedSession) return;
    const btn = event.submitter;
    setBusy(btn, true);
    try {
      await api(`/api/sessions/${encodeURIComponent(state.selectedSession)}`, { method: "PATCH", body: formValues(event.currentTarget) });
      await selectSession(state.selectedSession);
      await loadAll();
      toast("会话已保存");
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

  $("#prompt-cleanup-preview").onclick = (event) => runPromptCleanup(false, event.currentTarget);
  $("#prompt-cleanup-apply").onclick = (event) => runPromptCleanup(true, event.currentTarget);
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
