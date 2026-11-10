/** A persona's visual memories (DESIGN.md "A persona's page", *Memories*; binding mock
 *  `G.vmHTML` in docs/mocks/growbot-personas/shared.js): a grid of saved views, each with an
 *  armed Delete, and a sheet that shows all of one memory's views. The page owns the list and
 *  the arming, so the grid and the sheet arm the same Delete. */
import { useState } from "react";
import { type GrowbotMemory, type GrowbotMemoryKind, growbotMemoryViewUrl } from "../api/client";
import { day, memoryLabel } from "../growbot";
import { Sheet } from "./Sheet";

const KIND_WORD: Record<GrowbotMemoryKind, string> = {
  person: "A person",
  object: "A thing",
  handwriting: "Handwriting",
  scene: "A scene",
  place: "A place",
};

// The placeholder while a view loads, or if it can't: a glyph, never a face.
function Glyph({ kind }: { kind: GrowbotMemoryKind }) {
  const path =
    kind === "person" ? (
      <>
        <circle cx="12" cy="8" r="4" />
        <path d="M4 21c1-4 4-6 8-6s7 2 8 6" />
      </>
    ) : kind === "handwriting" ? (
      <>
        <rect x="5" y="3" width="14" height="18" rx="2" />
        <path d="M8 8h8M8 12h8M8 16h5" />
      </>
    ) : kind === "scene" ? (
      <>
        <rect x="3" y="5" width="18" height="14" rx="2" />
        <path d="M3 16l5-5 4 4 3-3 6 6" />
      </>
    ) : kind === "place" ? (
      <>
        <path d="M12 21s-6-6.2-6-11a6 6 0 0 1 12 0c0 4.8-6 11-6 11z" />
        <circle cx="12" cy="10" r="2" />
      </>
    ) : (
      <>
        <path d="M5 8h11v8a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z" />
        <path d="M16 10h2a2 2 0 0 1 0 4h-2" />
      </>
    );
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
      focusable="false"
    >
      {path}
    </svg>
  );
}

/** One view, square: the glyph shows until the picture loads, and stays if it can't. */
function View({ pid, m, i, alt }: { pid: string; m: GrowbotMemory; i: number; alt: string }) {
  const [state, setState] = useState<"wait" | "ok" | "bad">("wait");
  return (
    <span className={`gb-vm-pic${state === "ok" ? " ok" : ""}`}>
      <Glyph kind={m.kind} />
      {state !== "bad" && (
        <img
          src={growbotMemoryViewUrl(pid, m.id, i)}
          alt={alt}
          loading="lazy"
          decoding="async"
          onLoad={() => setState("ok")}
          onError={() => setState("bad")}
        />
      )}
    </span>
  );
}

interface Props {
  pid: string;
  memories: GrowbotMemory[];
  isArmed: (m: GrowbotMemory) => boolean;
  /** Arms on the first tap, forgets on the second. Once it is gone it leaves the list, and
   *  the sheet showing it closes with it. */
  onDelete: (m: GrowbotMemory) => void;
}

function DeleteButton({
  m,
  armed,
  block = false,
  onTap,
}: {
  m: GrowbotMemory;
  armed: boolean;
  block?: boolean;
  onTap: () => void;
}) {
  const label = memoryLabel(m);
  return (
    <button
      type="button"
      className={
        block ? `gb-btn block danger${armed ? " armed" : ""}` : `gb-vm-del${armed ? " armed" : ""}`
      }
      aria-label={armed ? `Tap again to forget ${label}` : `Delete ${label}`}
      onClick={onTap}
    >
      {armed ? "Tap again" : "Delete"}
    </button>
  );
}

export function GrowbotMemoryGrid({ pid, memories, isArmed, onDelete }: Props) {
  const [looking, setLooking] = useState<string | null>(null);
  const open = memories.find((m) => m.id === looking) ?? null;
  return (
    <>
      <ul className="gb-vm-grid" aria-label="Saved views">
        {memories.map((m) => {
          const label = memoryLabel(m);
          return (
            <li key={m.id} className="gb-vm">
              <button
                type="button"
                className="gb-vm-look"
                aria-label={`Look at ${label}`}
                onClick={() => setLooking(m.id)}
              >
                <View pid={pid} m={m} i={0} alt={label} />
              </button>
              <span className="gb-vm-cap">{label}</span>
              <span className="gb-vm-when">{m.saved_at ? day(m.saved_at) : "date unknown"}</span>
              <DeleteButton m={m} armed={isArmed(m)} onTap={() => onDelete(m)} />
            </li>
          );
        })}
      </ul>
      {open && (
        <Sheet title={memoryLabel(open)} onClose={() => setLooking(null)}>
          <ul className="gb-vm-views" aria-label="Its saved views">
            {Array.from({ length: Math.max(1, open.views) }, (_, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a view is its index on the box.
              <li key={i}>
                <View
                  pid={pid}
                  m={open}
                  i={i}
                  alt={`${memoryLabel(open)}, view ${i + 1} of ${Math.max(1, open.views)}`}
                />
              </li>
            ))}
          </ul>
          <p className="gb-hint">
            {KIND_WORD[open.kind] ?? KIND_WORD.object}
            {open.saved_at ? ` · saved ${day(open.saved_at)}` : ""}
          </p>
          {open.desc && <p className="gb-pv">{open.desc}</p>}
          <div className="gb-sheet-foot">
            <DeleteButton m={open} armed={isArmed(open)} block onTap={() => onDelete(open)} />
          </div>
        </Sheet>
      )}
    </>
  );
}
