"""凭证：容错读取、JWT uid 解析、落库形态与预刷新判据。"""

from __future__ import annotations

import time

import pytest

from src.provider.base import UpstreamProtocolViolation
from src.provider.magicrew.credential import (
    MagicrewCredential,
    epoch_seconds,
    uid_from_jwt,
)
from tests.provider.magicrew.conftest import JWT, b64url, make_jwt


def test_from_dict_non_object_is_empty():
    credential = MagicrewCredential.from_dict(["not", "a", "dict"])
    assert credential.jwt == ""
    assert credential.needs_refresh(0)


def test_from_dict_reads_snake_and_camel_keys():
    credential = MagicrewCredential.from_dict({
        "jwt": "j1",
        "organizationCode": "org",
        "apiKey": "mgw_x",
        "apiKeyExpiresAt": "1700000000",
        "refreshToken": "r1",
        "refreshTokenExpiresAt": 1700000001.9,
        "userId": 77,
        "nickName": " nick ",
    })
    assert credential == MagicrewCredential(
        jwt="j1", organization_code="org", api_key="mgw_x",
        api_key_expires_at=1700000000, refresh_token="r1",
        refresh_token_expires_at=1700000001, uid="77", nickname="nick")


def test_from_dict_falls_back_to_jwt_uid():
    credential = MagicrewCredential.from_dict({"token": make_jwt({"sub": "s-9"})})
    assert credential.uid == "s-9"


def test_epoch_seconds_tolerates_dirty_values():
    assert epoch_seconds(None) == 0
    assert epoch_seconds(True) == 0
    assert epoch_seconds(12) == 12
    assert epoch_seconds(12.9) == 12
    assert epoch_seconds("1700000000") == 1700000000
    assert epoch_seconds(" 12.5 ") == 12
    assert epoch_seconds("soon") == 0
    assert epoch_seconds(["x"]) == 0


def test_uid_from_jwt_claims_order_and_failures():
    assert uid_from_jwt(make_jwt({"uid": "u1", "sub": "s"})) == "u1"
    assert uid_from_jwt(make_jwt({"user_id": "u2"})) == "u2"
    assert uid_from_jwt(make_jwt({"userId": "u3"})) == "u3"
    assert uid_from_jwt(make_jwt({"sub": "u4"})) == "u4"
    assert uid_from_jwt("no-dots") == ""
    assert uid_from_jwt("a.!!!.c") == ""
    assert uid_from_jwt("a.a.c") == ""
    assert uid_from_jwt("a.中.c") == ""
    assert uid_from_jwt("a." + b64url(b"not json") + ".c") == ""
    assert uid_from_jwt("a." + b64url(b'["x"]') + ".c") == ""
    assert uid_from_jwt(make_jwt({"uid": 1})) == ""
    assert uid_from_jwt(make_jwt({})) == ""


def test_from_import_accepts_bare_jwt_and_aliases():
    assert MagicrewCredential.from_import(JWT).jwt == JWT
    assert MagicrewCredential.from_import({"token": "t"}).jwt == "t"
    assert MagicrewCredential.from_import({"access_token": "a"}).jwt == "a"


@pytest.mark.parametrize("raw", [{"uid": "1"}, ["x"], 3, None])
def test_from_import_rejects_missing_jwt(raw):
    with pytest.raises(UpstreamProtocolViolation) as error:
        MagicrewCredential.from_import(raw)
    assert "JWT" in str(error.value)


def test_to_dict_omits_empty_fields():
    empty = MagicrewCredential(jwt="j")
    assert empty.to_dict() == {"jwt": "j"}
    full = MagicrewCredential(
        jwt="j", organization_code="o", api_key="k",
        api_key_expires_at=11, refresh_token="r",
        refresh_token_expires_at=22, uid="u", nickname="n")
    assert full.to_dict() == {
        "jwt": "j", "organization_code": "o", "api_key": "k",
        "api_key_expires_at": 11, "refresh_token": "r",
        "refresh_token_expires_at": 22, "uid": "u", "nickname": "n",
    }


def test_needs_refresh_matrix():
    now = 1_000
    # 没有 key：随时可签
    assert MagicrewCredential(jwt="j").needs_refresh(60, now)
    # 到期未知：不猜 TTL
    assert not MagicrewCredential(jwt="j", api_key="k").needs_refresh(60, now)
    # 已进入 skew 窗口
    assert MagicrewCredential(
        jwt="j", api_key="k", api_key_expires_at=1_030).needs_refresh(60, now)
    # 尚未进入窗口
    assert not MagicrewCredential(
        jwt="j", api_key="k", api_key_expires_at=2_000).needs_refresh(60, now)
    # now 缺省取当前时间
    credential = MagicrewCredential(
        jwt="j", api_key="k", api_key_expires_at=int(time.time()) + 5)
    assert credential.needs_refresh(60)


def test_needs_refresh_caps_skew_at_key_lifetime_scale():
    """默认预刷新窗口是 24h，而网关 key 只活 2h：不封顶会把「刚签的 key」
    每轮都重签一次，把在途请求的 key 吊销掉。"""
    now = 1_000
    credential = MagicrewCredential(
        jwt="j", api_key="k", api_key_expires_at=now + 3600)
    # 24h 的 skew 被压到 30min：距离到期还有 1h 时不刷新
    assert not credential.needs_refresh(24 * 3600, now)
    # 进入 30min 窗口后才刷新
    assert credential.needs_refresh(24 * 3600, now + 3600 - 1800)
