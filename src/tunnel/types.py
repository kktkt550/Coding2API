"""Cloudflare Tunnel（自托管专用）共享类型与常量。

令牌不进 HOT_SETTINGS：它不是可热更的普通配置，必须以密文落库且永不回显。
进程参数固定带 `--protocol http2`（UDP/QUIC 在部分网络不可达，http2 更稳）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TUNNEL_TOKEN_KEY = "cf_tunnel_token_enc"
TUNNEL_ENABLED_KEY = "cf_tunnel_enabled"
TUNNEL_PROTOCOL = "http2"
TUNNEL_LOG_LIMIT = 200
TUNNEL_TOKEN_MAX_LENGTH = 8192
TUNNEL_RELEASE_BASE = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download")

TunnelStatus = Literal[
    "disabled", "stopped", "downloading", "starting", "running",
    "restarting", "error",
]
TunnelBinarySource = Literal["system", "managed", "none"]


@dataclass
class TunnelLogEntry:
    at: int
    level: Literal["info", "out", "err"]
    line: str


@dataclass
class TunnelBinaryInfo:
    path: str = ""
    source: TunnelBinarySource = "none"
    version: str | None = None


@dataclass
class TunnelState:
    """`GET /api/tunnel` 的快照；令牌永不回显，只回 token_saved 布尔。"""

    supported: bool
    enabled: bool = False
    status: TunnelStatus = "disabled"
    token_saved: bool = False
    # APP_SECRET 变更后旧密文解不开：提示用户重填，不显示明文。
    token_error: bool = False
    binary: TunnelBinaryInfo = field(default_factory=TunnelBinaryInfo)
    pid: int | None = None
    started_at: int | None = None
    restart_count: int = 0
    last_error: str | None = None
    logs: list[TunnelLogEntry] = field(default_factory=list)
    protocol: str = TUNNEL_PROTOCOL


@dataclass
class TunnelPatch:
    token: str | None = None
    enabled: bool | None = None
