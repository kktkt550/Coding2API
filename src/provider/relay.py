"""代理池（Cloudflare Relay 中转）。

与 Bun-CF 版同构的移植：条目存 `runtime_settings` 单行 JSON（key 固定，
改名 = 数据丢失），渠道绑定同样单行 JSON；resolver 是同步、纯内存、O(1)、
永不抛错的 `credential_id → provider → proxy_id → enabled url` 链，任一环
缺失回退直连。

发送层集成（`proxy.py` / `lazy_clients.py`）：命中 relay 的请求把 URL 改写为
relay 地址并携带 `x-relay-target` / `x-relay-path` 两个头（由部署的 Relay
Worker 还原转发）；relay 与 httpx 代理（`PROVIDER_PROXIES` / 出口池）互斥。

持久化经 `RuntimeSettingsRepository` 直读直写——刻意绕开 `RuntimeSettings`
热更白名单：这两个 key 是结构化 JSON，不是标量配置，不该混进热更校验。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import re
import time
import uuid
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..compat.openai.request import InvalidRequest
from ..db.repo import RuntimeSettingsRepository
from ..engine.model_resolver import KNOWN_PROVIDERS

logger = logging.getLogger(__name__)

# 持久化 key：runtime_settings 单行 JSON，不得改名（改名 = 数据丢失）。
RELAY_POOL_KEY = "relay_proxy_pool"
RELAY_BINDINGS_KEY = "relay_provider_bindings"

# 与 engine.model_resolver.KNOWN_PROVIDERS 同源（不含 generic 通用上游——
# 通用端点用户自带出口，不走 relay）；未知 provider 写入 400、读取忽略。
RELAY_PROVIDERS: tuple[str, ...] = tuple(
    provider for provider in KNOWN_PROVIDERS if provider != "generic")

# 空串 = auto；顺序即前端下拉顺序。
PLACEMENT_REGIONS: tuple[str, ...] = (
    "", "wnam", "enam", "sam", "weur", "eeur", "me", "afr", "apac", "oce",
)
PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RELAY_TRACE_TARGET = "https://www.cloudflare.com"
RELAY_TRACE_PATH = "/cdn-cgi/trace"
RELAY_TEST_TIMEOUT_SECONDS = 10.0
RELAY_DEPLOY_TIMEOUT_SECONDS = 60.0

# ------------------------------------------------------------ URL 归一化

# 本地域名黑名单：localhost / *.localhost / *.local / *.internal / *.home.arpa。
_LOCAL_DOMAIN_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_LOCAL_DOMAIN_NAMES = ("localhost", "local", "internal", "home.arpa")


def _is_local_domain(hostname: str) -> bool:
    name = hostname.lower().rstrip(".")
    return name in _LOCAL_DOMAIN_NAMES or name.endswith(_LOCAL_DOMAIN_SUFFIXES)


def normalize_relay_url(raw: Any) -> str:
    """URL 归一化 + SSRF 校验。合法返回 `origin + path`（去尾斜杠），否则 `""`。

    七步口径与 Bun-CF 版一致：仅 https、拒 userinfo、hostname 非空且小写、
    本地域名黑名单、字面 IP 命中内网/回环/保留段即拒。**不做 DNS 解析**：
    写路径不引入外部网络依赖（解析期 DNS rebinding 由 Worker 侧域名兜底）。
    """
    if not isinstance(raw, str):
        return ""
    trimmed = raw.strip().rstrip("/")
    if not trimmed:
        return ""
    parts = urlsplit(trimmed)
    if parts.scheme.lower() != "https":
        return ""
    # 拒绝 userinfo（URL 即凭据，不能夹带用户名密码）
    if parts.username or parts.password:
        return ""
    hostname = parts.hostname or ""
    if not hostname:
        return ""
    if _is_local_domain(hostname):
        return ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified):
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"https://{hostname.lower()}{port}{parts.path.rstrip('/')}"


def normalize_placement_region(raw: Any) -> str:
    """地区 hint：trim + lowercase；短 hint（≤12 字符的合法形态）原样，其余 auto。"""
    trimmed = raw.strip().lower() if isinstance(raw, str) else ""
    if not trimmed:
        return ""
    return trimmed if re.fullmatch(r"[a-z][a-z0-9_-]{1,11}", trimmed) else ""


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_relay_entry(partial: dict[str, Any],
                          previous: dict[str, Any] | None = None,
                          credentials: dict[str, Any] | None = None,
                          ) -> dict[str, Any] | None:
    """条目归一化。`cf_account_id` / `cf_api_token_enc` 只接受服务端内部传入
    （`credentials` 参数，来自 DB 读取或部署组装）：请求体里的同名字段一律忽略，
    防止客户端注入密文。
    """
    entry_id = str(partial.get("id") or "").strip()
    name = str(partial.get("name") or "").strip()
    url = normalize_relay_url(partial.get("url"))
    if not entry_id or not name or not url:
        return None
    now = _iso_now()
    stored = credentials or {}
    entry: dict[str, Any] = {
        "id": entry_id,
        "name": name,
        "type": "relay",
        "url": url,
        "enabled": partial.get("enabled") is not False,
        "created_at": stored.get("created_at")
        or (previous or {}).get("created_at") or now,
        "updated_at": stored.get("updated_at") or now,
    }
    # 请求体显式给 region 就用它，否则沿用 previous（编辑不丢地区）。
    if "region" in partial:
        entry["region"] = normalize_placement_region(partial.get("region"))
    elif (previous or {}).get("region"):
        entry["region"] = normalize_placement_region(previous["region"])
    else:
        entry["region"] = ""
    account_id = str(stored.get("cf_account_id") or "").strip()
    if account_id:
        entry["cf_account_id"] = account_id
    token_enc = str(stored.get("cf_api_token_enc") or "").strip()
    if token_enc:
        entry["cf_api_token_enc"] = token_enc
    return entry


def parse_pool_json(raw: str | None) -> list[dict[str, Any]]:
    """整池 JSON 容错：坏 JSON / 非数组 → []；逐条归一化，坏元素跳过，
    按 id 去重、按 created_at 排序。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        entry = normalize_relay_entry(item, None, {
            "cf_account_id": item.get("cf_account_id")
            if isinstance(item.get("cf_account_id"), str) else None,
            "cf_api_token_enc": item.get("cf_api_token_enc")
            if isinstance(item.get("cf_api_token_enc"), str) else None,
            "created_at": item.get("created_at")
            if isinstance(item.get("created_at"), str) else None,
            "updated_at": item.get("updated_at")
            if isinstance(item.get("updated_at"), str) else None,
        })
        if entry is None or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        entries.append(entry)
    entries.sort(key=lambda item: (item["created_at"], item["id"]))
    return entries


def parse_bindings_json(raw: str | None) -> dict[str, str]:
    """绑定 JSON 容错：坏 JSON / 非对象 → {}；只留白名单渠道、值为非空字符串。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    allowed = set(RELAY_PROVIDERS)
    result: dict[str, str] = {}
    for provider, proxy_id in parsed.items():
        if provider not in allowed or not isinstance(proxy_id, str) or not proxy_id:
            continue  # 空串 = 直连，不入映射
        result[provider] = proxy_id
    return result


def to_view(entry: dict[str, Any]) -> dict[str, Any]:
    """列表响应投影：**显式字段白名单**构造，剥掉 `cf_api_token_enc` /
    `cf_account_id`，只回「是否已记住」布尔。
    """
    return {
        "id": entry["id"],
        "name": entry["name"],
        "type": "relay",
        "url": entry["url"],
        "enabled": entry["enabled"],
        "region": entry.get("region") or "",
        "credentials_saved": bool(entry.get("cf_api_token_enc")),
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
    }


# ------------------------------------------------------------ 凭据加解密

def encrypt_secret(app_secret: str, plaintext: str) -> str:
    """AES-256-GCM：`base64(nonce).base64(cipher)`。密钥从 APP_SECRET 派生。"""
    key = hashlib.sha256(app_secret.encode("utf-8")).digest()
    nonce = uuid.uuid4().bytes
    cipher = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return (base64.b64encode(nonce).decode("ascii") + "."
            + base64.b64encode(cipher).decode("ascii"))


def decrypt_secret(app_secret: str, token_enc: str) -> str:
    nonce_b64, _, cipher_b64 = token_enc.partition(".")
    if not nonce_b64 or not cipher_b64:
        raise ValueError("malformed secret payload")
    key = hashlib.sha256(app_secret.encode("utf-8")).digest()
    nonce = base64.b64decode(nonce_b64)
    cipher = base64.b64decode(cipher_b64)
    plain = AESGCM(key).decrypt(nonce, cipher, None)
    return plain.decode("utf-8")


# ------------------------------------------------------------ Relay Worker 部署

# 内联 Relay Worker：读两个 x-relay 头、删除 host、还原 Host、流式透传。
RELAY_WORKER_CODE = """export default {
  async fetch(request) {
    const target = request.headers.get("x-relay-target");
    const relayPath = request.headers.get("x-relay-path") || "/";

    if (!target) {
      return new Response(JSON.stringify({ error: "Missing x-relay-target header" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      });
    }

    const targetUrl = target.replace(/\\/$/, "") + relayPath;
    const init = {
      method: request.method,
      headers: new Headers(request.headers),
    };

    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
      init.duplex = "half";
    }

    init.headers.delete("x-relay-target");
    init.headers.delete("x-relay-path");
    init.headers.delete("host");

    try {
      const response = await fetch(targetUrl, init);
      return new Response(response.body, {
        status: response.status,
        headers: response.headers,
      });
    } catch (error) {
      return new Response(JSON.stringify({ error: "relay upstream failed" }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  },
};
"""


def build_script_metadata(region: str) -> dict[str, Any]:
    """Cloudflare multipart 的 metadata 部分：地区 hint 决定 placements。"""
    metadata: dict[str, Any] = {
        "main_module": "index.js",
        "compatibility_date": "2024-03-20",
        "observability": {"enabled": True},
    }
    placement = normalize_placement_region(region)
    if placement:
        metadata["placements"] = [{"hint": placement}]
    return metadata


# 部署失败归因分类：四类，全部映射为可读中文，绝不回显原始 body / Token。
DEPLOY_ERROR_MESSAGES: dict[str, str] = {
    "credentials": ("Cloudflare 凭据无效或权限不足，"
                    "请确认 API Token 具备 Workers Scripts: Edit 权限"),
    "network": "Cloudflare 接口暂时不可用或网络异常，请稍后重试",
    "name_taken": "该 Worker 名称已被占用，请换一个名称",
    "subdomain": "Worker 已部署但无法获取 workers.dev 子域，请在 Cloudflare 控制台配置子域后重试",
}


# 凭据/权限类失败的关键词（CF code 4xx / token / permission 等）。
_CREDENTIAL_ERROR_PATTERN = re.compile(
    r"(^|\D)4\d\d(\D|$)|token|authentication|unauthori[sz]ed|permission"
    r"|forbidden|invalid request", re.I)


class RelayDeployError(Exception):
    """部署失败载体：只带分类与安全的短原因，绝不携带 Token / 原始响应。"""

    def __init__(self, kind: str, safe_reason: str = "") -> None:
        super().__init__(safe_reason or DEPLOY_ERROR_MESSAGES.get(kind, kind))
        self.kind = kind


def classify_deploy_error(error: BaseException) -> str:
    """把任意部署异常归类为可读中文（管理 API 直接回给前端）。"""
    if isinstance(error, RelayDeployError):
        return DEPLOY_ERROR_MESSAGES[error.kind]
    raw = str(error)
    if re.search(r"10037|already exists|name.*taken", raw, re.I):
        return DEPLOY_ERROR_MESSAGES["name_taken"]
    if _CREDENTIAL_ERROR_PATTERN.search(raw):
        return DEPLOY_ERROR_MESSAGES["credentials"]
    return DEPLOY_ERROR_MESSAGES["network"]


def _cf_api_error(response: httpx.Response, fallback: str) -> str:
    """CF 错误提取：`errors[0].message`；json 解析失败兜底。"""
    try:
        payload = response.json()
        return (payload.get("errors") or [{}])[0].get("message") or fallback
    except Exception:  # noqa: BLE001 - 非 JSON 错误体，用 fallback
        return fallback


def _classify_upload_failure(status: int, detail: str) -> str:
    if re.search(r"10037|already exists|name.*taken", detail, re.I):
        return "name_taken"
    if status >= 500:
        return "network"
    if status >= 400:
        return "credentials"
    return "network"


def upload_relay_script(account_id: str, api_token: str,
                        project_name: str, region: str) -> None:
    """上传脚本：`PUT /accounts/{id}/workers/scripts/{name}`，multipart。"""
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/workers/scripts/{project_name}")
    files = {
        "index.js": ("index.js", RELAY_WORKER_CODE, "application/javascript+module"),
        "metadata": ("metadata.json",
                     json.dumps(build_script_metadata(region)),
                     "application/json"),
    }
    try:
        response = httpx.put(url, headers={"Authorization": f"Bearer {api_token}"},
                             files=files, timeout=RELAY_DEPLOY_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        raise RelayDeployError("network", "upload network failure") from None
    if not response.ok:
        detail = _cf_api_error(response, "Failed to upload Worker to Cloudflare")
        raise RelayDeployError(_classify_upload_failure(response.status_code, detail),
                               "upload rejected")


def get_workers_dev_url(account_id: str, api_token: str, project_name: str) -> str:
    """取 workers.dev URL：先 best-effort 打开 subdomain，再取账户 subdomain。"""
    base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
    auth = {"Authorization": f"Bearer {api_token}"}
    # best-effort：已开启时该调用可能报错，忽略即可。
    with contextlib.suppress(httpx.HTTPError):
        httpx.post(f"{base}/workers/scripts/{project_name}/subdomain",
                   headers={**auth, "Content-Type": "application/json"},
                   json={"enabled": True}, timeout=RELAY_DEPLOY_TIMEOUT_SECONDS)
    try:
        response = httpx.get(f"{base}/workers/subdomain", headers=auth,
                             timeout=RELAY_DEPLOY_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        raise RelayDeployError("subdomain", "subdomain network failure") from None
    if not response.ok:
        raise RelayDeployError("subdomain", f"subdomain lookup {response.status_code}")
    try:
        subdomain = str((response.json().get("result") or {}).get("subdomain") or "")
    except Exception:  # noqa: BLE001 - 非 JSON 响应按空处理
        subdomain = ""
    if not subdomain:
        raise RelayDeployError("subdomain", "empty subdomain")
    return f"https://{project_name}.{subdomain}.workers.dev"


def deploy_relay_worker(account_id: str, api_token: str,
                        project_name: str, region: str) -> str:
    """部署（或带新地区重新上传）：成功返回 `https://{name}.{subdomain}.workers.dev`。"""
    upload_relay_script(account_id, api_token, project_name, region)
    return get_workers_dev_url(account_id, api_token, project_name)


# ------------------------------------------------------------ 池快照 + resolver

# 惰性补全条目的存活期与自愈上限。
CREDENTIAL_TTL_SECONDS = 60.0
CREDENTIAL_EXTRA_MAX = 10_000


class RelayPoolStore:
    """代理池内存快照 + 写后失效 + 同步 resolver。

    稳定态 **0 次 SQLite / 请求**：启动 `refresh()` 全量装载凭证→渠道映射；
    新凭证首次 miss 才惰性 `provider_of` 一次（同步 SQLite 单行查询，毫秒级）；
    `resolve()` 纯内存 O(1)、永不抛错。快照 copy-on-write 整体替换，resolver
    永远读到某一刻的完整快照，无半更新。
    """

    def __init__(self, settings_repo: RuntimeSettingsRepository, credentials: Any) -> None:
        self._repo = settings_repo
        self._credentials = credentials
        self._entries: list[dict[str, Any]] = []
        self._enabled_urls: dict[str, str] = {}
        self._bindings: dict[str, str] = {}
        self._credential_providers: dict[str, str] = {}
        # 惰性补全的增量层：credential_id → (provider, 装载时刻 monotonic)。
        self._credential_extra: dict[str, tuple[str, float]] = {}
        # 绑定指向停用/已删除的去重告警，避免刷屏。
        self._warned_missing: set[str] = set()

    def refresh(self) -> None:
        """读 2 个 JSON key + 全量凭证映射，构建完整新快照后一次赋值。"""
        raw = self._repo.load()
        entries = parse_pool_json(raw.get(RELAY_POOL_KEY))
        bindings = parse_bindings_json(raw.get(RELAY_BINDINGS_KEY))
        enabled_urls = {entry["id"]: entry["url"]
                        for entry in entries if entry["enabled"]}
        binding_map = {provider: proxy_id
                       for provider, proxy_id in bindings.items() if proxy_id}
        credential_providers = {pair["id"]: pair["provider"]
                                for pair in self._credentials.all_provider_pairs()
                                if pair["id"] and pair["provider"]}
        self._entries = entries
        self._enabled_urls = enabled_urls
        self._bindings = binding_map
        self._credential_providers = credential_providers

    def resolve(self, credential_id: str | None) -> str | None:
        """同步、纯内存、O(1)、永不抛错。返回启用 relay 的 url 或 None（直连）。"""
        try:
            if not credential_id:
                return None
            provider = self._credential_providers.get(credential_id)
            if provider is None:
                extra = self._credential_extra.get(credential_id)
                if extra is not None:
                    provider, loaded_at = extra
                    if time.monotonic() - loaded_at > CREDENTIAL_TTL_SECONDS:
                        # TTL 过期仍先用旧值，本次不阻塞；下次再自愈。
                        self._reload_extra(credential_id)
                else:
                    # 首次 miss：同步补全一次（SQLite 单行查询），本次即可用。
                    self._reload_extra(credential_id)
                    provider = self._credential_extra.get(credential_id, ("", 0.0))[0]
            if not provider:
                return None
            proxy_id = self._bindings.get(provider)
            if not proxy_id:
                return None  # 该渠道未绑定
            url = self._enabled_urls.get(proxy_id)
            if not url:
                self._warn_missing(proxy_id)
                return None  # 条目停用 / 已删除
            return url
        except Exception:  # noqa: BLE001 - 配置异常绝不抛给业务请求
            logger.warning("relay resolve failed, fallback to direct", exc_info=True)
            return None

    def _reload_extra(self, credential_id: str) -> None:
        """miss/TTL 过期时查一次 provider 并缓存；DB 异常缓存空值等 TTL 自愈。"""
        if len(self._credential_extra) >= CREDENTIAL_EXTRA_MAX:
            self._credential_extra.clear()
        try:
            provider = self._credentials.provider_of(credential_id) or ""
        except Exception:  # noqa: BLE001 - 见 docstring
            provider = ""
        self._credential_extra[credential_id] = (provider, time.monotonic())

    def _warn_missing(self, proxy_id: str) -> None:
        if proxy_id in self._warned_missing:
            return
        self._warned_missing.add(proxy_id)
        # 只记 proxy_id（非敏感），不记 url / 凭据。
        logger.warning("relay binding points to disabled/missing entry, "
                       "fallback to direct: %s", proxy_id)

    def list_entries(self) -> list[dict[str, Any]]:
        """全量条目（含停用），按 created_at 稳定排序。"""
        return list(self._entries)

    def get_bindings(self) -> dict[str, str]:
        return dict(self._bindings)

    def write_pool(self, entries: list[dict[str, Any]]) -> None:
        """整池一次 UPSERT + 写后失效（下一个请求即生效）。"""
        self._repo.set(RELAY_POOL_KEY, json.dumps(entries, ensure_ascii=False))
        self.refresh()

    def write_bindings(self, bindings: dict[str, str]) -> None:
        """绑定一次 UPSERT + 写后失效（空串/缺键 = 解绑）。"""
        self._repo.set(RELAY_BINDINGS_KEY, json.dumps(bindings, ensure_ascii=False))
        self.refresh()

    def invalidate_credentials(self) -> None:
        """凭证写点调用：立刻重建映射（当前口径靠 TTL 自愈，保留能力）。"""
        self._credential_extra.clear()
        self.refresh()


# ------------------------------------------------------------ 发送层集成

# 发送层 resolver：main 装配后注入；未装配时行为与直连完全一致。
_RELAY_RESOLVER: Any = None
_RELAY_WARNED = False


def set_relay_resolver(fn: Any) -> None:
    """由 build_app 装配；传 None 恢复「无 relay」行为（测试隔离用）。"""
    global _RELAY_RESOLVER, _RELAY_WARNED
    _RELAY_RESOLVER = fn
    if fn is None:
        _RELAY_WARNED = False


def relay_for_credential(credential_id: str | None) -> str | None:
    """凭证 → relay URL；未装配 / 未绑定 / 异常一律 None（回退直连）。"""
    global _RELAY_WARNED
    if _RELAY_RESOLVER is None:
        return None
    try:
        return _RELAY_RESOLVER(credential_id) or None
    except Exception:  # noqa: BLE001 - resolver 异常吞成 None + warn once
        if not _RELAY_WARNED:
            _RELAY_WARNED = True
            logger.warning("relay resolver failed, fallback to direct", exc_info=True)
        return None


def apply_relay(request: Any, relay_url: str) -> None:
    """把请求改写为经 relay Worker 转发：URL 指向 relay + 两个路由头。

    `x-relay-target` 携带上游 origin、`x-relay-path` 携带上游 path+query，
    Worker 据此还原转发。Host 头指向 relay（Worker 会删除入站 host 并按
    target 重设——HTTP/1.1 请求必须带 Host，删空会被边缘直接拒掉）。
    """
    url = request.url
    path = url.path or "/"
    query = url.query.decode("latin-1") if url.query else ""
    relay_base = relay_url.rstrip("/")
    target_origin = f"{url.scheme}://{url.host}"
    if url.port and not ((url.scheme == "https" and url.port == 443)
                         or (url.scheme == "http" and url.port == 80)):
        target_origin += f":{url.port}"
    relay_parsed = httpx.URL(relay_base)
    relay_host = relay_parsed.host or ""
    if relay_parsed.port and not (
            relay_parsed.scheme == "https" and relay_parsed.port == 443):
        relay_host += f":{relay_parsed.port}"
    request.url = httpx.URL(relay_base + path + (f"?{query}" if query else ""))
    request.headers["host"] = relay_host
    request.headers["x-relay-target"] = target_origin
    request.headers["x-relay-path"] = path + (f"?{query}" if query else "")


def test_relay_entry(relay_url: str) -> dict[str, Any]:
    """连通性测试：10s 超时、硬编码 trace 目标、标准 GET 直连（不走 resolver、
    不经绑定）。失败不自动停用条目；错误信息只回摘要，不含完整 relay URL。"""
    started = time.monotonic()
    try:
        response = httpx.get(
            relay_url.rstrip("/") + RELAY_TRACE_PATH,
            headers={"x-relay-target": RELAY_TRACE_TARGET,
                     "x-relay-path": RELAY_TRACE_PATH},
            timeout=RELAY_TEST_TIMEOUT_SECONDS)
        return {"ok": response.is_success, "status": response.status_code,
                "elapsedMs": int((time.monotonic() - started) * 1000)}
    except httpx.TimeoutException:
        return {"ok": False, "status": 0,
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "error": "连接超时（10 秒）"}
    except httpx.HTTPError:
        return {"ok": False, "status": 0,
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "error": "连接失败，请检查 relay 地址与网络"}


# ------------------------------------------------------------- 校验辅助

def validate_upsert_body(body: dict[str, Any]) -> tuple[str, str]:
    """新增/编辑请求体的公共校验：返回 (name, url)；非法抛 400。"""
    name = str(body.get("name") or "").strip()
    raw_url = str(body.get("url") or "").strip()
    if not name or not raw_url:
        raise InvalidRequest("代理名称与合法的 https relay 地址不能为空")
    if not normalize_relay_url(raw_url):
        raise InvalidRequest(
            "relay 地址必须是公网 https 地址，且不能包含用户名密码或内网地址")
    return name, raw_url


def normalize_entry_list(entries: Iterable[dict[str, Any]],
                         drop_id: str | None = None) -> list[dict[str, Any]]:
    """按 id 去重的池列表（写路径的内存组装步骤）。"""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        if entry["id"] in seen or entry["id"] == drop_id:
            continue
        seen.add(entry["id"])
        result.append(entry)
    return result
