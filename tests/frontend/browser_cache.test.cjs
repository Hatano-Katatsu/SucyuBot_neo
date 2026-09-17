"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const {create} = require("../../telegram_comfyui_selfie/static/browser_cache.js");
const {resolveSelectedSession} = require("../../telegram_comfyui_selfie/static/frontend_core.js");
const admin = {role: "admin", user_id: "admin", token: "never-store"};
const user = {role: "user", user_id: "2"};
const path = "/api/sessions/telegram%3A1/characters";
function storage() {
  const values = new Map();
  return {values, getItem: k => values.get(k) ?? null, setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k)};
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}

test("下次访问先显示上次记录，仍请求并保存最新内容，同路径请求合并", async () => {
  const disk = storage();
  const first = create({storage: disk, request: async () => ({characters: {旧角色: {persona: "之前"}}})});
  first.setIdentity(admin);
  await first.api(path);
  const network = deferred();
  let calls = 0;
  const next = create({storage: disk, request: () => {calls++; return network.promise;}});
  next.setIdentity(admin);
  let shown;
  const a = next.api(path, {onCached: data => {shown = data;}});
  const b = next.api(path);
  assert.equal(shown.characters.旧角色.persona, "之前");
  await Promise.resolve();
  assert.equal(calls, 1);
  network.resolve({characters: {新角色: {persona: "现在"}}});
  assert.deepEqual(await a, await b);
  assert.equal(next.read(path).characters.新角色.persona, "现在");
});

test("缓存按登录身份和请求中的会话角色隔离，切换账号清理原账号记录", async () => {
  const disk = storage();
  const cache = create({storage: disk, request: async url => ({summary: url})});
  cache.setIdentity(admin);
  const a = "/api/sessions/telegram%3A1/memories?character_key=A";
  const b = "/api/sessions/telegram%3A2/memories?character_key=B";
  await cache.api(a); await cache.api(b);
  assert.equal(cache.read(a).summary, a);
  assert.equal(cache.read(b).summary, b);
  cache.setIdentity(user);
  assert.equal(cache.read(a), null);
  assert.ok(![...disk.values.values()].some(v => v.includes("character_key=A")));
  cache.setIdentity(admin);
  assert.equal(cache.read(a), null);
});

test("只持久化白名单记录，密钥、身份令牌和导入原文不落盘", async () => {
  const disk = storage();
  const data = {summary: "记录", api_key: "key-value", nested: {token: "token-value", password: "pw"}, import_source: "raw", world_snapshot: "raw-world"};
  const cache = create({storage: disk, request: async () => data});
  cache.setIdentity(admin);
  for (const url of [path, "/api/models", "/api/config", "/api/auth/me", "/api/logs", "/api/world/sid"]) await cache.api(url);
  const saved = [...disk.values.values()].join("\n");
  assert.doesNotMatch(saved, /key-value|token-value|never-store|raw-world|"password"|\/api\/config|\/api\/models/);
  assert.equal(cache.read(path).summary, "记录");
  assert.equal(cache.read("/api/config"), null);
});

test("管理员选择跨重开保留，账号独立，失效用户回退，普通用户固定自己的身份", () => {
  const disk = storage();
  let cache = create({storage: disk});
  cache.setIdentity(admin);
  cache.rememberSelection("telegram:8", "telegram:9");
  cache = create({storage: disk}); cache.setIdentity(admin);
  assert.deepEqual(cache.restoreSelection(), {sessionId: "telegram:8", worldSessionId: "telegram:9"});
  const sessions = [{session_id: "telegram:1"}, {session_id: "telegram:8"}];
  assert.equal(resolveSelectedSession(sessions, cache.restoreSelection().sessionId, admin), "telegram:8");
  assert.equal(resolveSelectedSession(sessions, "telegram:deleted", admin), "telegram:1");
  cache.setIdentity(user); cache.rememberSelection("telegram:1", "telegram:9");
  assert.deepEqual(cache.restoreSelection(), {});
  assert.equal(resolveSelectedSession(sessions, "telegram:8", user), "telegram:2");
  cache.setIdentity({...admin, user_id: "another"});
  assert.deepEqual(cache.restoreSelection(), {sessionId: "", worldSessionId: ""});
  cache.setIdentity(admin);
  assert.equal(cache.restoreSelection().sessionId, "telegram:8");
});

test("缓存一天后失效，限制单条大小、总量和条目数", async () => {
  const disk = storage();
  let time = 100;
  let data = {diaries: [{content: "正常"}]};
  const cache = create({storage: disk, now: () => time, request: async () => data});
  cache.setIdentity(admin); await cache.api(path);
  time += 24 * 60 * 60 * 1000;
  assert.equal(cache.read(path), null);
  data = {summary: "长".repeat(270000)};
  await cache.api(path);
  assert.equal(cache.read(path), null);
  data = {summary: "文".repeat(30000)};
  for (let n = 0; n < 40; n++) {time++; await cache.api(`/api/sessions/${n}/diaries`);}
  const records = JSON.parse([...disk.values.entries()].find(([k]) => k.includes(":records:"))[1]);
  assert.ok(Object.keys(records).length <= 32);
  assert.ok(JSON.stringify(records).length * 2 < 2 * 1024 * 1024);
  assert.equal(cache.read("/api/sessions/0/diaries"), null);
  assert.ok(cache.read("/api/sessions/39/diaries"));
});

test("损坏或禁止 localStorage、配额耗尽均不阻断正常请求", async () => {
  const disk = storage();
  let value = 1;
  const cache = create({storage: disk, request: async () => ({value})});
  cache.setIdentity(admin); await cache.api(path);
  const key = [...disk.values.keys()].find(k => k.includes(":records:"));
  disk.values.set(key, "broken json");
  value = 2; assert.equal((await cache.api(path)).value, 2);
  disk.setItem = () => {throw new Error("quota");};
  value = 3; await cache.api(path); assert.equal(cache.read(path).value, 3);
  const disabled = create({storage: () => {throw new Error("disabled");}, request: async () => ({value: 4})});
  disabled.setIdentity(admin); assert.equal((await disabled.api(path)).value, 4);
  assert.equal(disabled.read(path).value, 4);
});

test("删除立即使缓存失效，之前的在途 GET 必须重新读取才能回填", async () => {
  const network = deferred();
  let calls = 0;
  const cache = create({storage: storage(), request: async (_, options) => {
    if (options.method === "DELETE") return {ok: true};
    calls++;
    return calls === 1 ? network.promise : {characters: {}};
  }});
  cache.setIdentity(admin);
  const old = cache.api(path);
  await Promise.resolve();
  await cache.api(path + "/old", {method: "DELETE"});
  assert.equal(cache.read(path), null);
  network.resolve({characters: {old: {}}});
  assert.deepEqual(await old, {characters: {}});
  assert.deepEqual(cache.read(path), {characters: {}});
  assert.equal(calls, 2);
});

test("账号变化后旧请求不得回填或返回成功，401 清缓存且不返回缓存作为网络结果", async () => {
  const network = deferred();
  let unauthorized = false;
  const cache = create({storage: storage(), request: async () => {
    if (unauthorized) throw Object.assign(new Error("expired"), {status: 401});
    return network.promise;
  }});
  cache.setIdentity(admin);
  const old = cache.api(path);
  cache.setIdentity(user);
  network.resolve({characters: {old: {}}});
  await assert.rejects(old, error => error.stale === true);
  await cache.api(path); assert.ok(cache.read(path));
  unauthorized = true;
  await assert.rejects(cache.api(path), error => error.status === 401);
  assert.equal(cache.read(path), null);
});

test("缓存渲染失败仍恢复为网络结果，未登录或认证失败不恢复上次记录", async () => {
  const disk = storage();
  const cache = create({storage: disk, request: async () => ({characters: {}})});
  cache.setIdentity(admin); await cache.api(path);
  assert.deepEqual(await cache.api(path, {onCached: () => {throw new Error("bad old shape");}}), {characters: {}});
  const unauthenticated = create({storage: disk});
  assert.equal(unauthenticated.read(path), null);
  unauthenticated.setIdentity(null);
  cache.setIdentity(user); cache.setIdentity(admin);
  assert.equal(cache.read(path), null);
});
