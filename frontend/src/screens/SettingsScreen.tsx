import { useEffect, useRef, useState } from "react";
import { emitReadAloudSettings } from "../agent/readAloudBus";
import type {
  BrainTtsHealth,
  DebugToken,
  FeedConfig,
  GmailSettings,
  GmailTestResult,
  ImageAnalysisMode,
} from "../api/client";
import { ApiError, api } from "../api/client";
import { BUILD_SHA, BUILD_TIME } from "../buildInfo";
import { FONT_SCALES, type FontScale, getFontScale, setFontScale } from "../fontScale";
import { isLocationCaptureEnabled, setLocationCaptureEnabled } from "../location";
import { type ThemePref, getThemePref, setThemePref } from "../theme";
import { TOKEN_RATES, type TokenRate, getTokenRate, setTokenRate } from "../tokenRate";
import { ReadTextScreen } from "./ReadTextScreen";

const THEME_OPTIONS: { value: ThemePref; label: string }[] = [
  { value: "system", label: "System" },
  { value: "dark", label: "Dark" },
  { value: "dark-bright", label: "Dark+" },
  { value: "light", label: "Light" },
];

const IMAGE_ANALYSIS_OPTIONS: { value: ImageAnalysisMode; label: string }[] = [
  { value: "ocr", label: "ocr only" },
  { value: "full", label: "full analysis" },
];

// A short, content-free phrase the "play sample" button renders so the owner can hear a
// voice/speaker before choosing it — never real answer text.
const VOICE_SAMPLE_TEXT = "This is how the assistant will sound when it reads your answers aloud.";

// Read-aloud models surfaced as one Kokoro | Native control: "kokoro" renders on the box (the
// on-box engine), "native" is the device's own voice.
type ReadAloudModel = "kokoro" | "native";
const MODEL_LABEL: Record<ReadAloudModel, string> = {
  kokoro: "Kokoro",
  native: "Native",
};

// Kokoro accent/gender from the voice-id prefix (af_ = American female, etc.), for a readable
// label in the Kokoro voice dropdown.
const KOKORO_ACCENT: Record<string, string> = {
  af: "American F",
  am: "American M",
  bf: "British F",
  bm: "British M",
};

// Prettify a Kokoro voice id for the picker: "kokoro-af_heart" -> "Heart · American F" (name +
// accent/gender from the prefix). An id without the accent prefix reads as "Kokoro · <name>".
function voiceLabel(id: string): string {
  const code = id.startsWith("kokoro-") ? id.slice("kokoro-".length) : id; // e.g. "af_heart"
  const m = /^([ab][fm])_(.+)$/.exec(code);
  const raw = (m?.[2] ?? code).replace(/_/g, " ");
  const name = raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : id;
  const prefix = m?.[1];
  const accent = prefix ? KOKORO_ACCENT[prefix] : undefined;
  return accent ? `${name} · ${accent}` : `Kokoro · ${name}`;
}

interface SettingsScreenProps {
  deviceLabel: string;
  onLogout: () => void;
}

export function SettingsScreen({ deviceLabel, onLogout }: SettingsScreenProps) {
  const [theme, setTheme] = useState<ThemePref>(getThemePref);
  const [fontScale, setScale] = useState<FontScale>(getFontScale);
  const [tokenRate, setRate] = useState<TokenRate>(getTokenRate);
  const [locationOn, setLocationOn] = useState<boolean>(isLocationCaptureEnabled);
  // Inline confirm per DESIGN.md — no window.confirm for destructive acts.
  const [confirmingLogout, setConfirmingLogout] = useState(false);
  // Image analysis is the FIRST server-synced setting (GET/PUT /api/settings
  // over app.settings): the worker reads it, so it must follow the account.
  // Theme and text size deliberately stay device-local for now.
  const [imageMode, setImageMode] = useState<ImageAnalysisMode | null>(null);
  // Stream real prompt/answer text to the on-box wall display (:8800). Off by default;
  // null until the server answers so the toggle doesn't flash the wrong state.
  const [brainStream, setBrainStream] = useState<boolean | null>(null);
  // Read the streamed wall-display turns aloud (on-box TTS). Off by default;
  // null until the server answers. Companion to the stream toggle above.
  const [brainReadAloud, setBrainReadAloud] = useState<boolean | null>(null);
  // The voice id the read-aloud speaks answers in, plus the box's installed voices
  // (null until fetched; [] when the display is unreachable / has no models) and the
  // "play sample" state. The sample audio ref lets a new sample stop the previous one.
  const [brainAnswerVoice, setBrainAnswerVoice] = useState<string | null>(null);
  // Which engine the read-aloud renders with: "piper" (the opaque on-box marker — Kokoro on the
  // box, native fallback) or "native" (the device's own voice). null until the server answers.
  const [brainEngine, setBrainEngine] = useState<"piper" | "native" | null>(null);
  const [voices, setVoices] = useState<string[] | null>(null);
  const [samplePlaying, setSamplePlaying] = useState(false);
  const [sampleError, setSampleError] = useState<string | null>(null);
  const sampleAudioRef = useRef<HTMLAudioElement | null>(null);
  // The owner's read-aloud respelling map {word: "say it like"} and its inline editor. null
  // until the server answers so the panel shows nothing rather than a flash of "empty". The
  // engine health drives the voice-engine chip. `pronPlayingKey` tags which row is auditioning
  // (a lexicon word, or "" for the add-form preview) so only that Test button shows playing.
  const [lexicon, setLexicon] = useState<Record<string, string> | null>(null);
  const [ttsHealth, setTtsHealth] = useState<BrainTtsHealth | null>(null);
  const [pronAdding, setPronAdding] = useState(false);
  const [pronWord, setPronWord] = useState("");
  const [pronSay, setPronSay] = useState("");
  const [pronPlayingKey, setPronPlayingKey] = useState<string | null>(null);
  // The "read custom text" overlay: paste arbitrary prose, play it in the chosen on-box voice
  // or export it to a WAV file. On-box engine only (it renders on the box), so it opens from the
  // voice picker below.
  const [readTextOpen, setReadTextOpen] = useState(false);
  // The owner's display timezone — synced from this device's zone on app load
  // (App.tsx); shown read-only so the owner knows which zone their times render
  // in. Falls back to the browser's detected zone before the server answers.
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [timezone, setTimezone] = useState<string>(browserZone);
  useEffect(() => {
    let stale = false;
    api
      .getSettings()
      .then((s) => {
        if (stale) return;
        setImageMode(s.image_analysis_mode);
        setBrainStream(s.brain_llm_stream);
        setBrainReadAloud(s.brain_read_aloud);
        setBrainAnswerVoice(s.brain_answer_voice);
        setBrainEngine(s.brain_read_aloud_engine);
        setLexicon(s.pronunciation_lexicon ?? {});
        if (s.owner_timezone) setTimezone(s.owner_timezone);
      })
      .catch(() => {
        // Unreachable backend: show the default; a tap still tries to save.
        if (!stale) {
          setImageMode("full");
          setBrainStream(false);
          setLexicon({});
        }
      });
    // The read-aloud engine's health drives the voice-engine chip. Its own defensive parse
    // resolves to the all-off shape on a 503/bad body, so this never rejects.
    api.brainTtsHealth().then((h) => {
      if (!stale) setTtsHealth(h);
    });
    return () => {
      stale = true;
    };
  }, []);

  // Which voices the box has installed, for the read-aloud voice picker. [] when the display
  // is unreachable / has no models.
  useEffect(() => {
    let stale = false;
    api
      .brainVoices()
      .then((v) => {
        if (!stale) setVoices(v);
      })
      .catch(() => {
        if (!stale) setVoices([]);
      });
    return () => {
      stale = true;
    };
  }, []);

  // Stop any sample still playing when the screen unmounts.
  useEffect(
    () => () => {
      sampleAudioRef.current?.pause();
      sampleAudioRef.current = null;
    },
    [],
  );

  // The archivist's Gmail connection. Status is booleans only (secrets never leave
  // the server); the three inputs are write-only — empty fields are left unchanged.
  const [gmail, setGmail] = useState<GmailSettings | null>(null);
  const [gmailId, setGmailId] = useState("");
  const [gmailSecret, setGmailSecret] = useState("");
  const [gmailToken, setGmailToken] = useState("");
  const [gmailSaving, setGmailSaving] = useState(false);
  const [gmailTest, setGmailTest] = useState<GmailTestResult | null>(null);
  const [gmailNotice, setGmailNotice] = useState<string | null>(null);
  useEffect(() => {
    let stale = false;
    api
      .getGmailSettings()
      .then((s) => {
        if (!stale) setGmail(s);
      })
      .catch(() => {});
    // The in-app Connect flow bounces back to /settings?gmail=connected|error; show
    // the outcome, refresh status, then strip the query so a reload doesn't repeat it.
    const outcome = new URLSearchParams(window.location.search).get("gmail");
    if (outcome) {
      setGmailNotice(
        outcome === "connected" ? "Gmail connected." : "Couldn't connect to Gmail — try again.",
      );
      window.history.replaceState(null, "", window.location.pathname);
    }
    return () => {
      stale = true;
    };
  }, []);

  // A full-page navigation (not fetch): OAuth consent needs a top-level redirect.
  function connectGmail() {
    window.location.href = "/api/settings/gmail/connect";
  }

  function saveGmail() {
    const patch: { client_id?: string; client_secret?: string; refresh_token?: string } = {};
    if (gmailId.trim()) patch.client_id = gmailId.trim();
    if (gmailSecret.trim()) patch.client_secret = gmailSecret.trim();
    if (gmailToken.trim()) patch.refresh_token = gmailToken.trim();
    setGmailSaving(true);
    setGmailTest(null);
    void api
      .updateGmailSettings(patch)
      .then((s) => {
        setGmail(s);
        setGmailId("");
        setGmailSecret("");
        setGmailToken("");
      })
      .finally(() => setGmailSaving(false));
  }

  function testGmail() {
    setGmailTest(null);
    void api.testGmailSettings().then(setGmailTest);
  }

  // The read-only appointments ICS feed — a revocable subscribe URL the owner
  // hands to a calendar app. Server-held token; absent => the feed is off.
  const [feed, setFeed] = useState<FeedConfig | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let stale = false;
    api
      .feedConfig()
      .then((f) => {
        if (!stale) setFeed(f);
      })
      .catch(() => {
        if (!stale) setFeed({ enabled: false, token: null });
      });
    return () => {
      stale = true;
    };
  }, []);

  const feedUrl =
    feed?.token != null
      ? `${window.location.origin}/api/feed/appointments.ics?token=${feed.token}`
      : "";

  function generateFeed() {
    setCopied(false);
    void api
      .rotateFeed()
      .then(setFeed)
      .catch(() => {});
  }

  function disableFeed() {
    setCopied(false);
    void api
      .disableFeed()
      .then(() => setFeed({ enabled: false, token: null }))
      .catch(() => {});
  }

  function copyFeed() {
    if (feedUrl) {
      void navigator.clipboard?.writeText(feedUrl);
      setCopied(true);
    }
  }

  // Debug access (Claude): owner-minted, revocable, time-boxed capability tokens.
  // The minted payload (server URL + key) is shown ONCE, here, to copy and hand off.
  const [debugTokens, setDebugTokens] = useState<DebugToken[] | null>(null);
  const [debugLabel, setDebugLabel] = useState("");
  const [debugTtl, setDebugTtl] = useState<number>(24);
  const [mintedPayload, setMintedPayload] = useState<string | null>(null);
  const [payloadCopied, setPayloadCopied] = useState(false);
  const [debugError, setDebugError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  function loadDebugTokens() {
    void api
      .debugTokens()
      .then(setDebugTokens)
      .catch(() => setDebugTokens([]));
  }
  useEffect(loadDebugTokens, []);

  function mintDebugToken() {
    setDebugError(null);
    setPayloadCopied(false);
    void api
      .mintDebugToken(debugLabel.trim() || "Claude debug", debugTtl)
      .then((m) => {
        setMintedPayload(m.payload);
        setDebugLabel("");
        loadDebugTokens();
      })
      .catch((e) => {
        setDebugError(
          e instanceof ApiError && e.status === 409
            ? "Debug access is off on the server (set JBRAIN_DEBUG_ACCESS_ENABLED)."
            : "Could not mint a token.",
        );
      });
  }

  function revokeDebugToken(id: string) {
    void api
      .revokeDebugToken(id)
      .then(loadDebugTokens)
      .catch(() => {});
    setRevoking(null);
  }

  function suspendDebugToken(id: string) {
    void api
      .suspendDebugToken(id)
      .then(loadDebugTokens)
      .catch(() => {});
  }

  function resumeDebugToken(id: string) {
    void api
      .resumeDebugToken(id)
      .then(loadDebugTokens)
      .catch(() => {});
  }

  const DEBUG_TTL_OPTIONS: { hours: number; label: string }[] = [
    { hours: 1, label: "1h" },
    { hours: 24, label: "24h" },
    { hours: 24 * 7, label: "7d" },
    { hours: 24 * 30, label: "30d" },
  ];

  // Show only live tokens (active or suspended); revoked/expired ones are dropped
  // rather than kept as history.
  const liveDebugTokens = (Array.isArray(debugTokens) ? debugTokens : []).filter(
    (t) => t.revoked_at == null && !(t.expires_at != null && new Date(t.expires_at) < new Date()),
  );

  function pick(pref: ThemePref) {
    setThemePref(pref);
    setTheme(pref);
  }

  function pickImageMode(mode: ImageAnalysisMode) {
    setImageMode(mode); // optimistic — the sync dot reports trouble
    void api.updateSettings({ image_analysis_mode: mode }).catch(() => {});
  }

  function pickBrainStream(on: boolean) {
    setBrainStream(on); // optimistic
    void api.updateSettings({ brain_llm_stream: on }).catch(() => {});
  }

  // Each read-aloud setter also broadcasts the change: the chat read-aloud hook lives in the
  // always-mounted HomeScreen, so this overlay is its only way to learn of a live change.
  function pickBrainReadAloud(on: boolean) {
    setBrainReadAloud(on); // optimistic
    emitReadAloudSettings({ brain_read_aloud: on });
    void api.updateSettings({ brain_read_aloud: on }).catch(() => {});
  }

  function pickAnswerVoice(id: string) {
    setBrainAnswerVoice(id); // optimistic
    setSampleError(null);
    emitReadAloudSettings({ brain_answer_voice: id });
    void api.updateSettings({ brain_answer_voice: id }).catch(() => {});
  }

  function pickEngine(next: "piper" | "native") {
    setBrainEngine(next); // optimistic
    setSampleError(null);
    emitReadAloudSettings({ brain_read_aloud_engine: next });
    void api.updateSettings({ brain_read_aloud_engine: next }).catch(() => {});
  }

  // The read-aloud model is a view over two settings. "native" is the device's own voice;
  // "kokoro" renders on-box (the opaque "piper" on-box engine). All installed voices are
  // Kokoro ids ("kokoro-*") now.
  const installedVoices = voices ?? [];
  const kokoroVoices = installedVoices.filter((v) => v.startsWith("kokoro-"));
  const currentModel: ReadAloudModel | null =
    brainEngine === null ? null : brainEngine === "native" ? "native" : "kokoro";
  // Kokoro is always offered — the box serves Kokoro on the on-box engine.
  const models: ReadAloudModel[] = ["kokoro", "native"];

  // Switch model: "native" flips the engine; "kokoro" ensures the on-box engine (still the
  // opaque "piper" literal) and steers the answer voice to a Kokoro id when it isn't already one.
  function pickModel(model: ReadAloudModel) {
    if (model === "native") {
      pickEngine("native");
      return;
    }
    if (brainEngine !== "piper") pickEngine("piper");
    const answerIsKokoro = (brainAnswerVoice ?? "").startsWith("kokoro-");
    if (!answerIsKokoro && kokoroVoices[0]) pickAnswerVoice(kokoroVoices[0]);
  }

  // Render + play a short sample of `voice` on the box, so a voice can be auditioned before
  // it's used. A new sample stops any previous one.
  function playVoiceSample(voice: string) {
    if (!voice) return;
    setSampleError(null);
    sampleAudioRef.current?.pause();
    sampleAudioRef.current = null;
    setSamplePlaying(true);
    void api
      .brainTts(voice, VOICE_SAMPLE_TEXT)
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        sampleAudioRef.current = audio;
        const done = () => {
          URL.revokeObjectURL(url);
          setSamplePlaying(false);
          if (sampleAudioRef.current === audio) sampleAudioRef.current = null;
        };
        audio.onended = done;
        audio.onerror = () => {
          done();
          setSampleError("Couldn't play a sample — is the box reachable?");
        };
        void audio.play().catch(() => {
          done();
          setSampleError("Couldn't play a sample.");
        });
      })
      .catch(() => {
        setSamplePlaying(false);
        setSampleError("Couldn't reach the box to render a sample.");
      });
  }

  function playSample() {
    if (brainAnswerVoice) playVoiceSample(brainAnswerVoice);
  }

  // Render + play arbitrary `text` in the chosen on-box voice, so the owner can hear how a
  // respelling will sound before saving it. Modeled on playVoiceSample; `key` tags which row is
  // auditioning (a lexicon word, or "" for the add-form preview) so only that Test button shows
  // playing. Reuses the shared sample-audio slot so a new play stops the previous one.
  function playText(text: string, key: string) {
    const value = text.trim();
    if (!value) return;
    const voice = brainAnswerVoice ?? kokoroVoices[0] ?? "kokoro-af_heart";
    setSampleError(null);
    sampleAudioRef.current?.pause();
    sampleAudioRef.current = null;
    setPronPlayingKey(key);
    void api
      .brainTts(voice, value)
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        sampleAudioRef.current = audio;
        const done = () => {
          URL.revokeObjectURL(url);
          setPronPlayingKey((k) => (k === key ? null : k));
          if (sampleAudioRef.current === audio) sampleAudioRef.current = null;
        };
        audio.onended = done;
        audio.onerror = () => {
          done();
          setSampleError("Couldn't play a sample — is the box reachable?");
        };
        void audio.play().catch(() => {
          done();
          setSampleError("Couldn't play a sample.");
        });
      })
      .catch(() => {
        setPronPlayingKey(null);
        setSampleError("Couldn't reach the box to render a sample.");
      });
  }

  // The respelling map is stored whole — PUT /api/settings REPLACES pronunciation_lexicon — so
  // every mutation sends the FULL map. Optimistic: the local map updates before the write lands.
  function saveLexicon(next: Record<string, string>) {
    setLexicon(next);
    void api.updateSettings({ pronunciation_lexicon: next }).catch(() => {});
  }

  function addPronunciation() {
    const word = pronWord.trim();
    const say = pronSay.trim();
    if (!word || !say) return;
    saveLexicon({ ...(lexicon ?? {}), [word]: say });
    setPronWord("");
    setPronSay("");
    setPronAdding(false);
  }

  function removePronunciation(word: string) {
    const next = { ...(lexicon ?? {}) };
    delete next[word];
    saveLexicon(next);
  }

  return (
    <main className="screen-body settings">
      <section className="settings-card">
        <h2 className="settings-label">Theme</h2>
        <div className="theme-picker" aria-label="Theme">
          {THEME_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              aria-pressed={theme === opt.value}
              className={`seg${theme === opt.value ? " seg-on" : ""}`}
              onClick={() => pick(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Text size</h2>
        <div className="theme-picker" aria-label="Text size">
          {FONT_SCALES.map((scale) => (
            <button
              key={scale}
              type="button"
              aria-pressed={fontScale === scale}
              className={`seg${fontScale === scale ? " seg-on" : ""}`}
              onClick={() => {
                setFontScale(scale);
                setScale(scale);
              }}
            >
              {scale}%
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Response typing speed</h2>
        <p className="settings-meta">
          how fast the assistant's answer types out, in tokens per second — the reveal is paced
          steadily so fast local models read as smooth typing rather than snapping in. Instant turns
          pacing off; the full answer shows the moment it lands.
        </p>
        <div className="theme-picker" aria-label="Response typing speed">
          {TOKEN_RATES.map((rate) => (
            <button
              key={rate}
              type="button"
              aria-pressed={tokenRate === rate}
              className={`seg${tokenRate === rate ? " seg-on" : ""}`}
              onClick={() => {
                setTokenRate(rate);
                setRate(rate);
              }}
            >
              {rate === 0 ? "Instant" : `${rate}/s`}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Image analysis</h2>
        <p className="settings-meta">
          how much a vision model reads from attached images — ocr only transcribes the text
          verbatim; full analysis adds a salient description the fact pipeline mines. either way,
          capture never waits — vision runs after sync.
        </p>
        <div className="theme-picker" aria-label="Image analysis">
          {IMAGE_ANALYSIS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              aria-pressed={imageMode === opt.value}
              className={`seg${imageMode === opt.value ? " seg-on" : ""}`}
              onClick={() => pickImageMode(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Stream LLM to wall display</h2>
        <p className="settings-meta">
          shows each chat turn on the on-box neural-brain display (:8800) as tendrils with the
          prompt and answer text streaming along them, plus a fade-out popup of the answer. this
          puts your real prompt and answer text on that display, which has no login — only turn it
          on when the display is the box's own monitor (or bound to localhost), never an exposed LAN
          screen. off by default.
        </p>
        <div className="theme-picker" aria-label="Stream LLM to wall display">
          {[true, false].map((on) => (
            <button
              key={on ? "on" : "off"}
              type="button"
              aria-pressed={brainStream === on}
              className={`seg${brainStream === on ? " seg-on" : ""}`}
              disabled={brainStream === null}
              onClick={() => pickBrainStream(on)}
            >
              {on ? "On" : "Off"}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Read wall display aloud</h2>
        <p className="settings-meta">
          speaks each streamed chat turn out loud on the box, rendered by Kokoro. companion to the
          stream toggle above — it reads the same prompt and answer text, so it only speaks when
          streaming is on and the display is the box's own monitor. the display shows its voice
          panel only while this is on and voices are installed. off by default.
        </p>
        <div className="theme-picker" aria-label="Read wall display aloud">
          {[true, false].map((on) => (
            <button
              key={on ? "on" : "off"}
              type="button"
              aria-pressed={brainReadAloud === on}
              className={`seg${brainReadAloud === on ? " seg-on" : ""}`}
              disabled={brainReadAloud === null}
              onClick={() => pickBrainReadAloud(on)}
            >
              {on ? "On" : "Off"}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Read-aloud voice</h2>
        <p className="settings-meta">
          how the assistant reads answers aloud in chat (and on the wall display). pick a model:{" "}
          <b>Kokoro</b> renders natural voices on the box; <b>Native</b> uses this device's built-in
          voice. Kokoro falls back to native when the box can't be reached.
        </p>
        <div className="theme-picker" aria-label="Read-aloud model">
          {models.map((model) => (
            <button
              key={model}
              type="button"
              aria-pressed={currentModel === model}
              className={`seg${currentModel === model ? " seg-on" : ""}`}
              disabled={brainEngine === null}
              onClick={() => pickModel(model)}
            >
              {MODEL_LABEL[model]}
            </button>
          ))}
        </div>
        {brainEngine !== "native" &&
          (voices === null ? (
            <div className="settings-value">…</div>
          ) : voices.length === 0 ? (
            <p className="settings-meta">
              no voices installed on the box, or the display is unreachable — install them with
              deploy/tts-stt/install-tts.sh. read-aloud uses this device's built-in voice until
              then.
            </p>
          ) : (
            <>
              <p className="settings-meta">
                Kokoro's natural English voices — American and British. play a sample to hear one
                before choosing it.
              </p>
              <label className="settings-field">
                Kokoro voice
                <select
                  aria-label="Kokoro voice"
                  value={brainAnswerVoice ?? ""}
                  onChange={(e) => pickAnswerVoice(e.target.value)}
                >
                  {/* Surface the saved Kokoro voice when the box doesn't list it so it isn't blank. */}
                  {brainAnswerVoice && !voices.includes(brainAnswerVoice) && (
                    <option value={brainAnswerVoice}>{voiceLabel(brainAnswerVoice)}</option>
                  )}
                  {kokoroVoices.map((v) => (
                    <option key={v} value={v}>
                      {voiceLabel(v)}
                    </option>
                  ))}
                </select>
              </label>
              <div className="settings-actions">
                <button
                  type="button"
                  className="seg"
                  disabled={!brainAnswerVoice || samplePlaying}
                  onClick={playSample}
                >
                  {samplePlaying ? "Playing…" : "Play sample"}
                </button>
                <button
                  type="button"
                  className="seg"
                  disabled={!brainAnswerVoice}
                  onClick={() => setReadTextOpen(true)}
                >
                  Read custom text
                </button>
              </div>
              {sampleError && <p className="settings-meta settings-error">{sampleError}</p>}
            </>
          ))}

        {/* Pronunciations — the owner's respelling map, on-box (Kokoro) only, gated the same
            way as the voice picker. The whole map is PUT on every edit (REPLACE semantics). */}
        {currentModel === "kokoro" && (
          <div className="pron">
            {ttsHealth && ttsHealth.g2p !== "unavailable" && (
              <div
                className={`pron-chip ${ttsHealth.g2p === "misaki" ? "pron-chip-ok" : "pron-chip-warn"}`}
              >
                <span className="pron-dot" />
                {ttsHealth.g2p === "misaki"
                  ? "Voice engine: misaki ✓"
                  : "Voice engine: espeak — pronunciations still apply, quality limited"}
              </div>
            )}
            <div className="pron-card">
              <div className="pron-card-h">
                <div className="pron-card-t">How to say a word</div>
                <div className="pron-card-d">
                  Type a word and how it should sound. Read-aloud says it your way — no phonetics
                  needed. Applies everywhere the box reads text.
                </div>
              </div>
              <div className="pron-rows" aria-label="Pronunciations">
                {lexicon === null ? null : Object.keys(lexicon).length === 0 ? (
                  <div className="pron-empty">No custom pronunciations yet. Add one below.</div>
                ) : (
                  Object.entries(lexicon).map(([word, say]) => (
                    <div className="pron-row" key={word}>
                      <span className="pron-word">{word}</span>
                      <span className="pron-arrow">→</span>
                      <span className="pron-say">{say}</span>
                      <button
                        type="button"
                        className={`pron-icon-btn pron-icon-play${pronPlayingKey === word ? " pron-playing" : ""}`}
                        title="Test"
                        aria-label={`Test ${word}`}
                        onClick={() => playText(say, word)}
                      >
                        {pronPlayingKey === word ? "❚❚" : "▷"}
                      </button>
                      <button
                        type="button"
                        className="pron-icon-btn"
                        title="Remove"
                        aria-label={`Remove ${word}`}
                        onClick={() => removePronunciation(word)}
                      >
                        ✕
                      </button>
                    </div>
                  ))
                )}
              </div>
              {pronAdding ? (
                <div className="pron-adder">
                  <div className="pron-two">
                    <label className="pron-field">
                      Word
                      <input
                        value={pronWord}
                        placeholder="Titusville"
                        aria-label="Word"
                        onChange={(e) => setPronWord(e.target.value)}
                      />
                    </label>
                    <label className="pron-field">
                      Say it like
                      <input
                        value={pronSay}
                        placeholder="Tight us ville"
                        aria-label="Say it like"
                        onChange={(e) => setPronSay(e.target.value)}
                      />
                    </label>
                  </div>
                  <div className="pron-actions">
                    <button
                      type="button"
                      className="pron-btn pron-btn-test"
                      disabled={!pronSay.trim()}
                      onClick={() => playText(pronSay, "")}
                    >
                      {pronPlayingKey === "" ? "❚❚ Playing" : "▷ Test"}
                    </button>
                    <button
                      type="button"
                      className="pron-btn pron-btn-ghost"
                      onClick={() => {
                        setPronAdding(false);
                        setPronWord("");
                        setPronSay("");
                      }}
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="pron-btn pron-btn-primary"
                      disabled={!pronWord.trim() || !pronSay.trim()}
                      onClick={addPronunciation}
                    >
                      Save
                    </button>
                  </div>
                </div>
              ) : (
                <button type="button" className="pron-add-row" onClick={() => setPronAdding(true)}>
                  ＋ Add a pronunciation
                </button>
              )}
            </div>
            <p className="pron-note">
              Tip: spell it the way it sounds, splitting into chunks with spaces — “Cholmondeley →
              Chumley”, “GIF → jiff”.
            </p>
          </div>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Time zone</h2>
        <p className="settings-meta">
          appointment times and other dates render in this zone — synced automatically from this
          device, so the assistant's answers match the cards.
        </p>
        <div className="settings-value" aria-label="Time zone">
          {timezone}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Gmail (Archivist)</h2>
        <p className="settings-meta">
          connects the Archivist agent to your Gmail so it can organize your mail. Paste the OAuth
          Client ID and secret from your Google Cloud "Web application" client, Save, then Connect
          to approve access. The Archivist reads, labels and archives — it never deletes. Secrets
          are stored on the server and never shown again. (A refresh token from the bootstrap script
          can be pasted instead, if you prefer.)
        </p>
        <div className="settings-value" aria-label="Gmail connection status">
          {gmail === null
            ? "…"
            : gmail.connected
              ? "Connected"
              : gmail.client_id_set || gmail.client_secret_set
                ? "Credentials saved — not connected yet"
                : "Not connected"}
        </div>
        <label className="settings-field">
          Client ID
          <input
            type="text"
            autoComplete="off"
            placeholder={gmail?.client_id_set ? "•••••• (saved)" : "…apps.googleusercontent.com"}
            value={gmailId}
            onChange={(e) => setGmailId(e.target.value)}
          />
        </label>
        <label className="settings-field">
          Client secret
          <input
            type="password"
            autoComplete="off"
            placeholder={gmail?.client_secret_set ? "•••••• (saved)" : ""}
            value={gmailSecret}
            onChange={(e) => setGmailSecret(e.target.value)}
          />
        </label>
        <label className="settings-field">
          Refresh token
          <input
            type="password"
            autoComplete="off"
            placeholder={gmail?.refresh_token_set ? "•••••• (saved)" : ""}
            value={gmailToken}
            onChange={(e) => setGmailToken(e.target.value)}
          />
        </label>
        <div className="settings-actions">
          <button
            type="button"
            className="seg"
            disabled={gmailSaving || (!gmailId.trim() && !gmailSecret.trim() && !gmailToken.trim())}
            onClick={saveGmail}
          >
            {gmailSaving ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            className="seg"
            disabled={!gmail?.client_id_set || !gmail?.client_secret_set}
            onClick={connectGmail}
          >
            {gmail?.connected ? "Reconnect Gmail" : "Connect Gmail"}
          </button>
          <button type="button" className="seg" disabled={!gmail?.connected} onClick={testGmail}>
            Test connection
          </button>
        </div>
        <p className="settings-meta">
          Save your Client ID and secret, then Connect to approve access in Google — no need to
          paste a refresh token by hand.
        </p>
        {gmailNotice && <p className="settings-meta">{gmailNotice}</p>}
        {gmailTest && (
          <p className={`settings-meta${gmailTest.ok ? "" : " settings-error"}`}>
            {gmailTest.detail}
          </p>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Capture location</h2>
        <p className="settings-meta">
          tags notes with where they were written — only when a fresh fix exists; capture never
          waits for GPS.
        </p>
        <div className="theme-picker" aria-label="Capture location">
          {[true, false].map((on) => (
            <button
              key={on ? "on" : "off"}
              type="button"
              aria-pressed={locationOn === on}
              className={`seg${locationOn === on ? " seg-on" : ""}`}
              onClick={() => {
                setLocationCaptureEnabled(on);
                setLocationOn(on);
              }}
            >
              {on ? "On" : "Off"}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Calendar feed</h2>
        <p className="settings-meta">
          subscribe a calendar app to your appointments, read-only. the link carries appointment
          titles from every domain — including health and finance — off your box into whatever
          calendar subscribes, so keep it private; disable it to cut access instantly.
        </p>
        {feed?.enabled && feedUrl ? (
          <>
            <input
              className="feed-url"
              readOnly
              value={feedUrl}
              aria-label="Calendar feed URL"
              onFocus={(e) => e.currentTarget.select()}
            />
            <div className="settings-actions">
              <button type="button" className="seg" onClick={copyFeed}>
                {copied ? "Copied" : "Copy link"}
              </button>
              <button type="button" className="seg" onClick={generateFeed}>
                Regenerate
              </button>
              <button type="button" className="btn-destructive" onClick={disableFeed}>
                Disable
              </button>
            </div>
          </>
        ) : (
          <button type="button" className="seg" onClick={generateFeed} disabled={feed === null}>
            Generate link
          </button>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Debug access (Claude)</h2>
        <p className="settings-meta">
          mint a revocable, time-boxed token an assistant uses to iterate on prompts against your
          local model, run read-only SQL, read logs, and switch model routing — live. the token
          carries a key into your box, including health, finance, and location data, so treat it
          like a password: share it only with a session you trust and revoke it the moment you're
          done.
        </p>
        <div className="settings-actions" aria-label="New debug token">
          <input
            className="feed-url"
            value={debugLabel}
            placeholder="Label (e.g. Claude session)"
            aria-label="Debug token label"
            onChange={(e) => setDebugLabel(e.currentTarget.value)}
          />
          <div className="theme-picker" aria-label="Token lifetime">
            {DEBUG_TTL_OPTIONS.map((opt) => (
              <button
                key={opt.hours}
                type="button"
                aria-pressed={debugTtl === opt.hours}
                className={`seg${debugTtl === opt.hours ? " seg-on" : ""}`}
                onClick={() => setDebugTtl(opt.hours)}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <button type="button" className="seg" onClick={mintDebugToken}>
            Mint token
          </button>
        </div>
        {debugError && <p className="settings-meta settings-error">{debugError}</p>}
        {mintedPayload && (
          <>
            <p className="settings-meta">
              copy this now — it is shown once and can't be recovered. paste it to the assistant.
            </p>
            <input
              className="feed-url"
              readOnly
              value={mintedPayload}
              aria-label="Debug token payload"
              onFocus={(e) => e.currentTarget.select()}
            />
            <div className="settings-actions">
              <button
                type="button"
                className="seg"
                onClick={() => {
                  void navigator.clipboard?.writeText(mintedPayload);
                  setPayloadCopied(true);
                }}
              >
                {payloadCopied ? "Copied" : "Copy token"}
              </button>
              <a
                className="seg"
                href={`/debug-console.html#${mintedPayload}`}
                target="_blank"
                rel="noreferrer"
              >
                Open console
              </a>
              <button type="button" className="seg" onClick={() => setMintedPayload(null)}>
                Done
              </button>
            </div>
          </>
        )}
        {liveDebugTokens.length > 0 && (
          <ul className="debug-token-list" aria-label="Debug tokens">
            {liveDebugTokens.map((t) => {
              const status = t.suspended_at ? "suspended" : "active";
              return (
                <li key={t.id} className="debug-token-row">
                  <div>
                    <span className="settings-value">{t.label}</span>
                    <span className={`debug-token-status debug-token-${status}`}> {status}</span>
                    <p className="settings-meta">
                      {t.expires_at
                        ? `expires ${new Date(t.expires_at).toLocaleString()}`
                        : "no expiry"}
                      {t.last_used_at
                        ? ` · last used ${new Date(t.last_used_at).toLocaleString()}`
                        : " · never used"}
                    </p>
                  </div>
                  <div className="debug-token-actions">
                    {status === "active" ? (
                      <button type="button" className="seg" onClick={() => suspendDebugToken(t.id)}>
                        Suspend
                      </button>
                    ) : (
                      <button type="button" className="seg" onClick={() => resumeDebugToken(t.id)}>
                        Resume
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-destructive"
                      onClick={() =>
                        revoking === t.id ? revokeDebugToken(t.id) : setRevoking(t.id)
                      }
                      onBlur={() => setRevoking(null)}
                    >
                      {revoking === t.id ? "Tap to confirm" : "Revoke"}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="settings-card">
        <h2 className="settings-label">Session</h2>
        <p className="settings-meta">{deviceLabel}</p>
        <button
          type="button"
          className="btn-destructive"
          onClick={() => (confirmingLogout ? onLogout() : setConfirmingLogout(true))}
          onBlur={() => setConfirmingLogout(false)}
        >
          {confirmingLogout ? "Tap again to confirm" : "Log out"}
        </button>
        {/* Build stamp — the only reliable way to confirm which bundle a cached PWA is
            actually running (a deploy/service-worker check). */}
        <p className="settings-meta settings-build">
          build {BUILD_SHA}
          {BUILD_TIME ? ` · ${new Date(BUILD_TIME).toLocaleString()}` : ""}
        </p>
      </section>

      {readTextOpen && brainAnswerVoice && (
        <ReadTextScreen voice={brainAnswerVoice} onClose={() => setReadTextOpen(false)} />
      )}
    </main>
  );
}
