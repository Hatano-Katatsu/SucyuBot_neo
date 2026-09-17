(function exposeBrowserCache(root, factory) {
  const exports = factory();
  if (typeof module === "object" && module.exports) module.exports = exports;
  else root.SucyuBrowserCache = exports;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PREFIX = "sucyubot:web:v1:";
  const LAST_OWNER = PREFIX + "owner";
  const MAX_AGE = 24 * 60 * 60 * 1000;
  const MAX_BYTES = 2 * 1024 * 1024;
  const MAX_ENTRY_BYTES = 512 * 1024;
  const MAX_ENTRIES = 32;

  function identity(auth) {
    return ["admin", "user"].includes(auth?.role) && auth?.user_id
      ? `${auth.role}:${auth.user_id}` : "";
  }

  function cacheable(path) {
    return /^\/api\/sessions(?:\?[^#]*)?$/.test(path)
      || /^\/api\/sessions\/[^/]+\/(characters|memories|diaries|history-summary)(?:\?[^#]*)?$/.test(path);
  }

  // 缓存仅用于只读预览；凭据、导入原文和大背景附件不落浏览器。
  function snapshot(data) {
    return JSON.parse(JSON.stringify(data, (key, value) =>
      /token|password|api_key|secret/i.test(key) || ["import_source", "world_snapshot"].includes(key) ? undefined : value));
  }

  function create({ storage = () => globalThis.localStorage, request, now = Date.now } = {}) {
    let owner = "", generation = 0, entries = {};
    const pending = new Map();
    const key = () => PREFIX + "records:" + encodeURIComponent(owner);
    const prefKey = () => PREFIX + "selection:" + encodeURIComponent(owner);
    const store = () => typeof storage === "function" ? storage() : storage;
    function readJSON(name) {
      try { return JSON.parse(store()?.getItem(name) || "null"); } catch (_) { return null; }
    }
    function remove(name) { try { store()?.removeItem(name); } catch (_) { /* 禁用存储时仍可使用页面。 */ } }
    function put(name, value) {
      try { store()?.setItem(name, JSON.stringify(value)); } catch (_) { remove(name); /* 配额不足时只用内存。 */ }
    }
    function prune(value) {
      const valid = Object.entries(value || {}).filter(([path, entry]) => cacheable(path)
        && entry && Number.isFinite(entry.at) && entry.at <= now() && now() - entry.at < MAX_AGE
        && entry.data && typeof entry.data === "object");
      valid.sort((a, b) => b[1].at - a[1].at);
      const result = {};
      let bytes = 0;
      for (const [path, entry] of valid.slice(0, MAX_ENTRIES)) {
        const size = JSON.stringify(entry).length * 2;
        if (size > MAX_ENTRY_BYTES || bytes + size > MAX_BYTES) continue;
        result[path] = entry;
        bytes += size;
      }
      return result;
    }
    function setIdentity(auth) {
      const next = identity(auth);
      if (next === owner && next) return false;
      const prior = owner || readJSON(LAST_OWNER);
      if (prior && prior !== next) remove(PREFIX + "records:" + encodeURIComponent(prior));
      owner = next;
      generation++;
      entries = owner ? prune(readJSON(key())) : {};
      if (owner) { put(LAST_OWNER, owner); put(key(), entries); }
      else remove(LAST_OWNER);
      return true;
    }
    function read(path) {
      if (!owner || !cacheable(path)) return null;
      entries = prune(readJSON(key()) || entries);
      return entries[path] ? snapshot(entries[path].data) : null;
    }
    function write(path, data) {
      if (!owner || !cacheable(path)) return;
      entries = prune({ ...(readJSON(key()) || entries), [path]: { at: now(), data: snapshot(data) } });
      put(key(), entries);
    }
    function invalidate() {
      generation++;
      entries = {};
      if (owner) remove(key());
    }
    function restoreSelection() {
      if (!owner.startsWith("admin:")) return {};
      const saved = readJSON(prefKey()) || {};
      return { sessionId: typeof saved.sessionId === "string" ? saved.sessionId : "",
        worldSessionId: typeof saved.worldSessionId === "string" ? saved.worldSessionId : "" };
    }
    function rememberSelection(sessionId, worldSessionId) {
      if (owner.startsWith("admin:")) put(prefKey(), {
        sessionId: String(sessionId || "").slice(0, 200), worldSessionId: String(worldSessionId || "").slice(0, 200),
      });
    }
    function staleError() {
      return Object.assign(new Error("记录已变化，请刷新"), { stale: true });
    }
    async function api(path, options = {}, retry = 0) {
      const { onCached, ...init } = options;
      const mutation = (init.method || "GET").toUpperCase() !== "GET";
      if (mutation) {
        invalidate();
        try { return await request(path, init); } finally { invalidate(); }
      }
      const capturedOwner = owner, version = generation;
      const id = `${owner}\n${version}\n${path}`;
      let network = pending.get(id);
      if (!network) {
        network = Promise.resolve().then(() => request(path, init));
        pending.set(id, network);
      }
      try {
        if (typeof onCached === "function") {
          const cached = read(path);
          // 损坏的旧浏览器记录不能阻止本次网络读取。
          if (cached) { try { onCached(cached); } catch (_) { invalidate(); } }
        }
        const data = await network;
        if (capturedOwner !== owner) throw staleError();
        // 写操作期间的旧 GET 不回填；合并一次新的读取，避免刚删除的内容复活。
        if (cacheable(path) && version !== generation) {
          if (retry >= 1) throw staleError();
          return await api(path, init, retry + 1);
        }
        write(path, data);
        return data;
      } catch (error) {
        if ([401, 403, 404].includes(error.status) && capturedOwner === owner) invalidate();
        throw error;
      } finally {
        if (pending.get(id) === network) pending.delete(id);
      }
    }
    function storageChanged(event) {
      if (event.key === key() && event.newValue === null) { generation++; entries = {}; }
      return event.key === LAST_OWNER && event.newValue !== JSON.stringify(owner);
    }
    return { api, setIdentity, read, invalidate, restoreSelection, rememberSelection, storageChanged };
  }
  return Object.freeze({ create, cacheable, identity });
}));
