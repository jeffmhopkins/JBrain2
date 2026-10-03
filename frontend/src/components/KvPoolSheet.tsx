// A pool model's KV pool (Flash-Next: one shared pool, eight role-pinned slots), read-only.
// The server owns the window and the slot split and refuses changes (409), so the On-box row
// shows one quiet line in place of the window/slot selects and this Sheet lists the slots.
// GUI gate F3b, variant C — docs/mocks/kv-pool/c-slot-sheet.html, DESIGN.md "KV pool view".
// Live per-slot use is not in the API yet; the mock's in-use tile, lit ticks and filled bars
// stay out until it is, rather than being drawn from numbers nobody measured.

import { useEffect, useState } from "react";
import type { KvPool } from "../api/client";
import { useEngineSnapshot } from "../engineState";
import { Sheet } from "./Sheet";
import { ArrowRightIcon, InfoIcon, LockIcon } from "./icons";

// What each role's slot serves — the tap-to-expand line. The API carries only a short label,
// and this sentence is the owner-facing explanation of the routing, so it lives here. A Map,
// so a role named like an Object.prototype key can never read an inherited value.
export const SLOT_SERVES: ReadonlyMap<string, string> = new Map([
  ["interactive", "Your chat turns and the omnibox."],
  ["ingest", "Note ingest and the analysis pipeline."],
  ["scheduled", "Workflow runs fired on a schedule."],
  ["research", "Deep research and the sub-agents it fans out."],
  ["jcode", "jcode coding sessions."],
  ["workshop", "Wiki writing, note edits and guided intake."],
  ["pet", "The kid pet."],
  ["small", "Short one-off prompts."],
]);

// Not the screen's fmtTokens: that one goes decimal at M, and the pool and its caps are
// powers of two, so a decimal "1.4M" of caps beside a "1M" pool would misstate the
// overcommit (the caps add up to more than the pool). Binary throughout, and the k/M boundary
// is taken after rounding so a just-under-1M count can never print as "1024k".
export function fmtTokens(n: number): string {
  if (n < 1024) return `${n}`;
  const k = Math.round(n / 1024);
  if (k >= 1024) return `${+(n / 1_048_576).toFixed(2)}M`;
  return `${k}k`;
}

// Whether the pool model is off-engine — its engine is not the one serving — rather than
// blocked for some other reason (hosting off, not installed, switched unavailable), which
// would make "the engine is stopped" false. The engine store is the source; the load
// route's own sentence is the fallback for a server or a moment without it.
export function poolOffEngine(
  engine: string | undefined,
  loaded: boolean,
  blockedReason: string | null | undefined,
  effective: string | null | undefined,
): boolean {
  if (loaded) return false;
  if (engine !== undefined && effective) return engine !== effective;
  return blockedReason?.startsWith("Runs on the ") ?? false;
}

export function KvPoolLine({
  pool,
  title,
  engine,
  loaded,
  blockedReason,
}: {
  pool: KvPool;
  title: string;
  engine: string | undefined;
  loaded: boolean;
  blockedReason: string | null | undefined;
}) {
  const [open, setOpen] = useState(false);
  const effective = useEngineSnapshot().state?.effective;
  const offEngine = poolOffEngine(engine, loaded, blockedReason, effective);
  return (
    <div className="llm-local-ctx kvp-line">
      <span className="llm-local-ctx-label">KV pool</span>
      <span className="kvp-sum">
        <b>{fmtTokens(pool.n_ctx)}</b> shared · {pool.slots.length} slots
      </span>
      {/* Unlit until the API reports per-slot use: a hint at the slot count, not a reading. */}
      <span className="kvp-ticks" aria-hidden="true">
        {pool.slots.map((s) => (
          <i key={s.slot} />
        ))}
      </span>
      <button
        type="button"
        className="kvp-open"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
      >
        View slots
        <ArrowRightIcon size={12} />
      </button>
      {open && (
        <KvPoolSheet
          pool={pool}
          title={title}
          offEngine={offEngine}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}

export function KvPoolSheet({
  pool,
  title,
  offEngine,
  onClose,
}: {
  pool: KvPool;
  title: string;
  offEngine: boolean;
  onClose: () => void;
}) {
  const [sel, setSel] = useState<number | null>(null);
  // Set only by an overflow link, so a plain tap never scrolls the list under the finger.
  const [jumpTo, setJumpTo] = useState<number | null>(null);
  const maxCap = Math.max(1, ...pool.slots.map((s) => s.cap));
  const capSum = pool.slots.reduce((a, s) => a + s.cap, 0);

  useEffect(() => {
    if (jumpTo === null) return;
    const row = document.getElementById(`kvp-slot-${jumpTo}`);
    row?.scrollIntoView?.({ block: "nearest" });
    // The link that was tapped is gone once its row collapses; land focus on the target
    // row so a keyboard or screen-reader user is not dropped back to the top of the sheet.
    row?.querySelector<HTMLButtonElement>(".kvp-slot-btn")?.focus();
    setJumpTo(null);
  }, [jumpTo]);

  function jump(slot: number) {
    setSel(slot);
    setJumpTo(slot);
  }

  return (
    <Sheet title={title} onClose={onClose}>
      <span className="kvp-lock">
        <LockIcon size={14} />
        set by the engine
      </span>
      <p className="kvp-lede">
        One shared memory pool; each job has its own slot.
        {offEngine ? " The engine is stopped, so these are the caps it will use." : ""}
      </p>
      <div className="kvp-stats">
        <div>
          <div className="kvp-k">Pool</div>
          <div className="kvp-v">
            {fmtTokens(pool.n_ctx)} <small>tokens</small>
          </div>
        </div>
        <div>
          <div className="kvp-k">Slots</div>
          <div className="kvp-v">{pool.slots.length}</div>
        </div>
        <div>
          <div className="kvp-k">Caps total</div>
          <div className="kvp-v">{fmtTokens(capSum)}</div>
        </div>
      </div>
      <ul className="kvp-list">
        {pool.slots.map((s) => {
          const open = sel === s.slot;
          const spill = s.overflow;
          const target = spill ? pool.slots.find((t) => t.role === spill) : undefined;
          return (
            <li
              key={s.slot}
              id={`kvp-slot-${s.slot}`}
              className={`kvp-slot kvp-role-${s.role}${open ? " hl" : ""}`}
            >
              <button
                type="button"
                className="kvp-slot-btn"
                aria-expanded={open}
                onClick={() => setSel(open ? null : s.slot)}
              >
                <span className="kvp-num">{s.slot}</span>
                <span className="kvp-mid">
                  <span className="kvp-top">
                    <span className="kvp-label">{s.label}</span>
                    <span className="llm-chip kvp-rchip">{s.role}</span>
                  </span>
                  <span className="kvp-capbar">
                    <i style={{ width: `${(s.cap / maxCap) * 100}%` }} />
                  </span>
                </span>
                <span className="kvp-val">
                  {fmtTokens(s.cap)}
                  <small>cap</small>
                </span>
              </button>
              {open && (
                <div className="kvp-exp">
                  {SLOT_SERVES.get(s.role) ?? `Serves the ${s.role} role.`}
                  {target && (
                    <>
                      {` When it fills, its prompts spill to ${target.label}. `}
                      <button type="button" className="kvp-link" onClick={() => jump(target.slot)}>
                        Show {target.label}
                      </button>
                    </>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <div className="kvp-foot">
        <InfoIcon size={15} />
        <span>
          {capSum > pool.n_ctx
            ? `Caps are per-slot limits. Together they come to ${fmtTokens(capSum)}, more than the ${fmtTokens(pool.n_ctx)} pool, because not every job runs at once. If the pool would overrun, the router frees an idle slot.`
            : `Caps are per-slot limits within the ${fmtTokens(pool.n_ctx)} pool.`}
        </span>
      </div>
      <button type="button" className="kvp-done" onClick={onClose}>
        Done
      </button>
    </Sheet>
  );
}
