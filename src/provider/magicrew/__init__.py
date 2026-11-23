"""超级麦吉（Magicrew）渠道。

与其余渠道的差异（决定了本包为什么这样切）：

1. **只能 JWT 手工导入**：上游无公开 OAuth，手机号登录协议未核实（参考实现里
   那条腿是零调用者的探索代码）⇒ 不实现 `start_auth` / `poll_auth` /
   `complete_callback` / `relogin`，与「方法缺失自然跳过」同一分工。
2. **两套鉴权**：业务 API（签发 key / 积分 / 目录 / 签到）用网页会话 JWT；
   推理网关用 JWT 现签的 `mgw_` api_key。签新 key 会吊销旧 key，所以只在
   缺失 / 过期 / 上游点名 token 无效时签（见 `client.stream_chat`）。
3. **HTTP 200 + 业务错误**：`{"code":403,"message":"token无效"}` 与成功同状态码，
   分类一律 body 优先（见 `events.classify_status`）。
4. **续期是 PUT + 一次性 refresh_token**：JWT 过期也能续；refresh_token 被上游
   明确作废时只能重新导入 JWT（`UpstreamReloginRequired`）。
5. **签到要求请求体是空串**（`content=b""`，不是省略 body / 不是 ``）。

额度只有剩余总量（上游不给总量），故 `remaining == total`，健康度是近似值。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ...provider.base import (
    CheckinResult,
    ErrKind,
    Event,
    Model,
    Quota,
    UpstreamProtocolViolation,
    UpstreamReloginRequired,
)
from ...tasks.pacer import paced
from . import events as magicrew_events
from .client import MagicrewClient, UpstreamHTTPError
from .credential import MagicrewCredential


@dataclass(slots=True)
class MagicrewProvider:
    """Provider 协议实现（细接口）。只依赖注入的 client，不 import config。"""

    client: MagicrewClient = field(default_factory=MagicrewClient)
    pacer: Any | None = None

    id: str = "magicrew"

    # ------------------------------------------------------ 凭证生命周期

    def import_credential(self, raw: dict) -> dict:
        """手工导入：裸 JWT / `{"jwt": "…"}` / token 别名；缺 JWT 直接拒绝。"""
        return MagicrewCredential.from_import(raw).to_dict()

    def classify(self, status: int, body: bytes) -> ErrKind:
        return magicrew_events.classify_status(status, body)

    def credential_from(self, credential_data: dict) -> MagicrewCredential:
        """供 RefreshTask 调 `needs_refresh`（签名固定，见 `tasks/refresh.py`）。"""
        return MagicrewCredential.from_dict(credential_data)

    async def refresh(self, credential_data: dict) -> dict:
        """续网关 key：有 refresh_token 走 PUT（一次性轮换），否则用 JWT 现签。"""
        credential = MagicrewCredential.from_dict(credential_data)
        if credential.refresh_token:
            try:
                issued = await self.client.refresh_gateway_key(
                    credential.refresh_token)
            except UpstreamHTTPError as error:
                if magicrew_events.refresh_token_invalid(error.body):
                    raise UpstreamReloginRequired(
                        "magicrew refresh token rejected; re-import the JWT") \
                        from error
                raise
        elif credential.jwt:
            issued = await self.client.issue_gateway_key(credential)
        else:
            raise UpstreamReloginRequired(
                "magicrew credential missing jwt and refresh_token")
        # 先取归一后的落库形态：issuer 只回 key 相关字段，其余（jwt / 组织码 /
        # uid / nickname）必须原样保留。
        data = credential.to_dict()
        data.update(issued)
        # 刚续到的 key 立刻进进程内缓存：下一个请求不必再签（再签会吊销它）
        self.client.remember_key(credential, issued)
        return data

    # ------------------------------------------------------------ 额度

    async def probe_quota(self, credential_data: dict) -> Quota:
        credential = MagicrewCredential.from_dict(credential_data)
        if not credential.jwt:
            return Quota(probe_failed=True, probed_at=int(time.time()))
        return await self.client.probe_quota(credential)

    # ------------------------------------------------------------ 签到

    async def checkin(self, credential_data: dict) -> CheckinResult:
        credential = MagicrewCredential.from_dict(credential_data)
        if not credential.jwt:
            return CheckinResult(ok=False, message="凭证缺少 jwt")
        try:
            return await self.client.checkin(credential)
        except UpstreamHTTPError as error:
            if error.kind() is ErrKind.DEAD:
                return CheckinResult(
                    ok=False, message="登录态已失效，请重新导入 JWT")
            return CheckinResult(ok=False, message=f"签到失败（HTTP {error.status}）")
        except (UpstreamProtocolViolation, OSError):
            return CheckinResult(ok=False, message="签到响应格式异常")

    async def checkin_status(self, credential_data: dict) -> dict:
        credential = MagicrewCredential.from_dict(credential_data)
        if not credential.jwt:
            return {"active": False, "today_checked_in": False}
        return await self.client.checkin_status(credential)

    def checkin_scope(self, credential_data: dict) -> str:
        """签到隔离键：uid；身份未知返回空串（调用方回落凭证 ID）。"""
        uid = MagicrewCredential.from_dict(credential_data).uid
        return f"magicrew|{uid}" if uid else ""

    # ------------------------------------------------------------ 模型

    async def list_models(self, credential_data: dict) -> list[Model]:
        credential = MagicrewCredential.from_dict(credential_data)
        if not credential.jwt:
            raise UpstreamProtocolViolation("magicrew credential missing jwt")
        return await self.client.fetch_models(credential)

    async def stream_chat(self, credential_data: dict, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        credential = MagicrewCredential.from_dict(credential_data)
        if not credential.jwt:
            raise UpstreamProtocolViolation("magicrew credential missing jwt")
        async with paced(self.pacer, credential.uid or "magicrew"):
            async for event in self.client.stream_chat(credential, payload, model):
                yield event

    async def aclose(self) -> None:
        await self.client.aclose()


__all__ = ["MagicrewProvider"]
