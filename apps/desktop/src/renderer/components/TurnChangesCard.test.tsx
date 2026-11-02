import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TurnChangesSummary } from "../../shared/workbench";
import { TurnChangesCard } from "./TurnChangesCard";

afterEach(cleanup);

const summary = (overrides: Partial<TurnChangesSummary> = {}): TurnChangesSummary => ({
  runId: "run-first", entryId: "entry-first", afterMessageIndex: 2, timestamp: 100,
  additions: 12, deletions: 5, truncated: false,
  files: [
    { path: "src/app.ts", status: "M", additions: 8, deletions: 2, binary: false },
    { path: "src/new.ts", status: "A", additions: 4, deletions: 0, binary: false },
    { path: "assets/avatar.png", status: "M", additions: 0, deletions: 0, binary: true },
    { path: "src/old.ts", status: "D", additions: 0, deletions: 3, binary: false },
  ], ...overrides,
});

describe("turn changes card", () => {
  it("previews three files and routes the card or a file to its exact recorded run", () => {
    const view = vi.fn();
    render(<TurnChangesCard change={summary()} onView={view} />);
    expect(screen.getByRole("region", { name: "本轮变更 4 个文件" })).toBeInTheDocument();
    expect(screen.getByLabelText("新增 12 行，删除 5 行")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByText("src/old.ts")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看变更" }));
    expect(view).toHaveBeenLastCalledWith("run-first");
    fireEvent.click(screen.getByRole("button", { name: "查看 src/app.ts 的变更" }));
    expect(view).toHaveBeenLastCalledWith("run-first", "src/app.ts");
    expect(screen.queryByRole("button", { name: /撤销/ })).not.toBeInTheDocument();
  });

  it("expands remaining files and can review a deleted file without invoking file-open", () => {
    const view = vi.fn();
    render(<TurnChangesCard change={summary()} onView={view} />);
    const expand = screen.getByRole("button", { name: "再显示 1 个文件" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(expand);
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getByRole("button", { name: "收起文件" })).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name: "查看 src/old.ts 的变更" }));
    expect(view).toHaveBeenCalledWith("run-first", "src/old.ts");
    fireEvent.click(screen.getByRole("button", { name: "收起文件" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("does not invent line counts for binary files and indicates incomplete evidence", () => {
    const file = { path: "file.bin", status: "M", additions: 0, deletions: 0, binary: true };
    render(<TurnChangesCard change={summary({ files: [file], additions: 0, deletions: 0, truncated: true })} onView={vi.fn()} />);
    expect(screen.getByText("二进制文件变更")).toBeInTheDocument();
    expect(within(screen.getByRole("listitem")).getByText("二进制")).toBeInTheDocument();
    expect(screen.queryByText("+0")).not.toBeInTheDocument();
    expect(screen.getByText(/变更预览不完整/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /再显示/ })).not.toBeInTheDocument();
  });

  it("does not carry expansion into a different turn and omits empty change sets", () => {
    const { rerender } = render(<TurnChangesCard change={summary()} onView={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "再显示 1 个文件" }));
    rerender(<TurnChangesCard change={summary({ runId: "run-second" })} onView={vi.fn()} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "再显示 1 个文件" })).toHaveAttribute("aria-expanded", "false");
    rerender(<TurnChangesCard change={summary({ files: [] })} onView={vi.fn()} />);
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
  });
});
