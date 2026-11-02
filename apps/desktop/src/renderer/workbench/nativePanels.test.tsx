import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BrowserSnapshot, WorkbenchNativeEvent } from "../../shared/workbench";
import type { WorkspacePayload } from "../../shared/types";
import { BrowserPanel } from "./nativePanels";
import { Workbench } from "./Workbench";

const scope = { workspacePath: "G:\\project", sessionId: "main" };
const listeners = new Set<(event: WorkbenchNativeEvent) => void>();
const request = vi.fn();
const snapshot = (key: string, url = "https://example.test/"): BrowserSnapshot => ({ ...scope, key, url, title: "Example", loading: false, canGoBack: false, canGoForward: false, error: null });
beforeEach(() => {
  listeners.clear();
  request.mockReset().mockImplementation(async (_method: string, params: { key: string }) => snapshot(params.key));
  window.desktop = { chooseWorkspace: async () => null, getBootstrap: async () => ({ platform: "win32", defaultWorkspace: scope.workspacePath }),
    workbenchRequest: request, onWorkbenchEvent: (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; } };
});
afterEach(cleanup);

it("ignores a retired StrictMode open response and closes only its own browser lease", async () => {
  const pending: { key: string; resolve: (value: BrowserSnapshot) => void }[] = [];
  request.mockImplementation((method: string, params: { key: string }) => method === "browser.open" ? new Promise((resolve) => pending.push({ key: params.key, resolve })) : Promise.resolve(snapshot(params.key)));
  const view = render(<StrictMode><BrowserPanel scope={scope} active /></StrictMode>);
  expect(pending).toHaveLength(2);
  expect(pending[0].key).not.toBe(pending[1].key);
  expect(request).toHaveBeenCalledWith("browser.close", { ...scope, key: pending[0].key });
  await act(async () => { pending[1].resolve(snapshot(pending[1].key)); });
  expect(screen.getByLabelText("网页地址")).toHaveValue("https://example.test/");
  await act(async () => { pending[0].resolve(snapshot(pending[0].key, "https://retired.test/")); });
  expect(screen.getByLabelText("网页地址")).toHaveValue("https://example.test/");
  view.unmount();
  expect(request).toHaveBeenCalledWith("browser.close", { ...scope, key: pending[1].key });
});

it("hides bounds when inactive, then restores the same owned browser", async () => {
  const view = render(<BrowserPanel scope={scope} active />);
  await waitFor(() => expect(request.mock.calls.some(([method, params]) => method === "browser.bounds" && params.visible === true)).toBe(true));
  const key = request.mock.calls.find(([method]) => method === "browser.open")![1].key;
  view.rerender(<BrowserPanel scope={scope} active={false} />);
  await waitFor(() => expect(request).toHaveBeenCalledWith("browser.bounds", expect.objectContaining({ ...scope, key, visible: false })));
  expect(request.mock.calls.filter(([method]) => method === "browser.close")).toHaveLength(0);
  view.rerender(<BrowserPanel scope={scope} active />);
  expect(request.mock.calls.filter(([method]) => method === "browser.open")).toHaveLength(1);
});

it("hides the native browser for the usage drawer and restores its existing page afterwards", async () => {
  const props = {
    workspace: { path: scope.workspacePath, sessionId: scope.sessionId } as WorkspacePayload,
    open: true, onClose: vi.fn(), width: 480, onWidth: vi.fn(),
  };
  const view = render(<Workbench {...props} />);
  fireEvent.click(screen.getByRole("button", { name: /^浏览器/ }));
  await waitFor(() => expect(request.mock.calls.some(([method, params]) => method === "browser.bounds" && params.visible === true)).toBe(true));
  const key = request.mock.calls.find(([method]) => method === "browser.open")![1].key;
  view.rerender(<Workbench {...props} suspended />);
  await waitFor(() => expect(request.mock.calls.filter(([method]) => method === "browser.bounds").at(-1)?.[1])
    .toEqual(expect.objectContaining({ ...scope, key, visible: false })));
  expect(screen.getByLabelText("网页地址")).toHaveValue("https://example.test/");
  expect(request.mock.calls.filter(([method]) => method === "browser.close")).toHaveLength(0);
  view.rerender(<Workbench {...props} />);
  await waitFor(() => expect(request.mock.calls.filter(([method]) => method === "browser.bounds").at(-1)?.[1])
    .toEqual(expect.objectContaining({ ...scope, key, visible: true })));
  expect(request.mock.calls.filter(([method]) => method === "browser.open")).toHaveLength(1);
});

it("preserves an edited address and ignores browser events for a retired lease", async () => {
  render(<BrowserPanel scope={scope} active />);
  const input = screen.getByLabelText("网页地址");
  await waitFor(() => expect(input).toHaveValue("https://example.test/"));
  const key = request.mock.calls.find(([method]) => method === "browser.open")![1].key;
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "localhost:3000" } });
  act(() => { listeners.forEach((listener) => listener({ ...scope, type: "browser.changed", browser: snapshot(key, "https://redirect.test/") })); });
  expect(input).toHaveValue("localhost:3000");
  fireEvent.blur(input);
  act(() => { listeners.forEach((listener) => listener({ ...scope, type: "browser.changed", browser: snapshot("retired", "https://retired.test/") })); });
  expect(input).toHaveValue("localhost:3000");
  fireEvent.click(screen.getByRole("button", { name: "前往" }));
  await waitFor(() => expect(request).toHaveBeenCalledWith("browser.navigate", { ...scope, key, url: "localhost:3000" }));
});
