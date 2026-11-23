/** 「代理池」管理页：统一管理 Cloudflare Relay 中转条目。
 *
 * 列表 / 新增编辑 / 删除 / 启停 / 连通性测试 / 一键部署 / 切换地区，
 * 全中文文案，复用 web/src/ui 与现有组件，不引入新 UI 基础库。
 * 数据走 api/client.ts + hooks.ts（react-query），写后 Toast、失败不残留假状态。
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Globe2,
  Network,
  Pencil,
  Plus,
  RefreshCw,
  Rocket,
  Trash2,
} from "lucide-react";
import { ApiError, api } from "../api/client";
import {
  useProxyPools,
  useRelayBindings,
  useProxyPoolMutation,
} from "../api/hooks";
import type {
  RelayDeployInput,
  RelayProxyEntryView,
  RelayRegionInput,
  RelayTestResult,
  RelayUpsertInput,
} from "../api/types";
import { useSessionContext } from "../Layout";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { ToastViewport, useToasts } from "../components/Toast";
import {
  Badge,
  Button,
  Checkbox,
  EmptyState,
  Field,
  Input,
  Notice,
  QueryErrorState,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

/** 地区下拉：与后端 PLACEMENT_REGIONS（10 项）逐字对齐。 */
const REGION_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "", label: "自动" },
  { value: "wnam", label: "wnam · 北美西部" },
  { value: "enam", label: "enam · 北美东部" },
  { value: "sam", label: "sam · 南美" },
  { value: "weur", label: "weur · 西欧" },
  { value: "eeur", label: "eeur · 东欧" },
  { value: "me", label: "me · 中东" },
  { value: "afr", label: "afr · 非洲" },
  { value: "apac", label: "apac · 亚太" },
  { value: "oce", label: "oce · 大洋洲" },
];

const PROJECT_NAME_PATTERN = /^[a-z0-9][a-z0-9-]*$/;

type DialogState =
  | { kind: "none" }
  | { kind: "edit"; entry: RelayProxyEntryView | null }
  | { kind: "delete"; entry: RelayProxyEntryView }
  | { kind: "deploy" }
  | { kind: "region"; entry: RelayProxyEntryView };

interface TestState {
  status: "idle" | "pending" | "done";
  result?: RelayTestResult;
}

/** 轻量模态：fixed 遮罩 + role=dialog（与 ChangePasswordDialog 同款形态）。 */
function Modal({
  title,
  children,
  onClose,
  testId,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
  testId?: string;
}) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-testid={testId}
        className="w-full max-w-md animate-in rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
      >
        <div className="mb-4 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          <Button type="button" variant="ghost" size="sm" onClick={onClose} aria-label="关闭">
            关闭
          </Button>
        </div>
        {children}
      </div>
    </div>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "操作失败，请稍后重试";
}

export function ProxyPoolsPage() {
  const session = useSessionContext();
  const username = session.username;
  const pools = useProxyPools(username);
  const bindings = useRelayBindings(username);
  const { toasts, push, dismiss } = useToasts();

  const entries = pools.data?.items ?? [];
  const bindingValues = bindings.data?.bindings ?? {};
  // 异常绑定：指向停用 / 已删除条目 → Notice 提示 + 请求回退直连。
  const abnormalBindingCount = Object.values(bindingValues).filter(
    (proxyId) => Boolean(proxyId) && !entries.some((entry) => entry.id === proxyId && entry.enabled),
  ).length;

  const [dialog, setDialog] = useState<DialogState>({ kind: "none" });
  const [tests, setTests] = useState<Record<string, TestState>>({});

  // ---------------------------------------------------------------- 写操作
  const save = useProxyPoolMutation(
    (args: { id: string; body: RelayUpsertInput }) => api.saveProxyPool(args.id, args.body),
    username,
  );
  const remove = useProxyPoolMutation((id: string) => api.deleteProxyPool(id), username);
  const toggle = useProxyPoolMutation(
    (args: { id: string; enabled: boolean }) => api.toggleProxyPool(args.id, args.enabled),
    username,
  );
  const deploy = useProxyPoolMutation((body: RelayDeployInput) => api.deployRelay(body), username);
  const region = useProxyPoolMutation(
    (args: { project: string; body: RelayRegionInput }) =>
      api.updateRelayRegion(args.project, args.body),
    username,
  );

  const closeDialog = () => setDialog({ kind: "none" });

  const onSave = async (id: string, body: RelayUpsertInput) => {
    try {
      await save.mutateAsync({ id, body });
      push({ tone: "ok", content: "已保存" });
      closeDialog();
    } catch (error) {
      push({ tone: "danger", content: `保存失败：${errorMessage(error)}` });
    }
  };

  const onDelete = async (entry: RelayProxyEntryView) => {
    try {
      await remove.mutateAsync(entry.id);
      push({ tone: "ok", content: `已删除「${entry.name}」` });
      closeDialog();
    } catch (error) {
      push({ tone: "danger", content: `删除失败：${errorMessage(error)}` });
    }
  };

  const onToggle = async (entry: RelayProxyEntryView) => {
    try {
      await toggle.mutateAsync({ id: entry.id, enabled: !entry.enabled });
      push({ tone: "ok", content: entry.enabled ? "已停用" : "已启用" });
    } catch (error) {
      push({ tone: "danger", content: `切换失败：${errorMessage(error)}` });
    }
  };

  const onTest = async (entry: RelayProxyEntryView) => {
    setTests((prev) => ({ ...prev, [entry.id]: { status: "pending" } }));
    try {
      const result = await api.testProxyPool(entry.id);
      setTests((prev) => ({ ...prev, [entry.id]: { status: "done", result } }));
    } catch (error) {
      setTests((prev) => ({
        ...prev,
        [entry.id]: {
          status: "done",
          result: { ok: false, status: 0, elapsedMs: 0, error: errorMessage(error) },
        },
      }));
    }
  };

  const onDeploy = async (body: RelayDeployInput) => {
    try {
      const result = await deploy.mutateAsync(body);
      push({ tone: "ok", content: `部署成功，URL：${result.deployUrl}` });
      closeDialog();
    } catch (error) {
      push({ tone: "danger", content: `部署失败：${errorMessage(error)}` });
    }
  };

  const onRegion = async (project: string, body: RelayRegionInput) => {
    try {
      const result = await region.mutateAsync({ project, body });
      push({ tone: "ok", content: `地区已切换为${result.region || "自动"}` });
      closeDialog();
    } catch (error) {
      push({ tone: "danger", content: `切换地区失败：${errorMessage(error)}` });
    }
  };

  // ---------------------------------------------------------------- 渲染
  if (pools.isLoading) return <PageSkeleton variant="table" />;
  if (pools.isError) {
    return (
      <div className="space-y-6">
        <PageHeader title="代理池" description="统一管理 Cloudflare Relay 中转，按渠道分组选用" />
        <QueryErrorState onRetry={() => void pools.refetch()} />
      </div>
    );
  }

  return (
    <div className="space-y-6" data-testid="proxy-pools-page">
      <PageHeader
        icon={<Network className="size-5" />}
        eyebrow="管理"
        title="代理池"
        description="统一管理 Cloudflare Relay 中转，按渠道分组选用；relay URL 即凭据，请勿外泄"
        actions={
          <>
            <Button
              type="button"
              variant="primary"
              data-testid="proxy-deploy-open"
              onClick={() => setDialog({ kind: "deploy" })}
            >
              <Rocket className="size-4" /> 部署 Relay
            </Button>
            <Button
              type="button"
              data-testid="proxy-add-open"
              onClick={() => setDialog({ kind: "edit", entry: null })}
            >
              <Plus className="size-4" /> 新增代理
            </Button>
          </>
        }
      />

      {abnormalBindingCount > 0 && (
        <Notice tone="warn" >
          <span data-testid="proxy-pool-binding-warning">
            有 {abnormalBindingCount} 个渠道分组绑定的代理已停用或已删除，这些分组当前直连
          </span>
          <Link className="ml-2 underline" to="/">前往「凭证管理」</Link>
        </Notice>
      )}

      {entries.length === 0 ? (
        <EmptyState
          data-testid="proxy-pool-empty"
          icon={<Globe2 className="size-5" />}
          title="还没有代理"
          description="部署一个 Cloudflare Relay Worker，或手工登记已有的 HTTPS relay 地址"
          action={
            <div className="flex flex-wrap items-center justify-center gap-2">
              <Button type="button" variant="primary" onClick={() => setDialog({ kind: "deploy" })}>
                部署 Relay Worker
              </Button>
              <Button type="button" onClick={() => setDialog({ kind: "edit", entry: null })}>
                手工新增 HTTPS relay 地址
              </Button>
            </div>
          }
        />
      ) : (
        <Table cards data-testid="proxy-pool-table">
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>名称</TableHead>
              <TableHead>URL</TableHead>
              <TableHead>地区</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>记住凭据</TableHead>
              <TableHead>最近测试</TableHead>
              <TableHead>操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map((entry) => {
              const test = tests[entry.id] ?? { status: "idle" as const };
              return (
                <TableRow key={entry.id} data-testid={`proxy-row-${entry.id}`}>
                  <TableCell label="ID">
                    <code className="text-xs">{entry.id}</code>
                  </TableCell>
                  <TableCell label="名称">{entry.name}</TableCell>
                  <TableCell label="URL">
                    <span className="block max-w-56 truncate text-xs" title={entry.url}>
                      {entry.url}
                    </span>
                    <span className="text-[11px] text-muted-foreground">URL 即凭据</span>
                  </TableCell>
                  <TableCell label="地区">{entry.region || "自动"}</TableCell>
                  <TableCell label="状态">
                    <button
                      type="button"
                      data-testid={`proxy-toggle-${entry.id}`}
                      onClick={() => void onToggle(entry)}
                      disabled={toggle.isPending}
                      className="rounded-md focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                      aria-label={`${entry.enabled ? "停用" : "启用"} ${entry.name}`}
                    >
                      <Badge tone={entry.enabled ? "accent" : "muted"}>
                        {entry.enabled ? "已启用" : "已停用"}
                      </Badge>
                    </button>
                  </TableCell>
                  <TableCell label="记住凭据">
                    {entry.credentials_saved ? "已记住" : "未记住"}
                  </TableCell>
                  <TableCell label="最近测试">
                    <span data-testid={`proxy-test-result-${entry.id}`}>
                      {test.status === "pending" && "测试中…"}
                      {test.status === "idle" && "未测试"}
                      {test.status === "done" && test.result?.ok &&
                        `✓ 成功 · ${test.result.elapsedMs} ms`}
                      {test.status === "done" && test.result && !test.result.ok &&
                        `✗ 失败 · ${test.result.status ? `HTTP ${test.result.status}` : test.result.error}`}
                    </span>
                  </TableCell>
                  <TableCell label="操作">
                    <div className="flex flex-wrap items-center gap-1">
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        data-testid={`proxy-edit-${entry.id}`}
                        onClick={() => setDialog({ kind: "edit", entry })}
                        aria-label={`编辑 ${entry.name}`}
                      >
                        <Pencil className="size-3.5" /> 编辑
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        data-testid={`proxy-test-${entry.id}`}
                        onClick={() => void onTest(entry)}
                        disabled={test.status === "pending"}
                      >
                        <RefreshCw className="size-3.5" />
                        {test.status === "pending" ? "测试中…" : "测试"}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        data-testid={`proxy-region-open-${entry.id}`}
                        onClick={() => setDialog({ kind: "region", entry })}
                      >
                        <Globe2 className="size-3.5" /> 切换地区
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        data-testid={`proxy-delete-${entry.id}`}
                        onClick={() => setDialog({ kind: "delete", entry })}
                        aria-label={`删除 ${entry.name}`}
                      >
                        <Trash2 className="size-3.5" /> 删除
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}

      {dialog.kind === "edit" && (
        <EditDialog entry={dialog.entry} pending={save.isPending} onClose={closeDialog} onSubmit={onSave} />
      )}
      {dialog.kind === "delete" && (
        <Modal title="删除代理" onClose={closeDialog} testId="proxy-delete-dialog">
          <p className="text-sm leading-relaxed">
            确认删除「{dialog.entry.name}」？绑定该代理的渠道分组将自动回退直连
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={closeDialog}>取消</Button>
            <Button
              type="button"
              variant="danger"
              data-testid="proxy-delete-confirm"
              disabled={remove.isPending}
              onClick={() => void onDelete(dialog.entry)}
            >
              确认删除
            </Button>
          </div>
        </Modal>
      )}
      {dialog.kind === "deploy" && (
        <DeployDialog pending={deploy.isPending} onClose={closeDialog} onSubmit={onDeploy} />
      )}
      {dialog.kind === "region" && (
        <RegionDialog
          entry={dialog.entry}
          pending={region.isPending}
          onClose={closeDialog}
          onSubmit={onRegion}
        />
      )}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}

// ---------------------------------------------------------------- 新增 / 编辑

function EditDialog({
  entry,
  pending,
  onClose,
  onSubmit,
}: {
  entry: RelayProxyEntryView | null;
  pending: boolean;
  onClose: () => void;
  onSubmit: (id: string, body: RelayUpsertInput) => Promise<void>;
}) {
  const isEdit = entry !== null;
  const [id, setId] = useState(entry?.id ?? "");
  const [name, setName] = useState(entry?.name ?? "");
  const [url, setUrl] = useState(entry?.url ?? "");
  const [region, setRegion] = useState(entry?.region ?? "");
  const [enabled, setEnabled] = useState(entry?.enabled ?? true);
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const trimmedName = name.trim();
    const trimmedUrl = url.trim().replace(/\/+$/, "");
    if (!trimmedName || !trimmedUrl) {
      setError("代理名称与合法的 https relay 地址不能为空");
      return;
    }
    if (!/^https:\/\//i.test(trimmedUrl)) {
      setError("relay 地址必须是公网 https 地址，且不能包含用户名密码或内网地址");
      return;
    }
    if (!isEdit && !id.trim()) {
      setError("请填写代理 ID");
      return;
    }
    setError("");
    await onSubmit(isEdit ? entry.id : id.trim(), {
      name: trimmedName,
      url: trimmedUrl,
      region,
      enabled,
    });
  };

  return (
    <Modal title={isEdit ? "编辑代理" : "新增代理"} onClose={onClose} testId="proxy-edit-dialog">
      <form className="space-y-3" onSubmit={(event) => void submit(event)}>
        <Field label="ID（新增必填，编辑只读）" htmlFor="proxy-id">
          <Input
            id="proxy-id"
            data-testid="proxy-id-input"
            value={id}
            disabled={isEdit}
            placeholder="my-relay"
            onChange={(event) => setId(event.target.value)}
          />
        </Field>
        <Field label="名称" htmlFor="proxy-name">
          <Input
            id="proxy-name"
            data-testid="proxy-name-input"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </Field>
        <Field label="URL（仅 https）" htmlFor="proxy-url">
          <Input
            id="proxy-url"
            data-testid="proxy-url-input"
            value={url}
            placeholder="https://xxx.workers.dev"
            onChange={(event) => setUrl(event.target.value)}
          />
        </Field>
        <Field label="地区" htmlFor="proxy-region">
          <Select
            id="proxy-region"
            data-testid="proxy-region-input"
            value={region}
            onChange={(event) => setRegion(event.target.value)}
          >
            {REGION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </Select>
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={enabled}
            onCheckedChange={(checked) => setEnabled(checked === true)}
            data-testid="proxy-enabled-input"
          />
          启用
        </label>
        {error && <Notice tone="danger"><span data-testid="proxy-edit-error">{error}</span></Notice>}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>取消</Button>
          <Button type="submit" variant="primary" data-testid="proxy-save" disabled={pending}>
            {pending ? "保存中…" : "保存"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- 部署

function DeployDialog({
  pending,
  onClose,
  onSubmit,
}: {
  pending: boolean;
  onClose: () => void;
  onSubmit: (body: RelayDeployInput) => Promise<void>;
}) {
  const [accountId, setAccountId] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [projectName, setProjectName] = useState(`relay-${Date.now().toString(36)}`);
  const [region, setRegion] = useState("");
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!accountId.trim() || !apiToken.trim()) {
      setError("请填写 Cloudflare Account ID 与 API Token");
      return;
    }
    if (!PROJECT_NAME_PATTERN.test(projectName.trim())) {
      setError("Worker 名称只能包含小写字母、数字和连字符，且以字母或数字开头");
      return;
    }
    setError("");
    await onSubmit({
      accountId: accountId.trim(),
      apiToken: apiToken.trim(),
      projectName: projectName.trim(),
      region,
      rememberCredentials: remember,
    });
    // 无论成功失败都立即清空 Token 输入框（绝不留在表单内存里）。
    setApiToken("");
  };

  return (
    <Modal title="部署 Relay Worker" onClose={onClose} testId="proxy-deploy-dialog">
      <form className="space-y-3" onSubmit={(event) => void submit(event)}>
        <Field label="Cloudflare Account ID" htmlFor="deploy-account">
          <Input
            id="deploy-account"
            data-testid="proxy-deploy-account"
            value={accountId}
            onChange={(event) => setAccountId(event.target.value)}
          />
        </Field>
        <Field label="API Token（仅当次使用，提交后清空）" htmlFor="deploy-token">
          <Input
            id="deploy-token"
            type="password"
            autoComplete="off"
            data-testid="proxy-deploy-token"
            value={apiToken}
            onChange={(event) => setApiToken(event.target.value)}
          />
        </Field>
        <Field label="Worker 名称" htmlFor="deploy-name">
          <Input
            id="deploy-name"
            data-testid="proxy-deploy-project"
            value={projectName}
            onChange={(event) => setProjectName(event.target.value)}
          />
        </Field>
        <Field label="地区" htmlFor="deploy-region">
          <Select
            id="deploy-region"
            data-testid="proxy-deploy-region"
            value={region}
            onChange={(event) => setRegion(event.target.value)}
          >
            {REGION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </Select>
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={remember}
            onCheckedChange={(checked) => setRemember(checked === true)}
            data-testid="proxy-deploy-remember"
          />
          记住凭据（加密存储，永不回显）
        </label>
        {error && <Notice tone="danger"><span data-testid="proxy-deploy-error">{error}</span></Notice>}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>取消</Button>
          <Button type="submit" variant="primary" data-testid="proxy-deploy-submit" disabled={pending}>
            {pending ? "部署中…" : "开始部署"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- 切换地区

function RegionDialog({
  entry,
  pending,
  onClose,
  onSubmit,
}: {
  entry: RelayProxyEntryView;
  pending: boolean;
  onClose: () => void;
  onSubmit: (project: string, body: RelayRegionInput) => Promise<void>;
}) {
  const [region, setRegion] = useState(entry.region || "");
  const [accountId, setAccountId] = useState("");
  const [apiToken, setApiToken] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!entry.credentials_saved && (!accountId.trim() || !apiToken.trim())) {
      setError("该代理未记住凭据，请填写 Cloudflare Account ID 与 API Token");
      return;
    }
    setError("");
    await onSubmit(entry.id, {
      region,
      ...(accountId.trim() ? { accountId: accountId.trim() } : {}),
      ...(apiToken.trim() ? { apiToken: apiToken.trim() } : {}),
      rememberCredentials: remember,
    });
    setApiToken("");
  };

  return (
    <Modal title={`切换地区 · ${entry.name}`} onClose={onClose} testId="proxy-region-dialog">
      <form className="space-y-3" onSubmit={(event) => void submit(event)}>
        <p className="text-xs leading-relaxed text-muted-foreground">
          凭据可留空：留空时复用该条目已加密记住的凭据；URL 不变，地区就地更新。
        </p>
        <Field label="地区" htmlFor="region-select">
          <Select
            id="region-select"
            data-testid="proxy-region-select"
            value={region}
            onChange={(event) => setRegion(event.target.value)}
          >
            {REGION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </Select>
        </Field>
        <Field label="Cloudflare Account ID（可留空）" htmlFor="region-account">
          <Input
            id="region-account"
            data-testid="proxy-region-account"
            value={accountId}
            onChange={(event) => setAccountId(event.target.value)}
          />
        </Field>
        <Field label="API Token（可留空）" htmlFor="region-token">
          <Input
            id="region-token"
            type="password"
            autoComplete="off"
            data-testid="proxy-region-token"
            value={apiToken}
            onChange={(event) => setApiToken(event.target.value)}
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={remember}
            onCheckedChange={(checked) => setRemember(checked === true)}
            data-testid="proxy-region-remember"
          />
          记住本次填写的凭据
        </label>
        {error && <Notice tone="danger"><span data-testid="proxy-region-error">{error}</span></Notice>}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>取消</Button>
          <Button type="submit" variant="primary" data-testid="proxy-region-submit" disabled={pending}>
            {pending ? "提交中…" : "切换地区"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
