import { useEffect, useRef, useState } from "react";
import { IMAGE_MIME_TYPES, isImageFile, isPendingImage, readAttachmentFile, validateAttachmentFiles,
  type PendingAttachment } from "./imageAttachments";

// Native and fallback delivery can race, but a later context-menu paste has no
// keyboard event to reset the marker and must remain a separate user action.
const NATIVE_PASTE_RACE_WINDOW_MS = 500;

/** Async local reads are owned by the session that started them. */
export function useAttachments(owner: string | null, supported: boolean, onError: (message: string) => void) {
  const [images, setImages] = useState<PendingAttachment[]>([]);
  const [reading, setReading] = useState(false);
  const [clipboardPending, setClipboardPending] = useState(false);
  const ownerRef = useRef(owner);
  const supportedRef = useRef(supported);
  const generation = useRef(0);
  const readingRef = useRef(false);
  const imagesRef = useRef(images);
  const pasteEpoch = useRef(0);
  const pastedByFallback = useRef<{ epoch: number; handledAt: number } | null>(null);
  const pasteTimer = useRef<number | null>(null);
  ownerRef.current = owner;
  supportedRef.current = supported;
  imagesRef.current = images;

  const invalidateClipboardFallback = () => {
    pasteEpoch.current += 1;
    pastedByFallback.current = null;
    if (pasteTimer.current !== null) window.clearTimeout(pasteTimer.current);
    pasteTimer.current = null;
  };
  const cancelClipboardFallback = () => {
    invalidateClipboardFallback();
    setClipboardPending(false);
  };
  useEffect(() => { cancelClipboardFallback(); }, [owner, supported]);
  useEffect(() => () => { invalidateClipboardFallback(); generation.current++; }, []);

  const clear = () => {
    cancelClipboardFallback();
    generation.current += 1;
    readingRef.current = false;
    imagesRef.current = [];
    setImages([]);
    setReading(false);
  };
  useEffect(() => { clear(); }, [owner]);

  const addPastedFiles = (files: readonly File[]) => {
    const fallback = pastedByFallback.current;
    const alreadyHandled = fallback?.epoch === pasteEpoch.current
      && Date.now() - fallback.handledAt < NATIVE_PASTE_RACE_WINDOW_MS;
    cancelClipboardFallback();
    if (!alreadyHandled) void addFiles(files);
  };

  // Native paste is preferred. Read through the Web Clipboard API only when a
  // user presses paste and the host never delivers usable clipboard files.
  const prepareClipboardPaste = () => {
    cancelClipboardFallback();
    if (!supported || !owner || typeof navigator.clipboard?.read !== "function") return;
    // Block sending before the fallback timer or clipboard permission/read has
    // finished; file-reader exclusivity remains separate in readingRef.
    setClipboardPending(true);
    const token = pasteEpoch.current;
    const originalOwner = owner;
    const originalGeneration = generation.current;
    const stillCurrent = () => pasteEpoch.current === token && ownerRef.current === originalOwner
      && generation.current === originalGeneration && supportedRef.current === supported;
    pasteTimer.current = window.setTimeout(() => {
      pasteTimer.current = null;
      void (async () => {
        try {
          const items = await navigator.clipboard.read();
          if (!stillCurrent()) return;
          const files: File[] = [];
          for (const item of items) {
            const mime = item.types.find((type) => IMAGE_MIME_TYPES.some((allowed) => allowed === type));
            if (!mime) continue;
            const blob = await item.getType(mime);
            if (!stillCurrent()) return;
            files.push(new File([blob], `clipboard-${files.length + 1}.${mime.split("/")[1]}`, { type: mime }));
          }
          if (files.length && stillCurrent()) {
            pastedByFallback.current = { epoch: token, handledAt: Date.now() };
            await addFiles(files);
          }
        } catch {
          if (stillCurrent()) onError("无法读取剪贴板附件，请重新复制后粘贴，或使用“添加文件”。");
        } finally {
          if (pasteEpoch.current === token) setClipboardPending(false);
        }
      })();
    }, 100);
  };

  const addFiles = async (files: readonly File[]) => {
    if (!files.length || !owner) return;
    if (!supported && files.some(isImageFile)) { onError("当前模型不支持图片输入，请先切换到支持视觉的模型。"); return; }
    if (readingRef.current) { onError("附件正在读取，请稍候再添加。"); return; }
    const originalOwner = owner;
    const token = generation.current;
    try {
      validateAttachmentFiles(files, imagesRef.current);
      readingRef.current = true;
      setReading(true);
      const loaded = await Promise.all(files.map(readAttachmentFile));
      if (ownerRef.current !== originalOwner || generation.current !== token) return;
      imagesRef.current = [...imagesRef.current, ...loaded];
      setImages(imagesRef.current);
    } catch (reason) {
      if (ownerRef.current === originalOwner && generation.current === token) {
        onError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (ownerRef.current === originalOwner && generation.current === token) {
        readingRef.current = false;
        setReading(false);
      }
    }
  };

  return { items: images, images: images.filter(isPendingImage), files: images.filter((item) => item.kind === "file"),
    reading: reading || clipboardPending, clear, addFiles, addPastedFiles, prepareClipboardPaste, cancelClipboardFallback,
    restore: (saved: readonly PendingAttachment[]) => {
      if (imagesRef.current.length) return;
      cancelClipboardFallback();
      generation.current += 1;
      readingRef.current = false;
      setReading(false);
      imagesRef.current = [...saved];
      setImages(imagesRef.current);
    },
    remove: (id: string) => {
      imagesRef.current = imagesRef.current.filter((image) => image.id !== id);
      setImages(imagesRef.current);
    } };
}

// Existing image-only callers share the same session-owned lifecycle.
export const useImageAttachments = useAttachments;
