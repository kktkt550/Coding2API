/** 管理台「Cloudflare Tunnel」卡片：填令牌、勾选启用、看状态/日志。
 *
 * 令牌只写不读：保存后输入框清空，服务端只回 token_saved 布尔值。
 * 运行态轻轮询（15 秒），进程崩溃重启也能在这里看到。
 */

import { useEffect, useState } from "react";
import { RefreshCw, RotateCw, Save, Trash2 } from "lucide-react";
import { useTunnel, useTunnelAction, useUpdateTunnel } from "../api/hooks";
import type { TunnelPatch } from "../api/types";
import { errorText } from "../lib/errors";
import { Badge, Button, Checkbox, Field, Input, Notice, Panel } from "../ui";
import type { Tone } from "../ui";

const STATUS_LABEL: Record<string, string> = {
  disabled: "未启用",
  stopped: "已停止",
  downloading: "正在下载 cloudflared",
  starting: "正在启动",
  running: "运行中",
  restarting: "异常重启中",
  error: "错误",
};

const STATUS_TONE: Record<string, Tone> = {
  disabled: "muted",
  stopped: "muted",
  downloading: "accent",
  starting: "accent",
  running: "ok",
  restarting: "warn",
  error: "danger",
};

const SOURCE_LABEL: Record<string, string> = {
  system: "系统已安装",
  managed: "自动下载",
  none: "尚未就绪",
};

export function TunnelPanel({ username }: { username: string }) {
  const query = useTunnel(username);
  const update = useUpdateTunnel(username);
  const action = useTunnelAction(username);
  const [token, setToken] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const state = query.data;
  const supported = state?.supported !== false;
  const busy = update.isPending || action.isPending;

  useEffect(() => {
    if (state?.enabled !== undefined) setEnabled(state.enabled);
  }, [state?.enabled]);

  const run = async (work: () => Promise<unknown>, ok: string) => {
    setError(null);
    try {
      await work();
      setNotice(ok);
      await query.refetch();
    } catch (caught) {
      setError(errorText(caught, "操作失败，请稍后重试。"));
    }
  };

  const save = async () => {
    const patch: TunnelPatch = {};
    if (token.trim()) patch.token = token.trim();
    if (enabled !== state?.enabled) patch.enabled = enabled;
    if (Object.keys(patch).length === 0) {
      setNotice("没有需要保存的修改。");
      return;
    }
    await run(() => update.mutateAsync(patch), "已保存并立即生效。");
    setToken("");
  };

  const clearToken = async () => {
    await run(() => update.mutateAsync({ token: null, enabled: false }),
              "令牌已清除，隧道已关闭。");
    setToken("");
  };

  const status = state?.status ?? "disabled";
  return (
    <Panel
      title="Cloudflare Tunnel"
      data-testid="tunnel-panel"
      action={
        <Badge tone={STATUS_TONE[status] ?? "muted"} dot>
          <span data-testid="tunnel-status">{STATUS_LABEL[status] ?? status}</span>
        </Badge>
      }
    >
      {!supported ? (
        <Notice tone="warn">
          当前运行时不支持隧道功能（缺文件系统 / 子进程能力）。
        </Notice>
      ) : (
        <>
          <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
            把 Zero Trust 里复制的 Tunnel 令牌粘进来并勾选启用；回源地址（如
            <code className="mx-1">http://127.0.0.1:8000</code>）在 Cloudflare 控制台配置。
            未安装 cloudflared 时会按系统自动下载，启动参数固定使用 <code>http2</code> 协议。
          </p>

          {error && (
            <div className="mb-3" data-testid="tunnel-error" role="alert">
              <Notice tone="danger">{error}</Notice>
            </div>
          )}
          {notice && (
            <div className="mb-3" data-testid="tunnel-notice" role="status">
              <Notice tone="ok">{notice}</Notice>
            </div>
          )}
          {state?.token_error && (
            <div className="mb-3">
              <Notice tone="danger">
                令牌解密失败（可能是 APP_SECRET 已变更），请重新填写令牌。
              </Notice>
            </div>
          )}
          {state?.last_error && (
            <div className="mb-3">
              <Notice tone="warn">最近错误：{state.last_error}</Notice>
            </div>
          )}

          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-64 flex-1">
              <Field
                label="Tunnel 令牌"
                htmlFor="tunnel-token"
                hint={state?.token_saved ? "已保存令牌；留空表示不修改。" : undefined}
              >
                <Input
                  id="tunnel-token"
                  type="password"
                  autoComplete="off"
                  placeholder={state?.token_saved ? "已保存，留空表示不修改" : "eyJh..."}
                  value={token}
                  disabled={busy}
                  data-testid="input-tunnel-token"
                  onChange={(event) => setToken(event.target.value)}
                />
              </Field>
            </div>
            <div className="flex items-center gap-2 pb-1">
              <Checkbox
                id="tunnel-enabled"
                checked={enabled}
                disabled={busy}
                data-testid="input-tunnel-enabled"
                onCheckedChange={(checked) => setEnabled(checked === true)}
              />
              <label htmlFor="tunnel-enabled" className="text-xs">启用 Tunnel</label>
            </div>
            <Button
              size="sm"
              variant="primary"
              disabled={busy || query.isLoading}
              data-testid="tunnel-save"
              onClick={() => void save()}
            >
              <Save className="mr-1 size-3.5" />
              保存
            </Button>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={busy || !state?.enabled}
              data-testid="tunnel-restart"
              onClick={() => void run(() => action.mutateAsync("restart"), "已重启隧道。")}
            >
              <RotateCw className="mr-1 size-3.5" />
              重启
            </Button>
            <Button
              size="sm"
              disabled={busy}
              data-testid="tunnel-refresh"
              onClick={() => void run(() => action.mutateAsync("refresh"), "已重新下载并重启。")}
            >
              <RefreshCw className="mr-1 size-3.5" />
              重新下载二进制
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={busy || !state?.token_saved}
              data-testid="tunnel-clear-token"
              onClick={() => void clearToken()}
            >
              <Trash2 className="mr-1 size-3.5" />
              清除令牌
            </Button>
          </div>

          <div className="mt-3 flex flex-wrap gap-4 text-xs text-muted-foreground">
            <span data-testid="tunnel-binary">
              二进制：{SOURCE_LABEL[state?.binary?.source ?? "none"] ?? "未知"}
              {state?.binary?.version ? `（${state.binary.version}）` : ""}
            </span>
            <span>协议：{state?.protocol ?? "http2"}</span>
            <span data-testid="tunnel-restarts">重启次数：{state?.restart_count ?? 0}</span>
          </div>

          {state?.logs && state.logs.length > 0 && (
            <pre
              className="mt-3 max-h-48 overflow-auto rounded-md bg-muted/40 p-2 text-xs leading-relaxed"
              data-testid="tunnel-logs"
            >
              {state.logs.slice(-20).map((entry, index) => (
                <div key={`${entry.at}-${index}`}>
                  {new Date(entry.at * 1000).toLocaleTimeString()} [{entry.level}] {entry.line}
                </div>
              ))}
            </pre>
          )}
        </>
      )}
    </Panel>
  );
}
