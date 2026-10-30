import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";
import { UsageAnalysisLauncher } from "./UsageAnalysisLauncher";

const moduleState = vi.hoisted(() => ({
  loaded: vi.fn(), mounted: vi.fn(), fail: false, gate: undefined as Promise<void> | undefined,
}));

vi.mock("./UsageAnalysisDrawer", async () => {
  moduleState.loaded();
  await moduleState.gate;
  return { default: ({ open, sessionId, onClose, onAfterClose }: {
    open: boolean; sessionId: string; onClose: () => void; onAfterClose?: () => void;
  }) => {
    useEffect(() => { moduleState.mounted(); }, []);
    if (moduleState.fail) throw new Error("drawer load failure");
    return open ? <section role="dialog" aria-label="会话用量分析">
      <p>Analysis for {sessionId}</p>
      <button type="button" aria-label="关闭用量分析" onClick={() => { onClose(); onAfterClose?.(); }}>关闭</button>
    </section> : null;
  } };
});

beforeEach(() => { moduleState.fail = false; moduleState.mounted.mockClear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("usage analysis entry", () => {
  it("loads the drawer only after opening, allows cancelling loading and reopens without another module load", async () => {
    let release!: () => void;
    moduleState.gate = new Promise<void>((resolve) => { release = resolve; });
    render(<UsageAnalysisLauncher sessionId="session-1" workspacePath="G:/project" stats={{
      tokens: { input: 200, cache_read: 800, cache_write: 0, output: 250, total: 1250 }, cost: .0042,
      metrics: { cache_hit_rate: 80, input_tokens: 1000, context_tokens: 5000, context_window: 10000,
        context_percent: 50, context_estimated: false },
    }} />);
    const trigger = screen.getByRole("button", { name: "查看用量分析" });
    expect(trigger).toHaveAttribute("type", "button");
    expect(moduleState.loaded).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(trigger);
    await screen.findByText("正在加载用量分析…");
    const loading = screen.getByRole("dialog");
    expect(loading).toHaveTextContent("累计输入");
    expect(loading).toHaveTextContent("1K");
    expect(loading).toHaveTextContent("80%");
    expect(loading).toHaveTextContent("5,000");
    expect(loading.querySelector(".usage-analysis-skeleton")).toBeInTheDocument();
    await waitFor(() => expect(moduleState.loaded).toHaveBeenCalledOnce());
    fireEvent.keyDown(screen.getByRole("button", { name: "关闭用量分析" }), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    await act(async () => { release(); });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(trigger);
    expect(await screen.findByText("Analysis for session-1")).toBeInTheDocument();
    expect(moduleState.mounted).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "关闭用量分析" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(moduleState.loaded).toHaveBeenCalledOnce();
    fireEvent.click(trigger);
    expect(await screen.findByText("Analysis for session-1")).toBeInTheDocument();
    expect(moduleState.mounted).toHaveBeenCalledOnce();
  });

  it("closes the previous conversation when the keyed workspace/session changes", async () => {
    const { rerender } = render(<UsageAnalysisLauncher key="project:session-1"
      sessionId="session-1" workspacePath="G:/project" />);
    fireEvent.click(screen.getByRole("button", { name: "查看用量分析" }));
    await screen.findByText("Analysis for session-1");
    rerender(<UsageAnalysisLauncher key="project:session-2" sessionId="session-2" workspacePath="G:/project" />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看用量分析" }));
    expect(await screen.findByText("Analysis for session-2")).toBeInTheDocument();
    expect(screen.queryByText("Analysis for session-1")).not.toBeInTheDocument();
  });

  it("keeps the summary without offering an unbound request before a workspace is open", () => {
    render(<UsageAnalysisLauncher />);
    expect(screen.getByLabelText("会话用量指标")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看用量分析" })).not.toBeInTheDocument();
  });

  it("reports modal visibility to native workbench surfaces and clears it on close or navigation", async () => {
    const visibility = vi.fn();
    const view = render(<UsageAnalysisLauncher sessionId="session-1" workspacePath="G:/project" onOpenChange={visibility} />);
    fireEvent.click(screen.getByRole("button", { name: "查看用量分析" }));
    expect(visibility).toHaveBeenLastCalledWith(true);
    await screen.findByText("Analysis for session-1");
    fireEvent.click(screen.getByRole("button", { name: "关闭用量分析" }));
    expect(visibility).toHaveBeenLastCalledWith(false);
    fireEvent.click(screen.getByRole("button", { name: "查看用量分析" }));
    await screen.findByText("Analysis for session-1");
    expect(visibility).toHaveBeenLastCalledWith(true);
    view.unmount();
    expect(visibility).toHaveBeenLastCalledWith(false);
  });

  it("contains module/render failures and lets the user retry without losing the chat", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    moduleState.fail = true;
    render(<UsageAnalysisLauncher sessionId="session-1" workspacePath="G:/project" />);
    fireEvent.click(screen.getByRole("button", { name: "查看用量分析" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法打开用量分析，请重试。");
    moduleState.fail = false;
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("Analysis for session-1")).toBeInTheDocument();
  });
});
