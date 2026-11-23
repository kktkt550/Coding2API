"""超级麦吉接线：Settings 字段、端点白名单、热更节流、ToS 与注册表。"""

from __future__ import annotations

from src.config import validate_magicrew_endpoint_allowed
from src.engine.model_resolver import KNOWN_PROVIDERS
from src.provider.tos import tos_for
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.helpers.auth import settings_with as _settings
from tests.helpers.stores import MemoryStore as _MemoryStore


def test_magicrew_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.magicrew_api_endpoint == "https://i-magic-service.letsmagic.cn"
    assert settings.magicrew_allowed == ("https://i-magic-service.letsmagic.cn",)
    assert settings.magicrew_web_origin == "https://www.letsmagic.cn"
    assert "Chrome/141.0.7390.55" in settings.magicrew_user_agent
    blank = _settings(magicrew_allowed_endpoints=" https://a.example.com/ , , ")
    assert blank.magicrew_allowed == ("https://a.example.com",)


def test_magicrew_endpoint_whitelist():
    settings = _settings()
    assert validate_magicrew_endpoint_allowed(
        "https://i-magic-service.letsmagic.cn", settings)
    # 结尾斜杠按同一端点处理（_resolve_endpoint 会 strip）
    assert validate_magicrew_endpoint_allowed(
        "https://i-magic-service.letsmagic.cn/", settings)
    assert not validate_magicrew_endpoint_allowed(
        "https://evil.example.com", settings)


def test_magicrew_chat_min_interval_is_hot():
    assert "magicrew_chat_min_interval" in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    assert runtime.magicrew_chat_min_interval == 0
    runtime.set("magicrew_chat_min_interval", 2)
    assert runtime.magicrew_chat_min_interval == 2


def test_magicrew_registered_with_red_tos():
    assert "magicrew" in KNOWN_PROVIDERS
    # 条款 3.2/3.3 明文禁止脚本直调与账号池：评级必须是 red，不能粉饰成灰区
    risk = tos_for("magicrew")
    assert risk.level == "red"
    assert "letsmagic.cn/terms" in risk.basis