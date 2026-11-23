"""超级麦吉（Magicrew）上游客户端。

三条腿、两套鉴权：

1. **业务 API** `/api/v1/*`（签发/续期网关 key、模型目录、积分、每日签到）走
  网页会话 JWT；缺 `origin` / `referer` 会被 WAF 回 403 空 body。
2. **推理网关** `/v1/chat/completions` 走 JWT 现签的 `mgw_` api_key，且**只支持
   流式**（请求体必须带 `stream: true` + `stream_options.include_usage`）。
3. `PUT /api/v1/model-gateway/tokens` 是唯一不吃 JWT 的写请求：refresh_token
   一次性轮换，JWT 过期后靠它续网关 key。

**成败判据是业务码不是 HTTP 状态**：错配的 key 会得到 HTTP 200 +
`{"code":403,"message":"token无效"}`，content-type 还是 `application/json`。
所以推理前先看 content-type，非 SSE 一律按 `UpstreamHTTPError(200, body)` 上抛，
让 `classify_status` 按 body 归类（DEAD 由上层判，本层只负责重签一次）。

**重签会吊销旧 key**（上游每账号只保留一个有效网关 key）：只在「缺失 / 过期 /
上游点名 token 无效」时签，并用进程内缓存（按 JWT 哈希）避免每个请求都签。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import httpx

from ...engine.sse import iter_frames
from ...provider import base
from ...provider.base import (
    CheckinResult,
    Event,
    EventKind,
    Model,
    Quota,
)
from ..lazy_clients import LazyClients
from . import events as magicrew_events
from .credential import MagicrewCredential, epoch_seconds

#: 国内站（部署配置传入，默认值只用于单测 / 未配置时的兜底）
DEFAULT_HOST: Final = "https://i-magic-service.letsmagic.cn"
#: 网页 Origin / Referer 的来源站
DEFAULT_WEB_ORIGIN: Final = "https://www.letsmagic.cn"
#: 默认 UA：上游对整串精确匹配的脚本 UA 拉黑（`curl/…`、`Chrome/141.0.0.0` 会 405）
DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.7390.55 Safari/537.36"
)

EP_GATEWAY_TOKENS: Final = "/api/v1/model-gateway/tokens"
EP_MODELS: Final = "/api/v1/flows/models"
EP_POINTS: Final = "/api/v1/quota/points/total"
EP_LOGIN_GIFT: Final = "/api/v1/login-gift"
EP_CHAT: Final = "/v1/chat/completions"

#: 业务 API 超时（与 `LazyClients` 的短请求池一致）
API_TIMEOUT: Final = 30.0
#: 推理流：推理模型可能长时间只吐 reasoning，读不设短上限
CHAT_TIMEOUT: Final = 600.0

#: `_api_json` 未传 body
_NO_BODY: Final = object()
#: 签到要求「请求体存在且为空串」：不能省略 body，也不能发 ``
EMPTY_BODY: Final = object()

#: 北京时间（上游 `expires_at` 无时区，按 UTC+8 解释）
_CST = timezone(timedelta(hours=8))


class UpstreamHTTPError(base.UpstreamHTTPError):
    """上游非 2xx / HTTP 200 + 业务错误；`kind()` 走本渠道的 body 优先规则。"""

    classify_status = staticmethod(magicrew_events.classify_status)


def _payload(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


def _num(value: Any) -> float | None:
    """宽松取数（上游积分可能是 int / float / 字符串）。bool 不算。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _cst_epoch(value: Any) -> int | None:
    """`"2026-11-27 13:47:42"`（UTC+8，无时区）→ epoch 秒；解析不出返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int(parsed.replace(tzinfo=_CST).timestamp())


def _gateway_payload(data: dict[str, Any]) -> dict[str, Any]:
    """签发/续期响应 `data` → 落库子集；缺 `api_key` 即协议违规。"""
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise base.UpstreamProtocolViolation(
            "magicrew gateway token response missing api_key")
    out: dict[str, Any] = {"api_key": api_key.strip()}
    refresh_token = data.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token.strip():
        out["refresh_token"] = refresh_token.strip()
    for key in ("api_key_expires_at", "refresh_token_expires_at"):
        expires_at = epoch_seconds(data.get(key))
        if expires_at:
            out[key] = expires_at
    return out


class MagicrewClient(LazyClients):
    """上游 HTTP 客户端；host / web_origin / UA 可由部署配置覆盖。"""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        web_origin: str = DEFAULT_WEB_ORIGIN,
        user_agent: str = DEFAULT_USER_AGENT,
        *,
        stream_client: httpx.AsyncClient | None = None,
        short_client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.web_origin = web_origin.rstrip("/")
        self.user_agent = user_agent
        self._stream_client = stream_client
        self._short_client = short_client
        self._proxy = proxy
        #: jwt 哈希 → 最近签发/续期的网关 key（进程内）
        self._key_cache: dict[str, dict[str, Any]] = {}
        #: 签发 / 续期串行化：签新 key 会吊销旧 key，两条腿并发会互相烧掉
        self._issue_lock = asyncio.Lock()

    # ------------------------------------------------------------ 请求头

    def _base_headers(self, *, authorization: str = "",
                      accept: str = "application/json") -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": accept,
            "user-agent": self.user_agent,
            "origin": self.web_origin,
            "referer": f"{self.web_origin}/",
        }
        if authorization:
            headers["authorization"] = f"Bearer {authorization}"
        return headers

    def headers(self, credential: MagicrewCredential) -> dict[str, str]:
        """业务 API 头（JWT 鉴权 + 可选的 organization-code）。"""
        headers = self._base_headers(authorization=credential.jwt)
        if credential.organization_code:
            headers["organization-code"] = credential.organization_code
        return headers

    def chat_headers(self, api_key: str,
                     credential: MagicrewCredential) -> dict[str, str]:
        """推理网关头（`mgw_` key 鉴权 + SSE accept）。"""
        headers = self._base_headers(authorization=api_key,
                                     accept="text/event-stream")
        if credential.organization_code:
            headers["organization-code"] = credential.organization_code
        return headers

    # ------------------------------------------------------------ 短请求

    async def _request_json(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        *,
        body: Any = _NO_BODY,
        timeout: float = API_TIMEOUT,
    ) -> dict[str, Any]:
        """发一次请求 → 剥业务信封；非 2xx 与业务错误都走 `UpstreamHTTPError`。"""
        request: dict[str, Any] = {"headers": headers, "timeout": timeout}
        if body is EMPTY_BODY:
            request["content"] = b""
        elif body is not _NO_BODY:
            request["json"] = body
        try:
            response = await self._short.request(
                method, f"{self.host}{path}", **request)
        except httpx.TransportError as error:
            raise base.UpstreamTransportError(str(error)) from error
        if response.status_code >= 400:
            raw = await base.read_body_bounded(response)
            raise UpstreamHTTPError(response.status_code, raw,
                                    **base.retry_hints(response.headers))
        raw = await base.read_body_bounded(response)
        try:
            envelope = json.loads(raw)
        except ValueError as error:
            raise base.UpstreamProtocolViolation(
                f"non-JSON response from {path}") from error
        if not isinstance(envelope, dict):
            raise base.UpstreamProtocolViolation(
                f"non-object response from {path}")
        code = magicrew_events.business_code(envelope)
        # HTTP 200 + 业务错误：按 200 抛出，classify_status 会从 body 里读业务码
        if code is not None and code != magicrew_events.OK_CODE:
            raise UpstreamHTTPError(200, raw)
        return envelope

    async def _api_json(
        self,
        method: str,
        path: str,
        credential: MagicrewCredential,
        body: Any = _NO_BODY,
        timeout: float = API_TIMEOUT,
    ) -> dict[str, Any]:
        """JWT 业务请求：路径上统一带 `headers(credential)`。"""
        return await self._request_json(
            method, path, self.headers(credential),
            body=body, timeout=timeout)

    async def issue_gateway_key(self,
                                credential: MagicrewCredential) -> dict[str, Any]:
        """POST 签发 `mgw_` key（会吊销该账号此前的 key）。

        串行化是必须的：`RefreshTask` 与首个聊天请求可能同时发现 key 过期，
        两份 trip 各签一次 → 后者吊销前者 → 正在进行的请求拿废 key 去推理。
        锁是进程级的（同一凭证的签发本来就该唯一出口）。
        """
        async with self._issue_lock:
            envelope = await self._api_json(
                "POST", EP_GATEWAY_TOKENS, credential, body={})
            issued = _gateway_payload(_payload(envelope))
            self.remember_key(credential, issued)
            return issued

    def remember_key(self, credential: MagicrewCredential,
                     issued: dict[str, Any]) -> None:
        """把刚签发/续期的 key 记进进程内缓存（下次请求直接复用，不再签）。"""
        self._key_cache[self._cache_key(credential)] = issued

    async def refresh_gateway_key(self, refresh_token: str) -> dict[str, Any]:
        """PUT 用一次性 refresh_token 续 key；**不携带 JWT**（JWT 过期也要能续）。"""
        async with self._issue_lock:
            envelope = await self._request_json(
                "PUT", EP_GATEWAY_TOKENS, self._base_headers(),
                body={"refresh_token": refresh_token})
            return _gateway_payload(_payload(envelope))

    # ------------------------------------------------------------ 额度 / 模型

    async def probe_quota(self, credential: MagicrewCredential) -> Quota:
        """积分余额：上游只给剩余总量，故 `remaining == total`（健康度近似）。"""
        envelope = await self._api_json("GET", EP_POINTS, credential)
        data = _payload(envelope)
        total = _num(data.get("total_points"))
        probed_at = int(time.time())
        if total is None:
            return Quota(probe_failed=True, probed_at=probed_at)
        ladder: list[tuple[int, float]] = []
        packages: list[dict[str, Any]] = []
        details = data.get("expiring_quota_details")
        if isinstance(details, list):
            for item in details:
                if not isinstance(item, dict):
                    continue
                expires_at = _cst_epoch(item.get("expires_at"))
                if expires_at is None:
                    continue
                remaining = _num(item.get("remaining_points"))
                amount = remaining if remaining is not None else 0.0
                quota_id = item.get("quota_id")
                ladder.append((expires_at, amount))
                packages.append({
                    "name": quota_id if isinstance(quota_id, str) and quota_id
                    else "积分批次",
                    "total": amount,
                    "used": 0.0,
                    "end": expires_at,
                    "usable": True,
                })
        ladder.sort(key=lambda pair: pair[0])
        return Quota(
            remaining=total, total=total,
            cycle_end=ladder[0][0] if ladder else None,
            expiry_ladder=ladder or None,
            packages=packages or None,
            probed_at=probed_at,
        )

    async def fetch_models(self, credential: MagicrewCredential) -> list[Model]:
        """`data.models` → 模型表；缺 value 的脏条目跳过，空列表按协议违规。"""
        envelope = await self._api_json("GET", EP_MODELS, credential)
        items = _payload(envelope).get("models")
        if not isinstance(items, list):
            raise base.UpstreamProtocolViolation(
                "magicrew models response missing models list")
        models: list[Model] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            label = item.get("label")
            name = label.strip() if isinstance(label, str) and label.strip() \
                else value.strip()
            vision = item.get("vision")
            models.append(Model(
                id=value.strip(), name=name,
                supports_images=vision if isinstance(vision, bool) else None))
        if not models:
            raise base.UpstreamProtocolViolation(
                "magicrew models api returned no models")
        return models

    # ------------------------------------------------------------ 签到

    async def checkin(self, credential: MagicrewCredential) -> CheckinResult:
        """`POST /api/v1/login-gift`：`granted` 缺失是失败，`false` 才是已签。"""
        envelope = await self._api_json(
            "POST", EP_LOGIN_GIFT, credential, body=EMPTY_BODY)
        data = _payload(envelope)
        granted = data.get("granted")
        if not isinstance(granted, bool):
            # 缺字段不能当「今日已签」：那是静默漏领，用户以为签到了
            return CheckinResult(
                ok=False, message="响应里没有 granted 字段（上游形态可能已变）")
        message = data.get("message")
        message = message if isinstance(message, str) and message.strip() else ""
        if not granted:
            return CheckinResult(
                ok=True, already_checked_in=True,
                message=message or "今日已领取登录积分")
        return CheckinResult(ok=True, credit=_num(data.get("point_amount")),
                             message=message)

    async def checkin_status(self, credential: MagicrewCredential) -> dict:
        """只读积分（**不发签到请求**）；签到是用户/任务显式触发的动作。"""
        envelope = await self._api_json("GET", EP_POINTS, credential)
        return {
            "active": True,
            "today_checked_in": False,
            "total_credits": _num(_payload(envelope).get("total_points")),
            "activity_name": "每日登录积分",
        }

    # ------------------------------------------------------------ 推理

    @staticmethod
    def _cache_key(credential: MagicrewCredential) -> str:
        return hashlib.sha256(credential.jwt.encode("utf-8")).hexdigest()

    @staticmethod
    def _key_valid(api_key: str, expires_at: int, now: int) -> bool:
        if not api_key:
            return False
        return not (expires_at > 0 and now >= expires_at)

    async def _api_key_for(self, credential: MagicrewCredential,
                           cache_key: str) -> str:
        """取可用网关 key：凭证里的 → 进程内缓存 → 现签。

        顺序是刻意的：现签会吊销旧 key，能不签就不签。
        """
        now = int(time.time())
        if self._key_valid(credential.api_key,
                           credential.api_key_expires_at, now):
            return credential.api_key
        cached = self._key_cache.get(cache_key)
        if cached is not None and self._key_valid(
                str(cached.get("api_key") or ""),
                epoch_seconds(cached.get("api_key_expires_at")), now):
            return str(cached["api_key"])
        issued = await self.issue_gateway_key(credential)
        return str(issued["api_key"])

    async def _stream_once(self, credential: MagicrewCredential, api_key: str,
                           body: dict[str, Any]) -> AsyncIterator[Event]:
        """发一次推理请求并逐帧转事件（HTTP 错误 / 截断如实上抛）。"""
        try:
            async with self._stream.stream(
                "POST", f"{self.host}{EP_CHAT}", json=body,
                headers=self.chat_headers(api_key, credential),
                timeout=CHAT_TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    raise UpstreamHTTPError(
                        response.status_code,
                        await base.read_body_bounded(response),
                        **base.retry_hints(response.headers))
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type.lower():
                    # HTTP 200 + JSON 业务错误就靠这条：不能把它当 SSE 读
                    raise UpstreamHTTPError(
                        200, await base.read_body_bounded(response))
                finished = False
                ended = False
                async for frame in iter_frames(response.aiter_bytes()):
                    if frame.data.strip() == "[DONE]":
                        ended = True
                        break
                    for event in magicrew_events.frame_events(frame):
                        if event.kind is EventKind.ERROR:
                            # 流内错误已经带分类：交给引擎处置，不冒充「截断」
                            yield event
                            return
                        if event.kind is EventKind.FINISH:
                            finished = True
                        yield event
                if not finished and not ended:
                    raise base.UpstreamProtocolViolation(
                        "magicrew stream ended before finish")
        except httpx.TransportError as error:
            raise base.UpstreamTransportError(str(error)) from error

    async def stream_chat(self, credential: MagicrewCredential,
                          payload: dict[str, Any], model: str) -> AsyncIterator[Event]:
        """流式对话：api_key 失效时作废缓存、重签一次、重试一次。"""
        if not credential.jwt:
            raise base.UpstreamProtocolViolation(
                "magicrew credential missing jwt")
        body = copy.deepcopy(payload)
        body["model"] = model
        body["stream"] = True
        # 不发 include_usage 上游就不回 usage 帧（计费/占用条的唯一来源）
        body["stream_options"] = {"include_usage": True}
        cache_key = self._cache_key(credential)
        api_key = await self._api_key_for(credential, cache_key)
        emitted = False
        try:
            async for event in self._stream_once(credential, api_key, body):
                emitted = True
                yield event
            return
        except UpstreamHTTPError as error:
            # 已出帧后不重试：重试会把同一段内容发两遍；其它错误直接抛给上层
            if emitted or not magicrew_events.token_invalid(error.body):
                raise
        # 只在「上游点名 token 无效」且尚未出帧时重签；签新 key 会吊销旧 key
        issued = await self.issue_gateway_key(credential)
        async for event in self._stream_once(
                credential, str(issued["api_key"]), body):
            yield event
