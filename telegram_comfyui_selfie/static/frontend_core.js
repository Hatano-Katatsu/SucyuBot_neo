(function exposeFrontendCore(root, factory) {
  const core = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = core;
    return;
  }
  root.SucyuFrontendCore = core;
}(typeof globalThis !== "undefined" ? globalThis : this, function createFrontendCore() {
  "use strict";

  class ApiError extends Error {
    constructor(message, { status = 0, authExpired = false } = {}) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.authExpired = authExpired;
    }
  }

  function parseJsonText(raw) {
    if (!raw) return { parsed: true, value: {} };
    try {
      return { parsed: true, value: JSON.parse(raw) };
    } catch (_err) {
      return { parsed: false, value: {} };
    }
  }

  function responseErrorMessage(data, raw, status, statusText) {
    if (data && typeof data === "object" && !Array.isArray(data)) {
      const detail = typeof data.error === "string" ? data.error.trim() : "";
      if (detail) return detail;
    }
    const plainText = String(raw || "").trim();
    if (plainText) return plainText.slice(0, 200);
    return `${status}${statusText ? ` ${statusText}` : ""}`.trim() || "请求失败";
  }

  function parseApiResponse(response, raw = "") {
    const status = Number(response?.status) || 0;
    const statusText = String(response?.statusText || "");
    const { value: data } = parseJsonText(String(raw || ""));
    if (status === 401) {
      throw new ApiError("登录已过期，请重新登录", { status, authExpired: true });
    }
    const apiRejected = data && typeof data === "object" && !Array.isArray(data) && data.ok === false;
    if (!response?.ok || apiRejected) {
      throw new ApiError(responseErrorMessage(data, raw, status, statusText), { status });
    }
    return data;
  }

  function buildRequestOptions(options = {}) {
    const init = { ...options };
    if (init.body && typeof init.body !== "string") {
      init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
      init.body = JSON.stringify(init.body);
    }
    return init;
  }

  async function requestApi(fetchImpl, path, options = {}) {
    if (typeof fetchImpl !== "function") throw new TypeError("fetchImpl 必须是函数");
    const response = await fetchImpl(path, buildRequestOptions(options));
    return parseApiResponse(response, await response.text());
  }

  function isFiniteNumberInput(value) {
    const text = String(value ?? "").trim();
    return text === "" || Number.isFinite(Number(text));
  }

  function firstInvalidNumberField(fields) {
    return Array.from(fields || []).find(field => !isFiniteNumberInput(field?.value)) || null;
  }

  function cleanCommandList(value) {
    if (!Array.isArray(value)) return [];
    const seen = new Set();
    const commands = [];
    value.forEach(item => {
      if (typeof item !== "string") return;
      const command = item.trim().replace(/^\/+/, "");
      if (!command || seen.has(command)) return;
      seen.add(command);
      commands.push(command);
    });
    return commands;
  }

  function resolveCommands(remoteCommands, fallbackCommands) {
    const remote = cleanCommandList(remoteCommands);
    return remote.length ? remote : cleanCommandList(fallbackCommands);
  }

  function authenticatedSessionId(auth) {
    const userId = String(auth?.user_id || "").trim();
    return auth?.role !== "admin" && userId ? `telegram:${userId}` : "";
  }

  function resolveSelectedSession(sessions, selectedSession, auth = {}) {
    const fixedSession = authenticatedSessionId(auth);
    if (fixedSession) return fixedSession;
    const ids = (Array.isArray(sessions) ? sessions : [])
      .map(item => String(item?.session_id || "").trim())
      .filter(Boolean);
    const selected = String(selectedSession || "").trim();
    return selected && ids.includes(selected) ? selected : (ids[0] || "");
  }

  return Object.freeze({
    ApiError,
    authenticatedSessionId,
    buildRequestOptions,
    firstInvalidNumberField,
    isFiniteNumberInput,
    parseApiResponse,
    requestApi,
    resolveCommands,
    resolveSelectedSession,
  });
}));
