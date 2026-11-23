"""审计查看端点（admin only）。

只读为主：写入散落在各业务端点（登录、账号变动、凭证写操作），不在这里做。
另提供唯一的写入口 `DELETE /api/audit`（真清空，admin + CSRF + 留痕）。
响应形状与 /api/stats/events 的分页约定一致（limit/offset）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..audit.actions import ACTION_AUDIT_CLEAR, ACTION_LABELS, ACTIONS
from ..auth.rbac import require_admin
from .deps import Services, csrf_protected, principal_from_request, request_ip


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/audit")
    async def list_audit(actor: str = "", action: str = "", since: int = 0,
                         before: int = 0, limit: int = 100, offset: int = 0,
                         sort: str | None = None, order: str | None = None,
                         principal=Depends(principal_from_request)):
        require_admin(principal)
        rows = services.audit.query(
            actor=actor or None, action=action or None,
            since=since or None, before=before or None,
            limit=limit, offset=offset, sort=sort, order=order)
        # total 与 events 同一套过滤条件（见 AuditRepository.count 的说明）
        total = services.audit.count(
            actor=actor or None, action=action or None,
            since=since or None, before=before or None)
        return {"events": rows, "total": total,
                "actions": list(ACTIONS), "labels": ACTION_LABELS}

    @router.delete("/api/audit")
    async def clear_audit(request: Request,
                          _csrf: None = Depends(csrf_protected),
                          principal=Depends(principal_from_request)):
        """清空审计流水（admin + CSRF）：真删除、不可恢复。

        清空后**立即**写一条 `audit.clear`：表里至少留下「谁在什么时候清的」，
        之后的所有写操作照常留痕。历史审计是追责证据，入口只给 admin。
        """
        require_admin(principal)
        cleared = services.audit.clear()
        services.audit.record(
            actor=principal.username, action=ACTION_AUDIT_CLEAR,
            detail=f"已清空 {cleared} 条历史记录",
            ip=request_ip(request, services.settings))
        return {"ok": True, "cleared": cleared}

    return router
