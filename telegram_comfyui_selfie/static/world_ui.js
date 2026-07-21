"use strict";

// 实时动线、城市地点与角色生活线页面。依赖 app.js 的 state、api 与通用 DOM helper。
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
    const row = document.createElement("div");
    row.className = "session-item world-session-item" + (item.frozen ? " frozen" : "");
    row.dataset.sid = item.session_id;
    const frozenBadge = item.frozen ? ' <span class="frozen-badge">已冻结</span>' : "";
    const select = document.createElement("button");
    select.type = "button";
    select.className = "world-session-select";
    select.innerHTML = `<span class="session-title">${escapeHtml(item.character || item.chat_id)}${frozenBadge}</span><span class="session-meta">${escapeHtml(item.location || "未设置城市")} · UTC${escapeHtml(item.timezone || "-")} · 推送 ${escapeHtml(item.daily_push || "-")}</span>`;
    select.onclick = () => selectWorldSession(item.session_id);
    select.setAttribute("aria-label", `查看 ${item.character || item.chat_id || item.session_id} 的动线`);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "session-freeze-toggle";
    toggle.textContent = item.frozen ? "解冻" : "冻结";
    toggle.setAttribute("aria-label", `${item.frozen ? "解冻" : "冻结"} ${item.character || item.chat_id || item.session_id}`);
    toggle.onclick = async event => {
      event.stopPropagation();
      setBusy(toggle, true);
      try {
        await api(`/api/sessions/${encodeURIComponent(item.session_id)}/${item.frozen ? "unfreeze" : "freeze"}`, { method: "POST" });
        await loadWorldSessions();
        toast(item.frozen ? "会话已解冻" : "会话已冻结");
      } catch (err) {
        toast(err.message, "error");
      } finally {
        setBusy(toggle, false);
      }
    };
    row.append(select, toggle);
    list.appendChild(row);
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
      $("#world-title").textContent = "实时动线";
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

function renderPlaceHistory(history = []) {
  if (!history.length) return `<div class="empty-state">还没有位置轨迹，对话几轮后会自动记录。</div>`;
  const items = history.slice(-8);
  return `<div class="timeline-list">${items.map((item, idx) => {
    const isLast = idx === items.length - 1;
    const classes = isLast ? "timeline-item now" : "timeline-item";
    const sourceLabel = item.source === "tool" ? "工具声明" : item.source === "llm" ? "LLM 识别" : "";
    return `
      <article class="${classes}">
        <time>${escapeHtml(item.ago || "")}</time>
        <div>
          <strong>${escapeHtml(item.label || item.key || "未知地点")}</strong>
          ${sourceLabel ? `<p>来源 ${escapeHtml(sourceLabel)} · 置信度 ${Number(item.confidence || 0).toFixed(0)}%</p>` : ""}
          ${isLast ? `<div class="tag-row"><span>📍 当前位置</span></div>` : ""}
        </div>
      </article>
    `;
  }).join("")}</div>`;
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

function renderLifePlan(plan) {
  if (!plan || !plan.enabled) {
    return `<div class="empty-state small">生活线已关闭。</div>`;
  }
  if (!plan.exists) {
    return `<div class="empty-state small">还没有生活线，可点击右上角生成生活线。</div>`;
  }
  const today = plan.today || {};
  const statusLabel = value => ({
    active: "进行中",
    achieved: "已达成",
    abandoned: "已放下",
    planned: "待发生",
    done: "已发生",
    derailed: "偏离",
    skipped: "略过",
  }[value] || value || "-");
  const goalList = (items, kind) => {
    const isLong = kind === "long";
    const title = isLong ? "长期线" : "中期线";
    if (!items.length) return `<div class="empty-state small">暂无${title}。</div>`;
    return `<div class="life-goal-list">${items.map(item => `
      <article class="life-goal">
        <div>
          <strong>${escapeHtml(item.text || "")}</strong>
          ${item.dimension ? `<small class="life-goal-meta">维度：${escapeHtml(item.dimension)}</small>` : ""}
          ${item.motivation ? `<small>${escapeHtml(item.motivation)}</small>` : ""}
          ${item.parent_text ? `<small>承接${item.parent_dimension ? `「${escapeHtml(item.parent_dimension)}」` : ""}：${escapeHtml(item.parent_text)}</small>` : ""}
          ${item.progress_note ? `<small>${escapeHtml(item.progress_note)}</small>` : ""}
        </div>
        <div class="life-goal-side">
          <span>${escapeHtml(statusLabel(item.status))}</span>
          <div class="life-goal-actions">
            <button class="mini-btn" data-life-action="edit" data-kind="${isLong ? "long" : "mid"}" data-id="${escapeHtml(item.id || "")}">编辑</button>
            <button class="mini-btn danger" data-life-action="delete" data-kind="${isLong ? "long" : "mid"}" data-id="${escapeHtml(item.id || "")}">删除</button>
          </div>
        </div>
      </article>
    `).join("")}</div>`;
  };
  const eventList = (today.events || []).map(event => `
    <article class="life-event">
      <time>${escapeHtml(event.time_hint || "-")}</time>
      <div>
        <strong>${escapeHtml(event.text || "")}</strong>
        <small>${escapeHtml(event.place_label || event.place_key || "-")} · ${escapeHtml(statusLabel(event.status))}${event.related_mid_text ? ` · 关联：${escapeHtml(event.related_mid_text)}` : ""}</small>
        ${event.side_note ? `<p>${escapeHtml(event.side_note)}</p>` : ""}
      </div>
    </article>
  `).join("");
  return `
    <div class="life-plan-head">
      <span>角色：${escapeHtml(plan.character_key || "默认角色")}</span>
      <span>${escapeHtml(today.date || "未定日期")}${plan.updated_ago ? ` · ${escapeHtml(plan.updated_ago)}` : ""}</span>
    </div>
    <div class="life-plan-tools">
      <button class="mini-btn" data-life-action="regenerate">重生成目标</button>
      <button class="mini-btn" data-life-action="add" data-kind="long">新增长期</button>
      <button class="mini-btn" data-life-action="add" data-kind="mid">新增中期</button>
    </div>
    ${today.texture ? `<p class="life-texture">${escapeHtml(today.texture)}</p>` : `<div class="empty-state small">暂无生活底色。</div>`}
    <div class="life-plan-grid">
      <div>
        <h5>长期线</h5>
        ${goalList(plan.long_goals || [], "long")}
      </div>
      <div>
        <h5>中期线</h5>
        ${goalList(plan.mid_goals || [], "mid")}
      </div>
    </div>
    <div>
      <h5>今日片段</h5>
      ${eventList ? `<div class="life-event-list">${eventList}</div>` : `<div class="empty-state small">暂无今日片段。</div>`}
    </div>
  `;
}

function currentLifePlan() {
  return (state.worldPreview && state.worldPreview.life_plan) || {};
}

function findLifeGoal(kind, id) {
  const plan = currentLifePlan();
  const items = kind === "long" ? (plan.long_goals || []) : (plan.mid_goals || []);
  return items.find(item => String(item.id || "") === String(id || "")) || null;
}

function promptLifeGoalPayload(kind, existing = null) {
  // 返回一个 Promise，由调用方 await 获取 payload 或 null（取消）
  return new Promise((resolve) => {
    const isLong = kind === "long";
    const label = isLong ? "长期目标" : "中期目标";
    const plan = currentLifePlan();
    const activeLongs = (plan.long_goals || []).filter(item => (item.status || "active") === "active");

    const existing2 = document.getElementById("life-goal-dialog");
    if (existing2) existing2.remove();
    const dialog = document.createElement("dialog");
    dialog.id = "life-goal-dialog";
    dialog.style.cssText = "border:1px solid var(--line);border-radius:var(--radius-lg);padding:20px;max-width:520px;width:92vw;max-height:80vh;overflow:auto";
    const parentOptions = isLong ? "" : activeLongs.map(item =>
      `<option value="${escapeHtml(String(item.id || ""))}">${escapeHtml((item.dimension ? `[${item.dimension}] ` : "") + item.text)}</option>`
    ).join("");
    const dimensionOptions = existing?.dimension ? [existing.dimension] : [];
    if (!dimensionOptions.length) {
      const allLong = plan.long_goals || [];
      allLong.forEach(item => { if (item.dimension && !dimensionOptions.includes(item.dimension)) dimensionOptions.push(item.dimension); });
      ["生活", "理想", "爱好", "事业"].forEach(d => { if (!dimensionOptions.includes(d)) dimensionOptions.push(d); });
    }
    const dimOpts = dimensionOptions.map(d => `<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`).join("");

    dialog.innerHTML = `
      <form method="dialog">
        <h3 style="margin:0 0 14px">${existing ? "编辑" : "新增"}${label}</h3>
        <div style="display:grid;gap:12px">
          <label>文本<textarea name="text" rows="3" required>${escapeHtml(existing?.text || "")}</textarea></label>
          ${isLong ? `
          <label>目标维度（可自定义）<input name="dimension" list="life-goal-dimensions" value="${escapeHtml(existing?.dimension || "")}">
            <datalist id="life-goal-dimensions">${dimOpts}</datalist>
          </label>
          <label>内在动机<textarea name="motivation" rows="2">${escapeHtml(existing?.motivation || "")}</textarea></label>
          ` : `
          ${activeLongs.length ? `
          <label>承接的长期目标<select name="parent_id">
            ${parentOptions.replace(`value="${escapeHtml(String(existing?.parent_id || activeLongs[0]?.id || ""))}"`, `value="${escapeHtml(String(existing?.parent_id || activeLongs[0]?.id || ""))}" selected`)}
          </select></label>
          ` : `<p class="muted">暂无活跃的长期目标</p>`}
          <label>进展备注<textarea name="progress_note" rows="2">${escapeHtml(existing?.progress_note || "")}</textarea></label>
          `}
          <label>状态
            <select name="status">
              <option value="active"${(existing?.status || "active") === "active" ? " selected" : ""}>进行中</option>
              <option value="achieved"${existing?.status === "achieved" ? " selected" : ""}>已达成</option>
              <option value="abandoned"${existing?.status === "abandoned" ? " selected" : ""}>已放下</option>
            </select>
          </label>
        </div>
        <div class="form-actions" style="margin-top:14px;border-top:none;padding-top:0">
          <button type="button" value="cancel">取消</button>
          <button class="primary" type="submit" value="confirm">${existing ? "保存" : "新增"}</button>
        </div>
      </form>
    `;
    document.body.appendChild(dialog);
    dialog.querySelector('[value="cancel"]').onclick = () => { dialog.close(); resolve(null); };
    dialog.querySelector("form").onsubmit = (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const text = String(form.elements.text?.value || "").trim();
      if (!text) { toast("目标文本不能为空", "error"); return; }
      const payload = { kind, text };
      if (existing?.id) payload.id = existing.id;
      if (isLong) {
        payload.dimension = String(form.elements.dimension?.value || "").trim();
        payload.motivation = String(form.elements.motivation?.value || "").trim();
      } else {
        payload.parent_id = String(form.elements.parent_id?.value || "").trim();
        payload.progress_note = String(form.elements.progress_note?.value || "").trim();
      }
      payload.status = String(form.elements.status?.value || "active").trim() || "active";
      dialog.close();
      resolve(payload);
    };
    dialog.addEventListener("close", () => {
      // 如果 dialog 被关闭但没有通过 submit 处理（如按 ESC），resolve null
      // 但 submit handler 已经 resolve 了，这里需要避免重复 resolve
      dialog._resolved = true;
    });
    // 超时保护：监听 close 来兜底
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (!dialog._resolved) resolve(null);
    }, { once: true });
    dialog.showModal();
  });
}

async function applyLifePlanResponse(data) {
  state.worldPreview = data.world;
  renderWorldRoute(data.world);
}

async function handleLifePlanAction(event) {
  const btn = event.target.closest("[data-life-action]");
  if (!btn) return;
  if (!state.selectedWorldSession) return;
  const action = btn.dataset.lifeAction;
  const kind = btn.dataset.kind || "";
  const id = btn.dataset.id || "";
  setBusy(btn, true);
  try {
    if (action === "regenerate") {
      const instruction = window.prompt("目标指示（可留空）：", "");
      if (instruction === null) return;
      const data = await api(`/api/world/${encodeURIComponent(state.selectedWorldSession)}/life-plan`, {
        method: "POST",
        body: { instruction, regenerate_goals: true },
      });
      await applyLifePlanResponse(data);
      toast("生活主线已重生成");
    } else if (action === "add") {
      const payload = await promptLifeGoalPayload(kind);
      if (!payload) return;
      const data = await api(`/api/world/${encodeURIComponent(state.selectedWorldSession)}/life-plan/goals`, {
        method: "POST",
        body: payload,
      });
      await applyLifePlanResponse(data);
      toast("目标已新增");
    } else if (action === "edit") {
      const existing = findLifeGoal(kind, id);
      if (!existing) {
        toast("目标不存在", "error");
        return;
      }
      const payload = await promptLifeGoalPayload(kind, existing);
      if (!payload) return;
      const data = await api(`/api/world/${encodeURIComponent(state.selectedWorldSession)}/life-plan/goals/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: payload,
      });
      await applyLifePlanResponse(data);
      toast("目标已更新");
    } else if (action === "delete") {
      if (!window.confirm("确定删除这个目标？删除长期目标会同时删除承接它的中期目标。")) return;
      const data = await api(`/api/world/${encodeURIComponent(state.selectedWorldSession)}/life-plan/goals/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      await applyLifePlanResponse(data);
      toast("目标已删除");
    }
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
}

function renderWorldRoute(world) {
  const box = $("#world-content");
  if (!world) {
    box.innerHTML = `<div class="empty-state">没有动线数据。</div>`;
    return;
  }
  const session = world.session || {};
  $("#world-title").textContent = worldSessionTitle(session) || "实时动线";
  $("#world-subtitle").textContent = `${world.city || "未设置城市"} · UTC${world.timezone || "-"} · ${world.weather || "天气未知"}`;
  if (!world.enabled) {
    box.innerHTML = `<div class="empty-state">自动动线已关闭。可在"设置 → 推送与本地控制台 → 启用自动动线"打开。</div>`;
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
      <h4>生活线</h4>
      ${renderLifePlan(world.life_plan || {})}
    </section>
    <section class="world-block">
      <h4>最近轨迹</h4>
      ${renderPlaceHistory(current.character_place_history || [])}
    </section>
    <section class="world-block">
      <h4>后续可能去向</h4>
      <div class="chip-row">${renderCandidateChips(current.character_candidates || [])}</div>
    </section>
    <section class="world-block">
      <h4>场景约束</h4>
      <ul class="constraint-list">${constraints || "<li>暂无额外约束</li>"}</ul>
    </section>
    <section class="world-block">
      <h4>城市地点</h4>
      ${renderCatalog(world.catalog || {})}
    </section>
    <section class="world-block">
      <h4>今日参考</h4>
      ${renderTimeline(world.timeline || [])}
    </section>
  `;
}
