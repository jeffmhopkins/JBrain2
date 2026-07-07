import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isLocationCaptureEnabled } from "../location";
import { SettingsScreen } from "./SettingsScreen";

function setup() {
  render(<SettingsScreen deviceLabel="Test device" onLogout={vi.fn()} />);
}

// The screen loads the server-synced settings on mount; a stateful stub
// makes GET/PUT round-trip like the real /api/settings.
function stubSettingsFetch(initial: "full" | "ocr" = "full") {
  const state = {
    mode: initial,
    brainStream: false,
    brainReadAloud: false,
    brainAnswerVoice: "en_US-amy-medium",
    engine: "piper" as "piper" | "native",
  };
  const puts: unknown[] = [];
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const path = String(input);
    // The read-aloud voice picker loads the box's installed piper voices on mount.
    if (path === "/api/brain/voices") {
      return new Response(
        JSON.stringify({
          voices: [
            "en_US-amy-medium",
            "en_US-joe-medium",
            "en_US-libritts_r-medium#3922",
            "kokoro-af_heart",
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    // The voice explorer loads the multi-speaker roster on mount (a small stand-in list).
    if (path === "/api/brain/speakers") {
      return new Response(
        JSON.stringify({ speakers: { "en_US-libritts_r-medium": ["3922", "1234", "6272"] } }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    // A voice sample renders a WAV via the api proxy — an empty audio blob is enough.
    if (path.startsWith("/api/brain/tts")) {
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
    if (path !== "/api/settings") {
      throw new Error(`Unexpected fetch: ${path}`);
    }
    if ((init?.method ?? "GET").toUpperCase() === "PUT") {
      const body = JSON.parse(String(init?.body)) as {
        image_analysis_mode?: "full" | "ocr";
        brain_llm_stream?: boolean;
        brain_read_aloud?: boolean;
        brain_answer_voice?: string;
        brain_read_aloud_engine?: "piper" | "native";
      };
      puts.push(body);
      if (body.image_analysis_mode) state.mode = body.image_analysis_mode;
      if (typeof body.brain_llm_stream === "boolean") state.brainStream = body.brain_llm_stream;
      if (typeof body.brain_read_aloud === "boolean") state.brainReadAloud = body.brain_read_aloud;
      if (typeof body.brain_answer_voice === "string")
        state.brainAnswerVoice = body.brain_answer_voice;
      if (body.brain_read_aloud_engine) state.engine = body.brain_read_aloud_engine;
    }
    return new Response(
      JSON.stringify({
        image_analysis_mode: state.mode,
        brain_llm_stream: state.brainStream,
        brain_read_aloud: state.brainReadAloud,
        brain_answer_voice: state.brainAnswerVoice,
        brain_read_aloud_engine: state.engine,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return { puts, state };
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
  it("lists the box's voices (multi-speaker speakers prettified) and PUTs a pick", async () => {
    const { puts } = stubSettingsFetch();
    setup();
    const select = (await screen.findByLabelText("Read-aloud voice")) as HTMLSelectElement;
    // The curated multi-speaker entry shows its speaker after a dot.
    expect(within(select).getByRole("option", { name: "Libritts_r · 3922" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Amy" })).toBeInTheDocument();
    // A Kokoro voice reads with its engine and the lang/gender code dropped.
    expect(within(select).getByRole("option", { name: "Kokoro · Heart" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "en_US-libritts_r-medium#3922" } });
    await waitFor(() =>
      expect(puts).toContainEqual({ brain_answer_voice: "en_US-libritts_r-medium#3922" }),
    );
  });

  it("switches the read-aloud engine and hides the voice picker on Native", async () => {
    const { puts } = stubSettingsFetch();
    setup();
    const group = within(await screen.findByLabelText("Read-aloud engine"));
    // Defaults to Piper (on-box), so the voice picker is shown.
    await waitFor(() =>
      expect(group.getByRole("button", { name: "Piper" })).toHaveAttribute("aria-pressed", "true"),
    );
    expect(screen.getByLabelText("Read-aloud voice")).toBeInTheDocument();

    fireEvent.click(group.getByRole("button", { name: "Native" }));
    await waitFor(() => expect(puts).toContainEqual({ brain_read_aloud_engine: "native" }));
    // Native uses the device voice — the piper voice picker drops away.
    await waitFor(() => expect(screen.queryByLabelText("Read-aloud voice")).toBeNull());
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

  it("shuffles a random speaker, auditions it, and keeps it as the answer voice", async () => {
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
    const { puts } = stubSettingsFetch();
    setup();
    // Keep is disabled until a speaker has been auditioned.
    const explorer = within(await screen.findByLabelText("Discover a voice"));
    expect(explorer.getByRole("button", { name: "Keep this voice" })).toBeDisabled();
    // Shuffle auditions a random speaker (renders a sample) and enables Keep.
    fireEvent.click(explorer.getByRole("button", { name: "Shuffle" }));
    await waitFor(() => expect(played).toHaveLength(1));
    const keep = explorer.getByRole("button", { name: "Keep this voice" });
    await waitFor(() => expect(keep).not.toBeDisabled());
    fireEvent.click(keep);
    // The kept voice is one of the roster's speakers, saved as "<model>#<name>".
    await waitFor(() =>
      expect(
        puts.some(
          (p) =>
            typeof (p as { brain_answer_voice?: string }).brain_answer_voice === "string" &&
            /^en_US-libritts_r-medium#(3922|1234|6272)$/.test(
              (p as { brain_answer_voice: string }).brain_answer_voice,
            ),
        ),
      ).toBe(true),
    );
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
