"""代理池（Cloudflare Relay 中转）管理端点。

照 admin_credentials 的闭包注入模式：权限 / CSRF / 审计 / 脱敏全部在此收口。
成功响应为裸 JSON（与既有 /api/* 一致），校验失败抛 `InvalidRequest` 由全局
handler 映射 400，404 / 502 直接返回 `{error:{message,type}}`。

写路径固定「事务外 load → 内存组装 → 一次 set → refresh」；每个写成功后
`store.refresh()` 已在 write_* 内完成，「下一个请求即生效」。审计失败不阻断
主写（与 relay 池的数据完整性无关）。

权限口径（与 Bun-CF 版一致）：
- 列表 / 绑定读：operator 可读（relay URL 即凭据，viewer 不可读列表）；
- 增删改 / 启停 / 测试 / 部署 / 切地区：admin；
- 绑定写：operator（渠道分组归属的日常运维动作）。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..audit.actions import (
    ACTION_RELAY_BINDING,
    ACTION_RELAY_DELETE,
    ACTION_RELAY_DEPLOY,
    ACTION_RELAY_REGION,
    ACTION_RELAY_TOGGLE,
    ACTION_RELAY_UPSERT,
)
from ..auth.rbac import require_admin, require_operator
from ..compat.openai.request import InvalidRequest
from ..provider.relay import (
    PROJECT_NAME_PATTERN,
    RELAY_PROVIDERS,
    RelayDeployError,
    classify_deploy_error,
    decrypt_secret,
    deploy_relay_worker,
    encrypt_secret,
    normalize_placement_region,
    normalize_relay_entry,
    normalize_relay_url,
    test_relay_entry,
    to_view,
    validate_upsert_body,
)
from .deps import Services, csrf_protected, principal_from_request

logger = logging.getLogger(__name__)


def _api_error(status: int, message: str, type_: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": type_}})


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    relay_store = services.relay
    if relay_store is None:  # pragma: no cover - 装配护栏（测试替身不挂该路由）
        raise RuntimeError("relay store not wired")

    def _audit(principal, action: str, target: str, detail: str) -> None:
        try:
            services.audit.record(actor=principal.username, action=action,
                                  target=target, detail=detail)
        except Exception:  # noqa: BLE001 - 审计不阻断主写
            logger.warning("relay audit record failed", exc_info=True)

    # #1 列表（operator 可读，含 url；viewer 无权限）
    @router.get("/api/proxy-pool")
    async def list_proxy_pool(principal=Depends(principal_from_request)):
        require_operator(principal)
        return {"items": [to_view(entry) for entry in relay_store.list_entries()]}

    # #2 新增 / 编辑（admin + CSRF，upsert by id）
    @router.post("/api/proxy-pool/{proxy_id}")
    async def upsert_proxy_pool(proxy_id: str, payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_admin(principal)
        proxy_id = _text(proxy_id)
        name, raw_url = validate_upsert_body(payload)
        entries = relay_store.list_entries()
        previous = next((entry for entry in entries if entry["id"] == proxy_id), None)
        # 请求体里的 cf_account_id / cf_api_token_enc 一律忽略：只从 previous 继承。
        entry = normalize_relay_entry(
            {"id": proxy_id, "name": name, "url": raw_url,
             "enabled": payload.get("enabled"),
             "region": payload.get("region")},
            previous)
        if entry is None:
            raise InvalidRequest("代理名称与合法的 https relay 地址不能为空")
        next_entries = [item for item in entries if item["id"] != proxy_id]
        next_entries.append(entry)
        relay_store.write_pool(next_entries)
        _audit(principal, ACTION_RELAY_UPSERT, proxy_id,
               "编辑" if previous else "新增")
        return {"ok": True, "item": to_view(entry)}

    # #3 删除（admin + CSRF，条目不存在也返回 ok，幂等）
    @router.delete("/api/proxy-pool/{proxy_id}")
    async def delete_proxy_pool(proxy_id: str,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_admin(principal)
        proxy_id = _text(proxy_id)
        next_entries = [entry for entry in relay_store.list_entries()
                        if entry["id"] != proxy_id]
        relay_store.write_pool(next_entries)
        _audit(principal, ACTION_RELAY_DELETE, proxy_id, "删除")
        return {"ok": True}

    # #4 启停（admin + CSRF）
    @router.post("/api/proxy-pool/{proxy_id}/toggle")
    async def toggle_proxy_pool(proxy_id: str, payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_admin(principal)
        proxy_id = _text(proxy_id)
        enabled = payload.get("enabled")
        # 类型必须严格是布尔：`bool("false") is True`，收到字符串会把「停用」
        # 静默执行成「启用」（写操作语义反转）。
        if not isinstance(enabled, bool):
            raise InvalidRequest("启用状态必须是 true 或 false")
        entries = relay_store.list_entries()
        previous = next((entry for entry in entries if entry["id"] == proxy_id), None)
        if previous is None:
            return _api_error(404, "代理条目不存在", "not_found_error")
        updated = {**previous, "enabled": enabled, "updated_at": _now_iso()}
        relay_store.write_pool([item if item["id"] != proxy_id else updated
                                for item in entries])
        _audit(principal, ACTION_RELAY_TOGGLE, proxy_id,
               "启用" if enabled else "停用")
        return {"ok": True, "item": to_view(updated)}

    # #5 连通性测试（admin + CSRF；失败不是 HTTP 错误，回 ok:false）
    @router.post("/api/proxy-pool/{proxy_id}/test")
    async def test_proxy_pool(proxy_id: str,
                              _csrf: None = Depends(csrf_protected),
                              principal=Depends(principal_from_request)):
        require_admin(principal)
        proxy_id = _text(proxy_id)
        entry = next((item for item in relay_store.list_entries()
                      if item["id"] == proxy_id), None)
        if entry is None:
            return _api_error(404, "代理条目不存在", "not_found_error")
        # 读端点不写审计：测试不改变配置。
        return test_relay_entry(entry["url"])

    # #6 部署 Relay（admin + CSRF；失败零写库）
    @router.post("/api/relay/deploy")
    async def deploy_relay(payload: dict,
                           _csrf: None = Depends(csrf_protected),
                           principal=Depends(principal_from_request)):
        require_admin(principal)
        account_id = _text(payload.get("accountId"))
        api_token = _text(payload.get("apiToken"))
        if not account_id or not api_token:
            raise InvalidRequest("请填写 Cloudflare Account ID 与 API Token")
        project_name = _text(payload.get("projectName")) or _default_project_name()
        if not PROJECT_NAME_PATTERN.fullmatch(project_name):
            raise InvalidRequest(
                "Worker 名称只能包含小写字母、数字和连字符，且以字母或数字开头")
        region = normalize_placement_region(payload.get("region"))

        # 部署失败：完全不写库，避免半条脏条目。
        try:
            deploy_url = deploy_relay_worker(account_id, api_token, project_name, region)
        except RelayDeployError as error:
            logger.warning("relay deploy failed: %s", error.kind)
            return _api_error(502, classify_deploy_error(error), "upstream_error")
        normalized_url = normalize_relay_url(deploy_url)
        if not normalized_url:
            logger.warning("relay deploy failed: network (empty url)")
            return _api_error(502, classify_deploy_error(
                RelayDeployError("network")), "upstream_error")

        entries = relay_store.list_entries()
        previous = next((entry for entry in entries if entry["id"] == project_name), None)
        remember = payload.get("rememberCredentials") is True
        entry = normalize_relay_entry(
            {"id": project_name,
             "name": previous["name"] if previous else project_name,
             "url": normalized_url, "enabled": True, "region": region},
            previous,
            {"cf_account_id": account_id if remember
             else (previous or {}).get("cf_account_id"),
             "cf_api_token_enc": encrypt_secret(services.settings.app_secret, api_token)
             if remember else (previous or {}).get("cf_api_token_enc")})
        if entry is None:
            logger.warning("relay deploy failed: network (normalize)")
            return _api_error(502, classify_deploy_error(
                RelayDeployError("network")), "upstream_error")
        relay_store.write_pool([item for item in entries if item["id"] != project_name]
                               + [entry])
        _audit(principal, ACTION_RELAY_DEPLOY, project_name,
               f"部署并切换地区 -> {region}" if region else "部署")
        return {"deployUrl": normalized_url, "region": region,
                "credentialsSaved": remember}

    # #7 切换地区（admin + CSRF；凭据可留空，解密失败给明确中文提示）
    @router.post("/api/relay/{project}/region")
    async def update_relay_region(project: str, payload: dict,
                                  _csrf: None = Depends(csrf_protected),
                                  principal=Depends(principal_from_request)):
        require_admin(principal)
        project = _text(project)
        entries = relay_store.list_entries()
        previous = next((entry for entry in entries if entry["id"] == project), None)
        if previous is None:
            return _api_error(404, "代理条目不存在", "not_found_error")

        account_id = _text(payload.get("accountId"))
        api_token = _text(payload.get("apiToken"))
        if not account_id or not api_token:
            if not previous.get("cf_account_id") or not previous.get("cf_api_token_enc"):
                raise InvalidRequest("该代理未记住凭据，请填写 Cloudflare Account ID 与 API Token")
            try:
                account_id = previous["cf_account_id"]
                api_token = decrypt_secret(services.settings.app_secret,
                                           previous["cf_api_token_enc"])
            except Exception:  # noqa: BLE001 - 密文损坏 / APP_SECRET 变更
                raise InvalidRequest(
                    "记住的凭据已失效（APP_SECRET 可能已变更），"
                    "请重新填写 Cloudflare Account ID 与 API Token") from None
        region = normalize_placement_region(payload.get("region"))
        try:
            # projectName 不变 → URL 不变；只重传脚本换 placements。
            deploy_relay_worker(account_id, api_token, previous["id"], region)
        except RelayDeployError as error:
            logger.warning("relay region failed: %s", error.kind)
            return _api_error(502, classify_deploy_error(error), "upstream_error")

        remember = payload.get("rememberCredentials") is True and bool(api_token)
        updated = normalize_relay_entry(
            {"id": previous["id"], "name": previous["name"],
             "url": previous["url"], "enabled": previous["enabled"],
             "region": region},
            previous,
            {"cf_account_id": account_id if remember
             else previous.get("cf_account_id"),
             "cf_api_token_enc": encrypt_secret(services.settings.app_secret, api_token)
             if remember else previous.get("cf_api_token_enc"),
             # DB 读取路径：保留既有时间戳，避免每次切区重写 created_at。
             "created_at": previous.get("created_at"),
             "updated_at": _now_iso()})
        if updated is None:
            logger.warning("relay region failed: network (normalize)")
            return _api_error(502, classify_deploy_error(
                RelayDeployError("network")), "upstream_error")
        relay_store.write_pool([item if item["id"] != project else updated
                                for item in entries])
        _audit(principal, ACTION_RELAY_REGION, project,
               f"切换地区 -> {region or '自动'}")
        return {"region": region, "credentialsSaved": remember}

    # #8 绑定读（viewer 可读；不含任何 relay URL / 凭据）
    @router.get("/api/relay/bindings")
    async def get_relay_bindings(principal=Depends(principal_from_request)):
        return {"bindings": relay_store.get_bindings()}

    # #9 绑定写（operator + CSRF；空串 = 解绑）
    @router.put("/api/relay/bindings/{provider}")
    async def set_relay_binding(provider: str, payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_operator(principal)
        provider = _text(provider)
        if provider not in RELAY_PROVIDERS:
            raise InvalidRequest(f"未知的渠道分组：{provider}")
        proxy_id = _text(payload.get("proxy_id"))
        if proxy_id and not any(entry["id"] == proxy_id
                                for entry in relay_store.list_entries()):
            raise InvalidRequest("代理条目不存在，请刷新代理池后重试")
        # 指向已停用条目：允许写入（UI 给异常提示、请求回退直连），不静默丢绑定。
        bindings = relay_store.get_bindings()
        if proxy_id:
            bindings[provider] = proxy_id
        else:
            bindings.pop(provider, None)
        relay_store.write_bindings(bindings)
        _audit(principal, ACTION_RELAY_BINDING, provider,
               f"绑定 -> {proxy_id}" if proxy_id else "解绑")
        return {"ok": True, "bindings": relay_store.get_bindings()}

    return router


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while value:
        out = digits[value % 36] + out
        value //= 36
    return out or "0"


def _default_project_name() -> str:
    """默认 Worker 名：`relay-{毫秒 base36}`（与 Bun-CF 版同形）。"""
    return f"relay-{_base36(int(time.time() * 1000))}"
