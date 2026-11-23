"""cloudflared 进程守护：开关/令牌落库后由本模块拉起、崩了退避重启、退出时清理。

启动参数固定追加 `--protocol http2`；令牌走 TUNNEL_TOKEN 环境变量，
不出现在命令行（避免 `ps` / 审计日志泄露）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from ..compat.openai.request import InvalidRequest
from .binary import platform_info, remove_managed, resolve_binary
from .store import TunnelStore
from .types import (
    TUNNEL_LOG_LIMIT,
    TUNNEL_PROTOCOL,
    TunnelBinaryInfo,
    TunnelLogEntry,
    TunnelPatch,
    TunnelState,
)

logger = logging.getLogger(__name__)

MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
STABLE_AFTER_SECONDS = 30.0
STOP_WAIT_SECONDS = 3.0


class TunnelManager:
    """cloudflared 子进程的生命周期管理（单实例；状态进程内、重启清零）。"""

    def __init__(self, store: TunnelStore, data_dir: str, *,
                 resolve=None, mirror: str | None = None,
                 backoff_base: float = MIN_BACKOFF_SECONDS) -> None:
        self._store = store
        self._data_dir = data_dir
        self._resolve_fn = resolve or resolve_binary
        self._mirror = mirror
        self._backoff_base = backoff_base
        self._backoff = backoff_base
        self._config: dict = {"enabled": False, "token": None,
                              "token_saved": False, "token_error": False}
        self._process: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._wanted = False
        self._stable_handle: asyncio.TimerHandle | None = None
        self._logs: list[TunnelLogEntry] = []
        self._state = TunnelState(supported=True)

    # ------------------------------------------------------------ 管理面
    async def sync(self) -> None:
        """启动装配后调用一次：按持久开关决定是否拉起进程。"""
        self._config = self._store.load()
        self._state.enabled = self._config["enabled"]
        self._state.token_saved = self._config["token_saved"]
        self._state.token_error = self._config["token_error"]
        if self._config["enabled"] and self._config["token"]:
            if not self._wanted:
                self._wanted = True
                await self._start()
        else:
            await self.stop()
            self._set_status("error" if self._config["enabled"] else "disabled")
            if self._config["enabled"] and self._config["token_error"]:
                self._state.last_error = (
                    "隧道令牌解密失败（APP_SECRET 可能已变更），请重新填写")

    async def configure(self, patch: TunnelPatch) -> None:
        """管理界面保存：写库后立即生效。"""
        current = self._config
        effective_token = (None if patch.token is None
                           else (patch.token.strip() or current.get("token")))
        effective_enabled = (patch.enabled if patch.enabled is not None
                             else current["enabled"])
        if effective_enabled and not effective_token:
            raise InvalidRequest("请先填写 Cloudflare Tunnel 令牌，再启用隧道")
        self._store.write(patch)
        await self.sync()

    async def restart(self) -> None:
        """手动重启（先停再按当前开关拉起）。"""
        should_run = self._config["enabled"] and self._config.get("token")
        await self.stop()
        self._backoff = self._backoff_base
        if should_run:
            self._wanted = True
            await self._start()

    async def refresh_binary(self) -> None:
        """删除受管二进制并重新启动；系统安装的二进制不受影响。"""
        should_run = self._config["enabled"] and self._config.get("token")
        await self.stop()
        remove_managed(self._data_dir, platform_info())
        self._state.binary = TunnelBinaryInfo()
        if should_run:
            self._wanted = True
            await self._start()

    async def shutdown(self) -> None:
        """进程退出时调用：不再重启，并终止子进程。"""
        self._wanted = False
        await self.stop()

    def payload(self) -> dict:
        """`GET /api/tunnel` 快照（logs 拷贝防中途变更）。"""
        state = self._state
        return {
            "supported": state.supported,
            "enabled": state.enabled,
            "status": state.status,
            "token_saved": state.token_saved,
            "token_error": state.token_error,
            "binary": {"path": state.binary.path, "source": state.binary.source,
                       "version": state.binary.version},
            "pid": state.pid,
            "started_at": state.started_at,
            "restart_count": state.restart_count,
            "last_error": state.last_error,
            "logs": [vars(entry) for entry in self._logs],
            "protocol": state.protocol,
        }

    # ------------------------------------------------------------ 进程面
    async def _start(self) -> None:
        if not self._wanted or self._process is not None or (
                self._restart_task is not None):
            return
        try:
            await self._do_start()
        except Exception as error:  # noqa: BLE001 - 下载/启动失败都走退避重启
            if not self._wanted:
                return
            self._log("err", f"启动 cloudflared 失败: {error}")
            self._schedule_restart(f"启动 cloudflared 失败: {error}")

    async def _do_start(self) -> None:
        token = self._config.get("token")
        if not token:
            self._set_status("error", "未配置隧道令牌")
            return
        if self._state.binary.source == "none":
            self._set_status("downloading")
            resolved = self._resolve_fn(self._data_dir, base=self._mirror)
            self._state.binary = TunnelBinaryInfo(
                path=resolved["path"], source=resolved["source"],
                version=resolved.get("version"))
        self._set_status("starting", None)
        env = {**os.environ, "TUNNEL_TOKEN": token,
               # 官方文档里 --no-autoupdate 对应的环境变量是 NO_AUTOUPDATE。
               "NO_AUTOUPDATE": "true"}
        process = await asyncio.create_subprocess_exec(
            self._state.binary.path, "tunnel", "--no-autoupdate",
            "--protocol", TUNNEL_PROTOCOL, "run",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env)
        self._process = process
        self._state.pid = process.pid
        self._state.started_at = int(time.time())
        self._set_status("running")
        for stream, level in ((process.stdout, "out"), (process.stderr, "err")):
            if stream is not None:
                asyncio.get_running_loop().create_task(self._pump(stream, level))
        # 稳定运行一段时间后把退避重置，避免长期运行后一次抖动就等 30 秒。
        loop = asyncio.get_running_loop()
        self._stable_handle = loop.call_later(
            STABLE_AFTER_SECONDS, lambda: setattr(self, "_backoff",
                                                  self._backoff_base))
        self._watch_task = asyncio.get_running_loop().create_task(
            self._watch(process))

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        """跟随一个子进程直到退出；被 stop/restart 替换掉的旧进程不再改状态。"""
        exit_code = await process.wait()
        if self._process is not process:
            return
        self._process = None
        self._watch_task = None
        self._state.pid = None
        self._state.started_at = None
        if self._stable_handle is not None:
            self._stable_handle.cancel()
            self._stable_handle = None
        self._log("info", f"cloudflared 退出，code={exit_code}")
        if self._wanted:
            self._schedule_restart(f"cloudflared 退出，code={exit_code}")
        else:
            self._set_status("stopped")

    def _schedule_restart(self, reason: str) -> None:
        self._state.last_error = reason
        self._state.restart_count += 1
        delay = self._backoff
        self._backoff = min(MAX_BACKOFF_SECONDS, self._backoff * 2)
        self._set_status("restarting")
        loop = asyncio.get_running_loop()

        async def _restart() -> None:
            await asyncio.sleep(delay)
            self._restart_task = None
            if self._wanted:
                await self._start()

        self._restart_task = loop.create_task(_restart())

    async def stop(self) -> None:
        self._wanted = False
        if self._restart_task is not None:
            self._restart_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_task
            self._restart_task = None
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), STOP_WAIT_SECONDS)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                    await process.wait()
        self._state.pid = None
        self._state.started_at = None
        self._set_status("stopped")

    def _set_status(self, status: str, error: str | None = None) -> None:
        self._state.status = status  # type: ignore[assignment]
        if error is not None:
            self._state.last_error = error
        if status == "running":
            self._state.last_error = None

    def _log(self, level: str, line: str) -> None:
        text = line[:500]
        if not text:
            return
        self._logs.append(
            TunnelLogEntry(at=int(time.time()), level=level, line=text))  # type: ignore[arg-type]
        if len(self._logs) > TUNNEL_LOG_LIMIT:
            del self._logs[:len(self._logs) - TUNNEL_LOG_LIMIT]

    async def _pump(self, stream: asyncio.StreamReader, level: str) -> None:
        """跟随子进程输出流，按行进日志环；流关闭即结束。"""
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                self._log(level, raw.decode("utf-8", "replace").rstrip("\r\n"))
        except (ValueError, OSError):
            pass  # 流随子进程关闭 / 行超长截断
