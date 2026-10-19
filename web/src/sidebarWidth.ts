/** The persisted width behind a resizable sidebar; see SidebarResizer. */
import { useEffect, useState } from "react";

export const MIN_WIDTH = 200;
export const MAX_WIDTH = 640;

export function clampWidth(w: number): number {
  return Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, w)));
}

export interface SidebarWidth {
  width: number;
  defaultWidth: number;
  setWidth: (w: number) => void;
  /** Put on the view's root: sets `--sidebar-width`, which the sidebar's CSS reads. */
  style: React.CSSProperties;
}

/** A sidebar width persisted in localStorage under `storageKey`. */
export function useSidebarWidth(storageKey: string, defaultWidth: number): SidebarWidth {
  const [width, setWidth] = useState(() => {
    const saved = Number(localStorage.getItem(storageKey));
    return saved ? clampWidth(saved) : defaultWidth;
  });
  useEffect(() => {
    localStorage.setItem(storageKey, String(width));
  }, [storageKey, width]);
  return {
    width,
    defaultWidth,
    setWidth,
    style: { "--sidebar-width": `${width}px` } as React.CSSProperties,
  };
}
