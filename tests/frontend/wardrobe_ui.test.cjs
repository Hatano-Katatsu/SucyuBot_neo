"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../../telegram_comfyui_selfie/static/character_ui.js"), "utf8");

function fixture(api) {
  const target = {isConnected: true, children: [], replaceChildren() {this.children = [];}, append(x) {this.children.push(x);}};
  const button = {disabled: true};
  const status = {};
  const elements = {"#outfit-preview-image": target, "#outfit-preview-generate": button, "#outfit-preview-status": status};
  const state = {selectedSession: "telegram:123", selectedCharacter: "角色", characterData: {characters: {角色: {}}}};
  const context = vm.createContext({state, api, $: selector => elements[selector], document: {createElement: tag => ({tag, setAttribute() {}})}});
  vm.runInContext(source, context);
  return {context, state, target, button, status};
}

test("有缓存时直接展示，手动刷新使用同一搭配键", async () => {
  const calls = [];
  const ui = fixture(async (url, options) => {
    calls.push({url, options});
    return {preview: {key: "outfit-key", cached: true, image_url: options ? "/fresh.png" : "/saved.png"}};
  });
  await ui.context.loadOutfitPreview();
  assert.equal(calls.length, 1);
  assert.equal(ui.target.children[0].src, "/saved.png");
  await ui.button.onclick();
  assert.equal(calls[1].options.method, "POST");
  assert.match(calls[1].url, /key=outfit-key&refresh=1$/);
  assert.equal(ui.target.children[0].src, "/fresh.png");
  assert.equal(ui.button.disabled, false);
});

test("切换会话后旧预览请求不得覆盖新页面", async () => {
  let resolve;
  const ui = fixture(() => new Promise(done => {resolve = done;}));
  const pending = ui.context.loadOutfitPreview();
  ui.state.selectedSession = "telegram:456";
  resolve({preview: {key: "old", cached: true, image_url: "/other-user.png"}});
  await pending;
  assert.equal(ui.target.children.length, 0);
  assert.equal(ui.button.onclick, undefined);
});

test("生成失败保留旧预览并允许重试", async () => {
  const ui = fixture(async (_, options) => {
    if (options) throw new Error("上游暂不可用");
    return {preview: {key: "saved", cached: true, image_url: "/saved.png"}};
  });
  await ui.context.loadOutfitPreview();
  await ui.button.onclick();
  assert.equal(ui.target.children[0].src, "/saved.png");
  assert.equal(ui.status.textContent, "上游暂不可用");
  assert.equal(ui.button.disabled, false);
});

test("日记默认收起并按页显示，翻页不会遗漏边界记录", () => {
  let pageButtons;
  const box = {
    innerHTML: "", scrollIntoView() {},
    querySelectorAll(selector) {
      if (selector !== "[data-diary-page]") return [];
      pageButtons = [...this.innerHTML.matchAll(/data-diary-page="(-?\d+)"/g)].map(match => ({dataset: {diaryPage: match[1]}}));
      return pageButtons;
    },
  };
  const context = vm.createContext({$: selector => selector === "#diary-manager" ? box : {}, escapeHtml: value => String(value).replaceAll("<", "&lt;")});
  vm.runInContext(source, context);
  const diaries = Array.from({length: 7}, (_, i) => ({diary_date: `2026-09-0${i + 1}`, content: `<script>entry ${i}</script>`, updated_at: 1}));
  context.renderDiaries(diaries, "sid", "character");
  assert.equal((box.innerHTML.match(/<details class="diary-note">/g) || []).length, 6);
  assert.doesNotMatch(box.innerHTML, /<script>|entry 6/);
  pageButtons[1].onclick();
  assert.equal((box.innerHTML.match(/<details class="diary-note">/g) || []).length, 1);
  assert.match(box.innerHTML, /entry 6/);
  assert.doesNotMatch(box.innerHTML, /entry 5/);
  pageButtons[0].onclick();
  assert.match(box.innerHTML, /entry 0/);
});
