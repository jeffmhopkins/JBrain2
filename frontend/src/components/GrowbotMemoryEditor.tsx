/** The editor for one of a persona's own memories (owner, 2026-11-12: "basically all these
 *  should be editable by me"). The creature writes them itself; a correction here is where it
 *  goes on from. Each field is capped where the creature caps it, and the box cleans it the same
 *  way and checkpoints first. Refusals land in place and say Nothing was saved. */
import { useState } from "react";
import {
  type GrowbotPersona,
  type GrowbotRegion,
  type GrowbotSoul,
  type GrowbotWritten,
  api,
} from "../api/client";
import { MEMORY_CAPS as CAP, MEMORY, type MemoryId, when, words } from "../growbot";
import { Sheet } from "./Sheet";

interface Props {
  p: GrowbotPersona;
  soul: GrowbotSoul;
  which: MemoryId;
  openOn: string | null;
  onClose: () => void;
  onSaved: (out: GrowbotWritten) => void;
}

interface Line {
  t: number;
  txt: string;
}

const WHAT: Record<MemoryId, string> = {
  identity:
    "Who it thinks it is, in its own words. Its dreams keep adding to this and dropping the oldest sentences.",
  bond: "The one line about its bond with its person. Leave it empty and a dream may write it again.",
  scratch: "What it's doing right now, and the rules it has learned (one per line).",
  goals: "The name it calls itself, and what it wants (one per line, four at most).",
  log: "Its diary, newest first. Change a line's words, or empty it to take it out.",
};

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** One line per non-empty row of a textarea. */
function lines(text: string): string[] {
  return text
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
}

export function GrowbotMemoryEditor({ p, soul, which, openOn, onClose, onSaved }: Props) {
  const info = MEMORY.find((m) => m.id === which) ?? MEMORY[0];
  const [text, setText] = useState(() =>
    which === "identity" ? str(soul.identity) : which === "bond" ? str(soul.identityCore) : "",
  );
  const [state, setState] = useState(str(soul.scratch?.state));
  const [rules, setRules] = useState(() =>
    (soul.scratch?.rules ?? []).map(words).filter(Boolean).join("\n"),
  );
  const [selfName, setSelfName] = useState(str(soul.goals?.selfName));
  const [wants, setWants] = useState(() =>
    (soul.goals?.wants ?? []).map(words).filter(Boolean).join("\n"),
  );
  const [diary, setDiary] = useState<Line[]>(() =>
    (soul.log ?? [])
      .filter((l): l is Line => typeof l?.txt === "string" && typeof l.t === "number")
      .map((l) => ({ t: l.t, txt: l.txt }))
      .sort((a, b) => b.t - a.t),
  );
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  function value(): unknown {
    if (which === "identity") {
      if (text.trim().length < CAP.identityMin) {
        throw new Error(
          `It needs at least ${CAP.identityMin} characters, or the creature starts over from its seed. Nothing was saved.`,
        );
      }
      return text;
    }
    if (which === "bond") return text;
    if (which === "scratch") {
      const r = lines(rules);
      if (r.length > CAP.rules) {
        throw new Error(
          `That's ${r.length} rules; the most it keeps is ${CAP.rules}. Nothing was saved.`,
        );
      }
      return { state, rules: r };
    }
    if (which === "goals") {
      const w = lines(wants);
      if (w.length > CAP.wants) {
        throw new Error(
          `That's ${w.length} wants; the most it keeps is ${CAP.wants}. Nothing was saved.`,
        );
      }
      return { selfName, wants: w };
    }
    return diary.filter((l) => l.txt.trim());
  }

  async function save() {
    let v: unknown;
    try {
      v = value();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return;
    }
    setBusy(true);
    setErr("");
    try {
      onSaved(await api.editGrowbotRegion(p.id, info.up as GrowbotRegion, v));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  const error = err ? (
    <p className="gb-msg bad" role="alert">
      {err}
    </p>
  ) : null;

  return (
    <Sheet title={`${info.label} — ${p.name}`} onClose={onClose}>
      <p className="gb-hint dim">{WHAT[which]}</p>
      {(which === "identity" || which === "bond") && (
        <label className="gb-field">
          {info.label}
          <textarea
            rows={which === "identity" ? 8 : 3}
            maxLength={which === "identity" ? CAP.identity : CAP.bond}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
      )}
      {which === "scratch" && (
        <>
          <label className="gb-field">
            Right now
            <input value={state} maxLength={CAP.state} onChange={(e) => setState(e.target.value)} />
          </label>
          <label className="gb-field">
            Rules it has learned
            <textarea rows={6} value={rules} onChange={(e) => setRules(e.target.value)} />
          </label>
        </>
      )}
      {which === "goals" && (
        <>
          <label className="gb-field">
            Calls itself
            <input
              value={selfName}
              maxLength={CAP.selfName}
              onChange={(e) => setSelfName(e.target.value)}
            />
          </label>
          <label className="gb-field">
            Wants
            <textarea rows={4} value={wants} onChange={(e) => setWants(e.target.value)} />
          </label>
        </>
      )}
      {which === "log" &&
        (diary.length ? (
          diary.map((l, i) => (
            <label key={`${l.t}-${i}`} className="gb-field">
              {when(new Date(l.t).toISOString())}
              <input
                value={l.txt}
                maxLength={CAP.line}
                onChange={(e) =>
                  setDiary((d) => d.map((x, j) => (j === i ? { ...x, txt: e.target.value } : x)))
                }
              />
            </label>
          ))
        ) : (
          <p className="gb-pv dim">No diary lines yet.</p>
        ))}
      {error}
      <p className="gb-hint dim gb-gap">
        Saving makes a checkpoint of {p.name} first, so you can undo it.
        {openOn ? ` It's open ${openOn}; it reloads with this the next time it's idle.` : ""}
      </p>
      <div className="gb-sheet-foot">
        <button
          type="button"
          className="gb-btn primary block"
          disabled={busy}
          onClick={() => void save()}
        >
          Save
        </button>
      </div>
    </Sheet>
  );
}
