from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


class GitUpdateMixin:
    """Git 自动更新 + 自重启：仅管理员可触发。

    实现 TODO #2 需求：
    - /更新 或 /git更新 命令：git fetch + 显示可更新 commit + git pull --ff-only + 自重启
    - WebUI 管理员按钮：调用 /api/admin/git-update 走同样的流程
    - Git 拉取使用 Telegram 代理配置（HTTP/HTTPS/SOCKS）
    """

    # ---------- 权限 ----------
    def _is_admin_chat(self, chat_id: int | str) -> bool:
        """管理员判定：admin_chat_ids 优先，为空时回退 allowed_chat_ids。"""
        admins = self.config.get("admin_chat_ids") or []
        if admins:
            return str(chat_id) in {str(x) for x in admins}
        allowed = self.config.get("allowed_chat_ids") or []
        # 白名单模式下，所有白名单用户视为管理员（单 owner bot 的常见形态）
        return bool(allowed) and str(chat_id) in {str(x) for x in allowed}

    # ---------- 代理 ----------
    def _git_proxy_env(self) -> dict[str, str]:
        """把 telegram_proxy_url 转成 Git 可用的 HTTPS_PROXY/HTTP_PROXY/ALL_PROXY。

        - HTTP/HTTPS 代理：直接给 HTTP_PROXY/HTTPS_PROXY
        - SOCKS5 代理：Git 原生支持 socks5h://（libcurl 支持），用 ALL_PROXY
        - 不修改全局 git config，避免污染用户环境
        """
        proxy = (self._telegram_proxy_url() or "").strip()
        if not proxy:
            return {}
        env: dict[str, str] = {}
        lower = proxy.lower()
        if lower.startswith(("http://", "https://")):
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
        elif lower.startswith(("socks5://", "socks5h://", "socks4://", "socks4a://")):
            # Git 经 libcurl 走 SOCKS；优先 socks5h（远程 DNS），不支持时由调用方报错
            normalized = proxy
            if lower.startswith("socks5://"):
                # 强制改成 socks5h://，让 DNS 走代理端，避免本地 DNS 泄漏
                normalized = "socks5h://" + proxy[len("socks5://"):]
            env["ALL_PROXY"] = normalized
            env["all_proxy"] = normalized
        return env

    # ---------- Git 执行 ----------
    def _git_run(
        self,
        args: list[str],
        *,
        timeout: float = 30.0,
        capture: bool = True,
    ) -> subprocess.CompletedProcess:
        """在项目根目录执行 git 命令，注入代理环境变量。"""
        env = dict(os.environ)
        env.update(self._git_proxy_env())
        cmd = ["git", *args]
        logger.info("git run: %s (proxy_env_keys=%s)", " ".join(cmd), sorted(self._git_proxy_env().keys()))
        return subprocess.run(
            cmd,
            cwd=str(self._project_root()),
            env=env,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def _project_root(self) -> str:
        """Git 操作的根目录：config_path 所在目录的上一级（项目根）。"""
        # config_path 形如 D:/SucyuBot_neo/data/config.json，根目录是 D:/SucyuBot_neo
        return str(self.config_path.resolve().parent.parent)

    def _git_current_branch(self) -> str:
        result = self._git_run(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _git_current_head(self) -> str:
        result = self._git_run(["rev-parse", "HEAD"], timeout=10)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    # ---------- 主流程 ----------
    async def run_git_update(self) -> dict[str, Any]:
        """执行 git 更新流程，返回结构化结果。

        步骤：
        1. 记录当前 HEAD 和分支
        2. git fetch（带代理）
        3. 比较本地 HEAD 和远端 HEAD，列出 new commits
        4. 若有更新：git pull --ff-only
        5. 返回 {branch, old_head, new_head, commits, pulled, fetch_error, pull_error}
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_git_update_sync)

    def _run_git_update_sync(self) -> dict[str, Any]:
        branch = self._git_current_branch()
        old_head = self._git_current_head()
        result: dict[str, Any] = {
            "branch": branch,
            "old_head": old_head,
            "new_head": old_head,
            "commits": [],
            "pulled": False,
            "fetch_error": "",
            "pull_error": "",
        }

        # 1. fetch
        fetch = self._git_run(["fetch", "--all", "--prune"], timeout=60)
        if fetch.returncode != 0:
            result["fetch_error"] = (fetch.stderr or fetch.stdout or "").strip()
            # fetch 失败不继续 pull
            return result

        # 2. 列出本地 HEAD 到远端 HEAD 之间的新 commit
        upstream = f"origin/{branch}" if branch else "origin"
        log = self._git_run(["log", "--oneline", f"HEAD..{upstream}"], timeout=20)
        if log.returncode == 0:
            commits = [line.strip() for line in log.stdout.splitlines() if line.strip()]
            result["commits"] = commits
        else:
            # log 失败不致命，继续
            result["commits"] = []

        # 3. 没有新 commit 直接返回
        if not result["commits"]:
            return result

        # 4. pull --ff-only
        pull = self._git_run(["pull", "--ff-only"], timeout=120)
        if pull.returncode != 0:
            result["pull_error"] = (pull.stderr or pull.stdout or "").strip()
            return result

        result["pulled"] = True
        result["new_head"] = self._git_current_head()
        return result

    def _format_git_update_report(self, result: dict[str, Any]) -> str:
        """把 run_git_update 的结果格式化为用户可读的中文报告。"""
        lines = ["Git 更新结果", f"分支：{result.get('branch') or '(未知)'}"]
        old = (result.get("old_head") or "")[:8]
        new = (result.get("new_head") or "")[:8]
        lines.append(f"旧 HEAD：{old}")
        lines.append(f"新 HEAD：{new}")
        commits = result.get("commits") or []
        if commits:
            lines.append(f"更新提交（{len(commits)} 条）：")
            for c in commits[:20]:
                lines.append(f"  {c}")
            if len(commits) > 20:
                lines.append(f"  ...（共 {len(commits)} 条，已截断）")
        else:
            lines.append("没有新提交。")

        if result.get("fetch_error"):
            lines.append(f"fetch 失败：{result['fetch_error']}")
        if result.get("pull_error"):
            lines.append(f"pull 失败：{result['pull_error']}")
        if result.get("pulled"):
            lines.append("已成功拉取更新，服务即将自动重启。")
        else:
            lines.append("未执行 pull（无更新或失败）。")
        return "\n".join(lines)

    # ---------- 命令入口 ----------
    async def cmd_git_update(self, chat_id, session_id, arg):
        """处理 /更新 或 /git更新 命令。仅管理员可用。"""
        if not self._is_admin_chat(chat_id):
            await self.send_message(chat_id, "无权限：仅管理员可执行 /更新。")
            return
        await self.send_message(chat_id, "开始检查 Git 更新……")
        try:
            result = await self.run_git_update()
        except subprocess.TimeoutExpired as exc:
            await self.send_message(chat_id, f"Git 命令超时：{exc}")
            return
        except Exception as exc:
            await self.send_message(chat_id, f"Git 更新异常：{exc}")
            return

        report = self._format_git_update_report(result)
        await self.send_message(chat_id, report)

        # 拉取成功且有更新 → 自重启
        if result.get("pulled"):
            await self.send_message(chat_id, "3 秒后自动重启服务……")
            try:
                restart = self.prepare_process_restart()
                logger.info("git update triggered restart: %s", restart)
            except Exception as exc:
                await self.send_message(chat_id, f"准备重启失败：{exc}\n请手动重启服务。")
                return
            asyncio.create_task(self.shutdown_for_process_restart(delay=3.0))
