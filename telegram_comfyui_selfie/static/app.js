const state = {
  status: null,
  config: null,
  secretPresent: {},
  sessions: [],
  selectedSession: null,
};

const viewMeta = {
  overview: ["总览", "服务状态、连接测试和快捷入口"],
  settings: ["设置", "连接、模型、生图和推送参数"],
  sessions: ["会话", "每个 Telegram 会话的角色与推送状态"],
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
    ["default_purity", "默认纯良度", "text"],
    ["allow_llm_change_appearance", "允许模型改外型", "bool"],
    ["long_memory_enabled", "启用长期记忆注入", "bool"],
    ["long_memory_extract_enabled", "自动提取长期记忆", "bool"],
    ["long_memory_context_limit", "长期记忆注入条数", "number"],
    ["long_memory_db_path", "长期记忆数据库", "text"],
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
  ["custom_scheduled_persona", "会话人格", "textarea"],
  ["custom_positive_prefix", "身体特征", "textarea"],
  ["dynamic_appearance", "临时外型", "textarea"],
  ["custom_role_name", "角色类型", "text"],
  ["custom_bot_name", "角色名", "text"],
  ["custom_bot_self_name", "自称", "text"],
  ["custom_spatial_relationship", "空间关系", "textarea"],
  ["custom_location", "城市", "text"],
  ["custom_timezone_offset", "时区偏移", "text"],
  ["custom_current_style", "画风", "text"],
  ["custom_daily_selfie_limit", "每日推送覆盖", "text"],
  ["purity", "纯良度", "number"],
  ["custom_allow_llm_change_appearance", "模型改外型", "select:,true,false"],
];

const commands = ["菜单", "自拍", "天气", "天气设置", "画风", "角色", "外型", "人格", "纯良度", "新场景", "记忆", "记住", "忘记", "推送频率", "调度", "测试推送", "测试生图", "提示词", "生图状态", "管理", "turbo"];

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

function switchView(name) {
  $all(".nav").forEach(btn => btn.classList.toggle("active", btn.dataset.view === name));
  $all(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  $("#view-title").textContent = viewMeta[name][0];
  $("#view-subtitle").textContent = viewMeta[name][1];
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
  $("#bot-name").textContent = s.bot_username ? `@${s.bot_username}` : (s.token_configured ? "Token 已填写" : "Token 未填写");
  $("#status-web-url").textContent = s.web_url;
  $("#status-config-path").textContent = s.config_path;
  $("#status-state-path").textContent = s.state_path;
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

function fillCommandSelect() {
  const sel = document.querySelector("#command-form select[name=command]");
  sel.innerHTML = commands.map(cmd => `<option value="${cmd}">/${cmd}</option>`).join("");
}

async function initEvents() {
  $all(".nav").forEach(btn => btn.onclick = () => switchView(btn.dataset.view));
  $("#refresh-btn").onclick = () => loadAll().then(() => toast("已刷新"));
  $("#reload-sessions").onclick = () => loadAll().then(() => toast("会话已刷新"));

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
