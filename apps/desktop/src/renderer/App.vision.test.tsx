import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, WorkspacePayload } from "../shared/types";
import { App } from "./App";

const pngData = "iVBORw==";
const image = { data: pngData, mime_type: "image/png" };
const file = () => new File([new Uint8Array([137, 80, 78, 71])], "screen.png", { type: "image/png" });
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const baseWorkspace = (): WorkspacePayload => ({
  path: "G:\\Coding-agent", sessionId: "session-1",
  model: { id: "deepseek-flash", name: "Flash", provider: "deepseek", input: ["text", "image"] },
  thinkingLevel: null, tools: [], messages: [], collaborationMode: "default",
  planState: { phase: "idle", activePlanId: null, latestRevision: null, pendingQuestion: null },
  memory: { enabled: false, userId: null, projectId: null },
});

describe("desktop file and image input", () => {
  const request = vi.fn();
  let current: WorkspacePayload;
  let listener: ((event: RuntimeEvent) => void) | undefined;
  let sequence = 0;
  const emit = async (type: string, payload: Record<string, unknown> = {}) => {
    await act(async () => {
      listener?.({ v: 1, type: "event", seq: ++sequence, timestamp: sequence,
        sessionId: current.sessionId, runId: "run-1", event: { type, payload } });
      await new Promise(requestAnimationFrame);
    });
  };
  const mount = () => render(<VirtuosoMockContext.Provider value={{ viewportHeight: 900, itemHeight: 120 }}>
    <App />
  </VirtuosoMockContext.Provider>);
  const selectImage = async () => {
    await screen.findByRole("button", { name: "＋ 添加文件" });
    await waitFor(() => expect(screen.getByLabelText("选择文件")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("选择文件"), { target: { files: [file()] } });
    await screen.findByRole("img", { name: "screen.png" });
    await waitFor(() => expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled());
  };

  beforeEach(() => {
    current = baseWorkspace();
    sequence = 0;
    listener = undefined;
    request.mockReset();
    request.mockImplementation(async (method: string) => {
      if (method === "workspace.open" || method === "session.snapshot") return current;
      if (method === "session.list") return [{ id: "session-2", name: "Other session", message_count: 0 }];
      if (method === "session.open" || method === "session.new") return { ...current, sessionId: "session-2", messages: [] };
      if (method === "command.list") return [];
      if (method === "run.start") return { accepted: true, runId: "run-1" };
      return {};
    });
    window.agent = { request, onEvent: (cb) => { listener = cb; return () => undefined; }, onStatus: () => () => undefined };
    window.desktop = { chooseWorkspace: async () => null,
      getBootstrap: async () => ({ defaultWorkspace: "G:\\Coding-agent", platform: "win32" }) };
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
    else Reflect.deleteProperty(navigator, "clipboard");
  });

  it("sends image-only input without verbose hints and uses admission events once", async () => {
    let resolve: ((value: { runId: string }) => void) | undefined;
    const original = request.getMockImplementation()!;
    request.mockImplementation((method, params) => method === "run.start"
      ? new Promise<{ runId: string }>((done) => { resolve = done; }) : original(method, params));
    mount();
    await selectImage();
    expect(screen.queryByText(/图片将上传至当前模型供应商/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
    expect(request).toHaveBeenCalledWith("run.start", { text: "", images: [image] });
    expect(screen.queryByRole("img", { name: "附件图片 1" })).not.toBeInTheDocument();
    await emit("run.started", { text: "", images: [image] });
    await emit("run.started", { text: "", images: [image] });
    expect(screen.getAllByRole("img", { name: "附件图片 1" })).toHaveLength(1);
    await act(async () => { resolve?.({ runId: "run-1" }); });
    expect(screen.queryByRole("region", { name: "待发送附件" })).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "附件图片 1" })).toHaveAttribute("src", `data:image/png;base64,${pngData}`);
  });

  it("keeps the image and text for retry when run.start rejects without adding a user message", async () => {
    const original = request.getMockImplementation()!;
    request.mockImplementation((method, params) => method === "run.start"
      ? Promise.reject(new Error("图片校验失败")) : original(method, params));
    mount();
    await selectImage();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "解释截图" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("图片校验失败");
    expect(screen.getByRole("img", { name: "screen.png" })).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("解释截图");
    expect(screen.queryByRole("img", { name: "附件图片 1" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled();
  });

  it.each(["before", "after"])("restores retry attachments on async run.failed %s the RPC reply", async (order) => {
    let resolve: ((value: { runId: string }) => void) | undefined;
    const original = request.getMockImplementation()!;
    request.mockImplementation((method, params) => method === "run.start"
      ? new Promise<{ runId: string }>((done) => { resolve = done; }) : original(method, params));
    mount();
    await selectImage();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "解释截图" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
    await emit("run.started", { text: "解释截图", images: [image] });
    if (order === "after") await act(async () => { resolve?.({ runId: "run-1" }); });
    await emit("run.failed", { message: "模型连接失败" });
    if (order === "before") await act(async () => { resolve?.({ runId: "run-1" }); });
    expect(screen.getByRole("img", { name: "screen.png" })).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("解释截图");
    expect(screen.getAllByRole("img", { name: "附件图片 1" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled();
  });

  it.each(["paste", "drop"])("accepts %s images and lets the user remove them", async (kind) => {
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    if (kind === "paste") fireEvent.paste(textarea, { clipboardData: { files: [file()] } });
    else fireEvent.drop(textarea, { dataTransfer: { files: [file()] } });
    await screen.findByRole("img", { name: "screen.png" });
    fireEvent.click(screen.getByRole("button", { name: "移除图片 screen.png" }));
    expect(screen.queryByRole("img", { name: "screen.png" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
  });

  it("accepts clipboard image items when the files list is empty", async () => {
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    const imageFile = file();
    const getAsFile = vi.fn(() => imageFile);
    fireEvent.paste(textarea, { clipboardData: {
      files: [],
      items: [{ kind: "file", type: "image/png", getAsFile }],
      types: ["Files"],
    } });
    expect(await screen.findByRole("img", { name: "screen.png" })).toBeInTheDocument();
    expect(getAsFile).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled();
  });

  it("does not intercept ordinary text clipboard paste", async () => {
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    const getAsFile = vi.fn(() => null);
    const defaultAllowed = fireEvent.paste(textarea, { clipboardData: {
      files: [],
      items: [{ kind: "string", type: "text/plain", getAsFile }],
      types: ["text/plain"],
      getData: () => "ordinary text",
    } });
    expect(defaultAllowed).toBe(true);
    expect(getAsFile).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "待发送附件" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
  });

  it("waits for a pending clipboard fallback before sending text and image together", async () => {
    type ClipboardImage = { types: string[]; getType: () => Promise<Blob> };
    let finishRead: ((items: ClipboardImage[]) => void) | undefined;
    const read = vi.fn(() => new Promise<ClipboardImage[]>((done) => { finishRead = done; }));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { read } });
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(textarea, { target: { value: "识别一下" } });
    fireEvent.keyDown(textarea, { key: "v", ctrlKey: true });
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
    await waitFor(() => expect(read).toHaveBeenCalledOnce());
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
    await act(async () => { finishRead?.([{ types: ["image/png"], getType: async () => file() }]); });
    await screen.findByRole("img", { name: "clipboard-1.png" });
    await waitFor(() => expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled());
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(request).toHaveBeenCalledWith("run.start", { text: "识别一下", images: [image] });
  });

  it("allows ordinary text submission after native text paste cancels its pending fallback", async () => {
    const read = vi.fn();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { read } });
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(textarea, { target: { value: "普通文本" } });
    fireEvent.keyDown(textarea, { key: "v", ctrlKey: true });
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
    expect(fireEvent.paste(textarea, { clipboardData: {
      files: [], items: [], types: ["text/plain"], getData: () => "普通文本",
    } })).toBe(true);
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled();
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() => expect(request).toHaveBeenCalledWith("run.start", { text: "普通文本" }));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 120)); });
    expect(read).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not duplicate an image present in both clipboard items and files", async () => {
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    const imageFile = file();
    fireEvent.paste(textarea, { clipboardData: {
      files: [imageFile],
      items: [{ kind: "file", type: "image/png", getAsFile: () => imageFile }],
      types: ["Files"],
    } });
    expect(await screen.findByRole("img", { name: "screen.png" })).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "screen.png" })).toHaveLength(1);
  });

  it.each(["switch", "new"])("clears old-session attachments on %s", async (kind) => {
    mount();
    await selectImage();
    fireEvent.click(kind === "switch" ? await screen.findByRole("button", { name: /Other session/ }) : screen.getByTitle("新建会话"));
    await waitFor(() => expect(screen.queryByRole("img", { name: "screen.png" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
  });

  it("uses authoritative model capabilities without dropping pending attachments", async () => {
    mount();
    await selectImage();
    await emit("model.changed", { id: "text", name: "Text only", provider: "test", input: ["text"] });
    expect(screen.getByRole("button", { name: "＋ 添加文件" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeDisabled();
    expect(screen.getByRole("img", { name: "screen.png" })).toBeInTheDocument();
    await emit("model.changed", current.model);
    expect(screen.getByRole("button", { name: "发送 ↑" })).toBeEnabled();
  });

  it.each(["resolved", "rejected"])("ignores a model selection %s after switching sessions", async (outcome) => {
    const staleModel = { id: "stale-text", name: "Stale text model", provider: "stale-provider", input: ["text"] };
    const targetModel = { ...current.model, name: "New session vision" };
    let finishSelection: ((value: typeof staleModel) => void) | undefined;
    let failSelection: ((error: Error) => void) | undefined;
    const original = request.getMockImplementation()!;
    request.mockImplementation((method, params) => {
      if (method === "command.list") return Promise.resolve([{ name: "model", label: "切换模型", description: "选择模型" }]);
      if (method === "model.list") return Promise.resolve([{ ...staleModel, current: false }]);
      if (method === "model.select") return new Promise<typeof staleModel>((resolve, reject) => {
        finishSelection = resolve;
        failSelection = reject;
      });
      if (method === "session.open") return Promise.resolve({ ...current, sessionId: "session-2", model: targetModel });
      return original(method, params);
    });
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    fireEvent.change(textarea, { target: { value: "/model" } });
    fireEvent.click(await screen.findByRole("button", { name: /切换模型/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Stale text model/ }));
    expect(request).toHaveBeenCalledWith("model.select", { modelId: staleModel.id, provider: staleModel.provider });
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await screen.findByText("New session vision");
    await act(async () => {
      if (outcome === "resolved") finishSelection?.(staleModel);
      else failSelection?.(new Error("stale model failure"));
    });
    expect(screen.getByText("New session vision")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "＋ 添加文件" })).toBeEnabled();
    expect(screen.queryByText("Stale text model")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not leak delayed old-session image events into the selected session", async () => {
    mount();
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await waitFor(() => expect(screen.getByRole("textbox")).toBeEnabled());
    await emit("run.started", { text: "old-session image", images: [image] });
    expect(screen.queryByText("old-session image")).not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "附件图片 1" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "■ 停止" })).not.toBeInTheDocument();
  });

  it("shows persisted image-only messages on startup and after switching sessions", async () => {
    current.messages = [{ role: "user", content: [{ type: "image", ...image }], timestamp: 1 }];
    const original = request.getMockImplementation()!;
    request.mockImplementation((method, params) => method === "session.open"
      ? Promise.resolve({ ...current, sessionId: "session-2" }) : original(method, params));
    mount();
    expect(await screen.findByRole("img", { name: "附件图片 1" })).toHaveAttribute("src", `data:image/png;base64,${pngData}`);
    fireEvent.click(await screen.findByRole("button", { name: /Other session/ }));
    await waitFor(() => expect(screen.getByRole("img", { name: "附件图片 1" })).toBeInTheDocument());
  });

  it("rejects image paste for a text-only model and treats SVG as a file without rendering it", async () => {
    current.model.input = ["text"];
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    expect(screen.getByRole("button", { name: "＋ 添加文件" })).toBeEnabled();
    fireEvent.paste(textarea, { clipboardData: { files: [file()] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("当前模型不支持图片输入");
    await emit("model.changed", { ...current.model, input: ["text", "image"] });
    fireEvent.change(screen.getByLabelText("选择文件"), { target: { files: [new File(["x"], "bad.svg", { type: "image/svg+xml" })] } });
    expect(await screen.findByRole("button", { name: "移除文件 bad.svg" })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "bad.svg" })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalledWith("run.start", expect.anything());
  });

  it("selects and sends a file without text or a vision model, then displays the admitted file once", async () => {
    current.model.input = ["text"];
    mount();
    await screen.findByLabelText("选择文件");
    const selected = new File(["hello"], "说明.md", { type: "text/markdown" });
    fireEvent.change(screen.getByLabelText("选择文件"), { target: { files: [selected] } });
    await screen.findByRole("button", { name: "移除文件 说明.md" });
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
    expect(request).toHaveBeenCalledWith("run.start", { text: "", files: [{ name: "说明.md", mime_type: "text/markdown", data: btoa("hello") }] });
    const metadata = { name: "说明.md", mime_type: "text/markdown", size: 5, path: "G:/managed/file.md" };
    await emit("run.started", { text: "", files: [metadata] });
    await emit("run.started", { text: "", files: [metadata] });
    expect(document.querySelectorAll(".message-file")).toHaveLength(1);
    expect(document.querySelector(".message-file")).toHaveTextContent("说明.md");
  });

  it("pastes mixed attachments once and restores both on an asynchronous run failure", async () => {
    mount();
    const textarea = await screen.findByPlaceholderText("描述你想完成的任务…");
    const textFile = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.paste(textarea, { clipboardData: { types: ["Files"], files: [file(), textFile], items: [
      { kind: "file", type: "image/png", getAsFile: file },
      { kind: "file", type: "text/plain", getAsFile: () => textFile },
    ] } });
    await screen.findByRole("button", { name: "移除文件 notes.txt" });
    expect(screen.getAllByRole("img", { name: "screen.png" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "发送 ↑" }));
    expect(request).toHaveBeenCalledWith("run.start", { text: "", images: [image], files: [{ name: "notes.txt", mime_type: "text/plain", data: btoa("hello") }] });
    await waitFor(() => expect(screen.queryByRole("button", { name: "移除文件 notes.txt" })).not.toBeInTheDocument());
    await emit("run.failed", { message: "file request failed" });
    expect(screen.getByRole("button", { name: "移除文件 notes.txt" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "screen.png" })).toBeInTheDocument();
  });

  it("restores persisted file cards and hides only generated file context blocks", async () => {
    const metadata = { name: "history.json", mime_type: "application/json", size: 2, path: "G:/managed/history.json" };
    current.messages = [{ role: "user", timestamp: 1, attachments: [metadata], content: [
      { type: "text", text: "看看这个文件" },
      { type: "text", text: `<attached_file>\n${JSON.stringify(metadata)}\nprivate file body\n</attached_file>` },
    ] }];
    mount();
    await screen.findByText("history.json");
    expect(screen.getByText("看看这个文件")).toBeInTheDocument();
    expect(screen.queryByText(/private file body/)).not.toBeInTheDocument();
    expect(document.querySelector(".message-file")).toHaveTextContent("2 B");
  });
});
