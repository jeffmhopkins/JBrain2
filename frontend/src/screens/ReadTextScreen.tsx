// A full-screen "read custom text aloud" surface, opened from the Settings read-aloud
// voice picker. The owner pastes arbitrary prose (a book chapter, a note) into the text area —
// or uploads a .md/.txt file, whose contents drop straight into the area to review and edit —
// and either PLAYS it on the box in the chosen voice, or EXPORTS the whole thing to a single WAV
// file to keep. Both paths reuse the chat read-aloud pipeline: chunkStream splits the text into
// clips small enough for the /tts cap — normalized for the SAME engine an agent reply uses (a
// Kokoro voice on Kokoro's profile) — each renders on the box, and the clips play (gaplessly,
// one prefetched ahead) or concatenate into one WAV for download.
//
// Piper-engine only: it needs the box to render audio, so it's surfaced only when an on-box
// voice is chosen (never the device's Native voice, which can't be captured to a file).

import { useCallback, useEffect, useRef, useState } from "react";
import { chunkStream, engineForVoice } from "../agent/speakable.js";
import { api } from "../api/client";
import { ChevronLeftIcon } from "../components/icons";

interface ReadTextScreenProps {
  /** The on-box voice id (brain_answer_voice) clips render in — piper or kokoro. */
  voice: string;
  onClose: () => void;
}

// Render one clip on the box. The first clip keeps piper's default silence lead; continuation
// clips ask for lead=0 so a multi-clip render plays / exports gaplessly — same convention the
// chat read-aloud uses.
function renderClip(voice: string, text: string, first: boolean): Promise<Blob> {
  return api.brainTts(voice, text, first ? undefined : 0);
}

function writeAscii(bytes: Uint8Array, offset: number, ascii: string): void {
  for (let i = 0; i < ascii.length; i++) bytes[offset + i] = ascii.charCodeAt(i);
}

/**
 * Concatenate a sequence of WAV blobs (same voice → same PCM format) into one WAV blob:
 * keep the first file's `fmt ` chunk, splice every file's `data` payload together, and write
 * a fresh RIFF header sized for the whole. Walks each RIFF chunk list rather than assuming a
 * fixed 44-byte header, so a file that carries extra chunks still contributes its audio.
 */
export async function concatWav(blobs: Blob[]): Promise<Blob> {
  const buffers = await Promise.all(blobs.map((b) => b.arrayBuffer()));
  let fmtChunk: Uint8Array | null = null;
  const dataPayloads: Uint8Array[] = [];
  for (const buf of buffers) {
    if (buf.byteLength < 12) continue;
    const view = new DataView(buf);
    const bytes = new Uint8Array(buf);
    let o = 12; // skip "RIFF" + size + "WAVE"
    while (o + 8 <= view.byteLength) {
      const id = String.fromCharCode(
        bytes[o] ?? 0,
        bytes[o + 1] ?? 0,
        bytes[o + 2] ?? 0,
        bytes[o + 3] ?? 0,
      );
      const size = view.getUint32(o + 4, true);
      const payloadStart = o + 8;
      if (id === "fmt " && !fmtChunk) fmtChunk = bytes.slice(o, payloadStart + size);
      else if (id === "data") dataPayloads.push(bytes.slice(payloadStart, payloadStart + size));
      o = payloadStart + size + (size % 2); // chunks are word-aligned (pad byte on odd sizes)
    }
  }
  if (!fmtChunk) throw new Error("no fmt chunk in rendered audio");
  const dataLen = dataPayloads.reduce((n, d) => n + d.length, 0);
  const bodyLen = 4 /* "WAVE" */ + fmtChunk.length + 8 /* "data" + size */ + dataLen;
  const out = new Uint8Array(8 + bodyLen);
  const dv = new DataView(out.buffer);
  writeAscii(out, 0, "RIFF");
  dv.setUint32(4, bodyLen, true);
  writeAscii(out, 8, "WAVE");
  out.set(fmtChunk, 12);
  let p = 12 + fmtChunk.length;
  writeAscii(out, p, "data");
  dv.setUint32(p + 4, dataLen, true);
  p += 8;
  for (const d of dataPayloads) {
    out.set(d, p);
    p += d.length;
  }
  return new Blob([out], { type: "audio/wav" });
}

// A filesystem-safe name from the first few words of the text, so an exported chapter lands as
// something recognizable rather than a generic blob. Falls back when the text has no words.
function exportName(text: string): string {
  const slug = text
    .trim()
    .slice(0, 60)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${slug || "read-aloud"}.wav`;
}

function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function ReadTextScreen({ voice, onClose }: ReadTextScreenProps) {
  const [text, setText] = useState("");
  const [playing, setPlaying] = useState(false);
  const [exporting, setExporting] = useState(false);
  // Rendered clips so far during an export — drives the "Exporting N/M" progress label.
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A generation token bumped by every stop()/unmount so an in-flight fetch or clip from a
  // superseded playback bails without touching state, and the <audio> playing now (so stop
  // can pause it mid-clip).
  const genRef = useRef(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // The hidden file picker behind the "Upload .md" button.
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // Normalize custom text for the engine that renders the chosen voice — the same profile a chat
  // answer in this voice uses, so a Kokoro voice reads custom text exactly as it reads a reply.
  const engine = engineForVoice(voice);
  // Resolves the clip playing right now — pausing an <audio> fires no `ended`, so stop() calls
  // this to unstick the loop's `await playBlob` instead of leaving it hung on a paused clip.
  const playResolveRef = useRef<(() => void) | null>(null);

  const stop = useCallback(() => {
    genRef.current += 1;
    const audio = audioRef.current;
    audioRef.current = null;
    if (audio) {
      try {
        audio.pause();
      } catch {
        /* already torn down */
      }
    }
    playResolveRef.current?.();
    setPlaying(false);
  }, []);

  // Stop any playback when the screen unmounts (leaving the surface stops speech).
  useEffect(() => stop, [stop]);

  // Play one rendered clip, resolving when it ends / errors / is stopped.
  const playBlob = useCallback((blob: Blob): Promise<void> => {
    return new Promise<void>((resolve) => {
      let url = "";
      const finish = () => {
        if (url) {
          try {
            URL.revokeObjectURL(url);
          } catch {
            /* nothing to revoke */
          }
        }
        if (playResolveRef.current === finish) playResolveRef.current = null;
        resolve();
      };
      try {
        url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audioRef.current = audio;
        playResolveRef.current = finish;
        audio.onended = finish;
        audio.onerror = finish;
        void audio.play().catch(finish);
      } catch {
        finish();
      }
    });
  }, []);

  // Read an uploaded .md/.txt file's text into the area to review and edit before playing —
  // replacing whatever's there. Clears the input so re-picking the same file fires again.
  const onUpload = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setError(null);
    const reader = new FileReader();
    reader.onload = () => setText(String(reader.result ?? ""));
    reader.onerror = () => setError("Couldn't read that file.");
    reader.readAsText(file);
  }, []);

  const play = useCallback(() => {
    const clips = chunkStream(text, true, engine).chunks;
    if (!clips.length) return;
    setError(null);
    const myGen = ++genRef.current;
    setPlaying(true);
    // Render clips lazily but keep the in-flight request per index, so a clip playing now can
    // prefetch the next while it plays — render overlaps playback, so clips run gaplessly.
    const cache = new Map<number, Promise<Blob>>();
    const render = (i: number): Promise<Blob> => {
      let p = cache.get(i);
      if (!p) {
        p = renderClip(voice, clips[i] ?? "", i === 0);
        cache.set(i, p);
      }
      return p;
    };
    const run = async () => {
      for (let i = 0; i < clips.length; i++) {
        if (genRef.current !== myGen) return;
        const blobP = render(i);
        if (i + 1 < clips.length) void render(i + 1).catch(() => {}); // prefetch the next clip
        let blob: Blob;
        try {
          blob = await blobP;
        } catch {
          if (genRef.current === myGen) setError("Couldn't reach the box to render the audio.");
          break;
        }
        if (genRef.current !== myGen) return;
        await playBlob(blob);
        if (genRef.current !== myGen) return;
      }
      if (genRef.current === myGen) setPlaying(false);
    };
    void run();
  }, [text, voice, engine, playBlob]);

  const exportAudio = useCallback(async () => {
    const clips = chunkStream(text, true, engine).chunks;
    if (!clips.length) return;
    setError(null);
    setExporting(true);
    setProgress({ done: 0, total: clips.length });
    try {
      // Render up to a few clips at once (preserving order by index) so a long chapter doesn't
      // export one slow round-trip at a time, then splice the WAVs into one file.
      const blobs = new Array<Blob>(clips.length);
      let next = 0;
      let done = 0;
      const POOL = 4;
      await Promise.all(
        Array.from({ length: Math.min(POOL, clips.length) }, async () => {
          while (true) {
            const i = next++;
            if (i >= clips.length) break;
            blobs[i] = await renderClip(voice, clips[i] ?? "", i === 0);
            done += 1;
            setProgress({ done, total: clips.length });
          }
        }),
      );
      const wav = await concatWav(blobs);
      downloadBlob(wav, exportName(text));
    } catch {
      setError("Couldn't export the audio — is the box reachable?");
    } finally {
      setExporting(false);
      setProgress(null);
    }
  }, [text, voice, engine]);

  const empty = text.trim().length === 0;

  return (
    <div className="subscreen read-text-layer">
      <header className="top-bar">
        <button type="button" className="back-btn" onClick={onClose} aria-label="Back">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">Read custom text</span>
        </button>
      </header>
      <main className="screen-body read-text-body">
        <textarea
          className="read-text-area"
          aria-label="Text to read aloud"
          placeholder="Paste or type the text to read aloud — a note, a book chapter…"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        {error && <p className="settings-meta settings-error">{error}</p>}
        <div className="read-text-actions">
          <input
            ref={fileInputRef}
            type="file"
            accept=".md,.markdown,.txt,text/markdown,text/plain"
            className="visually-hidden"
            aria-hidden="true"
            tabIndex={-1}
            onChange={onUpload}
          />
          <button
            type="button"
            className="seg"
            disabled={playing || exporting}
            onClick={() => fileInputRef.current?.click()}
          >
            Upload .md
          </button>
          <button
            type="button"
            className="seg"
            disabled={empty || exporting}
            onClick={() => (playing ? stop() : play())}
          >
            {playing ? "Stop" : "Play"}
          </button>
          <button
            type="button"
            className="seg"
            disabled={empty || playing || exporting}
            onClick={() => void exportAudio()}
          >
            {exporting
              ? progress
                ? `Exporting ${progress.done}/${progress.total}…`
                : "Exporting…"
              : "Export audio"}
          </button>
        </div>
      </main>
    </div>
  );
}
