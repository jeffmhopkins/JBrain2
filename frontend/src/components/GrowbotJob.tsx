// A GrowBot flash job's three parts, shared by the Add a robot sheet and the New key sheet: the
// named phase rows (so a failure says which half broke), the failure's recovery, and the log.

import { useEffect, useRef } from "react";
import type { GrowbotFlashJob } from "../api/client";
import { FAIL_COPY, PHASES, PHASE_WORD } from "../growbot";

export function GrowbotPhases({ job }: { job: GrowbotFlashJob }) {
  // A new key is written over MicroPython that is already there, so there is nothing to load.
  const rows = PHASES.filter((p) => !(job.mode === "rekey" && p.id === "load"));
  return (
    <ul className="gb-phases" aria-label="Flash progress">
      {rows.map((p) => {
        const st = job.phases[p.id];
        return (
          <li key={p.id} className={st}>
            <span className="gb-phase-d" aria-hidden="true" />
            <span className="gb-phase-l">{p.label}</span>
            <span className="gb-phase-w">{PHASE_WORD[st]}</span>
          </li>
        );
      })}
    </ul>
  );
}

export function GrowbotFail({ job }: { job: GrowbotFlashJob }) {
  if (job.state !== "fail" || !job.fail) return null;
  const copy = FAIL_COPY[job.fail.code];
  return (
    <p className="gb-msg bad" role="alert">
      {copy ? (
        <>
          <strong>{copy.title}</strong> — {copy.body}
        </>
      ) : (
        job.fail.message
      )}
    </p>
  );
}

export function GrowbotLog({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLPreElement>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: follow the tail on every new line.
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);
  if (lines.length === 0) return null;
  return (
    <pre className="gb-log" ref={ref} aria-label="Flash log">
      {lines.map((l, i) => {
        const cls = /^(ERROR|FAILED)/.test(l) ? "e" : /^(OK|PAIRING CODE)/.test(l) ? "k" : "";
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: log lines repeat; position is identity.
          <span key={i} className={cls || undefined}>
            {l}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
