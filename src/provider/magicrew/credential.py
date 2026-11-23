"""超级麦吉（Magicrew）凭证。

**只有 JWT 是必需字段**：业务 API（签发网关 key / 积分为 / 模型目录）全靠它；
`refresh_token` 只是「JWT 过期后还能续」的补充（一次性轮换，见 `client.refresh_gateway_key`），
`api_key` 是推理网关的 `mgw_` 票据（可由 JWT 现签）。

上游没有公开 OAuth、手机号登录协议未核实 ⇒ 本渠道**只能由用户粘贴 JWT 手工导入**
（见包的模块文档）。因此本模块只负责：容错读取 + 归一 + 从 JWT payload 解 uid。

输入形态宽松：裸 JWT 字符串、`{"jwt": "…"}`、`{"token": "…"}`、
`{"access_token": "…"}`，以及 snake_case / camelCase 混合的落库 JSON。
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Any

from ..base import UpstreamProtocolViolation

#: JWT 的别名（导入时用户可能贴的是别的字段名）
JWT_KEYS = ("jwt", "token", "access_token")
#: uid 的别名（落库不同版本 / JWT claim 的常见写法）
UID_KEYS = ("uid", "user_id", "userId")
#: JWT payload 里可当作 uid 的 claim（按顺序取第一个非空字符串）
JWT_UID_CLAIMS = ("uid", "user_id", "userId", "sub")

#: 预刷新提前量的上限（秒）。
#:
#: `REFRESH_SKEW_HOURS` 默认 24h，而网关 key 只活 2 小时 —— 直接照用会让
#: 「刚签的 key 也算快过期」，每轮预刷新（默认 30min）都重签一次，把在途请求
#: 手里的 key 吊销掉。压到 30min：与刷新周期同量级，最早在剩余 30min 时续，
#: 正常一轮只续一次（实测口径见 `RefreshTask` 的 30min 间隔）。
REFRESH_SKEW_CAP_SECONDS = 1800


def _pick(raw: dict[str, Any], *names: str) -> str:
    """按顺序取第一个非空字符串 / 整数值。"""
    for name in names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def epoch_seconds(value: Any) -> int:
    """上游到期时间 → epoch 秒；解析不出返回 0（未知，不当成「已过期」）。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def uid_from_jwt(jwt: str) -> str:
    """JWT payload → uid；解不出返回空串（**绝不伪造**）。

    payload 是 base64url（无填充），坏段 / 非 JSON / 缺 claim 都只是「不知道 uid」，
    不能因此拒绝整张凭证——JWT 本身仍然可用。
    """
    parts = jwt.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return ""
    try:
        claims = json.loads(decoded)
    except ValueError:
        return ""
    if not isinstance(claims, dict):
        return ""
    for key in JWT_UID_CLAIMS:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(slots=True)
class MagicrewCredential:
    """归一后的超级麦吉凭证。"""

    jwt: str
    organization_code: str = ""
    api_key: str = ""
    api_key_expires_at: int = 0
    refresh_token: str = ""
    refresh_token_expires_at: int = 0
    uid: str = ""
    nickname: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> MagicrewCredential:
        """落库 JSON / 导入对象 → 凭证；缺 JWT 返回空凭证（调用方据此报错）。"""
        if not isinstance(raw, dict):
            return cls(jwt="")
        jwt = _pick(raw, *JWT_KEYS)
        uid = _pick(raw, *UID_KEYS) or uid_from_jwt(jwt)
        return cls(
            jwt=jwt,
            organization_code=_pick(raw, "organization_code", "organizationCode"),
            api_key=_pick(raw, "api_key", "apiKey"),
            api_key_expires_at=epoch_seconds(
                raw.get("api_key_expires_at", raw.get("apiKeyExpiresAt"))),
            refresh_token=_pick(raw, "refresh_token", "refreshToken"),
            refresh_token_expires_at=epoch_seconds(
                raw.get("refresh_token_expires_at", raw.get("refreshTokenExpiresAt"))),
            uid=uid,
            nickname=_pick(raw, "nickname", "nickName"),
        )

    @classmethod
    def from_import(cls, raw: Any) -> MagicrewCredential:
        """手工导入：裸 JWT / 含 `jwt`/`token`/`access_token` 的对象；缺 JWT 拒绝。"""
        if isinstance(raw, str):
            raw = {"jwt": raw}
        credential = cls.from_dict(raw) if isinstance(raw, dict) else cls(jwt="")
        if not credential.jwt:
            raise UpstreamProtocolViolation(
                'missing jwt: paste the JWT or {"jwt": "…"}')
        return credential

    def to_dict(self) -> dict[str, Any]:
        """落库形态：只写非空字段（空串 / 0 不落库，避免界面显示假值）。"""
        out: dict[str, Any] = {"jwt": self.jwt}
        for key in ("organization_code", "api_key", "refresh_token", "uid", "nickname"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.api_key_expires_at:
            out["api_key_expires_at"] = self.api_key_expires_at
        if self.refresh_token_expires_at:
            out["refresh_token_expires_at"] = self.refresh_token_expires_at
        return out

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        """是否该预刷新：已有 api_key 且（快过期或已过期）→ True。

        `api_key` 为空也算 True：可由 JWT 重新签发，不刷新等于放着能用的凭证不用。
        到期时间未知（0）且已有 key → False（不猜 TTL）。
        """
        if not self.api_key:
            return True
        if self.api_key_expires_at <= 0:
            return False
        current = int(now if now is not None else time.time())
        lead = min(skew_seconds, REFRESH_SKEW_CAP_SECONDS)
        return current + lead >= self.api_key_expires_at
