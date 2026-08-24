import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { onReadAloudSettings } from "../agent/readAloudBus";
import { isLocationCaptureEnabled } from "../location";
import { SettingsScreen } from "./SettingsScreen";

function setup() {
  render(<SettingsScreen deviceLabel="Test device" onLogout={vi.fn()} />);
}

// The screen loads the server-synced settings on mount; a stateful stub
// makes GET/PUT round-trip like the real /api/settings.
function stubSettingsFetch(
  initial: "full" | "ocr" = "full",
  opts: {
    answerVoice?: string;
    voices?: string[];
    lexicon?: Record<string, string>;
    failPut?: boolean;
  } = {},
) {
  const state = {
    mode: initial,
    brainStream: false,
    brainReadAloud: false,
    brainAnswerVoice: opts.answerVoice ?? "kokoro-af_heart",
    engine: "piper" as "piper" | "native",
    speed: 1.0,
    pitch: 0,
    chorus: false,
    robot: false,
    lexicon: opts.lexicon ?? {},
    tavilyEnabled: true,
    tavilyKeySet: false,
    f1916Enabled: true,
    f1916Registered: false,
    f1916Handle: "",
  };
  const boxVoices = opts.voices ?? ["kokoro-af_heart", "kokoro-am_michael", "kokoro-bf_emma"];
  const puts: unknown[] = [];
  const tavilyPuts: unknown[] = [];
  const f1916Posts: unknown[] = [];
  const ttsUrls: string[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input);
    // The read-aloud voice picker loads the box's installed Kokoro voices on mount.
    if (path === "/api/brain/voices") {
      return new Response(JSON.stringify({ voices: boxVoices }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The Pronunciations panel reads the read-aloud engine's health for its voice-engine chip.
    if (path === "/api/brain/tts/health") {
      return new Response(
        JSON.stringify({
          kokoro_available: true,
          g2p: "misaki",
          lexicon_entries: Object.keys(state.lexicon).length,
          voice_count: 3,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    // A voice sample renders a WAV via the api proxy — an empty audio blob is enough. Record the
    // request URL so a test can assert the owner's voice effects rode along.
    if (path.startsWith("/api/brain/tts")) {
      ttsUrls.push(path);
      return new Response(new Blob([], { type: "audio/wav" }), {
        status: 200,
        headers: { "Content-Type": "audio/wav" },
      });
    }
    // The calendar-feed section loads its config on mount; default to disabled.
    if (path.startsWith("/api/feed/appointments")) {
      return new Response(JSON.stringify({ enabled: false, token: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The debug-access section lists its tokens on mount; default to none.
    if (path.startsWith("/api/settings/debug-tokens")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The Gmail (Archivist) section loads its status on mount; default disconnected.
    if (path.startsWith("/api/settings/gmail")) {
      return new Response(
        JSON.stringify({
          client_id_set: false,
          client_secret_set: false,
          refresh_token_set: false,
          connected: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    // The Tavily section loads/saves its status; a stateful stub round-trips the toggle + key.
    if (path === "/api/settings/tavily/test") {
      return new Response(
        JSON.stringify({ ok: true, detail: "Tavily read 512 chars — key works." }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (path === "/api/settings/tavily") {
      if ((init?.method ?? "GET").toUpperCase() === "PUT") {
        const patch = JSON.parse(String(init?.body)) as {
          enabled?: boolean;
          api_key?: string;
          clear_key?: boolean;
        };
        tavilyPuts.push(patch);
        if (patch.enabled != null) state.tavilyEnabled = patch.enabled;
        if (patch.clear_key) state.tavilyKeySet = false;
        else if (patch.api_key) state.tavilyKeySet = true;
      }
      return new Response(
        JSON.stringify({
          enabled: state.tavilyEnabled,
          key_set: state.tavilyKeySet,
          wired: true,
          effective: state.tavilyEnabled && state.tavilyKeySet,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    // The 1f916 section loads its status; register/rotate/test answer with a fresh status.
    const f1916Status = () => ({
      enabled: state.f1916Enabled,
      registered: state.f1916Registered,
      handle: state.f1916Handle,
      signing_key_set: state.f1916Registered,
    });
    if (path === "/api/settings/1f916/register") {
      const body = JSON.parse(String(init?.body)) as { handle: string; model: string };
      f1916Posts.push(body);
      state.f1916Registered = true;
      state.f1916Handle = body.handle;
      return new Response(
        JSON.stringify({
          ok: true,
          detail: `Registered as @${body.handle} with the identity key bound.`,
          status: f1916Status(),
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (path === "/api/settings/1f916/rotate" || path === "/api/settings/1f916/test") {
      f1916Posts.push({ action: path.split("/").pop() });
      return new Response(
        JSON.stringify({ ok: true, detail: "the citizen answers", status: f1916Status() }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (path === "/api/settings/1f916") {
      if ((init?.method ?? "GET").toUpperCase() === "PUT") {
        const patch = JSON.parse(String(init?.body)) as { enabled?: boolean };
        f1916Posts.push(patch);
        if (patch.enabled != null) state.f1916Enabled = patch.enabled;
      }
      return new Response(JSON.stringify(f1916Status()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (path !== "/api/settings") {
      throw new Error(`Unexpected fetch: ${path}`);
    }
    if ((init?.method ?? "GET").toUpperCase() === "PUT") {
      if (opts.failPut) {
        // A transient save failure: record the attempt but persist nothing (the client should
        // surface the error and re-fetch, not silently keep the optimistic value).
        puts.push(JSON.parse(String(init?.body)));
        return new Response("boom", { status: 500 });
      }
      const body = JSON.parse(String(init?.body)) as {
        image_analysis_mode?: "full" | "ocr";
        brain_llm_stream?: boolean;
        brain_read_aloud?: boolean;
        brain_answer_voice?: string;
        brain_read_aloud_engine?: "piper" | "native";
        brain_answer_speed?: number;
        brain_answer_pitch?: number;
        brain_answer_chorus?: boolean;
        brain_answer_robot?: boolean;
        pronunciation_lexicon?: Record<string, string>;
      };
      puts.push(body);
      if (body.image_analysis_mode) state.mode = body.image_analysis_mode;
      if (typeof body.brain_llm_stream === "boolean") state.brainStream = body.brain_llm_stream;
      if (typeof body.brain_read_aloud === "boolean") state.brainReadAloud = body.brain_read_aloud;
      if (typeof body.brain_answer_voice === "string")
        state.brainAnswerVoice = body.brain_answer_voice;
      if (body.brain_read_aloud_engine) state.engine = body.brain_read_aloud_engine;
      if (typeof body.brain_answer_speed === "number") state.speed = body.brain_answer_speed;
      if (typeof body.brain_answer_pitch === "number") state.pitch = body.brain_answer_pitch;
      if (typeof body.brain_answer_chorus === "boolean") state.chorus = body.brain_answer_chorus;
      if (typeof body.brain_answer_robot === "boolean") state.robot = body.brain_answer_robot;
      // PUT replaces the whole map (REPLACE semantics), mirroring the backend.
      if (body.pronunciation_lexicon) state.lexicon = body.pronunciation_lexicon;
    }
    return new Response(
      JSON.stringify({
        image_analysis_mode: state.mode,
        brain_llm_stream: state.brainStream,
        brain_read_aloud: state.brainReadAloud,
        brain_answer_voice: state.brainAnswerVoice,
        brain_read_aloud_engine: state.engine,
        brain_answer_speed: state.speed,
        brain_answer_pitch: state.pitch,
        brain_answer_chorus: state.chorus,
        brain_answer_robot: state.robot,
        pronunciation_lexicon: state.lexicon,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return { puts, tavilyPuts, f1916Posts, state, ttsUrls };
}

beforeEach(() => {
  localStorage.clear();
  stubSettingsFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SettingsScreen capture location", () => {
  it("defaults the toggle to on", () => {
    setup();
    const group = screen.getByLabelText("Capture location");
    const on = group.querySelector('[aria-pressed="true"]');
    expect(on).toHaveTextContent("On");
  });

  it("persists off across remounts via localStorage", () => {
    setup();
    const group = within(screen.getByLabelText("Capture location"));
    fireEvent.click(group.getByRole("button", { name: "Off" }));
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("off");
    expect(isLocationCaptureEnabled()).toBe(false);
  });

  it("persists turning it back on", () => {
    localStorage.setItem("jbrain.captureLocation", "off");
    setup();
    const group = within(screen.getByLabelText("Capture location"));
    fireEvent.click(group.getByRole("button", { name: "On" }));
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("on");
    expect(isLocationCaptureEnabled()).toBe(true);
  });
});

describe("SettingsScreen stream-LLM-to-wall-display toggle", () => {
  it("defaults to Off and enables on tap (PUTs brain_llm_stream: true)", async () => {
    const { puts } = stubSettingsFetch();
    setup();
    const group = within(screen.getByLabelText("Stream LLM to wall display"));
    // Server answered Off (owner text stays off the unauthenticated display by default).
    await waitFor(() =>
      expect(group.getByRole("button", { name: "Off" })).toHaveAttribute("aria-pressed", "true"),
    );
    fireEvent.click(group.getByRole("button", { name: "On" }));
    await waitFor(() => expect(puts).toContainEqual({ brain_llm_stream: true }));
  });
});

describe("SettingsScreen Tavily web-fetch panel", () => {
  it("saves+tests a key (never echoing it) and toggles the tier off", async () => {
    const { tavilyPuts } = stubSettingsFetch();
    setup();

    // Loads enabled-but-keyless (the single-owner default): the status pill reads "No key".
    const status = await screen.findByLabelText("Tavily status");
    await waitFor(() => expect(status).toHaveTextContent("No key"));

    // Paste + "Save & test" → PUTs api_key then runs the probe; the field clears, pill goes Active.
    const keyField = screen.getByLabelText("API key") as HTMLInputElement;
    fireEvent.change(keyField, { target: { value: "tvly-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save & test" }));
    await waitFor(() => expect(tavilyPuts).toContainEqual({ api_key: "tvly-secret" }));
    await waitFor(() => expect(status).toHaveTextContent("Active"));
    expect(keyField.value).toBe(""); // the key is never held/echoed in the field after save
    expect(await screen.findByText(/key works/)).toBeInTheDocument();

    // The switch is on; flipping it PUTs enabled:false (the instant, no-terminal kill switch).
    const toggle = screen.getByRole("switch", { name: "Enable Tavily" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    fireEvent.click(toggle);
    await waitFor(() => expect(tavilyPuts).toContainEqual({ enabled: false }));
  });
});

describe("SettingsScreen 1f916 citizenship panel", () => {
  it("registers a citizen once (handle+model published), then offers Test/Rotate", async () => {
    const { f1916Posts } = stubSettingsFetch();
    setup();

    // Loads unregistered: the pill says so and the register form is shown.
    const status = await screen.findByLabelText("1f916 status");
    await waitFor(() => expect(status).toHaveTextContent("Not registered"));
    const registerButton = screen.getByRole("button", { name: "Register citizen" });
    expect(registerButton).toBeDisabled(); // both public fields are required first

    fireEvent.change(screen.getByLabelText("Handle (public, permanent)"), {
      target: { value: "jerv" },
    });
    fireEvent.change(screen.getByLabelText(/Model description/), {
      target: { value: "gpt-oss-120b on a home box" },
    });
    fireEvent.click(registerButton);
    await waitFor(() =>
      expect(f1916Posts).toContainEqual({ handle: "jerv", model: "gpt-oss-120b on a home box" }),
    );
    // The fresh status shows the public handle; the secret appears nowhere.
    await waitFor(() => expect(status).toHaveTextContent("@jerv"));
    expect(await screen.findByText(/Registered as @jerv/)).toBeInTheDocument();
    // Registered state swaps the form for Test + Rotate (register is one-time).
    expect(screen.queryByRole("button", { name: "Register citizen" })).toBeNull();
    expect(screen.getByRole("button", { name: "Rotate secret" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Test" }));
    await waitFor(() => expect(f1916Posts).toContainEqual({ action: "test" }));
  });

  it("flips reading off with the toggle (PUTs enabled:false)", async () => {
    const { f1916Posts } = stubSettingsFetch();
    setup();
    const toggle = await screen.findByRole("switch", { name: "Enable 1f916" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    fireEvent.click(toggle);
    await waitFor(() => expect(f1916Posts).toContainEqual({ enabled: false }));
  });
});

describe("SettingsScreen read-wall-display-aloud toggle", () => {
  it("defaults to Off and enables on tap (PUTs brain_read_aloud: true)", async () => {
    const { puts } = stubSettingsFetch();
    setup();
    const group = within(screen.getByLabelText("Read wall display aloud"));
    await waitFor(() =>
      expect(group.getByRole("button", { name: "Off" })).toHaveAttribute("aria-pressed", "true"),
    );
    fireEvent.click(group.getByRole("button", { name: "On" }));
    await waitFor(() => expect(puts).toContainEqual({ brain_read_aloud: true }));
  });
});

describe("SettingsScreen read-aloud voice picker", () => {
  it("defaults to the Kokoro model, listing the box's Kokoro voices (no Piper button)", async () => {
    const { puts } = stubSettingsFetch();
    setup();
    const models = within(await screen.findByLabelText("Read-aloud model"));
    expect(models.getByRole("button", { name: "Kokoro" })).toHaveAttribute("aria-pressed", "true");
    expect(models.getByRole("button", { name: "Native" })).toBeInTheDocument();
    // Piper is gone — Kokoro is the only on-box model.
    expect(models.queryByRole("button", { name: "Piper" })).toBeNull();
    const select = (await screen.findByLabelText("Kokoro voice")) as HTMLSelectElement;
    expect(within(select).getByRole("option", { name: "Heart · American F" })).toBeInTheDocument();
    expect(
      within(select).getByRole("option", { name: "Michael · American M" }),
    ).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Emma · British F" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "kokoro-am_michael" } });
    await waitFor(() => expect(puts).toContainEqual({ brain_answer_voice: "kokoro-am_michael" }));
  });

  it("keeps a saved Kokoro model selected on a box that lists no Kokoro voices", async () => {
    // brain_answer_voice is account-synced, but /tts/voices is per-box: a box without the Kokoro
    // weights lists none. The Kokoro model must still show selected and the saved voice must
    // surface, so the selection stays visible + recoverable.
    stubSettingsFetch("full", {
      answerVoice: "kokoro-af_sky",
      voices: ["kokoro-af_heart", "kokoro-am_michael"],
    });
    setup();
    const models = within(await screen.findByLabelText("Read-aloud model"));
    expect(models.getByRole("button", { name: "Kokoro" })).toHaveAttribute("aria-pressed", "true");
    const sub = (await screen.findByLabelText("Kokoro voice")) as HTMLSelectElement;
    expect(sub.value).toBe("kokoro-af_sky");
    expect(within(sub).getByRole("option", { name: "Sky · American F" })).toBeInTheDocument();
  });

  it("broadcasts a voice change so the mounted chat read-aloud hook picks it up", async () => {
    // The chat hook (HomeScreen) never unmounts, so a save here must reach it over the bus.
    const seen: Array<Record<string, unknown>> = [];
    const off = onReadAloudSettings((p) => seen.push(p as Record<string, unknown>));
    stubSettingsFetch();
    setup();
    const select = await screen.findByLabelText("Kokoro voice");
    fireEvent.change(select, { target: { value: "kokoro-am_michael" } });
    await waitFor(() => expect(seen).toContainEqual({ brain_answer_voice: "kokoro-am_michael" }));
    off();
  });

  it("switches to the Native model and hides the on-box voice picker", async () => {
    const { puts } = stubSettingsFetch();
    setup();
    const models = within(await screen.findByLabelText("Read-aloud model"));
    await waitFor(() =>
      expect(models.getByRole("button", { name: "Kokoro" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
    expect(screen.getByLabelText("Kokoro voice")).toBeInTheDocument();

    fireEvent.click(models.getByRole("button", { name: "Native" }));
    await waitFor(() => expect(puts).toContainEqual({ brain_read_aloud_engine: "native" }));
    // Native uses the device voice — the on-box voice picker drops away.
    await waitFor(() => expect(screen.queryByLabelText("Kokoro voice")).toBeNull());
  });

  it("toggles the chorus and robot voice effects, persisting + broadcasting each", async () => {
    const seen: Array<Record<string, unknown>> = [];
    const off = onReadAloudSettings((p) => seen.push(p as Record<string, unknown>));
    const { puts } = stubSettingsFetch();
    setup();
    const fx = within(await screen.findByLabelText("Voice character effects"));
    fireEvent.click(fx.getByRole("button", { name: "Chorus" }));
    await waitFor(() => expect(puts).toContainEqual({ brain_answer_chorus: true }));
    fireEvent.click(fx.getByRole("button", { name: "Robot" }));
    await waitFor(() => expect(puts).toContainEqual({ brain_answer_robot: true }));
    expect(seen).toContainEqual({ brain_answer_chorus: true });
    expect(seen).toContainEqual({ brain_answer_robot: true });
    off();
  });

  it("commits the reading-speed slider on release", async () => {
    const { puts } = stubSettingsFetch();
    setup();
    const slider = await screen.findByLabelText("Reading speed");
    fireEvent.change(slider, { target: { value: "1.5" } });
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(puts).toContainEqual({ brain_answer_speed: 1.5 }));
  });

  it("hides the chorus/robot toggles on the Native model (on-box-only effects)", async () => {
    stubSettingsFetch();
    setup();
    const models = within(await screen.findByLabelText("Read-aloud model"));
    fireEvent.click(models.getByRole("button", { name: "Native" }));
    await waitFor(() => expect(screen.queryByLabelText("Voice character effects")).toBeNull());
    // Speed still applies to the native voice, so its slider stays.
    expect(screen.getByLabelText("Reading speed")).toBeInTheDocument();
  });

  it("surfaces a failed save and reconciles the control to the server (no silent revert)", async () => {
    // The reported "settings often don't take": a transient PUT failure used to be swallowed, so
    // the toggle looked set but reverted on the next load. Now it must show an error AND snap back.
    stubSettingsFetch("full", { failPut: true });
    setup();
    const fx = within(await screen.findByLabelText("Voice character effects"));
    const chorus = fx.getByRole("button", { name: "Chorus" });
    expect(chorus).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(chorus);
    // Optimistic: it shows on immediately…
    await waitFor(() => expect(chorus).toHaveAttribute("aria-pressed", "true"));
    // …then the PUT 500s → a sync error appears and the control reconciles back to off.
    await screen.findByText(/couldn't save/i);
    await waitFor(() => expect(chorus).toHaveAttribute("aria-pressed", "false"));
  });

  it("plays a voice sample through the owner's effects (speed/chorus/robot ride along)", async () => {
    const { ttsUrls } = stubSettingsFetch();
    setup();
    const fx = within(await screen.findByLabelText("Voice character effects"));
    fireEvent.click(fx.getByRole("button", { name: "Chorus" }));
    fireEvent.click(fx.getByRole("button", { name: "Robot" }));
    fireEvent.click(await screen.findByRole("button", { name: "Play sample" }));
    await waitFor(() => expect(ttsUrls).toHaveLength(1));
    const q = new URL(ttsUrls[0] ?? "", "http://x").searchParams;
    expect(q.get("speed")).toBe("1");
    expect(q.get("chorus")).toBe("1");
    expect(q.get("robot")).toBe("1");
  });

  it("opens the read-custom-text surface from the voice picker", async () => {
    stubSettingsFetch();
    setup();
    const open = await screen.findByRole("button", { name: "Read custom text" });
    fireEvent.click(open);
    // The overlay is mostly a text area, with Play + Export controls.
    expect(await screen.findByLabelText("Text to read aloud")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export audio" })).toBeInTheDocument();
    // Its back button closes it again.
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(screen.queryByLabelText("Text to read aloud")).toBeNull());
  });

  it("renders a sample of the selected voice on tap", async () => {
    // Audio isn't implemented in jsdom — a stand-in captures play().
    const played: string[] = [];
    class FakeAudio {
      onended: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(src: string) {
        played.push(src);
      }
      play() {
        this.onended?.();
        return Promise.resolve();
      }
      pause() {}
    }
    vi.stubGlobal("Audio", FakeAudio);
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:x" });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
    setup();
    const sample = await screen.findByRole("button", { name: "Play sample" });
    fireEvent.click(sample);
    await waitFor(() => expect(played).toHaveLength(1));
  });
});

describe("SettingsScreen pronunciations", () => {
  // The inline list lives in the read-aloud voice card; scope queries to it so the
  // panel's Save/Test don't collide with the Gmail section's Save.
  async function pronPanel() {
    const card = (await screen.findByText("Read-aloud voice")).closest("section") as HTMLElement;
    return within(card);
  }

  it("renders the rows from pronunciation_lexicon", async () => {
    stubSettingsFetch("full", {
      lexicon: { Titusville: "Tight us ville", GIF: "jiff" },
    });
    setup();
    const pron = await pronPanel();
    expect(await pron.findByText("Titusville")).toBeInTheDocument();
    expect(pron.getByText("Tight us ville")).toBeInTheDocument();
    expect(pron.getByText("GIF")).toBeInTheDocument();
    expect(pron.getByText("jiff")).toBeInTheDocument();
  });

  it("adds a word, PUTting the full map including the new entry", async () => {
    const { puts } = stubSettingsFetch("full", { lexicon: { GIF: "jiff" } });
    setup();
    const pron = await pronPanel();
    // The empty state hides the form until the "Add" toggle opens it.
    fireEvent.click(await pron.findByRole("button", { name: /Add a pronunciation/ }));
    fireEvent.change(pron.getByLabelText("Word"), { target: { value: "Titusville" } });
    fireEvent.change(pron.getByLabelText("Say it like"), { target: { value: "Tight us ville" } });
    fireEvent.click(pron.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(puts).toContainEqual({
        pronunciation_lexicon: { GIF: "jiff", Titusville: "Tight us ville" },
      }),
    );
  });

  it("deletes a word, PUTting the map without it", async () => {
    const { puts } = stubSettingsFetch("full", {
      lexicon: { GIF: "jiff", Titusville: "Tight us ville" },
    });
    setup();
    const pron = await pronPanel();
    fireEvent.click(await pron.findByRole("button", { name: "Remove Titusville" }));
    await waitFor(() => expect(puts).toContainEqual({ pronunciation_lexicon: { GIF: "jiff" } }));
  });

  it("shows the misaki health chip", async () => {
    stubSettingsFetch("full");
    setup();
    expect(await screen.findByText(/Voice engine: misaki/)).toBeInTheDocument();
  });
});

describe("SettingsScreen response typing speed", () => {
  it("defaults the pick to 30/s", () => {
    setup();
    const group = screen.getByLabelText("Response typing speed");
    expect(group.querySelector('[aria-pressed="true"]')).toHaveTextContent("30/s");
  });

  it("persists a chosen rate across remounts via localStorage", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "45/s" }));
    expect(localStorage.getItem("jbrain.tokenRate")).toBe("45");
    expect(screen.getByRole("button", { name: "45/s" })).toHaveAttribute("aria-pressed", "true");
  });

  it("offers Instant as a zero-rate (pacing off) choice", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Instant" }));
    expect(localStorage.getItem("jbrain.tokenRate")).toBe("0");
  });
});

describe("SettingsScreen image analysis", () => {
  it("loads the server mode and marks it pressed (full is the default)", async () => {
    setup();
    const group = screen.getByLabelText("Image analysis");
    await waitFor(() =>
      expect(group.querySelector('[aria-pressed="true"]')).toHaveTextContent("full analysis"),
    );
    expect(screen.getByRole("button", { name: "ocr only" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("reflects a server-side ocr-only mode on load", async () => {
    stubSettingsFetch("ocr");
    setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "ocr only" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
  });

  it("saves a pick via PUT /api/settings and round-trips it", async () => {
    const { puts, state } = stubSettingsFetch("full");
    setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "full analysis" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "ocr only" }));
    // Optimistic press, then the PUT lands on the wire.
    expect(screen.getByRole("button", { name: "ocr only" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() => expect(puts).toEqual([{ image_analysis_mode: "ocr" }]));
    expect(state.mode).toBe("ocr");
  });
});

describe("SettingsScreen calendar feed", () => {
  function json(body: unknown) {
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  it("generates a subscribe link and shows the URL", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const path = String(input);
      if (path === "/api/settings") return json({ image_analysis_mode: "full" });
      if (path === "/api/feed/appointments" && (init?.method ?? "GET").toUpperCase() === "GET") {
        return json({ enabled: false, token: null });
      }
      if (path === "/api/feed/appointments/rotate") {
        return json({ enabled: true, token: "secret-tok" });
      }
      throw new Error(`Unexpected fetch: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    setup();

    // Disabled on load → a Generate button; after generating, the URL appears.
    fireEvent.click(await screen.findByRole("button", { name: "Generate link" }));
    const url = (await screen.findByLabelText("Calendar feed URL")) as HTMLInputElement;
    expect(url.value).toContain("/api/feed/appointments.ics?token=secret-tok");
  });
});

describe("SettingsScreen debug access", () => {
  function json(body: unknown, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  // A stateful stub for the debug-token endpoints (plus the settings/feed loads
  // the screen does on mount). `mintStatus` lets a test force the 409 path.
  function stubDebug(opts: { tokens?: unknown[]; mintStatus?: number } = {}) {
    const tokens = opts.tokens ?? [];
    const deletes: string[] = [];
    const suspends: string[] = [];
    const resumes: string[] = [];
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const path = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (path === "/api/settings") return json({ image_analysis_mode: "full" });
      if (path.startsWith("/api/feed/appointments")) return json({ enabled: false, token: null });
      if (path === "/api/settings/debug-tokens" && method === "GET") return json(tokens);
      if (path === "/api/settings/debug-tokens" && method === "POST") {
        if (opts.mintStatus) return json({ detail: "off" }, opts.mintStatus);
        return json({ id: "t1", label: "Claude", expires_at: null, payload: "PASTE-ME" }, 201);
      }
      if (path.endsWith("/suspend") && method === "POST") {
        suspends.push(path.split("/").at(-2) ?? "");
        return new Response(null, { status: 204 });
      }
      if (path.endsWith("/resume") && method === "POST") {
        resumes.push(path.split("/").at(-2) ?? "");
        return new Response(null, { status: 204 });
      }
      if (path.startsWith("/api/settings/debug-tokens/") && method === "DELETE") {
        deletes.push(path.split("/").pop() ?? "");
        return new Response(null, { status: 204 });
      }
      throw new Error(`Unexpected fetch: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    return { deletes, suspends, resumes };
  }

  const tokenRow = (over: Record<string, unknown> = {}) => ({
    id: "abc",
    label: "Phone debug",
    created_at: "2026-06-22T00:00:00Z",
    expires_at: "2099-01-01T00:00:00Z",
    last_used_at: null,
    revoked_at: null,
    suspended_at: null,
    ...over,
  });

  it("mints a token and reveals the one-time payload", async () => {
    stubDebug();
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "Mint token" }));
    const payload = (await screen.findByLabelText("Debug token payload")) as HTMLInputElement;
    expect(payload.value).toBe("PASTE-ME");
  });

  it("explains when debug access is disabled on the server", async () => {
    stubDebug({ mintStatus: 409 });
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "Mint token" }));
    expect(await screen.findByText(/Debug access is off/)).toBeInTheDocument();
  });

  it("lists an active token and revokes it on a confirmed tap", async () => {
    const { deletes } = stubDebug({ tokens: [tokenRow()] });
    setup();
    expect(await screen.findByText("Phone debug")).toBeInTheDocument();
    const revoke = screen.getByRole("button", { name: "Revoke" });
    fireEvent.click(revoke); // first tap arms the inline confirm
    fireEvent.click(screen.getByRole("button", { name: "Tap to confirm" }));
    await waitFor(() => expect(deletes).toEqual(["abc"]));
  });

  it("suspends an active token", async () => {
    const { suspends } = stubDebug({ tokens: [tokenRow()] });
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "Suspend" }));
    await waitFor(() => expect(suspends).toEqual(["abc"]));
  });

  it("resumes a suspended token", async () => {
    const { resumes } = stubDebug({
      tokens: [tokenRow({ suspended_at: "2026-06-22T01:00:00Z" })],
    });
    setup();
    // A suspended token shows its status and offers Resume instead of Suspend.
    expect(await screen.findByText("suspended")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(resumes).toEqual(["abc"]));
  });

  it("lists only active/suspended tokens, hiding revoked and expired ones", async () => {
    stubDebug({
      tokens: [
        tokenRow({ id: "a", label: "Active one" }),
        tokenRow({ id: "s", label: "Suspended one", suspended_at: "2026-06-22T01:00:00Z" }),
        tokenRow({ id: "r", label: "Revoked one", revoked_at: "2026-06-22T00:00:00Z" }),
        tokenRow({ id: "e", label: "Expired one", expires_at: "2000-01-01T00:00:00Z" }),
      ],
    });
    setup();
    expect(await screen.findByText("Active one")).toBeInTheDocument();
    expect(screen.getByText("Suspended one")).toBeInTheDocument();
    expect(screen.queryByText("Revoked one")).not.toBeInTheDocument();
    expect(screen.queryByText("Expired one")).not.toBeInTheDocument();
  });
});

describe("SettingsScreen time zone", () => {
  it("shows the stored owner timezone when the server has one", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const path = String(input);
      if (path.startsWith("/api/feed/appointments")) {
        return new Response(JSON.stringify({ enabled: false, token: null }), { status: 200 });
      }
      return new Response(
        JSON.stringify({ image_analysis_mode: "full", owner_timezone: "America/New_York" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    setup();
    expect(await screen.findByLabelText("Time zone")).toHaveTextContent("America/New_York");
  });
});

describe("SettingsScreen Gmail (Archivist)", () => {
  function json(body: unknown, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  // A stateful stub for the gmail credential endpoints (plus the other mount loads).
  function stubGmail() {
    const state = {
      client_id_set: false,
      client_secret_set: false,
      refresh_token_set: false,
      connected: false,
    };
    const puts: unknown[] = [];
    const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
      const path = String(input);
      if (path === "/api/settings") return json({ image_analysis_mode: "full" });
      if (path.startsWith("/api/feed/appointments")) return json({ enabled: false, token: null });
      if (path.startsWith("/api/settings/debug-tokens")) return json([]);
      if (path === "/api/settings/gmail") {
        if ((init?.method ?? "GET").toUpperCase() === "PUT") {
          const body = JSON.parse(String(init?.body)) as Record<string, string>;
          puts.push(body);
          if (body.client_id) state.client_id_set = true;
          if (body.client_secret) state.client_secret_set = true;
          if (body.refresh_token) {
            state.refresh_token_set = true;
            state.connected = true;
          }
        }
        return json(state);
      }
      throw new Error(`Unexpected fetch: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    return { puts };
  }

  it("saves pasted credentials and shows Connected", async () => {
    const { puts } = stubGmail();
    setup();
    expect(await screen.findByText("Not connected")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Client ID/), { target: { value: "cid" } });
    fireEvent.change(screen.getByLabelText(/Client secret/), { target: { value: "sec" } });
    fireEvent.change(screen.getByLabelText(/Refresh token/), { target: { value: "rt" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(puts).toEqual([{ client_id: "cid", client_secret: "sec", refresh_token: "rt" }]);
  });

  it("enables Connect once the client id + secret are saved (no token needed)", async () => {
    stubGmail();
    setup();
    // Disconnected on load: Connect is disabled until credentials exist.
    expect(await screen.findByText("Not connected")).toBeInTheDocument();
    const connect = () => screen.getByRole("button", { name: "Connect Gmail" });
    expect(connect()).toBeDisabled();

    // Save just the client id + secret (no refresh token) — the in-app path.
    fireEvent.change(screen.getByLabelText(/Client ID/), { target: { value: "cid" } });
    fireEvent.change(screen.getByLabelText(/Client secret/), { target: { value: "sec" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Credentials saved — not connected yet")).toBeInTheDocument();
    expect(connect()).toBeEnabled(); // ready to launch the OAuth consent
  });
});
