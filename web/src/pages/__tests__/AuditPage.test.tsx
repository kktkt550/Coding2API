import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuditPage } from "../AuditPage";
import { jsonResponse, renderPage, settle, userEvent } from "@/test-utils";

const LABELS = { login_success: "登录成功", user_disable: "禁用用户" };

function auditPayload(events: unknown[], total = events.length) {
  return { events, total, actions: ["login_success", "user_disable"], labels: LABELS };
}

function stubFetch(body: unknown, status = 200) {
  const spy = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    jsonResponse(body, status));
  vi.stubGlobal("fetch", spy);
  return spy;
}

const EVENT = {
  id: "audit_1", ts: 1_700_000_000, actor: "root", action: "login_success",
  target: null, detail: "", ip: "10.0.0.1", ok: 1,
};

describe("AuditPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染审计行：动作标签、对象、空字段回退", async () => {
    stubFetch(auditPayload([
      EVENT,
      { ...EVENT, id: "audit_2", action: "user_disable", target: "alice",
        detail: "停用", ok: 0 },
    ]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("audit-table")).toBeInTheDocument();
    const first = screen.getByTestId("audit-row-audit_1");
    expect(first).toHaveTextContent("登录成功");
    expect(first).toHaveTextContent("10.0.0.1");
    // 无 target / 无 detail → 破折号占位
    expect(first).toHaveTextContent("—");
    // 失败记录（ok=0）同样展示
    expect(screen.getByTestId("audit-row-audit_2")).toHaveTextContent("禁用用户");
  });

  it("筛选字段的 label 与控件关联（读屏能拿到字段名）", async () => {
    stubFetch(auditPayload([EVENT]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();

    // label 与控件必须真的绑上：getByLabelText 找不到就说明只是视觉上摆在一起
    expect(screen.getByLabelText("操作者")).toBe(screen.getByTestId("audit-actor"));
    expect(screen.getByLabelText("动作")).toBe(screen.getByTestId("audit-action"));
  });

  it("无记录时显示占位", async () => {
    stubFetch(auditPayload([]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("no-audit")).toBeInTheDocument();
  });

  it("提交操作者筛选会把 actor 带进查询", async () => {
    const spy = stubFetch(auditPayload([EVENT]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.type(screen.getByTestId("audit-actor"), "alice");
    await userEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => {
      const urls = spy.mock.calls.map(([url]) => String(url));
      expect(urls.some((url) => url.includes("actor=alice"))).toBe(true);
    });
  });

  it("切换动作筛选立即重新取数", async () => {
    const spy = stubFetch(auditPayload([EVENT]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.selectOptions(screen.getByTestId("audit-action"), "user_disable");
    await waitFor(() => {
      const urls = spy.mock.calls.map(([url]) => String(url));
      expect(urls.some((url) => url.includes("action=user_disable"))).toBe(true);
    });
  });

  it("重置按钮清筛选不删数据", async () => {
    const spy = stubFetch(auditPayload([EVENT]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.selectOptions(screen.getByTestId("audit-action"), "user_disable");
    await userEvent.click(screen.getByTestId("audit-reset"));
    await waitFor(() => {
      const urls = spy.mock.calls.map(([url]) => String(url));
      // 重置后不再带 action 过滤
      expect(urls.at(-1)).not.toContain("action=");
    });
    expect(screen.getByTestId("audit-action")).toHaveValue("");
    // 重置不触发 DELETE：数据仍在
    expect(spy.mock.calls.some(([, init]) => String(init?.method) === "DELETE")).toBe(false);
  });

  it("清空记录：两段确认后 DELETE /api/audit，重置筛选", async () => {
    const spy = stubFetch(auditPayload([EVENT]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();

    // 第一次点只进入确认态，不发请求
    await userEvent.click(screen.getByTestId("audit-clear"));
    expect(screen.getByTestId("audit-clear")).toHaveTextContent("确认清空？");
    expect(spy.mock.calls.some(([, init]) => String(init?.method) === "DELETE")).toBe(false);

    // 再点才真删
    await userEvent.click(screen.getByTestId("audit-clear"));
    await waitFor(() => {
      expect(spy.mock.calls.some(([, init]) =>
        String(init?.method) === "DELETE"
        && String(init?.body ?? "") === "" )).toBe(true);
    });
    // 清空后自动重取列表（否则页面停在旧数据上）
    await waitFor(() => {
      const urls = spy.mock.calls.map(([url]) => String(url));
      expect(urls.at(-1)).toContain("/api/audit");
    });
    await waitFor(() => {
      expect(screen.getByTestId("audit-clear")).toHaveTextContent("清空记录");
    });
  });

  it("分页：满页时可翻下一页，回退禁用上一页", async () => {
    const full = Array.from({ length: 50 }, (_, index) => ({
      ...EVENT, id: `audit_${index}`,
    }));
    // total > 本页条数 → 还有下一页（页数由后端 total 算出）
    const spy = stubFetch(auditPayload(full, 120));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("audit-prev")).toBeDisabled();
    await userEvent.click(screen.getByTestId("audit-next"));
    await waitFor(() => {
      const urls = spy.mock.calls.map(([url]) => String(url));
      expect(urls.some((url) => url.includes("offset=50"))).toBe(true);
    });
    expect(screen.getByTestId("audit-page-info")).toHaveTextContent("第 2 / 3 页");

    await userEvent.click(screen.getByTestId("audit-prev"));
    await waitFor(() =>
      expect(screen.getByTestId("audit-page-info")).toHaveTextContent("第 1 / 3 页"));
  });

  it("不满一页时下一页禁用", async () => {
    stubFetch(auditPayload([EVENT]));
    renderPage(<AuditPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("audit-next")).toBeDisabled();
  });
});
