/** A drag-to-resize sidebar, shared by every list + detail view (Chat, Tasks, Automations). */
import { clampWidth, MAX_WIDTH, MIN_WIDTH } from "./sidebarWidth";
import type { SidebarWidth } from "./sidebarWidth";
import "./SidebarResizer.css";

// Desktop-only drag handle, placed right after the sidebar. The width lives in
// a CSS variable, so the mobile layout's full-width sidebar simply ignores it.
export default function SidebarResizer({ sidebar }: { sidebar: SidebarWidth }) {
  const { width, defaultWidth, setWidth } = sidebar;
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const handle = e.currentTarget;
    handle.setPointerCapture(e.pointerId);
    const startX = e.clientX;
    const startWidth = width;
    document.body.classList.add("sidebar-resizing");
    const move = (ev: PointerEvent) => setWidth(clampWidth(startWidth + ev.clientX - startX));
    const up = () => {
      document.body.classList.remove("sidebar-resizing");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 40 : 10;
    if (e.key === "ArrowLeft") setWidth(clampWidth(width - step));
    else if (e.key === "ArrowRight") setWidth(clampWidth(width + step));
    else return;
    e.preventDefault();
  };
  return (
    <div
      className="sidebar-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuenow={width}
      aria-valuemin={MIN_WIDTH}
      aria-valuemax={MAX_WIDTH}
      tabIndex={0}
      title="Drag to resize, double-click to reset"
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
      onDoubleClick={() => setWidth(defaultWidth)}
    />
  );
}
