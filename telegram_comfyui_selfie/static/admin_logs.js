"use strict";

// 日志浏览与 LLM 用量统计页面。依赖 app.js 提供的 state、api、DOM 与 toast 基础设施。
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
  // 添加系统日志分类
  const systemLogs = [
    { id: "llm-debug", name: "LLM 调试日志", desc: "查看 LLM 请求/响应详情" },
    { id: "system-errors", name: "错误日志", desc: "查看系统错误信息" },
  ];
  (state.auth?.role === "admin" ? systemLogs : []).forEach(item => {
    const btn = document.createElement("button");
    btn.className = "session-item";
    btn.dataset.logType = item.id;
    btn.innerHTML = `<div class="session-title">${item.name}</div><div class="session-meta">${item.desc}</div>`;
    btn.onclick = () => selectSystemLog(item.id);
    list.appendChild(btn);
  });
  if (!state.logs.length) {
    list.innerHTML += `<div class="empty-state">暂无用户日志。用户与机器人交互后会自动生成。</div>`;
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

function formatLogChunkOption(chunk) {
  const size = chunk.size ? ` · ${formatBytes(chunk.size)}` : "";
  const ago = chunk.mtime_ago ? ` · ${chunk.mtime_ago}` : "";
  return `${chunk.label || chunk.name}${size}${ago}`;
}

function renderLogChunkSelector(chunks = [], activeChunk = "", onChange = null) {
  const bar = $("#log-chunk-bar");
  const select = $("#log-chunk-select");
  if (!bar || !select) return;
  if (!chunks.length) {
    bar.hidden = true;
    select.innerHTML = "";
    return;
  }
  bar.hidden = false;
  select.innerHTML = chunks.map(chunk => (
    `<option value="${escapeHtml(chunk.name || "")}">${escapeHtml(formatLogChunkOption(chunk))}</option>`
  )).join("");
  select.value = activeChunk || (chunks[0]?.name || "");
  select.onchange = () => {
    state.selectedLogChunk = select.value;
    if (onChange) onChange(select.value);
  };
}

function hideLogChunkSelector() {
  const bar = $("#log-chunk-bar");
  const select = $("#log-chunk-select");
  if (bar) bar.hidden = true;
  if (select) {
    select.innerHTML = "";
    select.onchange = null;
  }
  state.selectedLogChunk = "";
}

function hideLogPageControls() {
  const bar = $("#log-page-bar");
  if (bar) bar.hidden = true;
}

function renderLlmDebugPageControls(data, chunk) {
  const bar = $("#log-page-bar");
  const newer = $("#log-page-newer");
  const older = $("#log-page-older");
  const status = $("#log-page-status");
  if (!bar || !newer || !older || !status) return;
  bar.hidden = false;
  const stack = Array.isArray(state.llmDebugCursorStack) ? state.llmDebugCursorStack : [null];
  state.llmDebugCursorStack = stack.length ? stack : [null];
  newer.disabled = state.llmDebugCursorStack.length <= 1;
  older.disabled = !data.has_more || data.next_before === null || data.next_before === undefined;
  const page = state.llmDebugCursorStack.length;
  status.textContent = `第 ${page} 页 · 每页最多 ${data.limit || state.logTail} 条`;
  newer.onclick = () => {
    if (state.llmDebugCursorStack.length <= 1) return;
    state.llmDebugCursorStack.pop();
    const before = state.llmDebugCursorStack[state.llmDebugCursorStack.length - 1];
    selectSystemLog("llm-debug", chunk, before, true);
  };
  older.onclick = () => {
    if (older.disabled) return;
    state.llmDebugCursorStack.push(data.next_before);
    selectSystemLog("llm-debug", chunk, data.next_before, true);
  };
}

function renderFilteredLog(content) {
  state.logRawContent = String(content || "");
  const needle = ($("#log-filter")?.value || "").trim().toLowerCase();
  const shown = needle
    ? state.logRawContent.split(/\r?\n/).filter(line => line.toLowerCase().includes(needle)).join("\n")
    : state.logRawContent;
  $("#log-content").textContent = shown || (needle ? "没有匹配内容" : "（空）");
}

async function selectLog(chatId, chunk = null) {
  const sameLog = state.selectedLog === chatId && !state.selectedLogType;
  state.selectedLog = chatId;
  state.selectedLogType = null;
  hideLogPageControls();
  const chosenChunk = chunk === null ? (sameLog ? state.selectedLogChunk : "") : chunk;
  $("#log-title").textContent = `日志 · ${chatId}`;
  $all("#log-list .session-item").forEach(btn => btn.classList.toggle("active", btn.dataset.chat === chatId));
  const box = $("#log-content");
  box.textContent = "加载中...";
  try {
    const suffix = chosenChunk ? `&chunk=${encodeURIComponent(chosenChunk)}` : "";
    const data = await api(`/api/logs/${encodeURIComponent(chatId)}?tail=${state.logTail}${suffix}`);
    state.selectedLogChunk = data.chunk || chosenChunk || "";
    renderLogChunkSelector(data.chunks || [], state.selectedLogChunk, nextChunk => selectLog(chatId, nextChunk));
    renderFilteredLog(data.content || "");
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    hideLogChunkSelector();
    box.textContent = err.message;
  }
}

async function selectSystemLog(logType, chunk = null, before = null, keepPage = false) {
  const sameLog = state.selectedLogType === logType && !state.selectedLog;
  state.selectedLog = null;
  state.selectedLogType = logType;
  const chosenChunk = chunk === null ? (sameLog ? state.selectedLogChunk : "") : chunk;
  $("#log-title").textContent = logType === "llm-debug" ? "LLM 调试日志" : "错误日志";
  $all("#log-list .session-item").forEach(btn => btn.classList.toggle("active", btn.dataset.logType === logType));
  const box = $("#log-content");
  box.textContent = "加载中...";
  try {
    if (logType === "llm-debug") {
      if (!keepPage) state.llmDebugCursorStack = [null];
      const params = new URLSearchParams({ limit: String(Math.min(1000, state.logTail || 200)) });
      if (chosenChunk) params.set("chunk", chosenChunk);
      if (before !== null && before !== undefined) params.set("before", String(before));
      const data = await api(`/api/logs/llm-debug?${params.toString()}`);
      state.selectedLogChunk = data.chunk || chosenChunk || "";
      renderLogChunkSelector(data.chunks || [], state.selectedLogChunk, nextChunk => selectSystemLog(logType, nextChunk));
      renderLlmDebugPageControls(data, state.selectedLogChunk);
      const content = data.content || {};
      let text = "";
      Object.keys(content).forEach(key => {
        text += `=== ${key} ===\n`;
        (content[key] || []).forEach(entry => {
          text += `[${entry.time}] ${entry.model} (status: ${entry.status})\n`;
          if (entry.error) text += `  错误: ${entry.error}\n`;
          text += `  会话: ${entry.session_id || "-"}\n`;
          text += `  Profile: ${entry.profile_id || "-"}\n`;
          text += `  思考模式: ${entry.thinking ? "开" : "关"}\n`;
          text += `  请求: ${entry.request?.url}\n`;
          if (entry.request?.body?.messages) {
            const messages = entry.request.body.messages;
            text += `  消息数: ${messages.length}\n`;
          }
          if (entry.usage) {
            const u = entry.usage;
            text += `  --- Token 用量 ---\n`;
            text += `  Prompt Tokens: ${u.prompt_tokens || 0}\n`;
            text += `  Completion Tokens: ${u.completion_tokens || 0}\n`;
            text += `  Total Tokens: ${u.total_tokens || 0}\n`;
            text += `  缓存命中: ${u.cached_tokens || 0}\n`;
            text += `  缓存未命中: ${u.cache_miss_tokens || 0}\n`;
            text += `  缓存命中率: ${((u.cache_hit_rate || 0) * 100).toFixed(1)}%\n`;
          }
          text += "\n";
        });
      });
      renderFilteredLog(text || "暂无 LLM 调试日志");
    } else if (logType === "system-errors") {
      hideLogChunkSelector();
      hideLogPageControls();
      const data = await api("/api/logs/system-errors?limit=500");
      const errors = data.errors || [];
      if (errors.length === 0) {
        renderFilteredLog("暂无错误日志");
      } else {
        renderFilteredLog(errors.map(formatSystemErrorEntry).join("\n\n---\n\n"));
      }
    }
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    hideLogChunkSelector();
    hideLogPageControls();
    box.textContent = `加载失败: ${err.message}`;
  }
}

function prettyJson(value) {
  if (value === undefined || value === null || value === "") return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch (err) {
    return String(value);
  }
}

function formatSystemErrorEntry(entry) {
  const header = `[${entry.time || "-"}] [${entry.file}:${entry.line_no || "-"}] ${entry.kind || entry.tag || "ERROR"}`;
  const lines = [header];
  if (entry.session_id) lines.push(`会话: ${entry.session_id}`);
  const message = entry.message || entry.line || "";
  if (entry.error) lines.push(`错误: ${entry.error}`);
  if (entry.kind && message) {
    const brief = message.split(entry.kind, 1)[0].trim();
    if (brief) lines.push(brief);
  } else if (message) {
    lines.push(message);
  }
  if (entry.payload) {
    const request = entry.request !== undefined ? entry.request : entry.payload.request;
    const response = entry.response !== undefined ? entry.response : entry.payload.response;
    if (request !== undefined) lines.push(`请求:\n${prettyJson(request)}`);
    if (response !== undefined) lines.push(`返回:\n${prettyJson(response)}`);
    if (request === undefined && response === undefined) {
      lines.push(`详情:\n${prettyJson(entry.payload)}`);
    } else {
      const rest = { ...entry.payload };
      delete rest.request;
      delete rest.response;
      if (Object.keys(rest).length) lines.push(`摘要:\n${prettyJson(rest)}`);
    }
  } else if (entry.line && entry.line !== message) {
    lines.push(`原始行: ${entry.line}`);
  }
  return lines.join("\n");
}
