"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const cacheModule = require("../../telegram_comfyui_selfie/static/browser_cache.js");
const core = require("../../telegram_comfyui_selfie/static/frontend_core.js");
const read = file => fs.readFileSync(path.join(__dirname, "../../telegram_comfyui_selfie/static", file), "utf8");
function deferred() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}
function element() {
  return {innerHTML: "", dataset: {}, disabled: false, controls: [], notice: null,
    setAttribute(name, value) {this[name] = value;},
    querySelector(selector) {return selector.includes("cache-notice") ? this.notice : null;},
    querySelectorAll(selector) {return selector === "[data-cache-disabled]" ? this.controls.filter(c => c.dataset.cacheDisabled) : this.controls;},
    prepend(notice) {this.notice = notice; notice.remove = () => {this.notice = null;};},
  };
}
function appFixture(fetch) {
  const entries = new Map();
  const storage = {getItem: k => entries.get(k) || null, setItem: (k, v) => entries.set(k, v), removeItem: k => entries.delete(k)};
  const elements = new Map();
  const document = {querySelectorAll: () => [], createElement: () => element(),
    querySelector: selector => {
      if (selector.startsWith(".nav")) return null;
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    },
  };
  // 只跳过入口的自动启动，实际请求、初始化和选择函数都在 VM 内执行。
  const source = read("app.js");
  const context = vm.createContext({document, fetch, console,
    window: {SucyuFrontendCore: core, SucyuBrowserCache: {create: options => cacheModule.create({...options, storage})}, location: {hash: ""}},
  });
  vm.runInContext(source.slice(0, source.lastIndexOf("\nloadCommandSelect();")), context);
  vm.runInContext("globalThis.testState = state; globalThis.testCache = browserCache;", context);
  for (const name of ["renderChatIdOptions", "renderWorldSessionList", "loadFeedbackBoard", "renderStatus", "renderConfig", "loadGlobalModels", "applyHashRoute", "renderWardrobePanel", "toast"]) context[name] = () => {};
  return {context, storage, elements, state: context.testState, cache: context.testCache};
}

test("loadAll 验证登录后恢复管理员选中用户，切换即保存，已删除的用户先回退再取模型", async () => {
  const auth = {role: "admin", user_id: "admin"};
  let sessions = [{session_id: "telegram:1"}, {session_id: "telegram:8"}];
  const calls = [];
  const ui = appFixture(async (url, options) => {
    calls.push(url);
    assert.equal(options.cache, "no-store");
    const data = url === "/api/auth/me" ? {auth} : url === "/api/sessions" ? {sessions} : url === "/api/status" ? {status: {}} : {};
    return {ok: true, status: 200, text: async () => JSON.stringify(data)};
  });
  const seed = cacheModule.create({storage: ui.storage});
  seed.setIdentity(auth); seed.rememberSelection("telegram:8", "telegram:1");
  await ui.context.loadAll();
  assert.equal(calls[0], "/api/auth/me");
  assert.equal(ui.state.selectedSession, "telegram:8");
  assert.equal(ui.state.selectedWorldSession, "telegram:1");
  assert.ok(calls.some(url => url === "/api/models?user_id=8"));
  await ui.context.selectSession("telegram:1");
  assert.equal(ui.cache.restoreSelection().sessionId, "telegram:1");
  await ui.context.selectSession("telegram:8");
  sessions = [{session_id: "telegram:1"}];
  calls.length = 0;
  await ui.context.loadAll();
  assert.equal(ui.state.selectedSession, "telegram:1");
  assert.ok(!calls.includes("/api/models?user_id=8"));
  assert.ok(calls.includes("/api/models?user_id=1"));
});

test("缓存只读标记禁用编辑，刷新后只恢复缓存所禁用的控件", () => {
  const ui = appFixture();
  const box = element();
  const input = element(), permissionDisabled = element();
  permissionDisabled.disabled = true;
  box.controls = [input, permissionDisabled];
  ui.context.showCachedRecord(box, true);
  assert.equal(input.disabled, true);
  assert.match(box.notice.textContent, /上次记录/);
  ui.context.showCachedRecord(box, false);
  assert.equal(input.disabled, false);
  assert.equal(permissionDisabled.disabled, true);
  assert.equal(box.notice, null);
  assert.equal(box["aria-busy"], "false");
});

test("角色读取先渲染只读缓存，切换用户后旧请求不能覆盖当前用户", async () => {
  const a = deferred(), b = deferred();
  const elements = new Map();
  const state = {selectedSession: "telegram:1", selectedCharacter: null};
  const renders = [];
  const context = vm.createContext({state, $: selector => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  }, api: (url, options) => {
    options.onCached({active_id: "cached", characters: {cached: {persona: url}}});
    return url.includes("%3A1") ? a.promise : b.promise;
  }, showCachedRecord() {}, escapeHtml: String, toast() {}});
  vm.runInContext(read("character_ui.js"), context);
  context.renderCharacterPool = () => {};
  context.renderCharacterForm = () => renders.push([state.selectedSession, state.selectedCharacter, state.characterDataCached]);
  const old = context.loadCharacters();
  assert.deepEqual(renders.at(-1), ["telegram:1", "cached", true]);
  assert.equal(elements.get("#character-activate").disabled, true);
  state.selectedSession = "telegram:2";
  const next = context.loadCharacters();
  b.resolve({active_id: "fresh", characters: {fresh: {}}}); await next;
  a.resolve({active_id: "old", characters: {old: {}}}); await old;
  assert.equal(state.selectedCharacter, "fresh");
  assert.deepEqual(renders.at(-1), ["telegram:2", "fresh", false]);
});

test("日记缓存立即显示且只读，旧角色返回不覆盖新角色，最新响应解除只读", async () => {
  const first = deferred(), second = deferred();
  const box = element();
  const state = {selectedSession: "telegram:1", selectedCharacter: "A"};
  const renders = [], readonly = [];
  const context = vm.createContext({state, $: () => box, escapeHtml: String, toast() {},
    showCachedRecord: (_, cached) => readonly.push(cached),
    api: (url, options) => {
      options.onCached({diaries: [{content: "缓存"}]});
      return url.includes("character_key=A") ? first.promise : second.promise;
    },
  });
  vm.runInContext(read("character_ui.js"), context);
  context.renderDiaries = diaries => renders.push(diaries[0].content);
  const old = context.loadDiaries();
  assert.equal(renders.at(-1), "缓存"); assert.equal(readonly.at(-1), true);
  state.selectedCharacter = "B";
  const next = context.loadDiaries();
  second.resolve({diaries: [{content: "B 最新"}]}); await next;
  first.resolve({diaries: [{content: "A 旧请求"}]}); await old;
  assert.equal(renders.at(-1), "B 最新"); assert.equal(readonly.at(-1), false);
});

test("动线用户选择立即保存，过期动线请求不会混入另一用户", async () => {
  const first = deferred(), second = deferred();
  const state = {selectedWorldSession: "telegram:1"};
  const remembered = [], rendered = [];
  const context = vm.createContext({state, $: () => element(),
    rememberSessionSelection: () => remembered.push(state.selectedWorldSession),
    api: url => url.endsWith("%3A1") ? first.promise : second.promise,
    toast() {}, escapeHtml: String,
  });
  vm.runInContext(read("world_ui.js"), context);
  context.renderWorldSessionList = () => {};
  context.renderWorldRoute = world => rendered.push(world.user);
  const old = context.loadWorldRoute();
  const next = context.selectWorldSession("telegram:2");
  assert.equal(remembered.at(-1), "telegram:2");
  second.resolve({world: {user: 2}}); await next;
  first.resolve({world: {user: 1}}); await old;
  assert.deepEqual(rendered, [2]);
});
