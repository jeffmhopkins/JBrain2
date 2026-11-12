// The brain log for GrowBot's developer (DESIGN.md "GrowBot — the brain log"; binding mock
// docs/mocks/growbot-brain-log/a-share-sheet.html). One sheet from the Body tab: the window,
// whether what people said is hashed, the names to hide, and the download.

import { useState } from "react";
import { type GrowbotBrainLogRequest, api } from "../api/client";
import { splitNames, storeBrainLogNames, storedBrainLogNames } from "../growbot";
import { Sheet } from "./Sheet";

type Hours = GrowbotBrainLogRequest["hours"];

const WINDOWS: { hours: Hours; label: string }[] = [
  { hours: 1, label: "1 hour" },
  { hours: 3, label: "3 hours" },
  { hours: 6, label: "6 hours" },
];

interface Props {
  say: (text: string) => void;
  onClose: () => void;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function GrowbotBrainLogSheet({ say, onClose }: Props) {
  const [hours, setHours] = useState<Hours>(1);
  const [redact, setRedact] = useState(true);
  const [names, setNames] = useState(storedBrainLogNames);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function download() {
    if (busy) return;
    setBusy(true);
    setError("");
    storeBrainLogNames(names);
    try {
      const { blob, filename } = await api.growbotBrainLog({
        hours,
        redact,
        names: splitNames(names),
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1_000);
      say(`Saved ${filename} to Downloads. Send it to GrowBot's developer as it is.`);
      onClose();
    } catch (e) {
      setError(`The brain log didn't download — ${message(e)}`);
      setBusy(false);
    }
  }

  return (
    <Sheet title="Share a brain log" onClose={onClose}>
      <fieldset className="gb-seg pick" aria-label="How far back">
        {WINDOWS.map((w) => (
          <button
            key={w.hours}
            type="button"
            className={hours === w.hours ? "on" : ""}
            aria-pressed={hours === w.hours}
            onClick={() => setHours(w.hours)}
          >
            {w.label}
          </button>
        ))}
      </fieldset>
      <fieldset className="gb-choices">
        <legend>What people said</legend>
        <label className={`gb-choice${redact ? " sel" : ""}`}>
          <input
            type="radio"
            name="gb-brainlog-redact"
            checked={redact}
            onChange={() => setRedact(true)}
          />
          <span className="gb-tx">
            Hash what people said
            <em>
              Recommended. Transcripts and quoted speech become hashes, the way GrowBot's own diary
              does. The creature's words stay.
            </em>
          </span>
        </label>
        <label className={`gb-choice${redact ? "" : " sel"}`}>
          <input
            type="radio"
            name="gb-brainlog-redact"
            checked={!redact}
            onChange={() => setRedact(false)}
          />
          <span className="gb-tx">
            Keep what people said
            <em>Every transcript as it was heard.</em>
          </span>
        </label>
      </fieldset>
      <label className="gb-field">
        Names to hide
        <input
          value={names}
          placeholder="Comma-separated"
          autoComplete="off"
          maxLength={600}
          onChange={(e) => setNames(e.target.value)}
        />
      </label>
      <p className="gb-hint dim">
        Replaced with [name] everywhere in the file, in either mode. Remembered on this phone.
      </p>
      <p className="gb-hint dim">
        Every model turn, transcription and spoken line with where the time went, the creature's own
        diary, and a latency summary. No camera frames, keys, codes or box ids: personas and robots
        are numbered. The creature's soul and its own words go out as written, so list the names
        they use.
      </p>
      {error && (
        <p className="gb-msg bad" role="alert">
          {error}
        </p>
      )}
      <button
        type="button"
        className="gb-btn primary block"
        disabled={busy}
        onClick={() => void download()}
      >
        {busy ? "Gathering the brain log…" : "Download brain log (.zip)"}
      </button>
    </Sheet>
  );
}
