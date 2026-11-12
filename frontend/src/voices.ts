// The box's Kokoro voices (deploy/tts-stt/tts_server.py), named for people: shared by the
// read-aloud picker in Settings and each GrowBot persona's voice picker.

/** What tts-stt renders when it is given no voice (its CURATED_KOKORO_VOICES[0]). */
export const BOX_DEFAULT_VOICE = "kokoro-af_heart";

// Accent and gender from the voice id's prefix (af_ = American female, and so on).
const KOKORO_ACCENT: Record<string, string> = {
  af: "American F",
  am: "American M",
  bf: "British F",
  bm: "British M",
};

/** "kokoro-af_heart" -> "Heart · American F". An id without the accent prefix (a blend such
 *  as "kokoro-narrator") reads as "Kokoro · Narrator". */
export function voiceLabel(id: string): string {
  const code = id.startsWith("kokoro-") ? id.slice("kokoro-".length) : id;
  const m = /^([ab][fm])_(.+)$/.exec(code);
  const raw = (m?.[2] ?? code).replace(/_/g, " ");
  const name = raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : id;
  const prefix = m?.[1];
  const accent = prefix ? KOKORO_ACCENT[prefix] : undefined;
  return accent ? `${name} · ${accent}` : `Kokoro · ${name}`;
}
