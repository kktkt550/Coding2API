"""超级麦吉（Magicrew）错误分类与 SSE 帧映射。

两个和别的渠道不同的点：

1. **业务码优先于 HTTP 状态**：上游把失败装在 HTTP 200 的信封里
   （`{"code":403,"message":"token无效"}`）⇒ 只判状态码会把这次失败当成功。
2. **流内错误有两种信封**：标准 OpenAI `{"error": {...}}`，以及本渠道业务码
   `{"code": 4xx, "message": …}`（帧里出现非 1000 的 `code` 即错误）。

SSE 解析本身走 `engine.sse.iter_frames` + `provider.openai_chunk`，本模块只做
「帧 → 错误/事件」的语义映射，不写第二套解析器。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...engine.sse import SSEFrame
from ...provider.base import ErrKind, Event, EventKind
from ...provider.openai_chunk import openai_events, payload_of

#: 业务成功码
OK_CODE = 1000

#: api_key 失效（HTTP 200 + code 403 的典型文案）
_TOKEN_DEAD = re.compile(r"token\s*无效|token\s*invalid|unauthorized", re.I)
#: 额度 / 积分不足
_CREDIT = re.compile(r"额度|积分不足|余额不足")
#: 频率 / 限流
_RATE = re.compile(r"频率|限流")
#: 需要重新导入 JWT（refresh token 明确失效）的两个组成条件
_REFRESH = re.compile(r"refresh", re.I)
_EXPIRED = re.compile(r"无效|过期|invalid|expired", re.I)


def _decode(body: bytes | str | None) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


def _code(value: Any) -> int | None:
    """业务码 → int；上游可能是十进制字符串（`"403"`），非数字返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else None
    return None


def _envelope(body: bytes | str | None) -> tuple[int | None, str, str]:
    """body → `(业务码, message, 原文)`；非 JSON / 非对象时不猜码。"""
    text = _decode(body)
    if not text:
        return None, "", ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, "", text
    if not isinstance(parsed, dict):
        return None, "", text
    message = parsed.get("message")
    return _code(parsed.get("code")), message if isinstance(message, str) else "", text


def business_code(payload: dict[str, Any]) -> int | None:
    """JSON 对象里的业务码（成功码是 1000；缺码 / 非数字为 None）。"""
    return _code(payload.get("code"))


def token_invalid(body: bytes | str | None) -> bool:
    """body 是否在说 api_key 失效（重签唯一判据，刻意不吃 HTTP 401/403）。"""
    _, message, text = _envelope(body)
    return bool(_TOKEN_DEAD.search(f"{message}\n{text}"))


def refresh_token_invalid(body: bytes | str | None) -> bool:
    """body 是否在说 refresh_token 已作废（需人工重新导入 JWT）。"""
    _, message, text = _envelope(body)
    haystack = f"{message}\n{text}"
    return bool(_REFRESH.search(haystack) and _EXPIRED.search(haystack))


def classify_status(status: int, body: bytes | str | None = None) -> ErrKind:
    """上游失败 → 冷却分类；**body 优先**，无业务码才看 HTTP 状态。

    顺序照抄取证：先文本（`token无效` 是 key 失效的唯一判据），再业务码，
    最后才是 HTTP。HTTP 200 + 业务错误就靠这一步才不会落成 OTHER。
    """
    code, message, text = _envelope(body)
    haystack = f"{message}\n{text}".lower()
    if _TOKEN_DEAD.search(haystack):
        return ErrKind.DEAD
    # 2182 /「组织」= organization-code 配错：跳过整个上游，换号无用
    if code == 2182 or "组织" in haystack:
        return ErrKind.INVALID
    if code == 402 or _CREDIT.search(haystack):
        return ErrKind.CREDIT
    if code == 429 or _RATE.search(haystack):
        return ErrKind.SOFT
    if code in (401, 403):
        return ErrKind.DEAD
    # 有业务码但没命中任何已知语义：不拿 HTTP 状态二次归类（业务码才是判据）
    if code is not None:
        return ErrKind.OTHER
    if status in (401, 403):
        return ErrKind.DEAD
    if status == 402:
        return ErrKind.CREDIT
    if status in (400, 404, 422):
        return ErrKind.INVALID
    if status == 429:
        return ErrKind.SOFT
    return ErrKind.OTHER


def _error_event(error: dict[str, Any]) -> Event:
    """标准 OpenAI `{"error": {...}}` → 错误事件。"""
    message = error.get("message")
    return Event(
        kind=EventKind.ERROR,
        error_code=_code(error.get("code")),
        error_message=message if isinstance(message, str) else "",
        error_kind=classify_status(200, json.dumps(error, ensure_ascii=False)),
    )


def frame_events(frame: SSEFrame) -> list[Event]:
    """一帧 SSE → 若干中立事件。

    帧内业务码（非 1000）先于 OpenAI 形状判定：上游把鉴权失败也塞进流里，
    不拦会变成「模型不回复」而日志零报错。`[DONE]` / 空帧返回空列表。
    """
    payload = payload_of(frame, "magicrew")
    if payload is None:
        return []
    code = business_code(payload)
    if code is not None and code != OK_CODE:
        message = payload.get("message")
        return [Event(
            kind=EventKind.ERROR,
            error_code=code,
            error_message=message if isinstance(message, str) else "",
            error_kind=classify_status(
                200, json.dumps(payload, ensure_ascii=False)),
        )]
    return openai_events(frame, "magicrew", error_event=_error_event)
