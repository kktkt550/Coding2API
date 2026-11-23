# 本 fork 新增功能清单（上游同步必读）

> 本仓库（kktkt550/Coding2API）在 [RobbsLuo/Coding2API](https://github.com/RobbsLuo/Coding2API)
> 上游基础上新增了以下功能。**每次同步上游时，以下功能必须完整保留。**
> 若同步出现冲突，以本文档描述的功能语义为准手工解决，禁止用上游版本整文件覆盖下列文件。

---

## 一、功能与涉及文件

### 1. 代理池（Cloudflare Relay 中转池）

| 层 | 文件 | 说明 |
|---|---|---|
| 核心 | `src/provider/relay.py`（新建） | RelayPoolStore、URL 改写（`apply_relay`）、Worker 部署（`deploy_relay_worker`）、密钥加密（AESGCM）、SSRF 防护（`normalize_relay_url`） |
| 发送 | `src/provider/proxy.py` | `build_client` 加 `relay=` 参数；`_EgressGuardedClient.send` 开头改写到 relay |
| 客户端 | `src/provider/lazy_clients.py` | `_effective_relay()`；`_pooled` 缓存键含 relay；relay 与 proxy 互斥 |
| API | `src/api/admin_relay.py`（新建） | 9 个端点：`GET /api/proxy-pool`、增删/启停/测试、`POST /api/relay/deploy`、区域绑定、`GET/PUT /api/relay/bindings` |
| 装配 | `src/main.py` | `RelayPoolStore` 构建 + `set_relay_resolver`；`src/api/deps.py` Services 加 `relay` 字段 |
| 配置 | `src/runtime_settings.py` | 保留键 `relay_proxy_pool` / `relay_provider_bindings`（绕过热更白名单，见 `_RESERVED_SETTINGS_KEYS`） |
| 审计 | `src/audit/actions.py` | `ACTION_RELAY_*`（6 项） |
| 前端 | `web/src/pages/ProxyPoolsPage.tsx`（新建）、`web/src/components/GroupProxySelector.tsx`（新建）、`App.tsx` 路由 `/proxy-pools`（RequireAdmin）、`Sidebar.tsx`「代理池」入口 | |

### 2. Cloudflare Tunnel

| 层 | 文件 | 说明 |
|---|---|---|
| 守护 | `src/tunnel/`（新建目录：types/store/binary/manager） | asyncio 驱动 cloudflared 子进程 |
| API | `src/api/admin_tunnel.py`（新建） | `GET/PUT /api/tunnel`、`POST /api/tunnel/restart`、binary 检查/刷新 |
| 装配 | `src/main.py` + `src/config.py` | TunnelStore/TunnelManager 装配；`cf_tunnel_*` 配置字段 |
| 配置 | `src/runtime_settings.py` | 保留键 `cf_tunnel_token_enc` / `cf_tunnel_enabled` |
| 审计 | `src/audit/actions.py` | `ACTION_TUNNEL_*`（3 项） |
| 前端 | `web/src/components/TunnelPanel.tsx`（新建）、`SettingsPage.tsx` 任务分组 tab「Cloudflare Tunnel」（`value: "tunnel"`） | |

### 3. `GET /v1/models` 免鉴权可查看（可选鉴权）

- `src/api/deps.py`：`api_key_user_optional` + `_AnonymousPrincipal`——无 Key 匿名查看全量；**带 Key 则严格校验，错误 Key 不降级为匿名**
- `src/api/models.py`：两个路由改用 `Depends(api_key_user_optional)`

### 4. 凭证批量导出（仅管理员）

- `src/api/admin_credentials.py`：`GET /api/credentials/batch-export`（`require_admin`，返回 `{items, failed, count}` + no-store）
- `src/db/repo.py`：`CredentialRepository.all_provider_pairs()` / `all_ids()`；`src/db/crypto.py` `CredentialDecryptError`
- 审计：`ACTION_CREDENTIAL_BATCH_EXPORT`
- 前端：`CredentialsPage.tsx` 管理员可见「批量导出」按钮

### 5. 创建 API Key 支持自定义 key

- `src/api/admin_keys.py`：`create_key` 支持 `payload["key"]`（可打印 ASCII 校验 + `digest_exists` 预检，审计备注「；自定义 Key」）
- `src/db/repo.py`：`ApiKeyRepository.create(key=...)` + `digest_exists()`
- 前端：`ApiKeysPage.tsx`「自定义 key」输入框

### 6. 冷却时长热更（管理台可改）

- `src/runtime_settings.py`：`HOT_SETTINGS` 新增 10 项 `group="cooldown"`；`GATEWAY_GROUPS` 加 `("cooldown", "冷却时长")`；`_RESERVED_SETTINGS_KEYS`
- `src/config.py`：10 个同名冷却字段（env 默认值）
- `src/engine/scheduler.py`：Scheduler 接受 10 个 `int | Callable[[], int]` 取值器参数（`live()` 包装）；`next_credit_reset(hour=)`；`model_cooldown_duration` / `concurrency_cooldown_duration` 支持 base/cap/max_override 覆盖——**max_override 只替换默认渠道档，raccoon 30 分钟长窗口档恒用常量，不得被热更覆盖**

### 7. 日志 / 审计「真清空」

- `src/api/logs.py`：`DELETE /api/logs`（清内存环 + 落库表 + 审计 `ACTION_LOGS_CLEAR`）
- `src/api/admin_audit.py`：`DELETE /api/audit`（清库 + 必写 `audit.clear` 留痕）
- `src/db/repo.py`：`LogRepository.clear()` / `AuditRepository.clear()`
- 前端：`LogsPage.tsx` / `AuditPage.tsx` 两段式确认（3 秒回落）清空按钮；**AuditPage 清空成功后必须 `await refetch()` 自动刷新**

### 8. 冷却中的凭证也显示「强制恢复」

- `web/src/pages/credentials/row.tsx`：恢复按钮条件为 `(credential.disabled === 1 || cooldown > 0)`——冷却中的凭证同样可强制恢复

---

## 二、同步上游的操作要点

1. **用 merge，不要 squash / 覆盖**：
   ```bash
   git fetch https://github.com/RobbsLuo/Coding2API.git main
   git merge FETCH_HEAD
   ```
2. **已知冲突点**：`src/api/admin_credentials.py` import 区。上游是
   `from ..auth.rbac import require_operator`，**我们必须保留
   `from ..auth.rbac import require_admin, require_operator`**（批量导出端点依赖 `require_admin`）。
3. **冲突总原则**：涉及上表功能文件的冲突，先读懂两侧意图——保留我们的功能语义，同时吸收上游的修复/重构。上游若重构了某个被我们改过的函数（如 `scheduler.py`、`proxy.py`），要把我们的参数/分支**重新移植**到新实现上，而不是简单选边。
4. **`_RESERVED_SETTINGS_KEYS`**：新增热更项时若与代理池/Tunnel 保留键冲突，保留键继续绕过热更白名单。
5. **同步完成后必做验证**：
   ```bash
   ruff check src
   python -c "import src.main"
   cd web && npx tsc --noEmit && npx vitest run && npm run build
   ```
   后端 pytest 在 Windows 下的 PrivateAccessError / flock SystemExit 属平台限制，非回归。
6. **数据自动保留**：`runtime_settings` 表里的保留键（`relay_proxy_pool`、`relay_provider_bindings`、`cf_tunnel_*`）属于运行数据，代码合并不影响既有部署的数据。
