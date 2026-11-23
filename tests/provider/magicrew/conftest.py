"""超级麦吉渠道测试共享装配（本目录自包含，不依赖本地 .env）。"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from src.provider.magicrew.client import MagicrewClient
from src.provider.magicrew.credential import MagicrewCredential
from tests.helpers.http import provider_client


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(claims: dict[str, Any] | None = None) -> str:
    """造一张结构合法（签名随便）的 JWT，payload 可指定 claim。"""
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims if claims is not None else {}).encode())
    return f"{header}.{payload}.signature"


JWT = make_jwt({"uid": "42", "sub": "sub-42"})


@pytest.fixture
def credential() -> MagicrewCredential:
    return MagicrewCredential(jwt=JWT, uid="42")


def ok(data: Any) -> dict[str, Any]:
    return {"code": 1000, "message": "ok", "data": data}


def client(handler, **kw: Any) -> MagicrewClient:
    kw.setdefault("host", "https://mw.test")
    kw.setdefault("web_origin", "https://web.test")
    return provider_client(MagicrewClient, handler, **kw)


def handler_for(mapping: dict, *, calls: list[httpx.Request] | None = None):
    """按 (method, path) 返回 JSON / 文本 / Response；未命中抛断言。"""
    recorded = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        key = (request.method, request.url.path)
        value = mapping.get(key)
        if value is None:
            raise AssertionError(f"unexpected request {key}")
        if callable(value):
            return value(request)
        if isinstance(value, httpx.Response):
            return value
        if isinstance(value, str):
            return httpx.Response(200, text=value)
        return httpx.Response(200, json=value)

    handler.calls = recorded  # type: ignore[attr-defined]
    return handler


def sse_response(*payloads: dict[str, Any], done: bool = True) -> httpx.Response:
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(
        200, text=body, headers={"content-type": "text/event-stream"})


def chunk(delta: dict[str, Any], *, finish: str | None = None,
          usage: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "x", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload
