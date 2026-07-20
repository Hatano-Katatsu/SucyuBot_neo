"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const core = require("../../telegram_comfyui_selfie/static/frontend_core.js");

function response({ status = 200, statusText = "OK", ok = true } = {}) {
  return { status, statusText, ok };
}

test("非 JSON 错误体保留可读摘要并限制长度", () => {
  const raw = `<html>${"服务暂不可用".repeat(40)}</html>`;
  assert.throws(
    () => core.parseApiResponse(response({ status: 502, statusText: "Bad Gateway", ok: false }), raw),
    error => {
      assert.equal(error.name, "ApiError");
      assert.equal(error.status, 502);
      assert.equal(error.message, raw.slice(0, 200));
      return true;
    },
  );
});

test("JSON 业务错误优先使用 error 字段", () => {
  assert.throws(
    () => core.parseApiResponse(response(), JSON.stringify({ ok: false, error: "保存失败" })),
    error => error.message === "保存失败" && error.status === 200,
  );
});

test("401 被标记为认证过期且不会泄漏服务端错误体", () => {
  assert.throws(
    () => core.parseApiResponse(response({ status: 401, statusText: "Unauthorized", ok: false }), "internal detail"),
    error => {
      assert.equal(error.message, "登录已过期，请重新登录");
      assert.equal(error.status, 401);
      assert.equal(error.authExpired, true);
      return true;
    },
  );
});

test("请求封装序列化对象并透传自定义请求头", async () => {
  let captured;
  const data = await core.requestApi(async (path, init) => {
    captured = { path, init };
    return {
      ...response(),
      text: async () => JSON.stringify({ ok: true, value: 7 }),
    };
  }, "/api/example", {
    method: "POST",
    headers: { "X-Trace": "trace-id" },
    body: { value: 7 },
  });

  assert.deepEqual(data, { ok: true, value: 7 });
  assert.equal(captured.path, "/api/example");
  assert.equal(captured.init.body, '{"value":7}');
  assert.deepEqual(captured.init.headers, {
    "Content-Type": "application/json",
    "X-Trace": "trace-id",
  });
});

test("有限数值校验允许空值和有限数，拒绝 NaN 与无穷值", () => {
  ["", "  ", "0", "-1.25", "1e3"].forEach(value => {
    assert.equal(core.isFiniteNumberInput(value), true, value);
  });
  ["NaN", "Infinity", "-Infinity", "1e309", "12px"].forEach(value => {
    assert.equal(core.isFiniteNumberInput(value), false, value);
  });

  const invalid = { name: "temperature", value: "Infinity" };
  assert.equal(core.firstInvalidNumberField([{ value: "2" }, invalid, { value: "NaN" }]), invalid);
  assert.equal(core.firstInvalidNumberField([{ value: "" }, { value: "2" }]), null);
});

test("命令列表清理斜杠、空值和重复项，远端无效时回退", () => {
  assert.deepEqual(
    core.resolveCommands([" /天气 ", "天气", "自拍", "", null], ["菜单"]),
    ["天气", "自拍"],
  );
  assert.deepEqual(core.resolveCommands([], ["/菜单", "菜单", "角色"]), ["菜单", "角色"]);
});

test("会话选择对普通用户固定身份，对管理员清理失效选择", () => {
  const sessions = [{ session_id: "telegram:100" }, { session_id: "telegram:200" }];
  assert.equal(
    core.resolveSelectedSession(sessions, "telegram:200", { role: "user", user_id: "42" }),
    "telegram:42",
  );
  assert.equal(core.resolveSelectedSession(sessions, "telegram:200", { role: "admin" }), "telegram:200");
  assert.equal(core.resolveSelectedSession(sessions, "deleted", { role: "admin" }), "telegram:100");
  assert.equal(core.resolveSelectedSession([], "deleted", { role: "admin" }), "");
});
