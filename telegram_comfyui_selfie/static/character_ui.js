"use strict";

// 角色卡、头像、衣橱、记忆与日记页面。依赖 app.js 的 state、api 与通用 DOM helper。
function characterApiKey(id) {
  // 默认角色卡的 id 是 bot_name（如"蕾伊"），运行态键是空串；对 is_default 卡统一发
  // __default__ 占位，由后端归一，保证 WebUI 记忆/日记/历史与 Telegram 侧同键。
  const char = state.characterData?.characters?.[id];
  return char && char.is_default === true ? "__default__" : id;
}

function compactText(value, max = 80) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > max ? `${text.slice(0, Math.max(0, max - 1)).trim()}...` : text;
}

function characterAvatarUrl(characterId, char) {
  if (!state.selectedSession || !characterId || !char?.avatar_path) return "";
  const stamp = char.avatar_updated_at || char.avatar_path || "";
  return `/api/sessions/${encodeURIComponent(state.selectedSession)}/characters/${encodeURIComponent(characterId)}/avatar-image?v=${encodeURIComponent(stamp)}`;
}

function characterAvatarMarkup(char, characterId, className) {
  const url = characterAvatarUrl(characterId, char);
  return `<span class="${className} ${url ? "has-avatar" : "is-empty"}">${url ? `<img src="${escapeHtml(url)}" alt="">` : ""}</span>`;
}

function ensureAvatarPreview() {
  let overlay = $("#avatar-preview");
  if (overlay) return overlay;
  document.body.insertAdjacentHTML("beforeend", `
    <div id="avatar-preview" class="avatar-preview" hidden>
      <button class="avatar-preview-close" type="button" aria-label="关闭头像预览">×</button>
      <figure>
        <img alt="">
        <figcaption></figcaption>
      </figure>
    </div>
  `);
  overlay = $("#avatar-preview");
  const close = () => {
    overlay.hidden = true;
    const img = overlay.querySelector("img");
    if (img) img.removeAttribute("src");
  };
  overlay.addEventListener("click", event => {
    if (event.target === overlay) close();
  });
  overlay.querySelector(".avatar-preview-close").onclick = close;
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !overlay.hidden) close();
  });
  return overlay;
}

function openAvatarPreview(src, title) {
  if (!src) return;
  const overlay = ensureAvatarPreview();
  const img = overlay.querySelector("img");
  const caption = overlay.querySelector("figcaption");
  img.src = src;
  img.alt = title || "角色头像";
  caption.textContent = title || "角色头像";
  overlay.hidden = false;
}

function characterPill(label, value, className = "") {
  const text = compactText(value, 54);
  if (!text) return "";
  return `<span class="character-pill ${className}"><b>${escapeHtml(label)}</b>${escapeHtml(text)}</span>`;
}

function characterScheduleSummary(char = {}) {
  const workdayWake = char.workday_wake_time || "08:00";
  const workdaySleep = char.workday_sleep_time || "23:50";
  const weekendWake = char.weekend_wake_time || "08:00";
  const weekendSleep = char.weekend_sleep_time || "23:50";
  return `工作日 ${workdayWake}-${workdaySleep} / 周末 ${weekendWake}-${weekendSleep}`;
}

const wardrobeSlotLabels = {
  hair: "发型/发色",
  eyes: "眼睛",
  dress: "连衣裙",
  top: "上衣",
  bottom: "下装",
  outerwear: "外套",
  bra: "胸罩",
  panties: "内裤",
  legwear: "袜/腿部",
  footwear: "鞋",
  accessory: "配饰",
  other: "其他",
};

const wardrobeSlotOrder = Object.keys(wardrobeSlotLabels);
const closetSlotOrder = ["dress", "top", "bottom", "outerwear", "bra", "panties", "legwear", "footwear"];
const wardrobeStateLabels = {
  normal: "正常",
  half_off: "半脱",
  damaged: "破损",
  removed: "脱掉",
};
const wardrobeStateOptions = ["normal", "half_off", "damaged", "removed"];
const clothingColorWords = [
  ["dark blue", "深蓝色"],
  ["light blue", "浅蓝色"],
  ["black", "黑色"],
  ["white", "白色"],
  ["blue", "蓝色"],
  ["red", "红色"],
  ["pink", "粉色"],
  ["purple", "紫色"],
  ["green", "绿色"],
  ["yellow", "黄色"],
  ["brown", "棕色"],
  ["gray", "灰色"],
  ["grey", "灰色"],
];
const clothingMaterialWords = [
  ["cotton knit", "棉质针织"],
  ["silk", "丝绸"],
  ["cotton", "棉质"],
  ["knit", "针织"],
  ["lace", "蕾丝"],
  ["denim", "牛仔"],
  ["leather", "皮质"],
  ["wool", "羊毛"],
];
const clothingItemWords = [
  ["sleep dress", "睡裙"],
  ["nightgown", "睡裙"],
  ["nightdress", "睡裙"],
  ["slip dress", "吊带裙"],
  ["cardigan", "开衫"],
  ["t-shirt", "T恤"],
  ["shirt", "衬衫"],
  ["blouse", "衬衫"],
  ["jeans", "牛仔裤"],
  ["skirt", "裙子"],
  ["dress", "连衣裙"],
  ["shorts", "短裤"],
  ["pants", "长裤"],
  ["trousers", "长裤"],
  ["bra", "胸罩"],
  ["panties", "内裤"],
  ["stockings", "长袜"],
  ["socks", "袜子"],
  ["sneakers", "运动鞋"],
  ["heels", "高跟鞋"],
  ["shoes", "鞋"],
];

function hasCjk(value) {
  return /[\u3400-\u9fff]/.test(String(value || ""));
}

function localizeClothingTag(value, fallback = "衣物") {
  const raw = String(value || "").trim();
  if (!raw) return fallback;
  if (hasCjk(raw)) return raw;
  const text = raw.toLowerCase().replace(/_/g, " ");
  const parts = [];
  clothingColorWords.forEach(([key, label]) => {
    if (key === "blue" && (text.includes("dark blue") || text.includes("light blue"))) return;
    if (text.includes(key) && !parts.includes(label)) parts.push(label);
  });
  clothingMaterialWords.forEach(([key, label]) => {
    if ((key === "cotton" || key === "knit") && text.includes("cotton knit")) return;
    if (text.includes(key) && !parts.includes(label)) parts.push(label);
  });
  const item = clothingItemWords.find(([key]) => text.includes(key));
  if (item) parts.push(item[1]);
  if (parts.length) return parts.join("");
  return raw.replace(/_/g, " ");
}

function localizeClothingTags(value) {
  const raw = String(value || "").trim();
  if (!raw || hasCjk(raw)) return raw;
  return raw.split(",").map(part => localizeClothingTag(part)).filter(Boolean).join("，");
}

function wardrobeDisplayText(slot, value, displayNames = {}) {
  const named = String(displayNames?.[slot] || "").trim();
  return named || localizeClothingTags(value) || String(value || "");
}

function wardrobeSummaryText(items = {}, displayNames = {}, fallback = "") {
  const parts = wardrobeSlotOrder
    .filter(slot => String(items?.[slot] || "").trim())
    .map(slot => wardrobeDisplayText(slot, items[slot], displayNames))
    .filter(Boolean);
  return parts.length ? parts.join("，") : localizeClothingTags(fallback);
}

function closetDisplayName(name, entry = {}) {
  const raw = String(name || "").trim();
  if (raw === "public fallback top") return "公开兜底上衣";
  if (raw === "public fallback bottom") return "公开兜底下装";
  if (raw && hasCjk(raw)) return raw;
  return localizeClothingTag(entry.tags || raw || "", wardrobeSlotLabels[entry.slot] || "衣物");
}

function wardrobeRows(items = {}, options = {}) {
  const displayNames = options.displayNames || {};
  const states = options.states || {};
  const editableStates = Boolean(options.editableStates);
  const entries = Object.entries(items || {})
    .filter(([, value]) => String(value ?? "").trim())
    .sort(([a], [b]) => {
      const ai = wardrobeSlotOrder.indexOf(a);
      const bi = wardrobeSlotOrder.indexOf(b);
      if (ai === -1 && bi === -1) return a.localeCompare(b);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
  if (!entries.length) return `<div class="empty-state small">暂无。</div>`;
  return `<div class="wardrobe-rows">${entries.map(([slot, value]) => `
    <div class="wardrobe-row">
      <span>${escapeHtml(wardrobeSlotLabels[slot] || slot)}</span>
      <strong title="${escapeHtml(value)}">${escapeHtml(wardrobeDisplayText(slot, value, displayNames))}</strong>
      ${editableStates && closetSlotOrder.includes(slot) ? `
        <label class="wardrobe-state-control">
          <span>状态</span>
          <select data-wardrobe-action="set-item-state" data-slot="${escapeHtml(slot)}">
            ${wardrobeStateOptions.map(name => `<option value="${escapeHtml(name)}"${(states[slot] || "normal") === name ? " selected" : ""}>${escapeHtml(wardrobeStateLabels[name] || name)}</option>`).join("")}
          </select>
        </label>
      ` : (states[slot] ? `<em class="wardrobe-state-badge">${escapeHtml(wardrobeStateLabels[states[slot]] || states[slot])}</em>` : "")}
    </div>
  `).join("")}</div>`;
}

function normalizeTagsForCompare(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/_/g, " ")
    .split(",")
    .map(part => part.trim().replace(/\s+/g, " "))
    .filter(Boolean)
    .join(", ");
}

function closetRows(clothing = {}) {
  const closet = clothing.closet || {};
  const wardrobe = clothing.wardrobe || {};
  const displayNames = clothing.wardrobe_display || {};
  const bySlot = new Map();
  Object.entries(closet)
    .filter(([, entry]) => entry && String(entry.tags || "").trim())
    .sort((a, b) => Number(b[1].last_worn || b[1].added_at || 0) - Number(a[1].last_worn || a[1].added_at || 0))
    .forEach(([name, entry]) => {
      const slot = closetSlotOrder.includes(entry.slot) ? entry.slot : "other";
      if (!bySlot.has(slot)) bySlot.set(slot, []);
      bySlot.get(slot).push([name, entry]);
    });
  const slotOrder = [...closetSlotOrder, ...[...bySlot.keys()].filter(slot => !closetSlotOrder.includes(slot)).sort()];
  const openSlots = state.closetOpenSlots instanceof Set ? state.closetOpenSlots : new Set();
  return `<div class="closet-list">${slotOrder.map(slot => {
    const items = bySlot.get(slot) || [];
    const wornTags = normalizeTagsForCompare(wardrobe[slot]);
    const namedActive = String(displayNames[slot] || "").trim();
    const matched = items.find(([name, entry]) => name === namedActive || normalizeTagsForCompare(entry.tags) === wornTags);
    const activeName = wornTags ? (matched ? matched[0] : "") : "";
    const currentLabel = wornTags ? (activeName || namedActive || localizeClothingTags(wardrobe[slot])) : "空";
    return `
    <details class="closet-slot" data-closet-slot="${escapeHtml(slot)}"${openSlots.has(slot) ? " open" : ""}>
      <summary>
        <span class="closet-slot-name">${escapeHtml(wardrobeSlotLabels[slot] || slot)}</span>
        <strong class="closet-slot-current${wornTags ? "" : " is-empty"}" title="${escapeHtml(wardrobe[slot] || "")}">${escapeHtml(currentLabel)}</strong>
      </summary>
      <div class="closet-slot-options">
        <div class="closet-option${wornTags ? "" : " is-active"}">
          <button class="closet-choice" type="button" data-wardrobe-action="remove-slot" data-slot="${escapeHtml(slot)}"${wornTags ? "" : " disabled"}>空（这个槽位不穿）</button>
        </div>
        ${items.map(([name, entry]) => {
          const isActive = Boolean(wornTags) && (name === activeName || normalizeTagsForCompare(entry.tags) === wornTags);
          return `
        <div class="closet-option${isActive ? " is-active" : ""}">
          <button class="closet-choice" type="button" data-wardrobe-action="wear-closet" data-name="${escapeHtml(name)}" title="${escapeHtml(entry.tags || "")}"${isActive ? " disabled" : ""}>${escapeHtml(closetDisplayName(name, entry))}</button>
          <span class="closet-option-tools">
            <button class="ghost tiny" type="button" data-wardrobe-action="edit-closet" data-name="${escapeHtml(name)}" data-tags="${escapeHtml(entry.tags || "")}">改</button>
            <button class="ghost tiny danger" type="button" data-wardrobe-action="delete-closet" data-name="${escapeHtml(name)}">删</button>
          </span>
        </div>`;
        }).join("")}
        ${items.length ? "" : `<div class="empty-state small">这一类还没有收藏。</div>`}
      </div>
    </details>`;
  }).join("")}</div>`;
}

function renderRuntimeClothingPanel(char, isActive) {
  if (!isActive) return "";
  const clothing = state.characterData?.current_clothing || {};
  const current = clothing.dynamic_appearance || char.outfit || "";
  const currentSummary = wardrobeSummaryText(clothing.wardrobe || {}, clothing.wardrobe_display || {}, current);
  const nudityBadge = clothing.nudity
    ? `<span class="badge danger" title="持久裸体状态会影响后续生图">裸体状态：${escapeHtml(clothing.nudity)}</span>`
    : "";
  return `
    <section class="form-section character-section runtime-clothing-section">
      <div class="runtime-clothing-head">
        <div>
          <h3>当前衣柜 ${nudityBadge}</h3>
          <p>这里改的是当前角色现在穿在身上的衣服；会直接影响聊天后的生图。</p>
        </div>
        <div class="runtime-clothing-head-actions">
          <button class="ghost" type="button" data-wardrobe-action="clear-item-states">一键还原状态</button>
          <button class="ghost danger" type="button" data-wardrobe-action="clear">清空当前穿搭</button>
        </div>
      </div>
      <div class="wardrobe-editor">
        <textarea id="wardrobe-description" rows="2" placeholder="输入换装：例如 黑色丝绸睡裙，白色棉质针织开衫"></textarea>
        <div class="wardrobe-editor-actions">
          <button class="primary" type="button" data-wardrobe-action="apply">直接换上</button>
          <button class="ghost" type="button" data-wardrobe-action="save-closet">存进衣橱（暂不换）</button>
        </div>
      </div>
      <div class="runtime-clothing-layout">
        <section class="runtime-clothing-pane is-current">
          <div class="pane-title">
            <h4>身上穿着</h4>
            <span>换装去「衣橱收藏」操作；单件状态可在这里调整</span>
          </div>
          <div class="runtime-clothing-current">
            <span>当前摘要</span>
            <strong title="${escapeHtml(current)}">${escapeHtml(currentSummary || "未设置")}</strong>
          </div>
          ${wardrobeRows(clothing.wardrobe || {}, {
            displayNames: clothing.wardrobe_display || {},
            states: clothing.wardrobe_item_states || {},
            editableStates: true,
          })}
        </section>
        <section class="runtime-clothing-pane is-fallback">
          <div class="pane-title">
            <h4>公开兜底</h4>
            <span>只在外出/公开场景临时叠加</span>
          </div>
          ${wardrobeRows(clothing.public_fallback_outfit || {})}
          ${clothing.public_fallback_in_current ? `
            <button class="ghost" type="button" data-wardrobe-action="stash-public-fallback">从当前穿搭移出兜底</button>
          ` : ""}
        </section>
        <section class="runtime-clothing-pane is-closet">
          <div class="pane-title">
            <h4>衣橱收藏</h4>
            <span>点开槽位换穿、改名或删除；「空」= 该槽位不穿</span>
          </div>
          ${closetRows(clothing)}
        </section>
      </div>
    </section>
  `;
}

function startClosetEdit(button) {
  const row = button.closest(".closet-option");
  if (!row || row.classList.contains("is-editing")) return;
  row._closetOriginal = row.innerHTML;
  row.classList.add("is-editing");
  row.innerHTML = `
    <div class="closet-edit-form">
      <input type="text" data-closet-edit="name" placeholder="名称（衣橱里显示的名字）">
      <input type="text" data-closet-edit="tags" placeholder="英文标签（生图用，逗号分隔）">
      <div class="closet-edit-actions">
        <button class="primary tiny" type="button" data-wardrobe-action="save-edit-closet">保存</button>
        <button class="ghost tiny" type="button" data-wardrobe-action="cancel-edit-closet">取消</button>
      </div>
    </div>`;
  row.querySelector('[data-closet-edit="name"]').value = button.dataset.name || "";
  row.querySelector('[data-closet-edit="tags"]').value = button.dataset.tags || "";
  row.querySelector('[data-wardrobe-action="save-edit-closet"]').dataset.name = button.dataset.name || "";
}

function cancelClosetEdit(button) {
  const row = button.closest(".closet-option");
  if (!row || row._closetOriginal == null) return;
  row.innerHTML = row._closetOriginal;
  row.classList.remove("is-editing");
  delete row._closetOriginal;
}

function bindRuntimeClothingHandlers(container) {
  const panel = container.querySelector(".runtime-clothing-section");
  if (!panel) return;
  state.closetOpenSlots = state.closetOpenSlots instanceof Set ? state.closetOpenSlots : new Set();
  // details 的 toggle 不冒泡，用捕获阶段记录展开状态，重渲染后保持不收起。
  panel.addEventListener("toggle", event => {
    const slot = event.target?.dataset?.closetSlot;
    if (!slot) return;
    if (event.target.open) state.closetOpenSlots.add(slot);
    else state.closetOpenSlots.delete(slot);
  }, true);
  panel.addEventListener("change", async event => {
    const control = event.target.closest('select[data-wardrobe-action="set-item-state"]');
    if (!control || !panel.contains(control)) return;
    const body = {
      action: "set-item-state",
      slot: control.dataset.slot || "",
      state: control.value || "normal",
    };
    control.disabled = true;
    try {
      const sid = encodeURIComponent(state.selectedSession);
      await api(`/api/sessions/${sid}/wardrobe`, { method: "POST", body });
      await loadCharacters();
      toast("衣物状态已更新");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      control.disabled = false;
    }
  });
  panel.addEventListener("click", async event => {
    const button = event.target.closest("[data-wardrobe-action]");
    if (!button || !panel.contains(button)) return;
    if (button.tagName === "SELECT") return;
    const action = button.dataset.wardrobeAction;
    if (action === "edit-closet") {
      startClosetEdit(button);
      return;
    }
    if (action === "cancel-edit-closet") {
      cancelClosetEdit(button);
      return;
    }
    const body = { action };
    const editor = panel.querySelector("#wardrobe-description");
    if (action === "apply" || action === "save-closet") {
      body.description = String(editor?.value || "").trim();
      if (!body.description) {
        toast(action === "apply" ? "先输入要换成什么衣服。" : "先输入要收藏的衣物。", "error");
        return;
      }
    } else if (action === "remove-slot") {
      body.slot = button.dataset.slot || "";
    } else if (action === "wear-closet") {
      body.name = button.dataset.name || "";
    } else if (action === "save-edit-closet") {
      const row = button.closest(".closet-option");
      body.action = "closet-edit";
      body.name = button.dataset.name || "";
      body.new_name = String(row?.querySelector('[data-closet-edit="name"]')?.value || "").trim();
      body.tags = String(row?.querySelector('[data-closet-edit="tags"]')?.value || "").trim();
      if (!body.new_name) {
        toast("名称不能为空。", "error");
        return;
      }
      if (!body.tags) {
        toast("英文标签不能为空。", "error");
        return;
      }
    } else if (action === "delete-closet") {
      body.action = "closet-delete";
      body.name = button.dataset.name || "";
      if (!window.confirm(`确定从衣橱删除「${body.name}」吗？正穿在身上的不会被脱下。`)) return;
    } else if (action === "clear") {
      if (!window.confirm("确定清空当前穿搭吗？衣橱收藏不会删除。")) return;
    }
    setBusy(button, true);
    try {
      const sid = encodeURIComponent(state.selectedSession);
      await api(`/api/sessions/${sid}/wardrobe`, { method: "POST", body });
      await loadCharacters();
      toast(action === "save-closet" ? "已存进衣橱" : "衣柜已更新");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
}

function renderWardrobePanel() {
  const box = $("#wardrobe-manager");
  if (!box) return;
  if (!state.selectedSession || !state.selectedCharacter || !state.characterData) {
    box.innerHTML = `<div class="empty-state">选择角色后查看衣橱。</div>`;
    return;
  }
  const charId = state.selectedCharacter;
  const char = state.characterData.characters?.[charId];
  if (!char) {
    box.innerHTML = `<div class="empty-state">选择角色后查看衣橱。</div>`;
    return;
  }
  const isActive = charId === state.characterData.active_id;
  if (!isActive) {
    box.innerHTML = `<div class="empty-state">该角色不是当前角色，请先「设为当前」再操作衣橱。</div>`;
    return;
  }
  const html = renderRuntimeClothingPanel(char, true);
  if (!html) {
    box.innerHTML = `<div class="empty-state">衣橱暂不可用。</div>`;
    return;
  }
  box.innerHTML = html;
  bindRuntimeClothingHandlers(box);
}

function diaryTitle(content, date) {
  const first = String(content || "").split(/\r?\n/).map(line => line.trim()).find(Boolean) || "";
  const title = first.replace(/^#+\s*/, "").trim();
  return compactText(title || date || "日记", 34);
}

function diaryDateLabel(date) {
  const parsed = new Date(`${date}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return date || "-";
  const week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][parsed.getDay()];
  return `${date} · ${week}`;
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "checkpoint.json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
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
    const name = char.character || char.bot_name || id;
    const meta = [char.series || "未设定出处", char.role_name || "未设定类型"].filter(Boolean).join(" · ");
    const summary = compactText(char.persona || char.relationship || char.appearance || "", 58);
    return `
      <button class="character-card ${state.selectedCharacter === id ? "selected" : ""}" data-character-id="${escapeHtml(id)}" type="button">
        ${characterAvatarMarkup(char, id, "character-card-avatar")}
        <span class="character-card-main">
          <span class="character-card-title">${escapeHtml(name)}<span class="character-card-badges">${activeBadge}${defaultBadge}</span></span>
          <span class="character-card-meta">${escapeHtml(meta)}</span>
          ${summary ? `<span class="character-card-summary">${escapeHtml(summary)}</span>` : ""}
        </span>
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
  const overview = document.createElement("section");
  overview.className = "character-profile-strip";
  const overviewTitle = char.character || char.bot_name || state.selectedCharacter;
  const overviewSubtitle = [
    char.series || "未设定出处",
    char.role_name || "未设定类型",
    char.occupation || "",
  ].filter(Boolean).join(" · ");
  const overviewSummary = compactText(char.persona || char.relationship || char.appearance || "暂无核心描述", 180);
  const autoChange = char.allow_change_appearance === true || char.allow_change_appearance === "true"
    ? "开启"
    : (char.allow_change_appearance === false || char.allow_change_appearance === "false" ? "关闭" : "跟随全局");
  const overviewPills = [
    characterPill("对话名", char.bot_name || "-"),
    characterPill("自称", char.bot_self_name || "-"),
    characterPill("称呼用户", char.user_address || "-"),
    characterPill("关系", char.relationship || "-"),
    characterPill("作息", characterScheduleSummary(char)),
    characterPill("画风", char.style || "不注入"),
    characterPill("纯良度", char.purity ?? "-"),
    characterPill("自动换装", autoChange),
  ].join("");
  overview.innerHTML = `
    <div class="character-profile-media">
      ${characterAvatarMarkup(char, state.selectedCharacter, "character-profile-avatar")}
      <div class="character-profile-actions">
        <button type="button" id="character-avatar-generate">${char.avatar_path ? "重新生成头像" : "生成头像"}</button>
      </div>
    </div>
    <div class="character-profile-main">
      <div class="character-profile-title">
        <strong>${escapeHtml(overviewTitle)}</strong>
        <span>${isActive ? "当前角色" : "未激活"}</span>
        ${isDefault ? `<span>系统默认</span>` : ""}
      </div>
      <div class="character-profile-subtitle">${escapeHtml(overviewSubtitle || "基础身份未完整填写")}</div>
      <p>${escapeHtml(overviewSummary)}</p>
      <div class="character-profile-pills">${overviewPills}</div>
    </div>
    <input type="hidden" name="avatar_path" value="${escapeHtml(char.avatar_path || "")}">
    <input type="hidden" name="avatar_updated_at" value="${escapeHtml(char.avatar_updated_at || "")}">
    <input type="hidden" name="outfit" value="${escapeHtml(char.outfit || "")}">
  `;
  const profileAvatar = overview.querySelector(".character-profile-avatar.has-avatar");
  if (profileAvatar) {
    const previewUrl = characterAvatarUrl(state.selectedCharacter, char);
    profileAvatar.classList.add("is-clickable");
    profileAvatar.title = "查看头像大图";
    profileAvatar.tabIndex = 0;
    profileAvatar.setAttribute("role", "button");
    profileAvatar.setAttribute("aria-label", "查看头像大图");
    const openPreview = () => openAvatarPreview(previewUrl, overviewTitle);
    profileAvatar.onclick = openPreview;
    profileAvatar.onkeydown = event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openPreview();
      }
    };
  }
  form.appendChild(overview);

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

  renderWardrobePanel();

  loadHistorySummary();

  const checkpointRows = state.characterData.checkpoints?.[state.selectedCharacter] || [];
  const checkpointOptions = checkpointRows.length
    ? checkpointRows.map(item => `<option value="${escapeHtml(item.date)}">${escapeHtml(item.date)}</option>`).join("")
    : `<option value="">暂无检查点</option>`;
  const checkpointSection = document.createElement("section");
  checkpointSection.className = "form-section character-section";
  checkpointSection.innerHTML = `
    <h3 class="section-toggle" type="button">角色检查点</h3>
    <div class="field-grid">
      <div class="field-wrap full-width">
        <label for="character-checkpoint-select">JSON 检查点 <span class="muted">（dream 前自动生成，保留最近 7 天）</span></label>
        <div class="field-actions">
          <select id="character-checkpoint-select">${checkpointOptions}</select>
          <button type="button" id="export-character-checkpoint" ${checkpointRows.length ? "" : "disabled"}>导出检查点</button>
          <button type="button" id="export-current-checkpoint">导出当前状态</button>
        </div>
      </div>
    </div>
  `;
  form.appendChild(checkpointSection);

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

  const exportCheckpointBtn = $("#export-character-checkpoint");
  if (exportCheckpointBtn) {
    exportCheckpointBtn.onclick = () => exportSelectedCharacterCheckpoint(exportCheckpointBtn);
  }
  const exportCurrentBtn = $("#export-current-checkpoint");
  if (exportCurrentBtn) {
    exportCurrentBtn.onclick = () => exportCurrentCharacterCheckpoint(exportCurrentBtn);
  }
  const avatarBtn = $("#character-avatar-generate");
  if (avatarBtn) {
    avatarBtn.onclick = () => generateCharacterAvatar(avatarBtn);
  }

  form.onsubmit = async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true);
    try {
      const values = formValues(form);
      values.id = state.selectedCharacter;
      if (isDefault) values.is_default = true;
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
  const rawCharKey = state.selectedCharacter || "";
  const charKey = characterApiKey(rawCharKey);
  try {
    const data = await api(`/api/sessions/${sid}/history-summary?character_key=${encodeURIComponent(charKey)}`);
    if (state.selectedCharacter !== rawCharKey) return;
    editor.value = data.summary || "";
  } catch (_) {
    if (state.selectedCharacter !== rawCharKey) return;
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
  const rawCharKey = state.selectedCharacter;
  const charKey = encodeURIComponent(characterApiKey(rawCharKey));
  try {
    const data = await api(`/api/sessions/${sid}/memories?character_key=${charKey}`);
    if (state.selectedCharacter !== rawCharKey) return;
    const rows = (data.memories || []).map(mem => `
      <div class="manager-row memory-row${mem.kind === "user_profile" ? " is-user-profile" : ""}">
        ${mem.kind === "user_profile" ? `<div class="memory-row-label">置顶用户画像</div>` : ""}
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
    if (state.selectedCharacter !== rawCharKey) return;
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
  const rawCharKey = state.selectedCharacter;
  const charKey = encodeURIComponent(characterApiKey(rawCharKey));
  try {
    const data = await api(`/api/sessions/${sid}/diaries?character_key=${charKey}&limit=30`);
    if (state.selectedCharacter !== rawCharKey) return;
    renderDiaries(data.diaries || [], sid, charKey);
  } catch (err) {
    if (state.selectedCharacter !== rawCharKey) return;
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
    const content = diary.content || "";
    return `
      <article class="diary-note">
        <div class="diary-header">
          <div>
            <strong>${escapeHtml(diaryDateLabel(date))}</strong>
            <span>${escapeHtml(diaryTitle(content, date))}</span>
          </div>
          <time>${escapeHtml(updated)}</time>
        </div>
        <textarea class="diary-note-editor" data-diary-date="${escapeHtml(date)}" rows="9">${escapeHtml(content)}</textarea>
        <div class="diary-note-actions">
          <button data-diary-save="${escapeHtml(date)}" type="button">保存</button>
          <button class="danger" data-diary-delete="${escapeHtml(date)}" type="button">删除</button>
        </div>
      </article>
    `;
  }).join("");
  box.innerHTML = `
    <form id="diary-add-form" class="diary-compose">
      <input type="date" name="diary_date" value="${today}">
      <textarea name="content" placeholder="新增或覆盖一条日记" rows="4"></textarea>
      <button class="primary" type="submit">新增日记</button>
    </form>
    <div class="diary-note-grid">${rows || `<div class="empty-state">暂无日记。</div>`}</div>
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
  if (tab === "wardrobe") renderWardrobePanel();
}

async function exportSelectedCharacterCheckpoint(button) {
  if (!state.selectedSession || !state.selectedCharacter) return;
  const select = $("#character-checkpoint-select");
  const checkpointDate = select?.value || "";
  if (!checkpointDate) {
    toast("暂无可导出的检查点", "error");
    return;
  }
  setBusy(button, true);
  try {
    const sid = encodeURIComponent(state.selectedSession);
    const cid = encodeURIComponent(state.selectedCharacter);
    const data = await api(`/api/sessions/${sid}/characters/${cid}/checkpoints/${encodeURIComponent(checkpointDate)}`);
    downloadJson(data.filename, data.checkpoint);
    toast("检查点已导出");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function exportCurrentCharacterCheckpoint(button) {
  if (!state.selectedSession || !state.selectedCharacter) return;
  setBusy(button, true);
  try {
    const sid = encodeURIComponent(state.selectedSession);
    const cid = encodeURIComponent(state.selectedCharacter);
    const data = await api(`/api/sessions/${sid}/characters/${cid}/checkpoint-current`);
    downloadJson(data.filename, data.checkpoint);
    toast("当前状态已导出");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function generateCharacterAvatar(button) {
  if (!state.selectedSession || !state.selectedCharacter) return;
  setBusy(button, true);
  try {
    const sid = encodeURIComponent(state.selectedSession);
    const cid = encodeURIComponent(state.selectedCharacter);
    const data = await api(`/api/sessions/${sid}/characters/${cid}/avatar`, { method: "POST" });
    if (data.characters) {
      state.characterData.characters = data.characters;
    } else if (state.characterData?.characters?.[state.selectedCharacter]) {
      state.characterData.characters[state.selectedCharacter].avatar_path = data.avatar_path || "";
      state.characterData.characters[state.selectedCharacter].avatar_updated_at = data.avatar_updated_at || "";
    }
    renderCharacterPool();
    renderCharacterForm();
    toast("头像已生成");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
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

async function importCharacter() {
  if (!state.selectedSession) return;
  const raw = window.prompt("粘贴角色 JSON 或角色检查点 JSON：");
  if (!raw) return;
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    toast("JSON 无效：" + err.message, "error");
    return;
  }
  try {
    await importCharacterPayload(payload);
  } catch (err) {
    toast(err.message, "error");
  }
}
function selectedCharacterImportMode() {
  return $("#character-import-mode")?.value || "basic";
}

async function importCharacterPayload(payload) {
  if (!state.selectedSession) return;
  const sid = encodeURIComponent(state.selectedSession);
  const mode = selectedCharacterImportMode();
  const result = await api(`/api/sessions/${sid}/characters?import_mode=${encodeURIComponent(mode)}`, {
    method: "POST",
    body: payload,
  });
  await loadCharacters();
  const card = payload.character_card || {};
  const id = result.import_result?.character_id || payload.id || payload.character || payload.bot_name || card.id || card.character || card.bot_name;
  if (id && state.characterData?.characters?.[id]) selectCharacter(id);
  await loadMemories();
  await loadDiaries();
  const label = mode === "full" ? "完全覆盖" : mode === "memory" ? "长期记忆" : "基本字段";
  toast(payload.schema ? `检查点已导入（${label}）` : "角色已导入");
}

function importCharacterFile() {
  const input = $("#character-import-file-input");
  if (!input) return;
  input.value = "";
  input.click();
}

async function handleCharacterImportFile(event) {
  const file = event.currentTarget.files?.[0];
  if (!file) return;
  let payload;
  try {
    const text = await file.text();
    payload = JSON.parse(text);
  } catch (err) {
    toast("JSON 文件无效：" + err.message, "error");
    return;
  }
  try {
    await importCharacterPayload(payload);
  } catch (err) {
    toast(err.message, "error");
  }
}
