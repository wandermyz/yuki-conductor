import type { AttachmentRef } from "./chat/api";

/**
 * The files a paste carries, or none when it should paste as text.
 *
 * A screenshot or a file copied in Finder arrives as files only (plus the file
 * name as plain text). Copying from a spreadsheet or a web page also attaches
 * a rendered image beside its HTML and text; that one is meant as text.
 */
export function pastedFiles(e: React.ClipboardEvent): File[] {
  const data = e.clipboardData;
  const files = Array.from(data.files);
  if (!files.length) return [];
  if (data.getData("text/html") && data.getData("text/plain").trim()) return [];
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  // Clipboard images are all called "image.png": give each its own name.
  return files.map((f, i) =>
    /^image\.\w+$/.test(f.name)
      ? new File([f], `pasted-${stamp}${files.length > 1 ? `-${i + 1}` : ""}.${f.name.split(".")[1]}`, {
          type: f.type,
        })
      : f,
  );
}

const isImage = (a: AttachmentRef) =>
  a.mime_type?.startsWith("image/") ?? /\.(png|jpe?g|gif|webp|svg)$/i.test(a.filename);

/** The Markdown that shows an uploaded image inline: `![name](url)`. */
export const imageMarker = (a: AttachmentRef) => `![${a.filename}](${a.url})`;

/** The inline markers for the images among `uploads`, one per line. */
export function imageMarkers(uploads: AttachmentRef[]): string {
  return uploads.filter(isImage).map(imageMarker).join("\n");
}

/** Insert `snippet` into `text` at `at`, on lines of its own. */
export function insertAt(text: string, at: number, snippet: string): string {
  if (!snippet) return text;
  const before = text.slice(0, at);
  const after = text.slice(at);
  const lead = before && !before.endsWith("\n") ? "\n" : "";
  const trail = after && !after.startsWith("\n") ? "\n" : "";
  return before + lead + snippet + trail + after;
}

/** `text` without the inline marker of a removed attachment. */
export function withoutMarker(text: string, a: AttachmentRef): string {
  return text.split(imageMarker(a) + "\n").join("").split(imageMarker(a)).join("");
}
