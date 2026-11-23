"""Cloudflare Tunnel 管理端点（admin + CSRF）。

GET 只读快照（令牌永不回显）；PUT 保存令牌/开关并立即生效；
POST 手动重启 / 强制更新二进制。下载/解密类错误回 400 而不是 500
（用户可换镜像或重填令牌）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..audit.actions import (
    ACTION_TUNNEL_BINARY_REFRESH,
    ACTION_TUNNEL_RESTART,
    ACTION_TUNNEL_UPDATE,
)
from ..compat.openai.request import InvalidRequest
from ..tunnel.binary import TunnelDownloadError
from ..tunnel.types import TUNNEL_TOKEN_MAX_LENGTH, TunnelPatch
from .deps import Services, csrf_protected, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    tunnel = services.tunnel
    if tunnel is None:  # pragma: no cover - 装配护栏（测试替身不挂该路由）
        raise RuntimeError("tunnel manager not wired")

    def _audit(principal, action: str, detail: str) -> None:
        services.audit.record(actor=principal.username, action=action, detail=detail)

    @router.get("/api/tunnel")
    async def get_tunnel(principal=Depends(principal_from_request)):
        return tunnel.payload()

    @router.put("/api/tunnel")
    async def update_tunnel(payload: dict,
                            _csrf: None = Depends(csrf_protected),
                            principal=Depends(principal_from_request)):
        patch = TunnelPatch()
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise InvalidRequest("enabled 必须是 true/false")
            patch.enabled = payload["enabled"]
        if "token" in payload:
            raw = payload["token"]
            if raw is None:
                patch.token = None
            elif isinstance(raw, str) and raw.strip():
                token = raw.strip()
                if len(token) > TUNNEL_TOKEN_MAX_LENGTH:
                    raise InvalidRequest(
                        f"token 不能超过 {TUNNEL_TOKEN_MAX_LENGTH} 字符")
                if not all(ch.isalnum() or ch in "._-" for ch in token):
                    raise InvalidRequest(
                        "token 格式不合法（只允许字母、数字、点、下划线和连字符）")
                patch.token = token
            elif isinstance(raw, str):
                patch.token = ""  # 空串 = 保留原值
            else:
                raise InvalidRequest("token 必须是字符串或 null")
        if patch.enabled is None and patch.token is None:
            raise InvalidRequest("没有需要保存的变更")
        try:
            await tunnel.configure(patch)
        except TunnelDownloadError as error:
            raise InvalidRequest(str(error)) from None
        detail = "；".join(filter(None, (
            f"启用={'是' if patch.enabled else '否'}"
            if patch.enabled is not None else "",
            "令牌=已清除" if patch.token is None
            else "令牌=已更新" if patch.token else "",
        )))
        _audit(principal, ACTION_TUNNEL_UPDATE, detail)
        return tunnel.payload()

    @router.post("/api/tunnel/restart")
    async def restart_tunnel(_csrf: None = Depends(csrf_protected),
                             principal=Depends(principal_from_request)):
        try:
            await tunnel.restart()
        except TunnelDownloadError as error:
            raise InvalidRequest(str(error)) from None
        _audit(principal, ACTION_TUNNEL_RESTART, "手动重启")
        return tunnel.payload()

    @router.post("/api/tunnel/binary/refresh")
    async def refresh_tunnel_binary(_csrf: None = Depends(csrf_protected),
                                    principal=Depends(principal_from_request)):
        try:
            await tunnel.refresh_binary()
        except TunnelDownloadError as error:
            raise InvalidRequest(str(error)) from None
        _audit(principal, ACTION_TUNNEL_BINARY_REFRESH, "强制更新 cloudflared")
        return tunnel.payload()

    return router
