import { useEffect, useState } from "react";
import Markdown from "./Markdown";
import "./MarkdownPage.css";

interface MarkdownDocument {
  filename: string;
  text: string;
}

/** A standalone document, shared by every Markdown attachment link. */
export default function MarkdownPage({ fileId }: { fileId: string }) {
  const [document, setDocument] = useState<MarkdownDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const url = `/api/files/${encodeURIComponent(fileId)}`;

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${url}/markdown`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(response.status === 404 ? "Document not found" : "Unable to load document");
        }
        return response.json() as Promise<MarkdownDocument>;
      })
      .then((result) => {
        if (!controller.signal.aborted) setDocument(result);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      });
    return () => controller.abort();
  }, [url]);

  useEffect(() => {
    if (document) window.document.title = `${document.filename} · yuki conductor`;
  }, [document]);

  let content = <p role="status">Loading document…</p>;
  if (error) content = <p role="alert">{error}</p>;
  else if (document) content = <article><Markdown text={document.text} /></article>;

  return (
    <div className="document-page">
      <main className="document-content">
        <header className="document-header">
          <span className="document-filename">{document?.filename ?? "Markdown document"}</span>
          <nav aria-label="Document actions">
            <a href={`${url}?raw=1`} target="_blank" rel="noreferrer">View source</a>
            <a href={`${url}?download=1`}>Download</a>
          </nav>
        </header>
        {content}
      </main>
    </div>
  );
}
