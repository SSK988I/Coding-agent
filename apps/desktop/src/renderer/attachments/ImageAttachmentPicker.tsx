import { useId, useRef } from "react";
import type { FileAttachmentMetadata, ImageAttachmentPayload } from "../../shared/types";
import { imageDataUrl, isPendingImage, type PendingAttachment } from "./imageAttachments";

export function fileSizeText(size: number): string {
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MiB`
    : size >= 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${size} B`;
}

function FileIcon({ name }: { name: string }) {
  const extension = name.includes(".") ? name.split(".").at(-1) ?? "" : "";
  return <span className="attachment-file-icon" aria-hidden="true">
    <svg viewBox="0 0 24 28" fill="none" stroke="currentColor" strokeWidth="1.4">
      <path d="M4 2h10l6 6v18H4z M14 2v7h6" />
    </svg>
    <small>{/^[a-z\d]{1,5}$/i.test(extension) ? extension.toUpperCase() : "FILE"}</small>
  </span>;
}

function AttachmentLabel({ name, size }: { name: string; size: number }) {
  return <span className="attachment-description"><strong title={name}>{name}</strong><small>{fileSizeText(size)}</small></span>;
}

export function MessageFiles({ files }: { files: readonly FileAttachmentMetadata[] }) {
  return <div className="message-files">{files.map((file, index) => <div className="message-file" key={`${file.path}:${index}`}>
    <FileIcon name={file.name} /><AttachmentLabel name={file.name} size={file.size} />
  </div>)}</div>;
}

export function MessageImages({ images }: { images: readonly ImageAttachmentPayload[] }) {
  return <div className="message-images">{images.map((image, index) => {
    const src = imageDataUrl(image);
    return src ? <img key={index} src={src} alt={`附件图片 ${index + 1}`} loading="lazy" />
      : <span key={index}>图片无法显示</span>;
  })}</div>;
}

export function AttachmentPicker({ items, disabled, supported, reading, onFiles, onRemove }: {
  items: readonly PendingAttachment[];
  disabled: boolean;
  supported: boolean;
  reading: boolean;
  onFiles: (files: File[]) => void;
  onRemove: (id: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const limitsId = useId();
  const limits = "每次最多 8 个附件、合计 20 MiB；普通文件每个 10 MiB。图片最多 4 张、每张 5 MiB、合计 12 MiB。";
  return <>
    <div className="attachment-toolbar">
      <button type="button" className="attachment-trigger" disabled={disabled || reading}
        title="选择文件，也可以粘贴或拖入文件" aria-describedby={limitsId}
        onClick={() => inputRef.current?.click()}>＋ 添加文件</button>
      <input ref={inputRef} type="file" hidden multiple aria-label="选择文件"
        disabled={disabled || reading} onChange={(event) => {
          onFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }} />
      <span className="attachment-hint" title={limits}>{reading ? "正在读取附件…" : supported
        ? "支持文件与图片 · 最多 8 个"
        : "支持文件 · 当前模型不支持图片"}</span>
      <span id={limitsId} className="attachment-accessible-description">{limits}</span>
    </div>
    {items.length > 0 && <section className="pending-images" aria-label="待发送附件">
      {items.map((item) => isPendingImage(item) ? <div className="pending-image" key={item.id}>
        <img src={imageDataUrl(item) ?? undefined} alt={item.name} />
        <AttachmentLabel name={item.name} size={item.size} />
        <button type="button" aria-label={`移除图片 ${item.name}`} disabled={disabled || reading}
          onClick={() => onRemove(item.id)}>×</button>
      </div> : <div className="pending-file" key={item.id}>
        <FileIcon name={item.name} /><AttachmentLabel name={item.name} size={item.size} />
        <button type="button" aria-label={`移除文件 ${item.name}`} disabled={disabled || reading}
          onClick={() => onRemove(item.id)}>×</button>
      </div>)}
    </section>}
  </>;
}
