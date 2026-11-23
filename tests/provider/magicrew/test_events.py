"""错误分类矩阵（body 优先）与 SSE 帧映射。"""

from __future__ import annotations

import json

import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind, UpstreamProtocolViolation
from src.provider.magicrew import events


def _body(payload) -> bytes:
    return json.dumps(payload).encode()


def test_business_code_accepts_int_string_and_float():
    assert events.business_code({"code": 1000}) == 1000
    assert events.business_code({"code": "403"}) == 403
    assert events.business_code({"code": 403.0}) == 403
    assert events.business_code({"code": 403.5}) is None
    assert events.business_code({"code": "nope"}) is None
    assert events.business_code({"code": True}) is None
    assert events.business_code({"code": [403]}) is None
    assert events.business_code({}) is None


def test_token_invalid_markers():
    assert events.token_invalid(_body({"code": 403, "message": "token无效"}))
    assert events.token_invalid(b"Token Invalid")
    assert events.token_invalid("unauthorized")
    assert not events.token_invalid(_body({"code": 2182, "message": "组织不存在"}))
    assert not events.token_invalid(None)


def test_refresh_token_invalid_requires_both_halves():
    assert events.refresh_token_invalid(
        _body({"code": 403, "message": "refresh token 已过期"}))
    assert events.refresh_token_invalid("refresh_token invalid")
    assert not events.refresh_token_invalid("refresh token 还在")
    assert not events.refresh_token_invalid("token 已过期")


def test_classify_body_first_matrix():
    """HTTP 200 + 业务错误：分类必须来自 body，而不是状态码。"""
    assert events.classify_status(
        200, _body({"code": 403, "message": "token无效"})) is ErrKind.DEAD
    assert events.classify_status(400, b"unauthorized") is ErrKind.DEAD
    assert events.classify_status(
        400, _body({"code": 2182, "message": "用户所在组织不存在"})) is ErrKind.INVALID
    assert events.classify_status(
        200, _body({"message": "组织不存在"})) is ErrKind.INVALID
    assert events.classify_status(
        200, _body({"code": 14018, "message": "积分不足"})) is ErrKind.CREDIT
    assert events.classify_status(200, _body({"code": 402})) is ErrKind.CREDIT
    assert events.classify_status(500, "bad 额度") is ErrKind.CREDIT
    assert events.classify_status(200, b'{"message":"\xe9\x99\x90\xe6\xb5\x81"}') \
        is ErrKind.SOFT
    assert events.classify_status(200, _body({"code": 429})) is ErrKind.SOFT
    assert events.classify_status(200, _body({"code": 401})) is ErrKind.DEAD
    assert events.classify_status(200, _body({"code": "403"})) is ErrKind.DEAD


def test_classify_unknown_business_code_does_not_fall_back_to_http():
    assert events.classify_status(503, _body({"code": 9999})) is ErrKind.OTHER
    assert events.classify_status(200, _body({"code": 1000})) is ErrKind.OTHER


def test_classify_http_matrix_without_business_code():
    assert events.classify_status(401, b"") is ErrKind.DEAD
    assert events.classify_status(403, None) is ErrKind.DEAD
    assert events.classify_status(402, b"no") is ErrKind.CREDIT
    assert events.classify_status(400, b"no") is ErrKind.INVALID
    assert events.classify_status(404, b"no") is ErrKind.INVALID
    assert events.classify_status(422, b"no") is ErrKind.INVALID
    assert events.classify_status(429, b"no") is ErrKind.SOFT
    assert events.classify_status(500, b"no") is ErrKind.OTHER
    assert events.classify_status(200, b"") is ErrKind.OTHER


def test_classify_tolerates_non_object_json():
    assert events.classify_status(200, b"[1, 2]") is ErrKind.OTHER
    assert events.classify_status(200, "null") is ErrKind.OTHER


def test_frame_content_reasoning_usage_and_finish():
    frame = SSEFrame(event="", data=json.dumps({
        "model": "m1",
        "choices": [{"index": 0, "delta": {"reasoning_content": "想"},
                     "finish_reason": None}],
    }))
    parsed = events.frame_events(frame)
    assert parsed[0].kind is EventKind.REASONING
    assert parsed[0].content == "想"
    assert parsed[0].model == "m1"

    frame = SSEFrame(event="", data=json.dumps({
        "choices": [{"index": 0, "delta": {"content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }))
    kinds = [event.kind for event in events.frame_events(frame)]
    assert kinds == [EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]


def test_frame_done_and_empty_are_ignored():
    assert events.frame_events(SSEFrame(event="", data="[DONE]")) == []
    assert events.frame_events(SSEFrame(event="", data="")) == []


def test_frame_openai_error_envelope():
    frame = SSEFrame(event="", data=json.dumps(
        {"error": {"code": "403", "message": "token无效"}}))
    parsed = events.frame_events(frame)
    assert parsed[0].kind is EventKind.ERROR
    assert parsed[0].error_code == 403
    assert parsed[0].error_message == "token无效"
    assert parsed[0].error_kind is ErrKind.DEAD

    frame = SSEFrame(event="", data=json.dumps({"error": {"message": 7}}))
    parsed = events.frame_events(frame)
    assert parsed[0].error_code is None
    assert parsed[0].error_message == ""


def test_frame_business_code_error_forms():
    frame = SSEFrame(event="", data=json.dumps(
        {"code": 403, "message": "token无效"}))
    parsed = events.frame_events(frame)
    assert parsed[0].kind is EventKind.ERROR
    assert parsed[0].error_code == 403
    assert parsed[0].error_kind is ErrKind.DEAD

    frame = SSEFrame(event="", data=json.dumps({"code": "2182"}))
    parsed = events.frame_events(frame)
    assert parsed[0].error_code == 2182
    assert parsed[0].error_message == ""
    assert parsed[0].error_kind is ErrKind.INVALID


def test_frame_success_code_is_not_an_error():
    frame = SSEFrame(event="", data=json.dumps({
        "code": 1000,
        "choices": [{"index": 0, "delta": {"content": "ok"}}],
    }))
    parsed = events.frame_events(frame)
    assert parsed[0].kind is EventKind.CONTENT


@pytest.mark.parametrize("data", ["not json", "[1, 2]"])
def test_frame_broken_json_is_protocol_violation(data):
    with pytest.raises(UpstreamProtocolViolation):
        events.frame_events(SSEFrame(event="", data=data))
