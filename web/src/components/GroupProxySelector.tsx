/** 凭证页「渠道分组出站代理」选择器。
 *
 * 放在组头 `<button>` **外面的兄弟位置**（与「全选本渠道」Checkbox 同级）：
 * 点击 Select / 标签不会触发分组展开折叠，`onClick` / `onMouseDown` 再兜一层
 * stopPropagation，避免读屏 / 键盘焦点冲突与嵌套交互元素。
 *
 * 自包含：`useProxyPools` / `useRelayBindings` / `useRelayBindingMutation`
 * 都在此组件内调用，凭证页只需插入一个元素。
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/client";
import {
  useProxyPools,
  useRelayBindings,
  useRelayBindingMutation,
} from "../api/hooks";
import { useSessionContext } from "../Layout";
import { Badge, Select } from "../ui";
import { Tip } from "./Tip";
import { ToastViewport, useToasts } from "./Toast";
import type { RelayProxyEntryView } from "../api/types";

export interface GroupProxySelectorProps {
  /** 渠道 key（= credential.provider，绑定写入的 provider）。 */
  groupKey: string;
  /** 渠道中文名，用于 aria-label。 */
  groupLabel: string;
  /** admin / operator 才渲染可写 Select；viewer 整块不渲染。 */
  canWrite: boolean;
}

/** 选项文案：名称（ID · 地区）。 */
function optionLabel(entry: RelayProxyEntryView): string {
  const region = entry.region ? ` · ${entry.region}` : "";
  return `${entry.name}（${entry.id}${region}）`;
}

export function GroupProxySelector({
  groupKey,
  groupLabel,
  canWrite,
}: GroupProxySelectorProps) {
  const session = useSessionContext();
  const username = session.username;
  const pools = useProxyPools(username);
  const bindingsQuery = useRelayBindings(username);
  const mutation = useRelayBindingMutation(username);
  const { toasts, push, dismiss } = useToasts();

  const hasPoolData = Array.isArray(pools.data?.items);
  const hasBindingData = bindingsQuery.data?.bindings !== undefined;
  const entries = hasPoolData ? pools.data!.items : [];
  const bindings = hasBindingData ? bindingsQuery.data!.bindings : {};
  const current = bindings[groupKey] ?? "";
  const enabledEntries = entries.filter((entry) => entry.enabled);
  // 绑定指向停用 / 已删除：保留一个禁用的占位项，不静默丢失绑定值。
  const abnormal = Boolean(current) && !enabledEntries.some((entry) => entry.id === current);
  const isEmptyPool = enabledEntries.length === 0;

  // 本地选中态：onChange 立即持久化；失败回滚到原值（不保留「已生效」假状态）。
  const [selected, setSelected] = useState<string>(current);
  useEffect(() => {
    setSelected(current);
  }, [current, groupKey]);

  // viewer 不渲染；代理池 / 绑定接口不可用（旧后端、未登录、请求失败）时也不
  // 渲染「空白或不可操作的控件」——直接不显示，凭证页其余功能不受影响。
  if (!canWrite || !hasPoolData || !hasBindingData) return null;

  const onChange = async (next: string) => {
    const previous = current;
    setSelected(next); // 乐观展示
    try {
      await mutation.mutateAsync({ provider: groupKey, proxy_id: next });
      push({
        tone: "ok",
        testId: `group-proxy-toast-${groupKey}`,
        content: next ? "已保存该渠道分组的出站代理" : "已恢复直连",
      });
    } catch (error) {
      setSelected(previous); // 失败回滚
      const message = error instanceof ApiError ? error.message : "保存失败，请稍后重试";
      push({
        tone: "danger",
        testId: `group-proxy-toast-${groupKey}`,
        content: `保存失败：${message}`,
      });
    }
  };

  return (
    <span
      className="ml-auto flex shrink-0 items-center gap-1.5 pr-3"
      data-testid={`group-proxy-${groupKey}`}
      // 组头是 <button>；这里虽是它的兄弟节点，仍显式阻断冒泡，
      // 保证点击 Select / 标签 / Tip 都不会误触发分组展开折叠。
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <span className="text-[11px] whitespace-nowrap text-muted-foreground">代理：</span>
      {abnormal ? (
        <Tip content="原代理已停用或已删除，该分组当前直连，请改选其他代理或恢复直连">
          <span
            className="inline-flex items-center rounded-md border border-warn/40 bg-warn/12 px-2 py-1 text-[11px] text-warn-ink"
            data-testid={`group-proxy-abnormal-${groupKey}`}
            title="原代理已停用或已删除，该分组当前直连，请改选"
          >
            原代理（已停用 / 已删除）
          </span>
        </Tip>
      ) : null}
      <Select
        aria-label={`设置 ${groupLabel} 分组的出站代理`}
        data-testid={`group-proxy-select-${groupKey}`}
        className="w-44"
        value={selected}
        disabled={mutation.isPending || pools.isLoading}
        onChange={(event) => void onChange(event.target.value)}
      >
        <option value="">直连 / 不代理</option>
        {enabledEntries.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {optionLabel(entry)}
          </option>
        ))}
        {abnormal && (
          <option value={current} disabled>
            原代理（已停用 / 已删除）
          </option>
        )}
      </Select>
      <Badge
        tone={current && !abnormal ? "accent" : "muted"}
      >
        <span data-testid={`group-proxy-badge-${groupKey}`}>
          {current && !abnormal ? "经代理" : "直连"}
        </span>
      </Badge>
      {isEmptyPool && (
        <span className="text-[11px] whitespace-nowrap text-muted-foreground">
          暂无可用代理，前往
          <Link className="mx-0.5 underline" to="/proxy-pools">「代理池」</Link>
          部署
        </span>
      )}
      {toasts.length > 0 && <ToastViewport toasts={toasts} onDismiss={dismiss} />}
    </span>
  );
}
