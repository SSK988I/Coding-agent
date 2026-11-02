import type { FileAttachmentPayload, ImageAttachmentPayload } from "../../shared/types";

export const IMAGE_MIME_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"] as const;
export const IMAGE_ACCEPT = IMAGE_MIME_TYPES.join(",");
export const MAX_IMAGES = 4;
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024;

export interface PendingImage extends ImageAttachmentPayload {
  kind?: "image";
  id: string;
  name: string;
  size: number;
}

export interface PendingFile extends FileAttachmentPayload {
  kind: "file";
  id: string;
  size: number;
}

export type PendingAttachment = PendingImage | PendingFile;
export const MAX_ATTACHMENTS = 8;
export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024;

export function isPendingImage(item: PendingAttachment): item is PendingImage { return item.kind !== "file"; }

/** Only supported raster formats use the model's native image channel. */
export function isImageFile(file: File): boolean {
  return IMAGE_MIME_TYPES.some((mime) => mime === file.type);
}

export function clipboardAttachmentFiles(data: Pick<DataTransfer, "files" | "items">): File[] {
  const items = Array.from(data.items ?? []).flatMap((item) => {
    if (item.kind !== "file") return [];
    const file = item.getAsFile();
    if (!file) return [];
    return [file.type || !item.type ? file : new File([file], file.name, { type: item.type, lastModified: file.lastModified })];
  });
  return items.length ? items : Array.from(data.files ?? []);
}

export function validateAttachmentFiles(files: readonly File[], existing: readonly PendingAttachment[]): void {
  if (files.length + existing.length > MAX_ATTACHMENTS) throw new Error("每次最多添加 8 个附件。");
  validateImageFiles(files.filter(isImageFile), existing.filter(isPendingImage));
  let total = existing.reduce((size, item) => size + item.size, 0);
  for (const file of files) {
    if (!file.name || file.name.length > 255 || /[\\/\x00-\x1f\x7f]/.test(file.name)) throw new Error("文件名无效，请重命名后添加。");
    if (!isImageFile(file) && file.size > MAX_FILE_BYTES) throw new Error("单个文件不能超过 10 MiB。");
    total += file.size;
  }
  if (total > MAX_TOTAL_ATTACHMENT_BYTES) throw new Error("待发送附件合计不能超过 20 MiB。");
}

export async function readAttachmentFile(file: File): Promise<PendingAttachment> {
  if (isImageFile(file)) return readImageFile(file);
  const image = await readImageFile(file);
  return { ...image, kind: "file", mime_type: file.type || "application/octet-stream" };
}

/** Clipboard image items are not consistently mirrored in `files` by hosts. */
export function clipboardImageFiles(data: Pick<DataTransfer, "files" | "items">): File[] {
  const items = Array.from(data.items ?? []).flatMap((item) => {
    if (item.kind !== "file") return [];
    const file = item.getAsFile();
    if (!file) return [];
    const mime = file.type || item.type;
    if (!mime.startsWith("image/")) return [];
    return [file.type ? file : new File([file], file.name || "clipboard.png", {
      type: mime, lastModified: file.lastModified,
    })];
  });
  // Pick one representation rather than adding the same image twice.
  return items.length ? items : Array.from(data.files ?? []).filter((file) => file.type.startsWith("image/"));
}

export function validateImageFiles(files: readonly File[], existing: readonly PendingImage[]): void {
  if (files.length + existing.length > MAX_IMAGES) throw new Error("每次最多添加 4 张图片。");
  let total = existing.reduce((size, image) => size + image.size, 0);
  for (const file of files) {
    if (!IMAGE_MIME_TYPES.some((mime) => mime === file.type)) {
      throw new Error("仅支持 PNG、JPEG、GIF 和 WebP 图片。");
    }
    if (file.size === 0) throw new Error("不能添加空图片。");
    if (file.size > MAX_IMAGE_BYTES) throw new Error("每张图片不能超过 5 MiB。");
    total += file.size;
  }
  if (total > MAX_TOTAL_IMAGE_BYTES) throw new Error("待发送图片合计不能超过 12 MiB。");
}

export function readImageFile(file: File): Promise<PendingImage> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取文件：${file.name}`));
    reader.onabort = () => reject(new Error("文件读取已取消。"));
    reader.onload = () => {
      const value = typeof reader.result === "string" ? reader.result : "";
      const comma = value.indexOf(",");
      if (comma < 0) {
        reject(new Error(`无法读取文件：${file.name}`));
        return;
      }
      resolve({ id: crypto.randomUUID(), name: file.name, size: file.size,
        mime_type: file.type, data: value.slice(comma + 1) });
    };
    reader.readAsDataURL(file);
  });
}

/** Never render remote URLs, SVG or arbitrary persisted content as attachments. */
export function imageDataUrl(image: ImageAttachmentPayload): string | null {
  if (!IMAGE_MIME_TYPES.some((mime) => mime === image.mime_type)
    || !image.data || image.data.length > Math.ceil(MAX_IMAGE_BYTES / 3) * 4
    || !/^[A-Za-z0-9+/]+={0,2}$/.test(image.data)) return null;
  return `data:${image.mime_type};base64,${image.data}`;
}
