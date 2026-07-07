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
  selectedLogType: null,
  selectedLogChunk: "",
  profiles: {},
};

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
  ["模型运行参数（模型 profile 在角色页按用户配置；生图后端只读 YAML）", [
    ["default_chat_model_profile", "默认对话模型 profile", "model_select"],
    ["default_fast_model_profile", "默认快速模型 profile", "model_select"],
    ["default_vision_model_profile", "默认视觉模型 profile", "model_select"],
    ["photo_caption_wait_seconds", "纯图片等待配文秒数", "number"],
    ["chat_reply_length", "回复长度", "select:,简短,适中,详细"],
    ["chat_llm_temperature", "回复温度", "text"],
    ["chat_llm_max_tokens", "回复 max_tokens（含思考输出上限）", "text"],
    ["chat_llm_top_p", "回复 top_p（核采样，砍胡话尾巴，留空不下发）", "text"],
    ["chat_llm_frequency_penalty", "回复频率惩罚（抗复读/车轱辘话，留空不下发）", "text"],
    ["chat_llm_presence_penalty", "回复存在惩罚（推话题发散，默认空=关）", "text"],
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
    ["post_chat_push_enabled", "对话后续场推送", "bool"],
    ["post_chat_push_delay_min_minutes", "续场最短延迟(分钟)", "number"],
    ["post_chat_push_delay_max_minutes", "续场最长延迟(分钟)", "number"],
    ["post_chat_push_daily_limit", "每日续场推送上限", "number"],
    ["post_chat_push_cooldown_minutes", "续场冷却(分钟)", "number"],
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
    ["short_context_reset_gap_hours", "短期场景超时小时", "text"],
    ["scene_stale_minutes", "场景断档感知分钟", "number"],
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
  ["关系与背景", [
    ["relationship", "空间关系", "textarea", "wide"],
    ["age_stage", "年龄段", "select:,minor,adult", "third"],
    ["occupation", "职业", "text", "third"],
    ["day_anchor", "白天去向", "select:,company,school,factory,farm,construction,medical,retail,delivery,driver,home,flexible", "third"],
  ]],
  ["边界", [
    ["purity", "纯良度", "number", "third"],
  ]],
];

const commands = ["创建角色", "初始化", "菜单", "帮助", "创建OC", "新建角色", "自拍", "拍照", "天气", "天气设置", "画风", "角色", "外型", "人格", "纯良度", "新场景", "记忆", "记住", "忘记", "推送频率", "调度", "手动推送", "测试生图", "提示词", "生图状态", "管理", "turbo"];

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
  return `
    <section class="form-section character-section runtime-clothing-section">
      <div class="runtime-clothing-head">
        <div>
          <h3>当前衣柜</h3>
          <p>这里改的是当前角色现在穿在身上的衣服；会直接影响聊天后的生图。</p>
        </div>
        <button class="ghost danger" type="button" data-wardrobe-action="clear">清空当前穿搭</button>
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
            <span>只读 · 换装去「衣橱收藏」操作</span>
          </div>
          <div class="runtime-clothing-current">
            <span>当前摘要</span>
            <strong title="${escapeHtml(current)}">${escapeHtml(currentSummary || "未设置")}</strong>
          </div>
          ${wardrobeRows(clothing.wardrobe || {}, { displayNames: clothing.wardrobe_display || {} })}
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
  panel.addEventListener("click", async event => {
    const button = event.target.closest("[data-wardrobe-action]");
    if (!button || !panel.contains(button)) return;
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

function delay(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms));
}

function switchView(name) {
  $all(".nav").forEach(btn => btn.classList.toggle("active", btn.dataset.view === name));
  $all(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  $("#view-title").textContent = viewMeta[name][0];
  $("#view-subtitle").textContent = viewMeta[name][1];
  if (name === "overview") loadFeedbackBoard();
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
  loadFeedbackBoard();
  // 获取模型 profile 列表供配置页下拉框使用
  try {
    const modelData = await api(modelApiUrl());
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
  extraNodes.forEach(node => wrap.appendChild(node));
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
  const runtimeClothing = renderRuntimeClothingPanel(char, isActive);
  if (runtimeClothing) {
    const wrap = document.createElement("div");
    wrap.innerHTML = runtimeClothing;
    form.appendChild(wrap.firstElementChild);
  }

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
  form.querySelectorAll(".runtime-clothing-section .section-toggle").forEach(toggle => {
    toggle.onclick = () => {
      const grid = toggle.parentElement?.querySelector(".runtime-clothing-grid");
      if (!grid) return;
      grid.classList.toggle("collapsed");
      toggle.classList.toggle("collapsed");
    };
  });
  bindRuntimeClothingHandlers(form);

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
  const charKey = rawCharKey;
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
  const charKey = encodeURIComponent(rawCharKey);
  try {
    const data = await api(`/api/sessions/${sid}/memories?character_key=${charKey}`);
    if (state.selectedCharacter !== rawCharKey) return;
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
  const charKey = encodeURIComponent(rawCharKey);
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
    return `${profile} / ${model} / ${configured}`;
  };
  const adminScopeControl = state.auth?.role === "admin"
    ? `<label>保存范围<select name="_scope"><option value="user">当前用户私有</option><option value="global">全局 profile</option></select></label>`
    : "";
  box.innerHTML = `
    <form id="model-settings-form" class="model-settings-form">
      <label>对话模型<select name="chat_profile_id"><option value="">默认</option>${options}</select></label>
      <label>快速模型<select name="fast_profile_id"><option value="">默认</option>${options}</select></label>
      <label>视觉模型<select name="vision_profile_id"><option value="">关闭</option>${options}</select></label>
      <button class="primary" type="submit">保存模型选择</button>
    </form>
    <div class="model-current">
      <div>当前用户: ${escapeHtml(data.user_id || selectedModelUserId() || "-")}</div>
      <div>对话: ${escapeHtml(resolvedText("chat"))}</div>
      <div>快速: ${escapeHtml(resolvedText("image"))}</div>
      <div>视觉: ${escapeHtml(resolvedText("vision"))}</div>
    </div>
    <form id="model-profile-form" class="inline-manager-form">
      ${adminScopeControl}
      <label>Profile ID<input name="profile_id" placeholder="deepseek-v4-pro" autocomplete="off"></label>
      <label>显示名称<input name="name" placeholder="DeepSeek V4 Pro" autocomplete="off"></label>
      <label>Base URL<input name="base_url" placeholder="https://api.example.com/v1" autocomplete="off"></label>
      <label>API Key<input name="api_key" type="password" placeholder="留空保留旧密钥" autocomplete="new-password"></label>
      <label>模型名<input name="model" placeholder="deepseek-chat" autocomplete="off"></label>
      <label>最大 tokens<input name="max_tokens" type="number" min="1" step="1" placeholder="可选"></label>
      <label>超时秒数<input name="timeout" type="number" min="1" step="1" placeholder="可选"></label>
      <button type="submit">保存模型 profile</button>
      <p class="muted">仅填写常用模型字段；api_key 返回时会显示为 ********，保存空值或 ******** 会保留原密钥。Thinking 使用默认策略。</p>
    </form>
  `;
  box.querySelector("[name=chat_profile_id]").value = settings.chat_profile_id || "";
  box.querySelector("[name=fast_profile_id]").value = settings.fast_profile_id || "";
  box.querySelector("[name=vision_profile_id]").value = settings.vision_profile_id || "";
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
    if (values._scope) body._scope = values._scope;
    await api(modelApiUrl(`/api/models/${encodeURIComponent(profileId)}`), { method: "POST", body });
    await loadModels();
    toast("模型 profile 已保存");
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
  const isLong = kind === "long";
  const label = isLong ? "长期目标" : "中期目标";
  const text = window.prompt(`${label}文本：`, existing?.text || "");
  if (text === null) return null;
  const cleanText = text.trim();
  if (!cleanText) {
    toast("目标文本不能为空", "error");
    return null;
  }
  const payload = { kind, text: cleanText };
  if (existing?.id) payload.id = existing.id;
  if (isLong) {
    const dimension = window.prompt("目标维度（如生活、理想、爱好、事业；可自定义）：", existing?.dimension || "");
    if (dimension === null) return null;
    payload.dimension = dimension.trim();
    const motivation = window.prompt("内在动机（可留空）：", existing?.motivation || "");
    if (motivation === null) return null;
    payload.motivation = motivation.trim();
  } else {
    const plan = currentLifePlan();
    const activeLongs = (plan.long_goals || []).filter(item => (item.status || "active") === "active");
    const parentHint = activeLongs.map(item => `${item.id}${item.dimension ? `(${item.dimension})` : ""}: ${item.text}`).join("\n");
    const fallbackParent = existing?.parent_id || activeLongs[0]?.id || "";
    const parentId = window.prompt(`承接的长期目标 ID：\n${parentHint || "暂无长期目标"}`, fallbackParent);
    if (parentId === null) return null;
    payload.parent_id = parentId.trim();
    const progress = window.prompt("进展备注（可留空）：", existing?.progress_note || "");
    if (progress === null) return null;
    payload.progress_note = progress.trim();
  }
  const status = window.prompt("状态 active / achieved / abandoned：", existing?.status || "active");
  if (status === null) return null;
  payload.status = status.trim() || "active";
  return payload;
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
      const payload = promptLifeGoalPayload(kind);
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
      const payload = promptLifeGoalPayload(kind, existing);
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
  systemLogs.forEach(item => {
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

async function selectLog(chatId, chunk = null) {
  const sameLog = state.selectedLog === chatId && !state.selectedLogType;
  state.selectedLog = chatId;
  state.selectedLogType = null;
  const chosenChunk = chunk === null ? (sameLog ? state.selectedLogChunk : "") : chunk;
  $("#log-title").textContent = `日志 · ${chatId}`;
  $all("#log-list .session-item").forEach(btn => btn.classList.toggle("active", btn.dataset.chat === chatId));
  try {
    const suffix = chosenChunk ? `&chunk=${encodeURIComponent(chosenChunk)}` : "";
    const data = await api(`/api/logs/${encodeURIComponent(chatId)}?tail=1000${suffix}`);
    const box = $("#log-content");
    state.selectedLogChunk = data.chunk || chosenChunk || "";
    renderLogChunkSelector(data.chunks || [], state.selectedLogChunk, nextChunk => selectLog(chatId, nextChunk));
    box.textContent = data.content || "（空）";
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    hideLogChunkSelector();
    $("#log-content").textContent = err.message;
  }
}

async function selectSystemLog(logType, chunk = null) {
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
      const suffix = chosenChunk ? `?chunk=${encodeURIComponent(chosenChunk)}` : "";
      const data = await api(`/api/logs/llm-debug${suffix}`);
      state.selectedLogChunk = data.chunk || chosenChunk || "";
      renderLogChunkSelector(data.chunks || [], state.selectedLogChunk, nextChunk => selectSystemLog(logType, nextChunk));
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
      box.textContent = text || "暂无 LLM 调试日志";
    } else if (logType === "system-errors") {
      hideLogChunkSelector();
      const data = await api("/api/logs/system-errors?limit=500");
      const errors = data.errors || [];
      if (errors.length === 0) {
        box.textContent = "暂无错误日志";
      } else {
        box.textContent = errors.map(formatSystemErrorEntry).join("\n\n---\n\n");
      }
    }
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    hideLogChunkSelector();
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
  document.addEventListener("click", handleLifePlanAction);
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
  $("#reload-logs").onclick = () => loadLogs().then(() => toast("日志列表已刷新"));
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
    setBusy(btn, true);
    try {
      await api("/api/feedback", {
        method: "POST",
        body: { session_id: state.selectedSession || "", content },
      });
      form.reset();
      await loadFeedbackBoard();
      toast("反馈已提交");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setBusy(btn, false);
    }
  };
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
      if (!state.selectedCharacter) { toast("请先选择角色", "error"); return; }
      const mode = btn.dataset.mode;
      const charLabel = state.characterData?.characters?.[state.selectedCharacter]?.character || state.selectedCharacter;
      if (!window.confirm(`确定为 ${charLabel} 触发 ${mode} 模式手动推送吗？`)) return;
      setBusy(btn, true);
      try {
        const data = await api(`/api/sessions/${encodeURIComponent(state.selectedSession)}/test-push`, {
          method: "POST",
          body: { character_key: state.selectedCharacter, mode },
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
  $all(".panel-head.collapsible").forEach(head => {
    head.onclick = () => {
      const body = head.nextElementSibling;
      if (body) body.classList.toggle("collapsed");
      head.classList.toggle("collapsed");
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
