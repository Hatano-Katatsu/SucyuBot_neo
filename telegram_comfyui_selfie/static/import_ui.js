"use strict";

// 导入只维护资料，不向 Telegram 发消息。所有文件文字通过 textContent/转义展示。
async function uploadTavernFile(file, sessionId) {
  if (!sessionId) { toast("请先选择用户", "error"); return; }
  if (file.size > 12 * 1024 * 1024) { toast("文件不能超过 12 MiB", "error"); return; }
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/imports?filename=${encodeURIComponent(file.name || "角色.json")}`, {
    method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/octet-stream"}, body: file,
  });
  const raw = await res.text();
  let data;
  try { data = frontendCore.parseApiResponse(res, raw); }
  catch (err) { if (err.authExpired) window.location.href = "/"; throw err; }
  showTavernImport(data.draft.id, sessionId);
}

function chooseWorldImport() {
  const sid = state.selectedWorldSession;
  if (!sid) return toast("请先选择用户", "error");
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,.png,application/json,image/png";
  input.onchange = async () => {
    if (!input.files[0]) return;
    try { await uploadTavernFile(input.files[0], sid); }
    catch (err) { toast(err.message, "error"); }
  };
  input.click();
}

function importField(label, name, value, multiline = false) {
  return `<label class="field"><span>${escapeHtml(label)}</span>${multiline
    ? `<textarea name="${name}" rows="5">${escapeHtml(value || "")}</textarea>`
    : `<input name="${name}" value="${escapeHtml(value || "")}">`}</label>`;
}

function importWorldLocation(world, prefix = "") {
  return `<label class="field"><span>世界类型</span><select name="${prefix}kind"><option value="real" ${world.kind === "real" ? "selected" : ""}>现实世界</option><option value="fictional" ${world.kind !== "real" ? "selected" : ""}>虚构世界</option></select></label>${importField("现实城市（虚构世界不使用）", prefix + "city", world.city)}`;
}

function showTavernImport(draftId, sid) {
  document.getElementById("tavern-import-dialog")?.remove();
  const dialog = document.createElement("dialog");
  dialog.id = "tavern-import-dialog";
  dialog.className = "import-dialog";
  dialog.innerHTML = '<h3>导入角色与世界背景</h3><div class="import-body" aria-live="polite">正在自动整理资料…</div><div class="form-actions"><button class="btn" data-close>稍后查看</button></div>';
  document.body.appendChild(dialog);
  dialog.querySelector("[data-close]").onclick = () => dialog.close();
  let timer;
  dialog.onclose = () => { clearTimeout(timer); dialog.remove(); };
  dialog.showModal();
  const path = `/api/sessions/${encodeURIComponent(sid)}/imports/${encodeURIComponent(draftId)}`;
  // 保存当前用户范围的草稿入口，刷新页面后仍可恢复。
  sessionStorage.setItem(`tavern-draft:${sid}`, draftId);
  async function refresh() {
    if (!dialog.isConnected || !dialog.open) return;
    try {
      const {draft} = await api(path);
      if (!dialog.isConnected) return;
      const box = dialog.querySelector(".import-body");
      if (draft.status === "pending") {
        box.textContent = draft.progress || "正在自动整理资料…";
        timer = setTimeout(refresh, 1800);
        return;
      }
      if (draft.status === "failed") {
        box.textContent = draft.error || "转换失败";
        const retry = document.createElement("button");
        retry.className = "btn"; retry.textContent = "重新转换";
        retry.onclick = async () => {
          retry.disabled = true;
          try { await api(path + "/retry", {method: "POST"}); timer = setTimeout(refresh, 800); }
          catch (err) { toast(err.message, "error"); retry.disabled = false; }
        };
        box.appendChild(retry);
        return;
      }
      if (draft.status === "committed") {
        sessionStorage.removeItem(`tavern-draft:${sid}`);
        box.textContent = "已创建，可在角色页或世界背景中查看。";
        return;
      }
      const value = draft.converted || {}, card = value.character || {}, world = value.world || {};
      box.innerHTML = `<form class="import-form">
        ${card.bot_name ? '<label class="field"><span>保存方式</span><select name="merge_character_id"><option value="">创建新角色</option></select></label><details class="merge-preview" hidden open><summary>将修改的角色资料</summary><pre class="import-summary"></pre><p>仅合并角色资料与背景关联，保留聊天、记忆和头像。</p></details>' : ""}
        ${card.bot_name ? importField("角色名称", "bot_name", card.bot_name) + importField("人设", "persona", card.persona, true) + importField("稳定外貌", "appearance", card.appearance, true) + importField("初始穿搭", "outfit", card.outfit) : ""}
        ${importField("世界名称", "world_name", world.name)}${importField("世界背景", "world_summary", world.summary, true)}
        ${importWorldLocation(world, "world_")}
        <label class="field"><span>图片分享方式</span><select name="world_photography"><option value="modern" ${world.photography === "modern" ? "selected" : ""}>允许自拍和现代摄影</option><option value="scene" ${world.photography !== "modern" ? "selected" : ""}>保留原设定，分享场景画面</option></select></label>
        <p class="muted">已整理 ${(world.entries || []).length} 项地点、人物与背景。创建后仍可编辑，不会切换当前角色或发送消息。</p>
        <details><summary>查看整理结果与待定内容</summary><pre class="import-summary"></pre></details>
        <div class="form-actions"><button class="btn primary" type="submit">${card.bot_name ? "创建角色与背景" : "创建世界背景"}</button></div>
      </form>`;
      box.querySelector("details:not(.merge-preview) pre").textContent = JSON.stringify({entries: world.entries, unresolved: value.unresolved, warnings: value.warnings}, null, 2);
      const mergeSelect = box.querySelector('[name="merge_character_id"]');
      const targets = draft.merge_targets || [];
      if (mergeSelect) {
        for (const target of targets) {
          const option = document.createElement("option"); option.value = target.id; option.textContent = `合并到 ${target.id}`; mergeSelect.appendChild(option);
        }
        const updateDiff = () => {
          const target = targets.find(t => t.id === mergeSelect.value), preview = box.querySelector(".merge-preview");
          preview.hidden = !target;
          box.querySelector('[type="submit"]').textContent = target ? "保存合并" : "创建角色与背景";
          if (target) {
            const form = Object.fromEntries(new FormData(box.querySelector("form")));
            const after = {...card, bot_name: form.bot_name, persona: form.persona, appearance: form.appearance, outfit: form.outfit};
            const changes = Object.entries(after).filter(([key, value]) => JSON.stringify(target.card[key] ?? "") !== JSON.stringify(value)).map(([key, value]) => ({field: key, before: target.card[key] ?? "", after: value}));
            changes.push({field: "世界背景关联", before: target.card.world_id || "未关联", after: form.world_name});
            preview.querySelector("pre").textContent = JSON.stringify(changes, null, 2);
          }
        };
        box.querySelector("form").addEventListener("input", updateDiff);
        mergeSelect.onchange = updateDiff;
      }
      box.querySelector("form").onsubmit = async event => {
        event.preventDefault();
        const button = event.currentTarget.querySelector('[type="submit"]');
        setBusy(button, true);
        try {
          const body = Object.fromEntries(new FormData(event.currentTarget));
          if (body.merge_character_id) body.merge_version = targets.find(t => t.id === body.merge_character_id)?.version;
          await api(path + "/commit", {method: "POST", body});
          sessionStorage.removeItem(`tavern-draft:${sid}`);
          dialog.close();
          if (state.selectedSession === sid) await loadCharacters();
          if (state.selectedWorldSession === sid) await loadWorldRoute();
          toast("导入完成");
        } catch (err) { toast(err.message, "error"); setBusy(button, false); }
      };
    } catch (err) {
      if (dialog.isConnected) dialog.querySelector(".import-body").textContent = err.message;
    }
  }
  refresh();
}

async function loadWorldProfiles(sid) {
  const box = document.getElementById("world-profiles");
  if (!box || !sid) return;
  const [{worlds, place_types}, characters] = await Promise.all([api(`/api/sessions/${encodeURIComponent(sid)}/worlds`), api(`/api/sessions/${encodeURIComponent(sid)}/characters`)]);
  if (state.selectedWorldSession !== sid) return;
  box.innerHTML = "";
  const draftId = sessionStorage.getItem(`tavern-draft:${sid}`);
  if (draftId) {
    const resume = document.createElement("button"); resume.className = "btn"; resume.textContent = "继续上次导入";
    resume.onclick = () => showTavernImport(draftId, sid); box.appendChild(resume);
  }
  for (const row of worlds || []) {
    const world = row.data || {}, panel = document.createElement("details");
    panel.innerHTML = `<summary>${escapeHtml(world.name || "世界背景")}</summary><form>
      ${importField("名称", "name", world.name)}${importField("背景", "summary", world.summary, true)}
      ${importWorldLocation(world)}
      <label class="field"><span>图片分享方式</span><select name="photography"><option value="modern" ${world.photography === "modern" ? "selected" : ""}>自拍与生活照片</option><option value="scene" ${world.photography !== "modern" ? "selected" : ""}>保留世界设定的场景画面</option></select></label>
      <details><summary>地点和人物详情</summary><div class="world-entry-editors"></div></details>
      <details><summary>来源与待整理内容</summary><pre class="import-summary world-sources"></pre></details>
      <button type="submit" class="btn">保存背景</button></form>
      <p class="world-linked muted"></p>
      <div class="form-actions"><select aria-label="关联角色"><option value="">选择要关联的角色</option></select><button class="btn" data-bind>关联此背景</button><button class="btn" data-unbind>解除关联</button></div>`;
    panel.querySelector(".world-sources").textContent = JSON.stringify({unresolved: world.unresolved || [], sources: world.sources || []}, null, 2);
    panel.querySelector(".world-linked").textContent = "本会话关联角色：" + (Object.entries(characters.characters || {}).filter(([, c]) => c.world_id === row.id).map(([key]) => key).join("、") || "暂无");
    const editors = panel.querySelector(".world-entry-editors");
    for (const entry of world.entries || []) {
      const item = document.createElement("div"); item.className = "world-entry";
      item.dataset.id = entry.id;
      item.innerHTML = `${importField("名称", "entry_name", entry.name)}${importField("内容", "entry_content", entry.content, true)}<label><input type="checkbox" name="entry_enabled" ${entry.enabled ? "checked" : ""}>启用</label> <label><input type="checkbox" name="entry_known" ${entry.known ? "checked" : ""}>角色已知</label>`;
      if (entry.type === "place") {
        const label = document.createElement("label"); label.className = "field"; label.textContent = "动线用途";
        const places = document.createElement("select"); places.name = "entry_place_key";
        for (const [key, name] of Object.entries({"": "未指定", ...(place_types || {})})) {
          const option = document.createElement("option"); option.value = key; option.textContent = name; option.selected = key === (entry.place_key || ""); places.appendChild(option);
        }
        label.appendChild(places); item.appendChild(label);
      }
      editors.appendChild(item);
    }
    panel.querySelector("form").onsubmit = async event => {
      event.preventDefault();
      const body = Object.fromEntries(new FormData(event.currentTarget));
      body.revision = row.revision;
      body.entries = [...editors.children].map(el => ({id: el.dataset.id, name: el.querySelector('[name="entry_name"]').value, content: el.querySelector('[name="entry_content"]').value, enabled: el.querySelector('[name="entry_enabled"]').checked, known: el.querySelector('[name="entry_known"]').checked, place_key: el.querySelector('[name="entry_place_key"]')?.value || ""}));
      try { await api(`/api/sessions/${encodeURIComponent(sid)}/worlds/${row.id}`, {method: "PUT", body}); await loadWorldProfiles(sid); toast("背景已保存"); }
      catch (err) { toast(err.message, "error"); }
    };
    const select = panel.querySelector('[aria-label="关联角色"]');
    for (const [key, card] of Object.entries(characters.characters || {})) {
      if (card.is_default) continue;
      const option = document.createElement("option"); option.value = key; option.textContent = card.bot_name || key; select.appendChild(option);
    }
    panel.querySelector("[data-bind]").onclick = async () => {
      if (!select.value) return;
      try { await api(`/api/sessions/${encodeURIComponent(sid)}/world-binding`, {method: "POST", body: {world_id: row.id, character_id: select.value}}); toast("已关联"); await loadWorldRoute(); }
      catch (err) { toast(err.message, "error"); }
    };
    panel.querySelector("[data-unbind]").onclick = async () => {
      if (!select.value || characters.characters?.[select.value]?.world_id !== row.id) return toast("请选择已关联此背景的角色", "error");
      try { await api(`/api/sessions/${encodeURIComponent(sid)}/world-binding`, {method: "POST", body: {world_id: "", character_id: select.value}}); await loadWorldRoute(); toast("已解除关联"); }
      catch (err) { toast(err.message, "error"); }
    };
    box.appendChild(panel);
  }
}
