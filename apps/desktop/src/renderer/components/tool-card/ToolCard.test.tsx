import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ToolCard,
  ToolRendererRegistry,
  classifyToolName,
  parseUnifiedDiff,
  type ToolCardData,
  type ToolRendererProps,
} from "./ToolCard";

afterEach(cleanup);

function tool(overrides: Partial<ToolCardData> = {}): ToolCardData {
  return {
    id: "tool-1",
    name: "bash",
    args: { command: "pnpm test", cwd: "E:\\code\\Coding-agent" },
    status: "done",
    result: { content: [{ type: "text", text: "all tests passed" }] },
    ...overrides,
  };
}

describe("tool renderer registry", () => {
  it.each([
    ["bash", "shell"],
    ["exec_command", "shell"],
    ["read", "read"],
    ["filesystem_read_file", "read"],
    ["write", "write"],
    ["apply_patch", "write"],
    ["grep", "search"],
    ["glob", "search"],
    ["mcp_custom_action", "generic"],
  ] as const)("classifies %s as %s", (name, expected) => {
    expect(classifyToolName(name)).toBe(expected);
  });

  it("allows a product-specific renderer to override the generic fallback", () => {
    function CustomRenderer(_props: ToolRendererProps) {
      return <div>custom jira card</div>;
    }
    const registry = new ToolRendererRegistry();
    const unregister = registry.register({
      id: "jira",
      category: "generic",
      matches: (name) => name === "jira.create_issue",
      component: CustomRenderer,
      priority: 500,
    });

    expect(registry.resolve("jira.create_issue").id).toBe("jira");
    unregister();
    expect(registry.resolve("jira.create_issue").id).toBe("generic");
  });
});

describe("ToolCard", () => {
  it("shows shell command and cwd and can collapse its output", () => {
    render(<ToolCard tool={tool()} />);

    expect(screen.getByText("pnpm test")).toBeInTheDocument();
    expect(screen.getByText("E:\\code\\Coding-agent")).toBeInTheDocument();
    expect(screen.getByTestId("shell-output")).toHaveTextContent("all tests passed");

    fireEvent.click(screen.getByRole("button", { name: "收起输出" }));
    expect(screen.queryByTestId("shell-output")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开输出" }));
    expect(screen.getByTestId("shell-output")).toBeInTheDocument();
  });

  it("visually truncates long shell output until explicitly expanded", () => {
    const output = Array.from({ length: 30 }, (_, index) => `line-${index}-${"x".repeat(20)}`).join("\n");
    render(
      <ToolCard
        tool={tool({ result: output })}
        outputLimit={{ maxCharacters: 180, maxLines: 8 }}
      />,
    );

    expect(screen.getByTestId("shell-output")).toHaveTextContent("可视截断");
    fireEvent.click(screen.getByRole("button", { name: "显示完整输出" }));
    expect(screen.getByTestId("shell-output")).toHaveTextContent("line-29");
    expect(screen.getByTestId("shell-output")).not.toHaveTextContent("可视截断");
  });

  it("extracts edit details.patch and assigns semantic diff line classes", () => {
    const patch = [
      "--- src/example.ts",
      "+++ src/example.ts",
      "@@ -1,3 +1,3 @@",
      " const before = true;",
      "-const answer = 41;",
      "+const answer = 42;",
    ].join("\n");
    const { container } = render(<ToolCard tool={tool({
      name: "edit",
      args: { path: "src/example.ts", edits: [{ oldText: "41", newText: "42" }] },
      result: {
        content: [{ type: "text", text: "Successfully replaced 1 block." }],
        details: { patch },
      },
    })} />);

    expect(screen.getByText("src/example.ts")).toBeInTheDocument();
    expect(container.querySelector(".tool-card-view__diff-line--deletion")).toHaveTextContent("-const answer = 41;");
    expect(container.querySelector(".tool-card-view__diff-line--addition")).toHaveTextContent("+const answer = 42;");
    expect(container.querySelector(".tool-card-view__diff-line--context")).toHaveTextContent("const before = true;");
    expect(container.querySelector(".tool-card-view__diff-line--hunk")).toHaveTextContent("@@ -1,3 +1,3 @@");
  });

  it("generates a unified diff preview for a write before a result exists", () => {
    const { container } = render(<ToolCard tool={tool({
      name: "write",
      status: "approval",
      args: { path: "src/new.ts", content: "export const ready = true;\n" },
      result: undefined,
    })} />);

    expect(screen.getByText("根据工具参数生成预览")).toBeInTheDocument();
    expect(container.querySelector(".tool-card-view__diff-line--meta")).toHaveTextContent("--- /dev/null");
    expect(container.querySelector(".tool-card-view__diff-line--addition")).toHaveTextContent("+export const ready = true;");
  });

  it("preserves approve and reject callbacks", () => {
    const onResolveApproval = vi.fn();
    const approval = {
      approvalId: "approval-1",
      toolCallId: "tool-1",
      toolName: "write",
      args: { path: "a.ts" },
    };
    render(<ToolCard
      tool={tool({ name: "write", status: "approval", approval })}
      onResolveApproval={onResolveApproval}
    />);

    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    fireEvent.click(screen.getByRole("button", { name: "允许一次" }));
    expect(onResolveApproval).toHaveBeenNthCalledWith(1, approval, false);
    expect(onResolveApproval).toHaveBeenNthCalledWith(2, approval, true);
  });
});

describe("parseUnifiedDiff", () => {
  it("distinguishes headers from additions and deletions", () => {
    const lines = parseUnifiedDiff("--- a/x\n+++ b/x\n-old\n+new\n same");
    expect(lines.map((line) => line.kind)).toEqual([
      "meta",
      "meta",
      "deletion",
      "addition",
      "context",
    ]);
  });
});
