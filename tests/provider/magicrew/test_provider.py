"""Provider 门面：导入、续期、签到、探活、模型与节流。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from src.provider.base import (
    CheckinResult,
    ErrKind,
    Event,
    EventKind,
    Model,
    Quota,
    UpstreamProtocolViolation,
    UpstreamReloginRequired,
)
from src.provider.magicrew import MagicrewProvider
from src.provider.magicrew.client import UpstreamHTTPError
from src.provider.magicrew.credential import MagicrewCredential
from tests.provider.magicrew.conftest import JWT, make_jwt


class FakeClient:
    """只实现门面用到的接口；调用记录便于断言（不替实现真实 HTTP）。"""

    def __init__(self, *, quota=None, models=None, events=None,
                 checkin=None, status=None) -> None:
        self.quota = quota
        self.models = models or []
        self._events = events or [Event(kind=EventKind.CONTENT, content="x")]
        self._checkin = checkin
        self._status = status or {}
        self.issued: dict[str, Any] = {"api_key": "mgw_issued"}
        self.refreshed: dict[str, Any] = {"api_key": "mgw_refreshed"}
        self.calls: list[Any] = []
        self.closed = False

    async def probe_quota(self, credential):
        self.calls.append(("probe", credential))
        return self.quota

    def remember_key(self, credential, issued):
        # 由 `MagicrewProvider.refresh` 调用；测试替身只记录，不缓存
        self.calls.append(("remember", issued))

    async def fetch_models(self, credential):
        self.calls.append(("models", credential))
        return self.models

    async def checkin(self, credential):
        self.calls.append(("checkin", credential))
        if self._checkin is None:
            raise AssertionError("unexpected checkin")
        return self._checkin

    async def checkin_status(self, credential):
        self.calls.append(("status", credential))
        return self._status

    async def issue_gateway_key(self, credential):
        self.calls.append(("issue", credential))
        return self.issued

    async def refresh_gateway_key(self, refresh_token):
        self.calls.append(("refresh", refresh_token))
        return self.refreshed

    async def stream_chat(self, credential, payload, model) -> AsyncIterator[Event]:
        self.calls.append(("stream", credential, payload, model))
        for event in self._events:
            yield event

    async def aclose(self):
        self.closed = True


class FakePacer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def wait_turn(self, key=None):
        self.calls.append(("wait", key))

    def release(self, key=None):
        self.calls.append(("release", key))


def _provider(client) -> MagicrewProvider:
    return MagicrewProvider(client=client)


def _raiser(error: BaseException):
    async def _call(*args):
        raise error
    return _call


def test_import_credential_normalizes_jwt():
    provider = _provider(FakeClient())
    assert provider.import_credential(JWT) == {"jwt": JWT, "uid": "42"}
    assert provider.import_credential({"token": make_jwt(), "organizationCode": "o"}) \
        == {"jwt": make_jwt(), "organization_code": "o"}
    with pytest.raises(UpstreamProtocolViolation):
        provider.import_credential({"uid": "1"})


def test_classify_is_body_first():
    provider = _provider(FakeClient())
    assert provider.classify(200, b'{"code":9999}') is ErrKind.OTHER
    assert provider.classify(
        200, json.dumps({"code": 403, "message": "token无效"}).encode()) \
        is ErrKind.DEAD


def test_credential_from_exposes_needs_refresh():
    credential = _provider(FakeClient()).credential_from({"jwt": JWT})
    assert isinstance(credential, MagicrewCredential)
    assert credential.needs_refresh(60, 1)


async def test_refresh_uses_refresh_token_and_preserves_identity():
    client = FakeClient()
    client.refreshed = {"api_key": "mgw_new", "refresh_token": "r2"}
    data = {
        "jwt": JWT, "organization_code": "org", "uid": "42",
        "nickname": "nick", "refresh_token": "r1",
    }
    refreshed = await _provider(client).refresh(data)

    assert refreshed == {
        "jwt": JWT, "organization_code": "org", "uid": "42",
        "nickname": "nick", "api_key": "mgw_new", "refresh_token": "r2",
    }
    # 续期后立刻记进进程内缓存：下一个请求复用，不再签（再签会吊销它）
    assert client.calls == [
        ("refresh", "r1"),
        ("remember", {"api_key": "mgw_new", "refresh_token": "r2"}),
    ]


async def test_refresh_rejected_refresh_token_requires_relogin():
    client = FakeClient()
    client.refresh_gateway_key = _raiser(UpstreamHTTPError(
        200, '{"code":403,"message":"refresh token 已过期"}'.encode()))
    with pytest.raises(UpstreamReloginRequired):
        await _provider(client).refresh({"jwt": JWT, "refresh_token": "r1"})


async def test_refresh_other_failure_propagates():
    client = FakeClient()
    client.refresh_gateway_key = _raiser(UpstreamHTTPError(500, b"down"))
    with pytest.raises(UpstreamHTTPError):
        await _provider(client).refresh({"jwt": JWT, "refresh_token": "r1"})


async def test_refresh_with_jwt_only_issues_key():
    client = FakeClient()
    refreshed = await _provider(client).refresh({"jwt": JWT, "uid": "42"})
    assert refreshed == {"jwt": JWT, "uid": "42", "api_key": "mgw_issued"}
    assert client.calls[0][0] == "issue"


async def test_refresh_without_credentials_requires_relogin():
    with pytest.raises(UpstreamReloginRequired):
        await _provider(FakeClient()).refresh({"organization_code": "o"})


async def test_probe_quota_without_jwt_is_unknown():
    quota = await _provider(FakeClient()).probe_quota({"uid": "42"})
    assert quota.probe_failed is True
    assert quota.probed_at


async def test_probe_quota_delegates():
    expected = Quota(remaining=5, total=5, probed_at=1)
    client = FakeClient(quota=expected)
    assert await _provider(client).probe_quota({"jwt": JWT}) is expected
    assert client.calls[0][1].jwt == JWT


async def test_checkin_branches():
    provider = _provider(FakeClient())
    result = await provider.checkin({})
    assert not result.ok and "jwt" in result.message

    ok = CheckinResult(ok=True, credit=5)
    provider = _provider(FakeClient(checkin=ok))
    assert (await provider.checkin({"jwt": JWT})).credit == 5

    provider = _provider(FakeClient())
    provider.client.checkin = _raiser(UpstreamHTTPError(
        200, '{"code":403,"message":"token无效"}'.encode()))
    result = await provider.checkin({"jwt": JWT})
    assert not result.ok and "重新导入 JWT" in result.message

    provider = _provider(FakeClient())
    provider.client.checkin = _raiser(UpstreamHTTPError(500, b"down"))
    result = await provider.checkin({"jwt": JWT})
    assert not result.ok and "HTTP 500" in result.message

    provider = _provider(FakeClient())
    provider.client.checkin = _raiser(UpstreamProtocolViolation("bad"))
    result = await provider.checkin({"jwt": JWT})
    assert not result.ok and "格式异常" in result.message

    provider = _provider(FakeClient())
    provider.client.checkin = _raiser(OSError("network"))
    result = await provider.checkin({"jwt": JWT})
    assert not result.ok and "格式异常" in result.message


async def test_checkin_status_branches():
    provider = _provider(FakeClient())
    assert await provider.checkin_status({}) == {
        "active": False, "today_checked_in": False}
    provider = _provider(FakeClient(status={"active": True}))
    assert await provider.checkin_status({"jwt": JWT}) == {"active": True}


def test_checkin_scope_uses_uid():
    provider = _provider(FakeClient())
    assert provider.checkin_scope({"jwt": make_jwt({"uid": "9"})}) == "magicrew|9"
    assert provider.checkin_scope({"jwt": JWT, "uid": "42"}) == "magicrew|42"
    assert provider.checkin_scope({"jwt": "not-a-jwt"}) == ""


async def test_list_models_requires_jwt_and_delegates():
    provider = _provider(FakeClient())
    with pytest.raises(UpstreamProtocolViolation):
        await provider.list_models({})
    expected = [Model(id="m")]
    provider = _provider(FakeClient(models=expected))
    assert await provider.list_models({"jwt": JWT}) == expected


async def test_stream_chat_requires_jwt():
    provider = _provider(FakeClient())
    with pytest.raises(UpstreamProtocolViolation):
        [event async for event in provider.stream_chat({}, {}, "m")]


async def test_stream_chat_paces_and_passes_through():
    pacer = FakePacer()
    provider = MagicrewProvider(client=FakeClient(), pacer=pacer)
    events = [event async for event in provider.stream_chat(
        {"jwt": JWT, "uid": "42"}, {"messages": []}, "m")]

    assert [event.content for event in events] == ["x"]
    assert provider.client.calls[0][0] == "stream"
    assert pacer.calls == [("wait", "42"), ("release", "42")]


async def test_stream_chat_without_uid_uses_channel_bucket():
    pacer = FakePacer()
    provider = MagicrewProvider(client=FakeClient(), pacer=pacer)
    [event async for event in provider.stream_chat({"jwt": "bad"}, {}, "m")]
    assert pacer.calls == [("wait", "magicrew"), ("release", "magicrew")]


async def test_aclose_closes_client():
    client = FakeClient()
    await _provider(client).aclose()
    assert client.closed is True
