"""Tunnel 配置读取：.env 提供默认值，管理台写入的 DB 覆盖值优先。

令牌两种来源：
- DB：管理台粘贴的明文经 APP_SECRET 加密后存 runtime_settings（前端永不回显）；
- .env：`CF_TUNNEL_TOKEN` 明文，只在进程内使用，不落库。
APP_SECRET 变更后旧 DB 密文解不开，标记 token_error 让界面提示重填。
"""

from __future__ import annotations

from ..db.crypto import CredentialCipher, CredentialDecryptError
from .types import TUNNEL_ENABLED_KEY, TUNNEL_TOKEN_KEY, TunnelPatch


class TunnelStore:
    """令牌与开关的持久化（runtime_settings 两行；经 repo 直读直写）。"""

    def __init__(self, repo, cipher: CredentialCipher,
                 default_token: str = "", default_enabled: bool = False) -> None:
        self._repo = repo
        self._cipher = cipher
        self._default_token = default_token
        self._default_enabled = default_enabled

    def load(self) -> dict:
        rows = self._repo.load()
        enabled_raw = rows.get(TUNNEL_ENABLED_KEY)
        enabled = (enabled_raw == "1" if enabled_raw is not None
                   else self._default_enabled)
        raw = rows.get(TUNNEL_TOKEN_KEY)
        token: str | None = None
        token_error = False
        if raw:
            try:
                token = self._cipher.decrypt(raw.encode("utf-8")).decode("utf-8")
            except (CredentialDecryptError, ValueError, UnicodeDecodeError):
                token_error = True
        else:
            # 没有 DB 覆盖时用 .env 的明文令牌（不落库、不回显）。
            token = self._default_token or None
        return {"enabled": enabled, "token": token,
                "token_saved": bool(raw) or bool(self._default_token),
                "token_error": token_error}

    def write(self, patch: TunnelPatch) -> dict:
        """token 空串 = 保留原值；None = 删除 DB 覆盖（回落 .env）；
        非空串 = 加密覆盖。enabled 不传则不动。"""
        if patch.token is None:
            self._repo.delete(TUNNEL_TOKEN_KEY)
        elif patch.token.strip():
            self._repo.set(
                TUNNEL_TOKEN_KEY,
                self._cipher.encrypt(patch.token.strip().encode("utf-8"))
                .decode("ascii"))
        if patch.enabled is not None:
            self._repo.set(TUNNEL_ENABLED_KEY, "1" if patch.enabled else "0")
        return self.load()
