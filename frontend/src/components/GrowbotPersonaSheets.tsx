/** The GrowBot screen's persona parts that more than one place shows: the avatar and where-line,
 *  the launch sheet (Open <name>), New persona and Import a soul file (DESIGN.md "GrowBot — the
 *  desk robot's screen"; wording from docs/mocks/growbot-personas/shared.js). */
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { type GrowbotCheckpoint, type GrowbotPersona, type GrowbotSoul, api } from "../api/client";
import {
  type Places,
  day,
  initial,
  onPhone,
  phoneName,
  riderOfPersonas,
  robotName,
  where,
} from "../growbot";
import { Sheet } from "./Sheet";

export type Say = (
  text: string,
  opts?: { href?: string; undo?: () => void; label?: string; onTap?: () => void },
) => void;

export interface TabDef<T extends string> {
  id: T;
  label: string;
  /** Read after the label ("Personas, 3"), never glued into it. */
  count?: number;
}

/** A segmented tab strip with the ARIA tabs pattern: one tab in the Tab order (roving
 *  tabindex), arrows and Home/End move between them, and each controls its panel `${base}-panel`. */
export function GrowbotTabs<T extends string>({
  base,
  label,
  tabs,
  value,
  onPick,
  inner = false,
}: {
  base: string;
  label: string;
  tabs: TabDef<T>[];
  value: T;
  onPick: (t: T) => void;
  inner?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  function key(e: KeyboardEvent<HTMLDivElement>) {
    const at = tabs.findIndex((t) => t.id === value);
    const to =
      e.key === "ArrowRight"
        ? (at + 1) % tabs.length
        : e.key === "ArrowLeft"
          ? (at - 1 + tabs.length) % tabs.length
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? tabs.length - 1
              : -1;
    const next = tabs[to];
    if (!next) return;
    e.preventDefault();
    onPick(next.id);
    ref.current?.querySelector<HTMLElement>(`#${base}-tab-${next.id}`)?.focus();
  }
  return (
    <div
      ref={ref}
      className={`gb-seg${inner ? " inner" : ""}`}
      role="tablist"
      aria-label={label}
      onKeyDown={key}
    >
      {tabs.map((t) => (
        <button
          key={t.id}
          id={`${base}-tab-${t.id}`}
          type="button"
          role="tab"
          aria-selected={value === t.id}
          aria-controls={`${base}-panel`}
          aria-label={t.count != null ? `${t.label}, ${t.count}` : undefined}
          tabIndex={value === t.id ? 0 : -1}
          className={value === t.id ? "on" : ""}
          onClick={() => onPick(t.id)}
        >
          {t.label}
          {t.count != null && (
            <span className="gb-n" aria-hidden="true">
              {t.count}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

export const NEARBY =
  "On Android, first allow Chrome to reach the home network: Settings → Apps → Chrome → Permissions → Nearby devices (or Local network) → Allow. Without it Chrome says ERR_TOO_MANY_RETRIES.";

/** The upstream soul cap (backend growbot/souls.py MAX_SOUL_BYTES): refused here before upload. */
const MAX_SOUL_BYTES = 1_000_000;

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** An initial in a disc: a violet ring while it wears the body, steel while open here. */
export function GrowbotAvatar({
  p,
  pl,
  small = false,
}: {
  p: GrowbotPersona;
  pl: Places;
  small?: boolean;
}) {
  const ring = p.wearing ? " wearing" : p.phone && p.phone === pl.thisPhone ? " here" : "";
  return (
    <span className={`gb-av${small ? " sm" : ""}${ring}`} aria-hidden="true">
      {initial(p.name)}
    </span>
  );
}

export function GrowbotWhere({ p, pl }: { p: GrowbotPersona; pl: Places }) {
  const w = where(p, pl);
  return (
    <span className="gb-state">
      <span className={`gb-dot ${w.level}`} aria-hidden="true" />
      <span className="gb-t2">{w.text}</span>
    </span>
  );
}

interface LaunchProps {
  p: GrowbotPersona;
  personas: GrowbotPersona[];
  pl: Places;
  up: boolean;
  onHere: (code: string | null) => void;
  onOther: () => void;
  onClose: () => void;
}

/** Open <name>: three big choices, each saying what happens to the body and to the phone it
 *  leaves before the tap. A second robot gets its own wearing choice. */
export function GrowbotLaunchSheet({ p, personas, pl, up, onHere, onOther, onClose }: LaunchProps) {
  const here = personas.find((x) => x.phone && x.phone === pl.thisPhone) ?? null;
  const repl = here && here.id !== p.id ? ` ${here.name} closes here — it's kept on the box.` : "";
  const fromPh =
    p.phone && p.phone !== pl.thisPhone ? ` It moves from ${phoneName(p.phone, pl)}.` : "";
  const herewear = !!p.phone && p.phone === pl.thisPhone;
  const worn = p.wearing ? robotName(p.wearing, pl) : "";
  const bodies = pl.robots;
  const first = bodies.find((r) => !r.revoked_at) ?? bodies[0];
  return (
    <Sheet title={`Open ${p.name}`} onClose={onClose}>
      <button type="button" className="gb-big" onClick={() => onHere(null)}>
        <span className="gb-k" aria-hidden="true" />
        <span className="gb-tx">
          <span className="gb-t1">On this phone, alone</span>
          <span className="gb-t2">
            No legs.{fromPh}
            {repl}
            {worn ? ` ${worn} stays where it is.` : ""}
          </span>
        </span>
      </button>
      {bodies.map((r) => {
        const b = r.name || r.code;
        const other = riderOfPersonas(personas, r.code);
        // Reopening what this phone already wears is a fresh tab on the same body: the relay
        // hands the body to the new socket, so it is offered, never greyed out.
        const again = p.wearing === r.code && herewear;
        const why = r.revoked_at
          ? `${b}'s key is revoked — reflash it first.`
          : again
            ? `Opens again, still wearing ${b}.${up && r.connected ? "" : ` ${b} is off — its legs move once it's on.`}`
            : `${
                other && other.id !== p.id
                  ? `${other.name} lets go and carries on alone${other.phone ? ` ${onPhone(other.phone, pl)}` : ""}.`
                  : `It takes ${b}'s legs.`
              }${up && r.connected ? "" : ` ${b} is off — its legs move once it's on.`}`;
        return (
          <button
            key={r.code}
            type="button"
            className="gb-big wear"
            disabled={!!r.revoked_at}
            onClick={() => onHere(r.code)}
          >
            <span className="gb-k" aria-hidden="true" />
            <span className="gb-tx">
              <span className="gb-t1">On this phone, wearing {b}</span>
              <span className="gb-t2">
                {why}
                {r.revoked_at || again ? "" : `${fromPh}${repl}`}
              </span>
            </span>
          </button>
        );
      })}
      <button type="button" className="gb-big" onClick={onOther}>
        <span className="gb-k other" aria-hidden="true" />
        <span className="gb-tx">
          <span className="gb-t1">On another phone…</span>
          <span className="gb-t2">
            {first
              ? `Two codes to scan, alone or wearing ${first.name || first.code}.`
              : "Two codes to scan."}
          </span>
        </span>
      </button>
    </Sheet>
  );
}

interface Seed {
  id: string | null;
  label: string;
  em: string;
}

interface NewProps {
  personas: GrowbotPersona[];
  initialSeed: string | null;
  say: Say;
  onMade: (id: string) => void;
  onImport: () => void;
  onClose: () => void;
}

/** New persona: its name, then where its personality starts — a fresh creature, or any
 *  checkpoint's personality (a seed is a personality; Duplicate copies a creature). */
export function GrowbotNewPersonaSheet({
  personas,
  initialSeed,
  say,
  onMade,
  onImport,
  onClose,
}: NewProps) {
  const [name, setName] = useState("");
  const [seed, setSeed] = useState<string | null>(initialSeed);
  const [cps, setCps] = useState<{ p: GrowbotPersona; c: GrowbotCheckpoint }[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // biome-ignore lint/correctness/useExhaustiveDependencies: read once, when the sheet opens.
  useEffect(() => {
    let live = true;
    const withCps = personas.filter((p) => p.checkpoints > 0);
    void Promise.all(withCps.map((p) => api.growbotPersona(p.id).catch(() => null))).then((all) => {
      if (!live) return;
      setCps(all.flatMap((d) => (d ? d.checkpoint_list.map((c) => ({ p: d, c })) : [])));
    });
    return () => {
      live = false;
    };
  }, []);
  const seeds: Seed[] = [
    {
      id: null,
      label: "A fresh creature",
      em: "Upstream's own starting personality — Sprout, just woken up.",
    },
    ...cps.map(({ p, c }) => ({
      id: c.id,
      label: `${p.name} — ${c.name}`,
      em: `Its personality from ${day(c.created_at)}. Memories start fresh.`,
    })),
  ];
  const n = name.trim();

  async function make() {
    if (!n || busy) return;
    setBusy(true);
    setError("");
    try {
      const made = await api.newGrowbotPersona(n, seed ?? undefined);
      say(
        seed
          ? `Made ${made.name} from ${made.made_from}.`
          : `Made ${made.name} as a fresh creature.`,
      );
      onMade(made.id);
    } catch (e) {
      setError(`${message(e)}`);
      setBusy(false);
    }
  }

  return (
    <Sheet title="New persona" onClose={onClose}>
      <label className="gb-field">
        Its name
        <input
          value={name}
          placeholder="e.g. Sprout"
          autoComplete="off"
          maxLength={80}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <fieldset className="gb-choices">
        <legend>Start from</legend>
        {seeds.map((s) => (
          <label key={s.id ?? "fresh"} className={`gb-choice${seed === s.id ? " sel" : ""}`}>
            <input
              type="radio"
              name="gb-seed"
              checked={seed === s.id}
              onChange={() => setSeed(s.id)}
            />
            <span className="gb-tx">
              {s.label}
              <em>{s.em}</em>
            </span>
          </label>
        ))}
      </fieldset>
      <p className="gb-hint dim">
        To start from a whole creature, memories and all, use Duplicate on it instead. To start from
        a file,{" "}
        <button type="button" className="gb-linkbtn inline" onClick={onImport}>
          import a soul file
        </button>
        .
      </p>
      {error && (
        <p className="gb-msg bad" role="alert">
          {error}
        </p>
      )}
      <div className="gb-sheet-foot">
        <button
          type="button"
          className="gb-btn primary block"
          disabled={!n || busy}
          onClick={() => void make()}
        >
          Make {n || "it"}
        </button>
      </div>
    </Sheet>
  );
}

type Picked =
  | { ok: true; soul: GrowbotSoul; file: string; size: number }
  | { ok: false; why: "big" | "bad" | "old"; size: number };

function readText(file: File): Promise<string> {
  return new Promise((done, fail) => {
    const r = new FileReader();
    r.onload = () => done(String(r.result ?? ""));
    r.onerror = () => fail(r.error);
    r.readAsText(file);
  });
}

/** Read a picked file and check it before anything changes: the size first, so a huge file
 *  costs nothing to refuse, then that it is a soul_format 3 document. */
export async function checkSoulFile(file: File): Promise<Picked> {
  if (file.size > MAX_SOUL_BYTES) return { ok: false, why: "big", size: file.size };
  try {
    const soul = JSON.parse(await readText(file)) as GrowbotSoul;
    if (!soul || typeof soul !== "object" || Array.isArray(soul)) {
      return { ok: false, why: "bad", size: file.size };
    }
    if (soul.soul_format !== 3) {
      // The box's own test (growbot/souls.py from_file): a numbered format or an identity is
      // an older soul, which the creature can re-export; anything else isn't a soul at all.
      const old = typeof soul.soul_format === "number" || "identity" in soul;
      return { ok: false, why: old ? "old" : "bad", size: file.size };
    }
    return { ok: true, soul, file: file.name, size: file.size };
  } catch {
    return { ok: false, why: "bad", size: file.size };
  }
}

function size(bytes: number): string {
  return bytes >= 1e6
    ? `${(bytes / 1e6).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1e3))} KB`;
}

interface ImportProps {
  /** The persona it may land over; null imports only as a new persona. */
  over: GrowbotPersona | null;
  say: Say;
  onMade: (id: string) => void;
  onOver: () => void;
  onClose: () => void;
}

export function GrowbotImportSheet({ over, say, onMade, onOver, onClose }: ImportProps) {
  const input = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<Picked | null>(null);
  const [as, setAs] = useState<"new" | "over">(over ? "over" : "new");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function pick(file: File | undefined) {
    if (!file) return;
    setError("");
    const got = await checkSoulFile(file);
    setPicked(got);
    // A file's meta is whatever its author wrote; only a string is a name.
    if (got.ok) setName(typeof got.soul.meta?.name === "string" ? got.soul.meta.name : "");
  }

  async function bring() {
    if (!picked?.ok || busy) return;
    setBusy(true);
    setError("");
    try {
      if (as === "over" && over) {
        const out = await api.importGrowbotPersona(picked.soul, { over: over.id });
        const cp = out.checkpoint;
        say(
          `${over.name} is now the imported soul, and its visual memories are cleared.${cp ? ` “${cp.name}” keeps what it was, visual memories too.` : ""}`,
          cp
            ? {
                undo: () => {
                  void api
                    .restoreGrowbotCheckpoint(over.id, cp.id)
                    .then(() => {
                      say(`Undone — ${over.name} is as it was before.`);
                      onOver();
                    })
                    .catch((e) => say(`It wasn't undone — ${message(e)}`));
                },
              }
            : undefined,
        );
        onOver();
      } else {
        const out = await api.importGrowbotPersona(
          picked.soul,
          name.trim() ? { name: name.trim() } : {},
        );
        say(`Imported ${out.persona.name} as a new persona.`);
        onMade(out.persona.id);
      }
    } catch (e) {
      setError(message(e));
      setBusy(false);
    }
  }

  const grown = picked?.ok && Array.isArray(picked.soul.log) ? picked.soul.log.length : 0;
  return (
    <Sheet title="Import a soul file" onClose={onClose}>
      <p className="gb-hint">
        A soul file is what Export makes, or one from upstream's soul gallery. It's checked before
        anything changes: its settings are cleaned the same way the creature cleans an import, and
        it can't remove the child-safety rules.
      </p>
      <input
        ref={input}
        type="file"
        accept=".json,application/json"
        className="gb-file"
        aria-label="Soul file"
        onChange={(e) => {
          void pick(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      {picked && !picked.ok && (
        <p className="gb-msg bad" role="alert">
          {picked.why === "big" ? (
            <>
              <strong>That file is too big</strong> — {size(picked.size)}, and a soul is at most 1
              MB. Nothing changed. Export it again from the creature, or pick another.
            </>
          ) : picked.why === "old" ? (
            <>
              <strong>That soul file is an older format.</strong> Open it in the creature's MIND tab
              and export it again. Nothing changed.
            </>
          ) : (
            <>
              <strong>That isn't a soul file</strong> — it has no soul format in it. Nothing
              changed. Pick a file that Export or the creature's MIND tab made.
            </>
          )}
        </p>
      )}
      {picked?.ok && (
        <>
          <p className="gb-msg ok">
            Checked — <span className="gb-mono">{picked.file}</span>, {size(picked.size)}
            {grown ? `, a grown creature with ${grown} diary line${grown === 1 ? "" : "s"}` : ""}.
          </p>
          <fieldset className="gb-choices">
            <legend>Bring it in</legend>
            <label className={`gb-choice${as === "new" ? " sel" : ""}`}>
              <input
                type="radio"
                name="gb-imp"
                checked={as === "new"}
                onChange={() => setAs("new")}
              />
              <span className="gb-tx">
                As a new persona
                <em>Nothing else changes.</em>
              </span>
            </label>
            {over && (
              <label className={`gb-choice${as === "over" ? " sel" : ""}`}>
                <input
                  type="radio"
                  name="gb-imp"
                  checked={as === "over"}
                  onChange={() => setAs("over")}
                />
                <span className="gb-tx">
                  Over {over.name}
                  <em>
                    Its visual memories are cleared. A checkpoint of {over.name} is saved first and
                    keeps them.
                  </em>
                </span>
              </label>
            )}
          </fieldset>
          {as === "new" && (
            <label className="gb-field">
              Its name
              <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
            </label>
          )}
        </>
      )}
      {error && (
        <p className="gb-msg bad" role="alert">
          {error}
        </p>
      )}
      <div className="gb-sheet-foot">
        {picked?.ok && (
          <button
            type="button"
            className="gb-btn primary block"
            disabled={busy}
            onClick={() => void bring()}
          >
            {as === "over" && over ? `Import over ${over.name}` : "Import"}
          </button>
        )}
        <button
          type="button"
          className={`gb-btn block${picked?.ok ? "" : " primary"}`}
          disabled={busy}
          onClick={() => input.current?.click()}
        >
          {picked ? "Choose another file" : "Choose a file"}
        </button>
      </div>
    </Sheet>
  );
}
