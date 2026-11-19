import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryStatePayload } from "../../../shared/types";
import { MemoryDrawer } from "./MemoryDrawer";

const baseMemory = (): MemoryStatePayload => ({
  enabled: true,
  autoExtractEnabled: true,
  userId: "local-user",
  projectId: "coding-agent",
  globalCount: 1,
  projectCount: 1,
  conflictCount: 1,
  pendingCount: 0,
  processingCount: 0,
  readyCount: 2,
  failedCount: 0,
});

describe("MemoryDrawer", () => {
  const request = vi.fn();
  const onClose = vi.fn();
  const onMemoryChange = vi.fn();

  beforeEach(() => {
    request.mockReset();
    onClose.mockReset();
    onMemoryChange.mockReset();
    request.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "memory.status") return baseMemory();
      if (method === "memory.list") {
        return [
          {
            scope: "global",
            record: {
              id: "mem-g1",
              key: "style.response",
              value: "使用简洁中文回复",
              kind: "preference",
              status: "active",
              authority: "explicit",
              updated_at: "2026-11-19",
            },
          },
          {
            scope: "project",
            record: {
              id: "mem-p1",
              key: "frontend.style",
              value: "简约低饱和线条风格",
              kind: "convention",
              status: "active",
              authority: "explicit",
              updated_at: "2026-11-19",
            },
          },
        ];
      }
      if (method === "memory.conflicts") {
        return [
          {
            scope: "project",
            conflict: {
              key: "pkg.manager",
              candidates: [
                { id: "c-1", value: "pnpm" },
                { id: "c-2", value: "npm" },
              ],
            },
          },
        ];
      }
      if (method === "memory.setEnabled") return { ...baseMemory(), enabled: Boolean(params?.enabled) };
      if (method === "memory.setAutoExtract") return { ...baseMemory(), autoExtractEnabled: Boolean(params?.enabled) };
      if (method === "memory.remember") return { record: { id: "mem-new" }, memory: baseMemory() };
      if (method === "memory.forget") return { removed: true, memory: baseMemory() };
      if (method === "memory.clear") return { count: 1, memory: baseMemory() };
      return {};
    });
    window.agent = {
      request,
      onEvent: () => () => undefined,
      onStatus: () => () => undefined,
    };
  });

  afterEach(cleanup);

  it("stays idle while closed and loads status, list and conflicts on open", async () => {
    const { rerender } = render(
      <MemoryDrawer open={false} memory={baseMemory()} disabled={false} onClose={onClose} onMemoryChange={onMemoryChange} />,
    );
    expect(request).not.toHaveBeenCalled();

    rerender(
      <MemoryDrawer open memory={baseMemory()} disabled={false} onClose={onClose} onMemoryChange={onMemoryChange} />,
    );
    expect(await screen.findByText("使用简洁中文回复")).toBeInTheDocument();
    expect(screen.getByText("简约低饱和线条风格")).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith("memory.status");
    expect(request).toHaveBeenCalledWith("memory.list", {});
    expect(request).toHaveBeenCalledWith("memory.conflicts", {});

    fireEvent.click(screen.getByRole("tab", { name: "冲突 1" }));
    expect(screen.getByText("pkg.manager")).toBeInTheDocument();
    expect(screen.getByText("pnpm")).toBeInTheDocument();
  });

  it("supports remembering, confirming forget, toggling switches and clearing project memories", async () => {
    render(
      <MemoryDrawer open memory={baseMemory()} disabled={false} onClose={onClose} onMemoryChange={onMemoryChange} />,
    );
    await screen.findByText("使用简洁中文回复");

    fireEvent.change(screen.getByLabelText("新增长期记忆"), { target: { value: "保留竖线执行树" } });
    fireEvent.change(screen.getByLabelText("记忆作用域"), { target: { value: "global" } });
    fireEvent.click(screen.getByRole("button", { name: "记住" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("memory.remember", {
      content: "保留竖线执行树",
      scope: "global",
    }));

    const forgetButtons = screen.getAllByRole("button", { name: "遗忘" });
    fireEvent.click(forgetButtons[0]);
    expect(request).not.toHaveBeenCalledWith("memory.forget", expect.anything());
    fireEvent.click(screen.getByRole("button", { name: "确认遗忘" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("memory.forget", {
      key: "mem-g1",
      scope: "global",
      confirmed: true,
    }));

    fireEvent.click(screen.getByRole("button", { name: /清空项目记忆/ }));
    expect(request).not.toHaveBeenCalledWith("memory.clear", expect.anything());
    fireEvent.click(screen.getByRole("button", { name: "确认清空项目" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("memory.clear", {
      allScopes: false,
      confirmed: true,
    }));
  });
});
