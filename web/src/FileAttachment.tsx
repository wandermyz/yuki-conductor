import { useRef, useState } from "react";
import "./fileAttachment.css";

export function isHtml(mime: string | null, filename: string): boolean {
  if (mime === "text/html" || mime === "application/xhtml+xml") return true;
  return /\.(html?|xhtml)$/i.test(filename);
}

function downloadUrl(url: string): string {
  return url + (url.includes("?") ? "&" : "?") + "download=1";
}

/**
 * Save a file to the device. Where the Web Share API takes files (iOS and
 * Android) this opens the share sheet, whose "Save to Files" is the only
 * reliable way to keep a file on iOS — doubly so in the home-screen app,
 * which has no downloads UI. Elsewhere it's a plain download.
 *
 * Safari only shares inside the tap that asked for it, and fetching the file
 * can outlast that; if it does, the file is kept and the next tap shares it
 * at once.
 */
function SaveButton({ url, filename }: { url: string; filename: string }) {
  const file = useRef<File | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "again">("idle");

  const canShareFiles =
    typeof navigator.canShare === "function" &&
    navigator.canShare({ files: [new File([""], filename)] });

  if (!canShareFiles) {
    return (
      <a className="file-action" href={downloadUrl(url)} download={filename}>
        Download
      </a>
    );
  }

  const share = async (f: File) => {
    try {
      await navigator.share({ files: [f] });
      setState("idle");
    } catch (err) {
      const name = (err as DOMException).name;
      // AbortError: the user closed the sheet. NotAllowedError: the tap's
      // activation ran out while fetching; the next tap shares straight away.
      setState(name === "NotAllowedError" ? "again" : "idle");
    }
  };

  const onClick = async () => {
    if (file.current) return share(file.current);
    setState("loading");
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      file.current = new File([blob], filename, { type: blob.type });
    } catch {
      setState("idle");
      window.location.href = downloadUrl(url);
      return;
    }
    await share(file.current);
  };

  return (
    <button type="button" className="file-action" onClick={onClick} disabled={state === "loading"}>
      {state === "loading" ? "Preparing…" : state === "again" ? "Tap again to save" : "Save"}
    </button>
  );
}

/** A non-media attachment: its name (opens it in a new tab) and a Save action. */
export function FileRow({ url, filename }: { url: string; filename: string }) {
  return (
    <div className="file-row">
      <a className="file-link" href={url} target="_blank" rel="noreferrer">
        📎 {filename}
      </a>
      <SaveButton url={url} filename={filename} />
    </div>
  );
}

/**
 * An HTML attachment rendered in place. The frame is sandboxed without
 * allow-same-origin, and the server sends the same sandbox as a CSP, so the
 * page runs its own scripts but never as the app.
 */
export function HtmlPreview({ url, filename }: { url: string; filename: string }) {
  return (
    <div className="html-preview">
      <iframe
        src={url}
        title={filename}
        sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads"
        loading="lazy"
      />
      <FileRow url={url} filename={filename} />
    </div>
  );
}

/** Any attachment that isn't an image, video or audio. */
export function FileAttachment({
  url,
  filename,
  mime = null,
}: {
  url: string;
  filename: string;
  mime?: string | null;
}) {
  return isHtml(mime, filename) ? (
    <HtmlPreview url={url} filename={filename} />
  ) : (
    <FileRow url={url} filename={filename} />
  );
}
