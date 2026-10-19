import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * A ```mermaid block rendered as SVG by pele (https://github.com/obsidianmd/pele).
 * pele is loaded on first use, and each diagram type on demand, so pages
 * without diagrams don't pay for it. Colours come from the `--pele-*` CSS
 * variables mapped to the app theme in index.css.
 */
function Mermaid({ source }: { source: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let mounted: { destroy(): void } | null = null;
    let cancelled = false;
    import("pele/lazy")
      .then(({ mountAsync }) =>
        mountAsync(el, source, {
          onError: (e) => setError(e instanceof Error ? e.message : String(e)),
        }),
      )
      .then((m) => {
        if (cancelled) m.destroy();
        else mounted = m;
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
      mounted?.destroy();
    };
  }, [source]);

  if (error) {
    return (
      <div className="mermaid-error">
        <pre>
          <code>{source}</code>
        </pre>
        <div className="mermaid-error-message">Diagram error: {error}</div>
      </div>
    );
  }
  return <div ref={ref} className="mermaid-diagram" />;
}

/** react-markdown hands each renderer its hast `node`; it isn't a DOM prop. */
function withoutNode<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const rest = { ...props };
  delete rest.node;
  return rest;
}

const components: Components = {
  a: (props) => <a {...withoutNode(props)} target="_blank" rel="noreferrer" />,
  code: (allProps) => {
    const { className, children, ...props } = withoutNode(allProps);
    if (/\blanguage-mermaid\b/.test(className ?? "")) {
      return <Mermaid source={String(children).replace(/\n$/, "")} />;
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },
  // A mermaid block's <pre> wrapper would box the diagram like code.
  pre: ({ node, children, ...props }) => {
    const first = node?.children?.[0];
    const lang =
      first && first.type === "element" ? String(first.properties?.className ?? "") : "";
    if (lang.includes("language-mermaid")) return <>{children}</>;
    return <pre {...props}>{children}</pre>;
  },
};

/** GitHub-flavoured Markdown with Mermaid diagrams. */
export default function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={`markdown ${className ?? ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
