import { useEffect, useRef, useState } from "react";

/** How far (px, after damping) a pull must reach to refresh on release. */
export const THRESHOLD = 64;
const MAX_PULL = 96;
/** Finger travel is halved, so the list follows with some resistance. */
const DAMPING = 0.5;
/** Keep the spinner up at least this long, so a fast refresh still registers. */
const MIN_SPIN_MS = 400;

export interface PullState {
  /** Current pull distance in px (0 when idle). */
  pull: number;
  refreshing: boolean;
}

/**
 * Touch pull-to-refresh on a scroll container: pulling down from the very
 * top past the threshold and letting go calls `onRefresh`. Touch only; a
 * mouse never triggers it. `disabled` stops a pull from starting (e.g. while
 * a drag-to-reorder holds the touch). Pass the returned `attach` as the
 * scroll container's `ref`; it's a callback ref, so the container may come and go.
 */
export function usePullToRefresh(
  onRefresh: () => Promise<unknown>,
  disabled = false,
): PullState & { attach: (el: HTMLElement | null) => void } {
  const [el, attach] = useState<HTMLElement | null>(null);
  const [state, setState] = useState<PullState>({ pull: 0, refreshing: false });
  const latest = useRef({ onRefresh, disabled, refreshing: false });
  useEffect(() => {
    latest.current.onRefresh = onRefresh;
    latest.current.disabled = disabled;
  }, [onRefresh, disabled]);

  useEffect(() => {
    if (!el) return;
    let start: { x: number; y: number } | null = null;
    let pull = 0;
    let pulling = false;

    const reset = () => {
      start = null;
      pulling = false;
      pull = 0;
    };
    const onStart = (e: TouchEvent) => {
      reset();
      if (latest.current.refreshing || e.touches.length !== 1 || el.scrollTop > 0) return;
      start = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    };
    const onMove = (e: TouchEvent) => {
      if (!start) return;
      if (latest.current.disabled) {
        reset();
        setState((s) => (s.pull ? { ...s, pull: 0 } : s));
        return;
      }
      const dx = e.touches[0].clientX - start.x;
      const dy = e.touches[0].clientY - start.y;
      if (!pulling) {
        // Wait until the gesture says what it is: upward or sideways means
        // an ordinary scroll or swipe, and this touch is left alone.
        if (Math.abs(dy) < 6 && Math.abs(dx) < 6) return;
        if (dy <= 0 || Math.abs(dx) > dy || el.scrollTop > 0) {
          start = null;
          return;
        }
        pulling = true;
      }
      // Don't let the page scroll or bounce under the pull.
      if (e.cancelable) e.preventDefault();
      pull = Math.min(Math.max(dy * DAMPING, 0), MAX_PULL);
      setState((s) => ({ ...s, pull }));
    };
    const onEnd = () => {
      const fire = pulling && pull >= THRESHOLD;
      reset();
      if (!fire) {
        setState((s) => (s.pull ? { ...s, pull: 0 } : s));
        return;
      }
      latest.current.refreshing = true;
      setState({ pull: 0, refreshing: true });
      const spin = new Promise((r) => setTimeout(r, MIN_SPIN_MS));
      Promise.allSettled([latest.current.onRefresh(), spin]).then(() => {
        latest.current.refreshing = false;
        setState({ pull: 0, refreshing: false });
      });
    };

    el.addEventListener("touchstart", onStart, { passive: true });
    // Not passive: a pull has to preventDefault the native scroll.
    el.addEventListener("touchmove", onMove, { passive: false });
    el.addEventListener("touchend", onEnd);
    el.addEventListener("touchcancel", onEnd);
    return () => {
      el.removeEventListener("touchstart", onStart);
      el.removeEventListener("touchmove", onMove);
      el.removeEventListener("touchend", onEnd);
      el.removeEventListener("touchcancel", onEnd);
    };
  }, [el]);

  return { ...state, attach };
}
