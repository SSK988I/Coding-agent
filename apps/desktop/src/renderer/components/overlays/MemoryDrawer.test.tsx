import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

  it("forgets an entire conflict group via two-step confirmation listing both affected candidates", async () => {
    let activeConflicts = [
      {
        scope: "project",
        conflict: {
          key: "pkg.manager",
          candidates: [
            { id: "c-1", value: "pnpm", authority: "explicit" },
            { id: "c-2", value: "npm", authority: "inferred" },
          ],
        },
      },
    ];
    request.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "memory.status") return { ...baseMemory(), conflictCount: activeConflicts.length };
      if (method === "memory.list") return [];
      if (method === "memory.conflicts") return activeConflicts;
      if (method === "memory.forget") {
        if (params?.key === "pkg.manager") activeConflicts = [];
        return { removed: true, memory: { ...baseMemory(), conflictCount: activeConflicts.length } };
      }
      return {};
    });

    render(
      <MemoryDrawer
        open
        workspaceKey='["E:\\\\code\\\\Coding-agent","coding-agent"]'
        memory={baseMemory()}
        disabled={false}
        onClose={onClose}
        onMemoryChange={onMemoryChange}
      />,
    );

    const conflictsTab = await screen.findByRole("tab", { name: "冲突 1" });
    fireEvent.click(conflictsTab);
    expect(screen.getByText("pkg.manager")).toBeInTheDocument();
    expect(screen.getByText("pnpm")).toBeInTheDocument();
    expect(screen.getByText("npm")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "遗忘" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "遗忘整组冲突" }));
    expect(request).not.toHaveBeenCalledWith("memory.forget", expect.anything());

    const confirmRegion = screen.getByRole("region", { name: "确认遗忘冲突组 pkg.manager" });
    expect(confirmRegion).toHaveTextContent("pkg.manager");
    expect(confirmRegion).toHaveTextContent("全部 2 个候选值");
    expect(within(confirmRegion).getByText("pnpm")).toBeInTheDocument();
    expect(within(confirmRegion).getByText("npm")).toBeInTheDocument();

    fireEvent.click(within(confirmRegion).getByRole("button", { name: "确认遗忘整组" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("memory.forget", {
      key: "pkg.manager",
      scope: "project",
      confirmed: true,
    }));
    expect(await screen.findByText("当前没有待解决的记忆冲突。")).toBeInTheDocument();
    expect(screen.queryByText("pnpm")).not.toBeInTheDocument();
    expect(screen.queryByText("npm")).not.toBeInTheDocument();
  });

  it("resets clear confirmation across workspace switches and discards late reads from the previous workspace", async () => {
    const projectAMemory: MemoryStatePayload = { ...baseMemory(), projectId: "project-a", projectCount: 1 };
    const projectBMemory: MemoryStatePayload = { ...baseMemory(), projectId: "project-b", projectCount: 1 };

    let resolveProjectAList!: (value: unknown) => void;
    let resolveProjectAStatus!: (value: unknown) => void;
    let activeProject: "a" | "b" = "a";

    request.mockImplementation((method: string) => {
      if (activeProject === "a") {
        if (method === "memory.status") return new Promise((resolve) => { resolveProjectAStatus = resolve; });
        if (method === "memory.list") return new Promise((resolve) => { resolveProjectAList = resolve; });
        if (method === "memory.conflicts") return Promise.resolve([]);
      }
      if (method === "memory.status") return Promise.resolve(projectBMemory);
      if (method === "memory.list") {
        return Promise.resolve([
          {
            scope: "project",
            record: {
              id: "mem-b1",
              key: "project.name",
              value: "项目 B 专属约定",
              kind: "convention",
              status: "active",
              authority: "explicit",
              updated_at: "2026-11-20",
            },
          },
        ]);
      }
      if (method === "memory.conflicts") return Promise.resolve([]);
      return Promise.resolve({});
    });

    const { rerender } = render(
      <MemoryDrawer
        open
        workspaceKey='["E:\\\\project-a","project-a"]'
        memory={projectAMemory}
        disabled={false}
        onClose={onClose}
        onMemoryChange={onMemoryChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /清空项目记忆/ }));
    expect(screen.getByRole("button", { name: "确认清空项目" })).toBeInTheDocument();

    // Close drawer, switch to Project B, and reopen before Project A's read finishes.
    rerender(
      <MemoryDrawer
        open={false}
        workspaceKey='["E:\\\\project-a","project-a"]'
        memory={projectAMemory}
        disabled={false}
        onClose={onClose}
        onMemoryChange={onMemoryChange}
      />,
    );

    activeProject = "b";
    rerender(
      <MemoryDrawer
        open
        workspaceKey='["E:\\\\project-b","project-b"]'
        memory={projectBMemory}
        disabled={false}
        onClose={onClose}
        onMemoryChange={onMemoryChange}
      />,
    );

    expect(await screen.findByText("项目 B 专属约定")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认清空项目" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /清空项目记忆/ })).toBeInTheDocument();

    // Now resolve the stale Project A load and confirm it is completely ignored.
    onMemoryChange.mockClear();
    await act(async () => {
      resolveProjectAStatus(projectAMemory);
      resolveProjectAList([
        {
          scope: "project",
          record: {
            id: "mem-a1",
            key: "project.name",
            value: "项目 A 迟到数据",
            kind: "convention",
            status: "active",
            authority: "explicit",
            updated_at: "2026-11-19",
          },
        },
      ]);
      await Promise.resolve();
    });

    expect(screen.queryByText("项目 A 迟到数据")).not.toBeInTheDocument();
    expect(screen.getByText("项目 B 专属约定")).toBeInTheDocument();
    expect(onMemoryChange).not.toHaveBeenCalled();
  });

  it("positions the memory drawer below the 48px Windows titleBarOverlay zone", async () => {
    const fsModule = "node:fs";
    const pathModule = "node:path";
    const fs = await import(/* @vite-ignore */ fsModule) as {
      readFileSync: (file: string, encoding: "utf8") => string;
    };
    const path = await import(/* @vite-ignore */ pathModule) as {
      resolve: (...parts: string[]) => string;
    };
    const stylesCss = fs.readFileSync(path.resolve("src/renderer/styles.css"), "utf8");
    const backdropBlock = stylesCss.match(/\.drawer-backdrop\s*\{[^}]*\}/);
    expect(backdropBlock).not.toBeNull();
    expect(backdropBlock?.[0]).toMatch(/inset:\s*48px\s+0\s+0\s+0\s*;/);
  });

  it("includes workspacePath and sessionId on memory write RPCs", async () => {
    render(
      <MemoryDrawer
        open
        workspaceKey={JSON.stringify(["E:\\code\\project-a", "project-a"])}
        workspacePath={"E:\\code\\project-a"}
        sessionId="session-a"
        memory={baseMemory()}
        disabled={false}
        onClose={onClose}
        onMemoryChange={onMemoryChange}
      />,
    );
    await screen.findByText("使用简洁中文回复");

    fireEvent.click(screen.getByRole("button", { name: /清空项目记忆/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认清空项目" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("memory.clear", {
      allScopes: false,
      confirmed: true,
      workspacePath: "E:\\code\\project-a",
      sessionId: "session-a",
    }));
  });
});
