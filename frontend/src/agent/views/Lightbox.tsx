import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

const MAX_SCALE = 5;
const DOUBLE_TAP_SCALE = 2.5;

type Transform = { scale: number; x: number; y: number };
const IDENTITY: Transform = { scale: 1, x: 0, y: 0 };

/** A full-screen image viewer with pinch/wheel zoom and drag-to-pan, rendered in a
 *  portal on `document.body` so it escapes the chat's stacking + overflow. Dismissed
 *  by the close button, a tap on the empty backdrop, or Escape. One finger pans (when
 *  zoomed), two fingers pinch, the wheel zooms toward the cursor, and a double-tap
 *  toggles between fit and a closer look. */
export function Lightbox({
  src,
  alt,
  onClose,
}: {
  src: string;
  alt: string;
  onClose: () => void;
}): ReactNode {
  const [t, setT] = useState<Transform>(IDENTITY);
  // Active pointers (id → last client position): one pans, two pinch.
  const pointers = useRef<Map<number, { x: number; y: number }>>(new Map());
  const pinch = useRef<{ dist: number; scale: number } | null>(null);
  const moved = useRef(false); // a drag/pan must not also register as a backdrop tap
  const lastTap = useRef(0);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    // Freeze the chat behind the viewer while it's open.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  const clamp = (s: number): number => Math.min(MAX_SCALE, Math.max(1, s));

  // Scale toward a screen point, keeping that point anchored. Snapping back to 1
  // recentres so the image can never be stranded off-screen at fit size.
  function zoomAround(cx: number, cy: number, nextScale: number): void {
    setT((cur) => {
      const scale = clamp(nextScale);
      if (scale === 1) return IDENTITY;
      const k = scale / cur.scale;
      return { scale, x: cx - (cx - cur.x) * k, y: cy - (cy - cur.y) * k };
    });
  }

  function spread(): number {
    const pts = [...pointers.current.values()];
    const [a, b] = pts;
    return a && b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
  }
  function midpoint(): { x: number; y: number } {
    const pts = [...pointers.current.values()];
    const [a, b] = pts;
    return a && b ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } : { x: 0, y: 0 };
  }

  function onPointerDown(e: ReactPointerEvent): void {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved.current = false;
    if (pointers.current.size === 2) {
      pinch.current = { dist: spread(), scale: t.scale };
      return;
    }
    // A double-tap (two quick single-finger downs at rest) toggles zoom at the point —
    // the touch equivalent of double-click, which is unreliable on mobile. Handling it
    // on the pointer stream keeps the image free of mouse-only click handlers.
    const now = Date.now();
    if (e.target instanceof HTMLImageElement && now - lastTap.current < 300) {
      if (t.scale > 1) setT(IDENTITY);
      else zoomAround(e.clientX, e.clientY, DOUBLE_TAP_SCALE);
    }
    lastTap.current = now;
  }

  function onPointerMove(e: ReactPointerEvent): void {
    const prev = pointers.current.get(e.pointerId);
    if (!prev) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 2 && pinch.current) {
      moved.current = true;
      zoomAround(
        midpoint().x,
        midpoint().y,
        pinch.current.scale * (spread() / (pinch.current.dist || 1)),
      );
    } else if (pointers.current.size === 1 && t.scale > 1) {
      const dx = e.clientX - prev.x;
      const dy = e.clientY - prev.y;
      if (dx || dy) moved.current = true;
      setT((cur) => ({ ...cur, x: cur.x + dx, y: cur.y + dy }));
    }
  }

  function onPointerUp(e: ReactPointerEvent): void {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
  }

  function onWheel(e: ReactWheelEvent): void {
    zoomAround(e.clientX, e.clientY, t.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
  }

  return createPortal(
    // biome-ignore lint/a11y/useKeyWithClickEvents: Esc (window handler) + the close button cover keyboard; the backdrop click is a pointer-only convenience
    <div
      className="fb-lightbox"
      aria-label={alt || "Image viewer"}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onWheel={onWheel}
      onClick={(e) => {
        if (e.target === e.currentTarget && !moved.current) onClose();
      }}
    >
      <button
        type="button"
        className="fb-lightbox-close"
        aria-label="Close image"
        onClick={onClose}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 6l12 12M18 6 6 18" />
        </svg>
      </button>
      <img
        className="fb-lightbox-img"
        src={src}
        alt={alt}
        draggable={false}
        style={{
          transform: `translate(${t.x}px, ${t.y}px) scale(${t.scale})`,
          cursor: t.scale > 1 ? "grab" : "zoom-in",
        }}
      />
    </div>,
    document.body,
  );
}
