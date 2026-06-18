from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ProcessRestartMixin:
    @staticmethod
    def _restart_helper_code() -> str:
        return r'''
import datetime
import json
import os
import socket
import subprocess
import sys
import time

payload = json.loads(sys.argv[1])
log_path = payload.get("log_path") or ""

def write_log(message):
    if not log_path:
        return
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass

try:
    write_log(f"restart helper started for old pid={payload.get('old_pid')}")
    time.sleep(float(payload.get("initial_delay", 0.8)))
    host = payload.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = int(payload.get("port") or 0)
    deadline = time.time() + float(payload.get("wait_timeout", 20.0))
    if port > 0:
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.25):
                    time.sleep(0.25)
                    continue
            except OSError:
                break
    kwargs = {"cwd": payload.get("cwd") or None, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(payload["cmd"], **kwargs)
    write_log(f"started new pid={proc.pid}")
except Exception as exc:
    write_log(f"restart helper failed: {exc!r}")
'''

    def _restart_command(self) -> list[str]:
        original = getattr(sys, "orig_argv", None)
        if isinstance(original, list) and len(original) > 1:
            return [sys.executable, *original[1:]]
        return [sys.executable, *sys.argv]

    def _restart_wait_host(self) -> str:
        host = str(self.config.get("web_host", "127.0.0.1") or "127.0.0.1").strip()
        if host in ("0.0.0.0", "::", ""):
            return "127.0.0.1"
        return host

    def _spawn_restart_helper(self) -> int:
        payload = {
            "old_pid": os.getpid(),
            "cmd": self._restart_command(),
            "cwd": str(Path.cwd()),
            "host": self._restart_wait_host(),
            "port": int(self.config.get("web_port", 8787) or 0) if self.config.get("web_enabled", True) else 0,
            "initial_delay": 0.8,
            "wait_timeout": 20.0,
            "log_path": str(self.state_path.with_name("restart.log")),
        }
        flags: dict[str, Any] = {}
        if os.name == "nt":
            flags["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        else:
            flags["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, "-c", self._restart_helper_code(), json.dumps(payload, ensure_ascii=False)],
            cwd=str(Path.cwd()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **flags,
        )
        return int(proc.pid)

    def prepare_process_restart(self) -> dict[str, Any]:
        if self._restart_requested:
            return {"old_pid": os.getpid(), "already_requested": True}
        self._restart_requested = True
        try:
            self._flush_sessions(force=True)
            self.save_config()
            helper_pid = self._spawn_restart_helper()
            return {
                "old_pid": os.getpid(),
                "helper_pid": helper_pid,
                "started_at": self.process_started_at,
                "restart_log": str(self.state_path.with_name("restart.log")),
            }
        except Exception:
            self._restart_requested = False
            raise

    async def shutdown_for_process_restart(self, delay: float = 0.35):
        await asyncio.sleep(delay)
        logger.info("full process restart requested; shutting down current process")
        if self._stop_event is not None:
            self._stop_event.set()
            return
        await self.stop_bot()
        await self.stop_web_console()
        await self.close()
