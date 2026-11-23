"""上游客户端：信封/鉴权头、签发续期、额度、目录、签到与流式推理。"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx
import pytest

from src.provider import base
from src.provider.base import (
    ErrKind,
    Event,
    EventKind,
    UpstreamProtocolViolation,
)
from src.provider.magicrew.client import (
    DEFAULT_HOST,
    DEFAULT_USER_AGENT,
    DEFAULT_WEB_ORIGIN,
    EP_CHAT,
    EP_GATEWAY_TOKENS,
    EP_LOGIN_GIFT,
    EP_MODELS,
    EP_POINTS,
    MagicrewClient,
    UpstreamHTTPError,
)
from src.provider.magicrew.credential import MagicrewCredential
from tests.provider.magicrew.conftest import (
    JWT,
    chunk,
    client,
    handler_for,
    ok,
    sse_response,
)

FUTURE = int(time.time()) + 3600
PAST = int(time.time()) - 3600


def _credential(**kw) -> MagicrewCredential:
    kw.setdefault("jwt", JWT)
    kw.setdefault("uid", "42")
    return MagicrewCredential(**kw)


def test_default_endpoints_are_the_domestic_site():
    instance = MagicrewClient()
    assert instance.host == DEFAULT_HOST
    assert instance.web_origin == DEFAULT_WEB_ORIGIN
    assert instance.user_agent == DEFAULT_USER_AGENT
    assert "Chrome/141.0.7390.55" in DEFAULT_USER_AGENT
    assert instance._key_cache == {}


def test_headers_carry_origin_referer_and_optional_org():
    instance = MagicrewClient(host="https://mw.test/", web_origin="https://web.test/")
    headers = instance.headers(_credential())
    assert headers["authorization"] == f"Bearer {JWT}"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert headers["origin"] == "https://web.test"
    assert headers["referer"] == "https://web.test/"
    assert headers["user-agent"] == DEFAULT_USER_AGENT
    assert "organization-code" not in headers

    headers = instance.headers(_credential(organization_code="org-1"))
    assert headers["organization-code"] == "org-1"


def test_chat_headers_use_api_key_and_sse_accept():
    instance = MagicrewClient(host="https://mw.test", web_origin="https://web.test")
    headers = instance.chat_headers("mgw_k", _credential(organization_code="o"))
    assert headers["authorization"] == "Bearer mgw_k"
    assert headers["accept"] == "text/event-stream"
    assert headers["organization-code"] == "o"
    assert "organization-code" not in instance.chat_headers("mgw_k", _credential())


async def test_issue_gateway_key_returns_subset_and_headers():
    calls: list[httpx.Request] = []
    handler = handler_for({("POST", EP_GATEWAY_TOKENS): ok({
        "api_key": " mgw_new ",
        "refresh_token": "r1",
        "api_key_expires_at": "1700000000",
        "refresh_token_expires_at": 1700000100,
    })}, calls=calls)
    instance = client(handler)
    issued = await instance.issue_gateway_key(_credential(organization_code="org"))

    assert issued == {
        "api_key": "mgw_new", "refresh_token": "r1",
        "api_key_expires_at": 1700000000,
        "refresh_token_expires_at": 1700000100,
    }
    request = calls[0]
    assert request.headers["authorization"] == f"Bearer {JWT}"
    assert request.headers["organization-code"] == "org"
    assert request.headers["origin"] == "https://web.test"
    assert request.headers["referer"] == "https://web.test/"
    assert json.loads(request.content) == {}


@pytest.mark.parametrize("data", [{}, {"api_key": "   "}, {"api_key": 1}, "text"])
async def test_issue_gateway_key_without_api_key_is_protocol_violation(data):
    handler = handler_for({("POST", EP_GATEWAY_TOKENS): ok(data)})
    with pytest.raises(UpstreamProtocolViolation):
        await client(handler).issue_gateway_key(_credential())


async def test_issue_gateway_key_skips_empty_optional_fields():
    handler = handler_for({("POST", EP_GATEWAY_TOKENS): ok({
        "api_key": "k", "refresh_token": "", "api_key_expires_at": "later",
    })})
    assert await client(handler).issue_gateway_key(_credential()) == {"api_key": "k"}


async def test_refresh_gateway_key_is_put_without_jwt():
    calls: list[httpx.Request] = []
    handler = handler_for({("PUT", EP_GATEWAY_TOKENS): ok({
        "api_key": "mgw_rotated", "refresh_token": "r2",
        "api_key_expires_at": 1700000000,
    })}, calls=calls)
    issued = await client(handler).refresh_gateway_key("r1")

    assert issued == {"api_key": "mgw_rotated", "refresh_token": "r2",
                      "api_key_expires_at": 1700000000}
    request = calls[0]
    assert "authorization" not in {key.lower() for key in request.headers}
    assert json.loads(request.content) == {"refresh_token": "r1"}


async def test_refresh_gateway_key_missing_api_key():
    handler = handler_for({("PUT", EP_GATEWAY_TOKENS): ok({"refresh_token": "r"})})
    with pytest.raises(UpstreamProtocolViolation):
        await client(handler).refresh_gateway_key("r")


async def test_api_http_error_keeps_status_and_retry_hints():
    response = httpx.Response(429, text="slow down",
                              headers={"retry-after": "30"})
    handler = handler_for({("GET", EP_POINTS): response})
    with pytest.raises(UpstreamHTTPError) as error:
        await client(handler).probe_quota(_credential())
    assert error.value.status == 429
    assert error.value.retry_after_seconds == 30


@pytest.mark.parametrize("code", [403, "403"])
async def test_business_error_on_http_200_is_raised(code):
    body = json.dumps({"code": code, "message": "token无效"})
    handler = handler_for({("GET", EP_POINTS):
                           httpx.Response(200, text=body)})
    with pytest.raises(UpstreamHTTPError) as error:
        await client(handler).probe_quota(_credential())
    assert error.value.status == 200
    assert error.value.kind() is ErrKind.DEAD


async def test_envelope_without_code_is_accepted():
    handler = handler_for({("GET", EP_MODELS): {
        "data": {"models": [{"value": "m", "label": " M "}]},
    }})
    models = await client(handler).fetch_models(_credential())
    assert models[0].id == "m"


async def test_non_json_and_non_object_are_protocol_violations():
    with pytest.raises(UpstreamProtocolViolation):
        await client(handler_for({("GET", EP_POINTS):
                                  httpx.Response(200, text="not json")})) \
            .probe_quota(_credential())
    with pytest.raises(UpstreamProtocolViolation):
        await client(handler_for({("GET", EP_POINTS):
                                  httpx.Response(200, json=[1, 2])})) \
            .probe_quota(_credential())


async def test_transport_error_is_wrapped():
    def boom(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(base.UpstreamTransportError):
        await client(handler_for({("GET", EP_POINTS): boom})).probe_quota(
            _credential())


async def test_probe_quota_builds_ladder_and_packages():
    handler = handler_for({("GET", EP_POINTS): ok({
        "total_points": "12.5",
        "expiring_quota_details": [
            {"quota_id": "q2", "expires_at": "2026-11-28 00:00:00",
             "remaining_points": 5},
            {"quota_id": "q1", "expires_at": "2026-11-27 13:47:42",
             "remaining_points": "2.5"},
            "dirty",
            {"quota_id": "bad", "expires_at": "not-a-date", "remaining_points": 1},
            {"quota_id": "none", "expires_at": None, "remaining_points": 1},
            {"quota_id": "blank", "expires_at": "  ", "remaining_points": 1},
            {"quota_id": 7, "expires_at": "2026-11-29 00:00:00"},
        ],
    })})
    quota = await client(handler).probe_quota(_credential())

    assert quota.remaining == 12.5
    assert quota.total == 12.5
    assert quota.probe_failed is False
    assert quota.probed_at
    assert [amount for _, amount in quota.expiry_ladder] == [2.5, 5.0, 0.0]
    assert quota.cycle_end == quota.expiry_ladder[0][0]
    assert [package["name"] for package in quota.packages] == ["q2", "q1", "积分批次"]
    assert quota.packages[0] == {
        "name": "q2", "total": 5.0, "used": 0.0,
        "end": quota.expiry_ladder[1][0], "usable": True,
    }
    # 2026-11-27 13:47:42 按 UTC+8 解释 = 05:47:42 UTC
    assert quota.expiry_ladder[0][0] == int(
        datetime(2026, 11, 27, 5, 47, 42, tzinfo=UTC).timestamp())


async def test_probe_quota_missing_total_is_probe_failed():
    handler = handler_for({("GET", EP_POINTS): ok({"expiring_quota_details": []})})
    quota = await client(handler).probe_quota(_credential())
    assert quota.probe_failed is True
    assert quota.remaining is None
    assert quota.probed_at


@pytest.mark.parametrize("total", ["soon", ["1"], True, None])
async def test_probe_quota_unparsable_total_is_probe_failed(total):
    handler = handler_for({("GET", EP_POINTS): ok({"total_points": total})})
    quota = await client(handler).probe_quota(_credential())
    assert quota.probe_failed is True


async def test_probe_quota_accepts_missing_or_bad_details():
    handler = handler_for({("GET", EP_POINTS): ok({
        "total_points": 0, "expiring_quota_details": {},
    })})
    quota = await client(handler).probe_quota(_credential())
    assert quota.remaining == 0
    assert quota.expiry_ladder is None
    assert quota.packages is None
    assert quota.cycle_end is None


async def test_fetch_models_skips_dirty_entries():
    handler = handler_for({("GET", EP_MODELS): ok({"models": [
        "dirty",
        {"label": "no value"},
        {"value": "  "},
        {"value": "m1", "label": " DeepSeek V4.1 Flash ", "vision": True},
        {"value": "m2", "label": "", "vision": "true"},
        {"value": "m3", "label": 7, "vision": False},
    ]})})
    models = await client(handler).fetch_models(_credential())

    assert [model.id for model in models] == ["m1", "m2", "m3"]
    assert [model.name for model in models] == [
        "DeepSeek V4.1 Flash", "m2", "m3"]
    assert [model.supports_images for model in models] == [True, None, False]


@pytest.mark.parametrize("data", [{}, {"models": "x"}, {"models": ["dirty"]}])
async def test_fetch_models_rejects_empty_or_broken(data):
    handler = handler_for({("GET", EP_MODELS): ok(data)})
    with pytest.raises(UpstreamProtocolViolation):
        await client(handler).fetch_models(_credential())


async def test_checkin_granted_returns_credit_and_sends_empty_body():
    calls: list[httpx.Request] = []
    handler = handler_for({("POST", EP_LOGIN_GIFT): ok({
        "granted": True, "point_amount": 5, "message": "",
    })}, calls=calls)
    result = await client(handler).checkin(_credential())

    assert result.ok is True
    assert result.credit == 5.0
    assert result.already_checked_in is False
    assert result.message == ""
    assert calls[0].method == "POST"
    assert calls[0].content == b""


async def test_checkin_already_claimed_uses_upstream_message():
    handler = handler_for({("POST", EP_LOGIN_GIFT): ok({
        "granted": False, "message": "今天已经领过了",
    })})
    result = await client(handler).checkin(_credential())
    assert result.ok is True
    assert result.already_checked_in is True
    assert result.message == "今天已经领过了"


async def test_checkin_already_claimed_default_message():
    handler = handler_for({("POST", EP_LOGIN_GIFT): ok({
        "granted": False, "message": "  ",
    })})
    result = await client(handler).checkin(_credential())
    assert result.message == "今日已领取登录积分"


@pytest.mark.parametrize("data", [{}, {"granted": None}, {"granted": "true"},
                                  {"granted": 1}])
async def test_checkin_missing_granted_is_failure(data):
    handler = handler_for({("POST", EP_LOGIN_GIFT): ok(data)})
    result = await client(handler).checkin(_credential())
    assert result.ok is False
    assert "granted" in result.message


async def test_checkin_status_only_reads_points():
    calls: list[httpx.Request] = []
    handler = handler_for({("GET", EP_POINTS): ok({"total_points": 33})},
                          calls=calls)
    status = await client(handler).checkin_status(_credential())

    assert status == {
        "active": True, "today_checked_in": False,
        "total_credits": 33.0, "activity_name": "每日登录积分",
    }
    assert [(request.method, request.url.path) for request in calls] == [
        ("GET", EP_POINTS)]


async def test_stream_chat_requires_jwt():
    instance = client(handler_for({}))
    with pytest.raises(UpstreamProtocolViolation):
        [event async for event in instance.stream_chat(
            _credential(jwt=""), {}, "m")]


async def test_stream_uses_credential_api_key_and_normal_events():
    calls: list[httpx.Request] = []
    handler = handler_for({("POST", EP_CHAT): sse_response(
        chunk({"reasoning_content": "想"}),
        chunk({"content": "你好"}),
        chunk({}, finish="stop", usage={
            "prompt_tokens": 3, "completion_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }),
    )}, calls=calls)
    payload = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.3}
    events = [event async for event in client(handler).stream_chat(
        _credential(api_key="mgw_live", api_key_expires_at=FUTURE), payload, "m1")]

    assert [event.kind for event in events] == [
        EventKind.REASONING, EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]
    assert events[0].content == "想"
    assert events[1].content == "你好"
    assert events[2].usage.input_tokens == 3
    assert events[2].usage.reasoning_tokens == 2
    assert events[3].finish_reason == "stop"
    # 请求体：覆写 model、强制流式、保留调用方 messages 的深拷贝
    request = calls[0]
    assert request.headers["authorization"] == "Bearer mgw_live"
    body = json.loads(request.content)
    assert body["model"] == "m1"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.3
    assert payload == {"messages": [{"role": "user", "content": "hi"}],
                       "temperature": 0.3}


async def test_stream_issues_key_when_missing_then_reuses_cache():
    calls: list[httpx.Request] = []

    def tokens(request):
        return httpx.Response(200, json=ok({
            "api_key": "mgw_issued", "api_key_expires_at": FUTURE}))

    handler = handler_for({
        ("POST", EP_GATEWAY_TOKENS): tokens,
        ("POST", EP_CHAT): sse_response(chunk({}, finish="stop")),
    }, calls=calls)
    instance = client(handler)
    credential = _credential()

    first = [event async for event in instance.stream_chat(credential, {}, "m")]
    second = [event async for event in instance.stream_chat(credential, {}, "m")]

    assert first and second
    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", EP_GATEWAY_TOKENS), ("POST", EP_CHAT), ("POST", EP_CHAT)]
    assert calls[1].headers["authorization"] == "Bearer mgw_issued"


async def test_stream_uses_unexpired_cached_key():
    calls: list[httpx.Request] = []
    handler = handler_for({("POST", EP_CHAT): sse_response(chunk({}, finish="stop"))},
                          calls=calls)
    instance = client(handler)
    credential = _credential(api_key="mgw_old", api_key_expires_at=PAST)
    instance._key_cache[instance._cache_key(credential)] = {
        "api_key": "mgw_cached", "api_key_expires_at": FUTURE}

    events = [event async for event in instance.stream_chat(credential, {}, "m")]
    assert events
    assert calls[0].headers["authorization"] == "Bearer mgw_cached"


async def test_stream_reissues_when_cached_key_expired():
    calls: list[httpx.Request] = []
    handler = handler_for({
        ("POST", EP_GATEWAY_TOKENS): ok({"api_key": "mgw_fresh"}),
        ("POST", EP_CHAT): sse_response(chunk({}, finish="stop")),
    }, calls=calls)
    instance = client(handler)
    credential = _credential(api_key="mgw_old", api_key_expires_at=PAST)
    instance._key_cache[instance._cache_key(credential)] = {
        "api_key": "mgw_stale", "api_key_expires_at": PAST}

    events = [event async for event in instance.stream_chat(credential, {}, "m")]
    assert events
    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", EP_GATEWAY_TOKENS), ("POST", EP_CHAT)]
    assert calls[1].headers["authorization"] == "Bearer mgw_fresh"


async def test_stream_reissues_and_retries_once_on_token_invalid():
    calls: list[httpx.Request] = []
    chats = {"count": 0}

    def chat(request):
        chats["count"] += 1
        if chats["count"] == 1:
            return httpx.Response(200, text=json.dumps(
                {"code": 403, "message": "token无效"}))
        return sse_response(chunk({"content": "ok"}, finish="stop"))

    handler = handler_for({
        ("POST", EP_GATEWAY_TOKENS): ok({"api_key": "mgw_new"}),
        ("POST", EP_CHAT): chat,
    }, calls=calls)
    events = [event async for event in client(handler).stream_chat(
        _credential(api_key="mgw_old", api_key_expires_at=FUTURE), {}, "m")]

    assert [event.content for event in events if event.kind is EventKind.CONTENT] \
        == ["ok"]
    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", EP_CHAT), ("POST", EP_GATEWAY_TOKENS), ("POST", EP_CHAT)]
    assert calls[2].headers["authorization"] == "Bearer mgw_new"


async def test_stream_retry_failure_propagates():
    calls: list[httpx.Request] = []

    def dead(request):
        return httpx.Response(200, text=json.dumps(
            {"code": 403, "message": "token无效"}))

    handler = handler_for({
        ("POST", EP_GATEWAY_TOKENS): ok({"api_key": "mgw_new"}),
        ("POST", EP_CHAT): dead,
    }, calls=calls)
    with pytest.raises(UpstreamHTTPError) as error:
        [event async for event in client(handler).stream_chat(
            _credential(api_key="mgw_old", api_key_expires_at=FUTURE), {}, "m")]

    assert error.value.status == 200
    assert error.value.kind() is ErrKind.DEAD
    assert len([c for c in calls if c.url.path == EP_GATEWAY_TOKENS]) == 1
    assert len([c for c in calls if c.url.path == EP_CHAT]) == 2


async def test_stream_non_token_error_is_not_retried():
    calls: list[httpx.Request] = []
    handler = handler_for({
        ("POST", EP_CHAT): httpx.Response(
            200, text=json.dumps({"code": 2182, "message": "用户所在组织不存在"})),
    }, calls=calls)
    with pytest.raises(UpstreamHTTPError) as error:
        [event async for event in client(handler).stream_chat(
            _credential(api_key="mgw_live", api_key_expires_at=FUTURE), {}, "m")]

    assert error.value.kind() is ErrKind.INVALID
    assert [c.url.path for c in calls] == [EP_CHAT]


async def test_stream_http_status_error():
    handler = handler_for({("POST", EP_CHAT): httpx.Response(500, text="down")})
    with pytest.raises(UpstreamHTTPError) as error:
        [event async for event in client(handler).stream_chat(
            _credential(api_key="mgw_live", api_key_expires_at=FUTURE), {}, "m")]
    assert error.value.status == 500


async def test_stream_truncated_before_finish_is_protocol_violation():
    handler = handler_for({("POST", EP_CHAT): sse_response(
        chunk({"content": "half"}), done=False)})
    with pytest.raises(UpstreamProtocolViolation):
        [event async for event in client(handler).stream_chat(
            _credential(api_key="mgw_live", api_key_expires_at=FUTURE), {}, "m")]


async def test_stream_done_without_finish_is_accepted():
    handler = handler_for({("POST", EP_CHAT): sse_response(
        chunk({"content": "all"}))})
    events = [event async for event in client(handler).stream_chat(
        _credential(api_key="mgw_live", api_key_expires_at=FUTURE), {}, "m")]
    assert [event.kind for event in events] == [EventKind.CONTENT]


async def test_stream_frame_error_becomes_error_event_and_stops():
    handler = handler_for({("POST", EP_CHAT): sse_response(
        {"code": 403, "message": "token无效"}, done=False)})
    events = [event async for event in client(handler).stream_chat(
        _credential(api_key="mgw_live", api_key_expires_at=FUTURE), {}, "m")]
    assert len(events) == 1
    assert events[0].kind is EventKind.ERROR
    assert events[0].error_kind is ErrKind.DEAD


async def test_stream_transport_error_is_wrapped():
    def boom(request):
        raise httpx.ReadError("boom", request=request)

    handler = handler_for({("POST", EP_CHAT): boom})
    with pytest.raises(base.UpstreamTransportError):
        [event async for event in client(handler).stream_chat(
            _credential(api_key="mgw_live", api_key_expires_at=FUTURE), {}, "m")]


async def test_stream_does_not_retry_after_emitting(monkeypatch):
    instance = client(handler_for({}))
    credential = _credential(api_key="mgw_live", api_key_expires_at=FUTURE)

    async def fake_stream(credential_arg, api_key, body):
        yield Event(kind=EventKind.CONTENT, content="partial")
        raise UpstreamHTTPError(
            200, json.dumps({"code": 403, "message": "token无效"}).encode())

    monkeypatch.setattr(instance, "_stream_once", fake_stream)
    with pytest.raises(UpstreamHTTPError):
        [event async for event in instance.stream_chat(credential, {}, "m")]


async def test_aclose_closes_injected_clients():
    instance = client(handler_for({}))
    await instance.aclose()
    assert instance._stream_client.is_closed
    assert instance._short_client.is_closed
