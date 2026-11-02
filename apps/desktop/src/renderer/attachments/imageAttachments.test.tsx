import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clipboardImageFiles, imageDataUrl, MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES, readImageFile, validateImageFiles,
  clipboardAttachmentFiles, MAX_FILE_BYTES, MAX_TOTAL_ATTACHMENT_BYTES, readAttachmentFile, validateAttachmentFiles,
  type PendingImage,
} from "./imageAttachments";
import { useImageAttachments } from "./useImageAttachments";

const png = () => new File([new Uint8Array([137, 80, 78, 71])], "screen.png", { type: "image/png" });
const pending = (size = 4): PendingImage => ({ id: "i", name: "screen.png", size,
  data: "iVBORw==", mime_type: "image/png" });
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const clipboardItem = (type = "image/png") => ({ types: [type],
  getType: vi.fn().mockResolvedValue(new Blob([new Uint8Array([137, 80, 78, 71])], { type })) });
const mockClipboard = (read = vi.fn().mockResolvedValue([clipboardItem()])) => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { read } });
  return read;
};
const mockLocalReads = () => vi.spyOn(FileReader.prototype, "readAsDataURL")
  .mockImplementation(function (this: FileReader) {
    Object.defineProperty(this, "result", { configurable: true, value: "data:image/png;base64,iVBORw==" });
    this.onload?.call(this, new ProgressEvent("load") as ProgressEvent<FileReader>);
  });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
  else Reflect.deleteProperty(navigator, "clipboard");
});

describe("image attachments", () => {
  it("reads bytes locally as bare base64 and builds a safe data preview", async () => {
    const image = await readImageFile(png());
    expect(image).toMatchObject({ name: "screen.png", size: 4, data: "iVBORw==", mime_type: "image/png" });
    expect(imageDataUrl(image)).toBe("data:image/png;base64,iVBORw==");
  });

  it.each(["image/svg+xml", "text/plain", ""])("rejects unsupported MIME %s", (type) => {
    expect(() => validateImageFiles([new File(["x"], "bad.png", { type })], [])).toThrow("仅支持");
    expect(imageDataUrl({ data: "eA==", mime_type: type })).toBeNull();
  });

  it("rejects empty images, per-file limits, count and aggregate bytes before reading", () => {
    expect(() => validateImageFiles([new File([], "empty.png", { type: "image/png" })], [])).toThrow("空图片");
    const huge = png();
    Object.defineProperty(huge, "size", { value: MAX_IMAGE_BYTES + 1 });
    expect(() => validateImageFiles([huge], [])).toThrow("5 MiB");
    expect(() => validateImageFiles([png()], Array.from({ length: 4 }, () => pending()))).toThrow("4 张");
    expect(() => validateImageFiles([png()], [pending(MAX_TOTAL_IMAGE_BYTES - 3)])).toThrow("12 MiB");
    expect(imageDataUrl({ data: "https://evil.example/image.png", mime_type: "image/png" })).toBeNull();
  });

  it("does not put a delayed old-session read into a new session", async () => {
    let reader: FileReader | undefined;
    vi.spyOn(FileReader.prototype, "readAsDataURL").mockImplementation(function (this: FileReader) { reader = this; });
    const onError = vi.fn();
    const { result, rerender } = renderHook(({ owner }) => useImageAttachments(owner, true, onError),
      { initialProps: { owner: "session-a" } });
    let read: Promise<void> | undefined;
    act(() => { read = result.current.addFiles([png()]); });
    expect(result.current.reading).toBe(true);
    act(() => { result.current.clear(); });
    rerender({ owner: "session-b" });
    await act(async () => {
      Object.defineProperty(reader, "result", { value: "data:image/png;base64,iVBORw==" });
      reader?.onload?.call(reader, new ProgressEvent("load") as ProgressEvent<FileReader>);
      await read;
    });
    expect(result.current.images).toEqual([]);
    expect(result.current.reading).toBe(false);
    expect(onError).not.toHaveBeenCalled();
  });

  it("preserves valid pending images when another selection is invalid", async () => {
    const onError = vi.fn();
    const { result } = renderHook(() => useImageAttachments("session-a", true, onError));
    await act(async () => { await result.current.addFiles([png()]); });
    await act(async () => { await result.current.addFiles([new File(["x"], "../bad.txt", { type: "text/plain" })]); });
    expect(result.current.images).toHaveLength(1);
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("文件名无效"));
  });

  it("uses item MIME when a clipboard image file has an empty MIME", () => {
    const file = new File(["image"], "screenshot", { lastModified: 123 });
    const images = clipboardImageFiles({ files: [] as unknown as FileList,
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }] as unknown as DataTransferItemList });
    expect(images).toHaveLength(1);
    expect(images[0]).toMatchObject({ type: "image/png", name: "screenshot", lastModified: 123, size: 5 });
  });

  it("falls back to files when clipboard items cannot supply an image", () => {
    const file = png();
    expect(clipboardImageFiles({ files: [file] as unknown as FileList,
      items: [{ kind: "file", type: "image/png", getAsFile: () => null },
        { kind: "string", type: "text/plain" }] as unknown as DataTransferItemList })).toEqual([file]);
  });
});

describe("general file attachments", () => {
  const note = () => new File(["hello"], "说明.md", { type: "text/markdown" });
  it("reads a document's bytes and permits empty files without claiming they are images", async () => {
    expect(await readAttachmentFile(note())).toMatchObject({ name: "说明.md", mime_type: "text/markdown", kind: "file", data: btoa("hello"), size: 5 });
    const empty = new File([], "empty.txt");
    expect(() => validateAttachmentFiles([empty], [])).not.toThrow();
    expect(await readAttachmentFile(empty)).toMatchObject({ name: "empty.txt", size: 0, data: "", mime_type: "application/octet-stream", kind: "file" });
  });

  it("does not duplicate mixed native clipboard files exposed through both items and files", () => {
    const files = [note(), png()];
    const items = files.map((file) => ({ kind: "file", type: file.type, getAsFile: () => file }));
    expect(clipboardAttachmentFiles({ files: files as unknown as FileList, items: items as unknown as DataTransferItemList })).toEqual(files);
  });

  it("enforces file and combined budgets before reading and keeps the existing image budget", () => {
    const file = note();
    Object.defineProperty(file, "size", { configurable: true, value: MAX_FILE_BYTES + 1 });
    expect(() => validateAttachmentFiles([file], [])).toThrow("10 MiB");
    Object.defineProperty(file, "size", { value: MAX_FILE_BYTES });
    expect(() => validateAttachmentFiles([file, file, note()], [])).toThrow("20 MiB");
    expect(() => validateAttachmentFiles(Array.from({ length: 9 }, note), [])).toThrow("8 个");
    expect(() => validateAttachmentFiles([png()], Array.from({ length: 4 }, () => pending()))).toThrow("4 张");
    expect(MAX_TOTAL_ATTACHMENT_BYTES).toBeGreaterThan(MAX_FILE_BYTES);
  });

  it("accepts ordinary files on text models and clears all drafts when their owner changes", async () => {
    const onError = vi.fn();
    const { result, rerender } = renderHook(({ owner }) => useImageAttachments(owner, false, onError), { initialProps: { owner: "a" } });
    await act(async () => { await result.current.addFiles([note()]); });
    expect(result.current.files).toHaveLength(1);
    expect(result.current.images).toEqual([]);
    expect(onError).not.toHaveBeenCalled();
    rerender({ owner: "b" });
    expect(result.current.items).toEqual([]);
  });
});

describe("explicit clipboard fallback", () => {
  it("reads no clipboard content until the user explicitly requests paste", async () => {
    vi.useFakeTimers();
    const read = mockClipboard();
    renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(read).not.toHaveBeenCalled();
  });

  it.each([{ owner: null, supported: true }, { owner: "session-a", supported: false }])(
    "does not read without an eligible session: %j", async ({ owner, supported }) => {
      vi.useFakeTimers();
      const read = mockClipboard();
      const { result } = renderHook(() => useImageAttachments(owner, supported, vi.fn()));
      act(() => { result.current.prepareClipboardPaste(); });
      await act(async () => { await vi.advanceTimersByTimeAsync(200); });
      expect(read).not.toHaveBeenCalled();
    },
  );

  it("uses the Web Clipboard API only when native image paste did not arrive", async () => {
    vi.useFakeTimers();
    const read = mockClipboard();
    mockLocalReads();
    const onError = vi.fn();
    const { result } = renderHook(() => useImageAttachments("session-a", true, onError));
    act(() => { result.current.prepareClipboardPaste(); });
    expect(result.current.reading).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(99); });
    expect(read).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(read).toHaveBeenCalledTimes(1);
    expect(result.current.images).toMatchObject([{ name: "clipboard-1.png", mime_type: "image/png" }]);
    expect(result.current.reading).toBe(false);
    expect(onError).not.toHaveBeenCalled();
  });

  it("cancels fallback for successful native paste and adds the image only once", async () => {
    vi.useFakeTimers();
    const read = mockClipboard();
    mockLocalReads();
    const { result } = renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    await act(async () => {
      result.current.prepareClipboardPaste();
      result.current.addPastedFiles([png()]);
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(read).not.toHaveBeenCalled();
    expect(result.current.images).toHaveLength(1);
    expect(result.current.reading).toBe(false);
  });

  it("does not read the clipboard after an ordinary text paste cancels fallback", async () => {
    vi.useFakeTimers();
    const read = mockClipboard();
    const { result } = renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    act(() => {
      result.current.prepareClipboardPaste();
      result.current.cancelClipboardFallback();
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(read).not.toHaveBeenCalled();
    expect(result.current.images).toEqual([]);
    expect(result.current.reading).toBe(false);
  });

  it("remains pending across deferred clipboard access and file reading", async () => {
    vi.useFakeTimers();
    let finishRead: (items: ReturnType<typeof clipboardItem>[]) => void = () => {};
    mockClipboard(vi.fn().mockImplementation(() => new Promise((resolve) => { finishRead = resolve; })));
    let reader: FileReader | undefined;
    vi.spyOn(FileReader.prototype, "readAsDataURL").mockImplementation(function (this: FileReader) { reader = this; });
    const { result } = renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    act(() => { result.current.prepareClipboardPaste(); });
    expect(result.current.reading).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(result.current.reading).toBe(true);
    await act(async () => { finishRead([clipboardItem()]); });
    expect(result.current.reading).toBe(true);
    expect(result.current.images).toEqual([]);
    await act(async () => {
      Object.defineProperty(reader, "result", { value: "data:image/png;base64,iVBORw==" });
      reader?.onload?.call(reader, new ProgressEvent("load") as ProgressEvent<FileReader>);
    });
    expect(result.current.reading).toBe(false);
    expect(result.current.images).toHaveLength(1);
  });

  it("does not let an older clipboard completion clear the next paste's pending state", async () => {
    vi.useFakeTimers();
    let finishOldRead: (items: ReturnType<typeof clipboardItem>[]) => void = () => {};
    mockClipboard(vi.fn().mockImplementation(() => new Promise((resolve) => { finishOldRead = resolve; })));
    const { result } = renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { finishOldRead([]); });
    expect(result.current.reading).toBe(true);
    act(() => { result.current.cancelClipboardFallback(); });
    expect(result.current.reading).toBe(false);
  });

  it.each(["session", "capability"])("clears pending immediately on %s invalidation", async (change) => {
    vi.useFakeTimers();
    let finishRead: (items: ReturnType<typeof clipboardItem>[]) => void = () => {};
    mockClipboard(vi.fn().mockImplementation(() => new Promise((resolve) => { finishRead = resolve; })));
    const { result, rerender } = renderHook(({ owner, supported }) => useImageAttachments(owner, supported, vi.fn()),
      { initialProps: { owner: "session-a", supported: true } });
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(result.current.reading).toBe(true);
    rerender({ owner: change === "session" ? "session-b" : "session-a", supported: change !== "capability" });
    expect(result.current.reading).toBe(false);
    await act(async () => { finishRead([clipboardItem()]); });
    expect(result.current.reading).toBe(false);
    expect(result.current.images).toEqual([]);
  });

  it("discards a pending fallback read when native paste arrives first", async () => {
    vi.useFakeTimers();
    let finishRead: (items: ReturnType<typeof clipboardItem>[]) => void = () => {};
    mockClipboard(vi.fn().mockImplementation(() => new Promise((resolve) => { finishRead = resolve; })));
    mockLocalReads();
    const { result } = renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await act(async () => {
      result.current.addPastedFiles([png()]);
      finishRead([clipboardItem()]);
    });
    expect(result.current.images).toMatchObject([{ name: "screen.png" }]);
  });

  it("ignores a late native event from the same paste but accepts the next explicit paste", async () => {
    vi.useFakeTimers();
    const read = mockClipboard();
    mockLocalReads();
    const { result } = renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await act(async () => { result.current.addPastedFiles([png()]); });
    expect(result.current.images).toHaveLength(1);
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(read).toHaveBeenCalledTimes(2);
    expect(result.current.images).toHaveLength(2);
  });

  it("accepts a later context-menu paste without a new keyboard event", async () => {
    vi.useFakeTimers();
    const read = mockClipboard();
    mockLocalReads();
    const { result } = renderHook(() => useImageAttachments("session-a", true, vi.fn()));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(result.current.images).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    await act(async () => { result.current.addPastedFiles([png()]); });
    expect(result.current.images).toMatchObject([{ name: "clipboard-1.png" }, { name: "screen.png" }]);
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("reports denied clipboard access with an actionable error", async () => {
    vi.useFakeTimers();
    mockClipboard(vi.fn().mockRejectedValue(new DOMException("Denied", "NotAllowedError")));
    const onError = vi.fn();
    const { result } = renderHook(() => useImageAttachments("session-a", true, onError));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("重新复制后粘贴"));
    expect(result.current.images).toEqual([]);
    expect(result.current.reading).toBe(false);
  });

  it.each(["resolve", "reject"])("ignores a late old-session clipboard %s", async (outcome) => {
    vi.useFakeTimers();
    let finishRead: (items: ReturnType<typeof clipboardItem>[]) => void = () => {};
    let failRead: (error: Error) => void = () => {};
    mockClipboard(vi.fn().mockImplementation(() => new Promise((resolve, reject) => {
      finishRead = resolve;
      failRead = reject;
    })));
    const localRead = mockLocalReads();
    const onError = vi.fn();
    const { result, rerender } = renderHook(({ owner }) => useImageAttachments(owner, true, onError),
      { initialProps: { owner: "session-a" } });
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    rerender({ owner: "session-b" });
    await act(async () => {
      if (outcome === "resolve") finishRead([clipboardItem()]);
      else failRead(new Error("late failure"));
    });
    expect(result.current.images).toEqual([]);
    expect(localRead).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("discards clipboard reads when image capability changes before they finish", async () => {
    vi.useFakeTimers();
    let finishRead: (items: ReturnType<typeof clipboardItem>[]) => void = () => {};
    mockClipboard(vi.fn().mockImplementation(() => new Promise((resolve) => { finishRead = resolve; })));
    const localRead = mockLocalReads();
    const { result, rerender } = renderHook(({ supported }) => useImageAttachments("session-a", supported, vi.fn()),
      { initialProps: { supported: true } });
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    rerender({ supported: false });
    await act(async () => { finishRead([clipboardItem()]); });
    expect(result.current.images).toEqual([]);
    expect(localRead).not.toHaveBeenCalled();
  });

  it("does not read files or report errors after unmounting", async () => {
    vi.useFakeTimers();
    let finishRead: (items: ReturnType<typeof clipboardItem>[]) => void = () => {};
    mockClipboard(vi.fn().mockImplementation(() => new Promise((resolve) => { finishRead = resolve; })));
    const localRead = mockLocalReads();
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useImageAttachments("session-a", true, onError));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    unmount();
    await act(async () => { finishRead([clipboardItem()]); });
    expect(localRead).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("does not reinterpret text or unsupported clipboard types as images", async () => {
    vi.useFakeTimers();
    const items = [clipboardItem("text/plain"), clipboardItem("image/svg+xml")];
    mockClipboard(vi.fn().mockResolvedValue(items));
    const localRead = mockLocalReads();
    const onError = vi.fn();
    const { result } = renderHook(() => useImageAttachments("session-a", true, onError));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(items.every((item) => item.getType.mock.calls.length === 0)).toBe(true);
    expect(localRead).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(result.current.images).toEqual([]);
    expect(result.current.reading).toBe(false);
  });

  it("enforces attachment limits on images read through the fallback", async () => {
    vi.useFakeTimers();
    mockClipboard(vi.fn().mockResolvedValue(Array.from({ length: 5 }, () => clipboardItem())));
    const localRead = mockLocalReads();
    const onError = vi.fn();
    const { result } = renderHook(() => useImageAttachments("session-a", true, onError));
    act(() => { result.current.prepareClipboardPaste(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(result.current.images).toEqual([]);
    expect(localRead).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("4 张"));
    expect(result.current.reading).toBe(false);
  });
});
