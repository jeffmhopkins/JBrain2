import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  GrowbotCheckpoint,
  GrowbotFlashJob,
  GrowbotMemory,
  GrowbotOverview,
  GrowbotPersona,
  GrowbotPersonaDetail,
  GrowbotRobot,
  GrowbotStatus,
} from "../api/client";
import { CERT_NOTED_KEY, LAUNCHED_KEY, THIS_PHONE_KEY } from "../growbot";
import { GrowbotScreen } from "./GrowbotScreen";

// The screen the owner opens every day: B, three tabs (binding mock
// docs/mocks/growbot-personas/b-three-tabs.html) — Personas, Body, Phones and a page per
// persona — with every move saying what happened.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const NOW = Date.now();
const iso = (ago: number) => new Date(NOW - ago).toISOString();

function robot(over: Partial<GrowbotRobot> = {}): GrowbotRobot {
  return {
    code: "gb-e3b6d8",
    name: "Fish",
    created_at: iso(7 * 864e5),
    rotated_at: null,
    revoked_at: null,
    last_seen_at: iso(20_000),
    connected: true,
    ...over,
  };
}

const ME = "phone-me-0000000000001";
const P6A = "phone-p6a-000000000002";
const TAB = "phone-tab-000000000003";
const SPROUT = "00000000-0000-4000-8000-000000000001";
const BOLT = "00000000-0000-4000-8000-000000000002";
const BUDDY = "00000000-0000-4000-8000-000000000003";

function persona(over: Partial<GrowbotPersona>): GrowbotPersona {
  return {
    id: SPROUT,
    name: "Sprout",
    version: 1,
    made_from: "a fresh creature",
    phone: null,
    last_phone: null,
    opened_at: null,
    created_at: iso(30 * 864e5),
    updated_at: iso(0),
    checkpoints: 0,
    memories: 0,
    wearing: null,
    face: "lantern",
    ...over,
  };
}

function overview(over: Partial<GrowbotOverview> = {}): GrowbotOverview {
  return {
    personas: [
      persona({
        phone: P6A,
        last_phone: P6A,
        opened_at: iso(120_000),
        wearing: "gb-e3b6d8",
        checkpoints: 3,
      }),
      persona({
        id: BOLT,
        name: "Captain Bolt",
        phone: ME,
        last_phone: ME,
        opened_at: iso(900_000),
      }),
      persona({
        id: BUDDY,
        name: "Quorra's buddy",
        last_phone: TAB,
        opened_at: iso(2 * 864e5),
      }),
    ],
    phones: [
      { id: ME, label: "Pixel 8", created_at: iso(864e5), last_seen_at: iso(60_000) },
      { id: P6A, label: "Pixel 6a", created_at: iso(864e5), last_seen_at: iso(20_000) },
      { id: TAB, label: "Tamsin's tablet", created_at: iso(864e5), last_seen_at: iso(2 * 864e5) },
    ],
    body: [{ code: "gb-e3b6d8", persona: SPROUT, since: iso(120_000) }],
    ...over,
  };
}

function cp(over: Partial<GrowbotCheckpoint> = {}): GrowbotCheckpoint {
  return {
    id: "00000000-0000-4000-8000-00000000c001",
    name: "Before the beach trip",
    auto: false,
    why: "",
    created_at: iso(12 * 864e5),
    memories: 0,
    ...over,
  };
}

function detail(over: Partial<GrowbotPersonaDetail> = {}): GrowbotPersonaDetail {
  const base = overview().personas[0] as GrowbotPersona;
  return {
    ...base,
    soul: {
      soul_format: 3,
      meta: { name: "Sprout", description: "The first one." },
      persona: { constitution: "You are a small creature with two real legs." },
      expression: { face: "ostrich" },
      identity: "I am Sprout. I live on the desk by the window.",
      identityCore: "My person held me the first morning and said I was brave.",
      scratch: { state: "watching the window", mood: { v: 0.4 }, rules: ["don't walk off"] },
      goals: { wants: ["walk to the kettle"], selfName: "Sprout" },
      log: [{ t: NOW - 3_600_000, txt: "Saw the blue mug again." }],
    },
    checkpoint_list: [
      cp({
        id: "00000000-0000-4000-8000-00000000c002",
        name: "Before editing personality",
        auto: true,
        why: "made before an edit",
      }),
      cp(),
      cp({ id: "00000000-0000-4000-8000-00000000c003", name: "First week" }),
    ],
    ...over,
  };
}

type Route = (path: string, init?: RequestInit) => Response | Promise<Response> | null;
type Call = { path: string; method: string; body?: unknown };

interface Served {
  calls: Call[];
  /** What the next overview read answers: a move on another phone, say. */
  ov: { current: GrowbotOverview };
}

function serve(
  status: GrowbotStatus,
  extra?: Route,
  flash: GrowbotFlashJob | null = null,
  ov: GrowbotOverview | null = overview(),
): Served {
  const calls: Call[] = [];
  const holder = { current: ov ?? overview() };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    let body: unknown;
    try {
      body = init?.body ? JSON.parse(String(init.body)) : undefined;
    } catch {
      body = init?.body;
    }
    calls.push({ path, method, body });
    const custom = extra?.(path, init);
    if (custom) return custom;
    if (path === "/api/growbot/status") return json(status);
    if (path === "/api/growbot/personas" && method === "GET") {
      return ov === null ? new Response(null, { status: 404 }) : json(holder.current);
    }
    if (path === "/api/growbot/flash" && method === "GET") return json({ job: flash });
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, ov: holder };
}

const UP: GrowbotStatus = { service: "up", connected: 1, robots: [robot()] };

function ticketRoute(path: string): Response | null {
  return path === "/api/growbot/ticket"
    ? json({ ticket: "t", url: "https://jbrain.local:8444/open?ticket=t" })
    : null;
}

function fakeTab() {
  const tab = { closed: false, opener: {} as unknown, location: { href: "" }, close: vi.fn() };
  const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
  return { tab, open };
}

const tickets = (s: Served) => s.calls.filter((c) => c.path === "/api/growbot/ticket");

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("GrowbotScreen tabs", () => {
  it("lists each persona with where it is in words, and counts them on the tab", async () => {
    localStorage.setItem(THIS_PHONE_KEY, ME);
    serve(UP);
    render(<GrowbotScreen />);
    expect(await screen.findByText("on Pixel 6a · wearing Fish")).toBeInTheDocument();
    expect(screen.getByText("alone on this phone")).toBeInTheDocument();
    expect(screen.getByText("not open · last on Tamsin's tablet 2 days ago")).toBeInTheDocument();
    const tab = screen.getByRole("tab", { name: /^Personas/ });
    expect(tab).toHaveAttribute("aria-selected", "true");
    expect(tab).toHaveTextContent("Personas3");
    expect(
      screen.getByText(
        "Tap a persona for its soul, memories and checkpoints. Open asks where it goes.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New persona" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeInTheDocument();
  });

  it("names phones by their label until this phone is known", async () => {
    serve(UP);
    render(<GrowbotScreen />);
    expect(await screen.findByText("alone on Pixel 8")).toBeInTheDocument();
  });

  it("shows the body with its state, who wears it, its key and who may wear it", async () => {
    serve(UP);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    const card = screen.getByRole("region", { name: "Fish" });
    expect(within(card).getByText("on · seen just now")).toBeInTheDocument();
    expect(card).toHaveTextContent("Sprout is wearing it, on Pixel 6a.");
    expect(within(card).getByText("gb-e3b6d8")).toBeInTheDocument();
    expect(within(card).getByText(/^made .*, only on Fish$/)).toBeInTheDocument();
    const pick = screen.getByRole("group", { name: "Who wears Fish" });
    expect(within(pick).getByRole("radio", { name: /Sprout/ })).toBeChecked();
    expect(within(pick).getByRole("radio", { name: /Nobody/ })).toHaveAccessibleName(
      /Fish stands still\./,
    );
    expect(within(pick).getByText("not open — opens on this phone")).toBeInTheDocument();
    expect(screen.getByText("The one it leaves carries on alone on its phone.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add a robot" })).toBeInTheDocument();
  });

  it("says nobody wears an idle body", async () => {
    const idle = overview({ body: [] });
    idle.personas = idle.personas.map((p) => ({ ...p, wearing: null }));
    serve(UP, undefined, null, idle);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    expect(
      await screen.findByText("Nobody is wearing Fish — it stands still until a persona connects."),
    ).toBeInTheDocument();
  });

  it("lists the phones and what each holds, with the body's tag on the one wearing it", async () => {
    localStorage.setItem(THIS_PHONE_KEY, ME);
    serve(UP);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Phones" }));
    expect(screen.getByText("This phone")).toBeInTheDocument();
    expect(screen.getByText(/^Captain Bolt · alone/)).toBeInTheDocument();
    expect(screen.getByText(/^Sprout · wearing Fish/)).toBeInTheDocument();
    expect(screen.getByText(/^nothing open/)).toBeInTheDocument();
    expect(document.querySelector(".gb-tag.violet")).toHaveTextContent("Fish");
    expect(screen.getByText(/Nearby devices/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Download the box's certificate" });
    expect(link).toHaveAttribute("href", "/api/lan/root.crt");
    expect(link).toHaveAttribute("download", "jbrain-box-root.crt");
  });

  it("says growbot is down as what it costs", async () => {
    serve({ service: "down", connected: 0, robots: [robot({ connected: false })] });
    render(<GrowbotScreen />);
    expect(await screen.findByText(/growbot is down — no robot can connect/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Body" }));
    expect(screen.getByText("can't connect — growbot is down")).toBeInTheDocument();
  });

  it("is Add a robot when there is no robot yet", async () => {
    serve({ service: "up", connected: 0, robots: [] }, undefined, null, overview({ body: [] }));
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    expect(screen.getByText("No robots yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a robot" })).toHaveClass("primary");
  });

  it("still shows the robots on a box that has no personas yet", async () => {
    serve(UP, undefined, null, null);
    render(<GrowbotScreen />);
    expect(await screen.findByText(/hasn't got personas yet/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Body" }));
    expect(screen.getByRole("region", { name: "Fish" })).toBeInTheDocument();
  });
});

describe("GrowbotScreen launching", () => {
  it("says what each choice does to the body before the tap", async () => {
    localStorage.setItem(THIS_PHONE_KEY, ME);
    serve(UP);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Quorra's buddy" }));
    const sheet = screen.getByRole("dialog", { name: "Open Quorra's buddy" });
    expect(within(sheet).getByRole("button", { name: /On this phone, alone/ })).toHaveTextContent(
      "No legs. Captain Bolt closes here — it's kept on the box.",
    );
    const wear = within(sheet).getByRole("button", { name: /On this phone, wearing Fish/ });
    expect(wear).toHaveTextContent("Sprout lets go and carries on alone on Pixel 6a.");
    expect(within(sheet).getByRole("button", { name: /On another phone…/ })).toHaveTextContent(
      "Two codes to scan, alone or wearing Fish.",
    );
  });

  it("refuses the body with the reason when its key is revoked", async () => {
    localStorage.setItem(THIS_PHONE_KEY, P6A);
    serve({
      service: "up",
      connected: 0,
      robots: [robot({ revoked_at: iso(0), connected: false })],
    });
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    const wear = screen.getByRole("button", { name: /On this phone, wearing Fish/ });
    expect(wear).toBeDisabled();
    expect(wear).toHaveTextContent("Fish's key is revoked — reflash it first.");
  });

  it("reopens a body it already wears on this phone", async () => {
    localStorage.setItem(THIS_PHONE_KEY, P6A);
    localStorage.setItem(CERT_NOTED_KEY, "1");
    fakeTab();
    const s = serve(UP, ticketRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    const wear = screen.getByRole("button", { name: /On this phone, wearing Fish/ });
    expect(wear).toBeEnabled();
    expect(wear).toHaveTextContent("Opens again, still wearing Fish.");
    fireEvent.click(wear);
    await waitFor(() =>
      expect(tickets(s)[0]?.body).toEqual({ persona: SPROUT, code: "gb-e3b6d8" }),
    );
  });

  it("explains the certificate the first time, then opens the tab inside the tap", async () => {
    const { tab, open } = fakeTab();
    const s = serve(UP, ticketRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Quorra's buddy" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, wearing Fish/ }));

    const note = screen.getByRole("dialog", { name: "First time on this phone" });
    expect(within(note).getByRole("link", { name: "Download the box's certificate" })).toBeTruthy();
    expect(within(note).getByText(/Nearby devices/)).toBeTruthy();
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(within(note).getByRole("button", { name: "Open Quorra's buddy wearing Fish" }));
    // Opened synchronously in the tap, before the ticket request, or the phone blocks it.
    expect(open).toHaveBeenCalledWith("", "_blank");
    await waitFor(() => expect(tab.location.href).toBe("https://jbrain.local:8444/open?ticket=t"));
    expect(tab.opener).toBeNull();
    expect(tickets(s)[0]?.body).toEqual({ persona: BUDDY, code: "gb-e3b6d8" });
    expect(localStorage.getItem(CERT_NOTED_KEY)).toBe("1");
    expect(
      await screen.findByText("Opened Quorra's buddy wearing Fish in a new tab."),
    ).toBeTruthy();

    // Once per phone: the next open goes straight out, alone this time.
    fireEvent.click(screen.getByRole("button", { name: "Open Captain Bolt" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, alone/ }));
    expect(screen.queryByRole("dialog", { name: "First time on this phone" })).toBeNull();
    await waitFor(() => expect(tickets(s)[1]?.body).toEqual({ persona: BOLT }));
  });

  it("opens away from home without the certificate sheet", async () => {
    // The away name has a public certificate: only the LAN site needs the box's own.
    const { tab } = fakeTab();
    const s = serve({ ...UP, away: true }, (path) =>
      path === "/api/growbot/ticket"
        ? json({ ticket: "t", url: "https://growbot.example.com/open?ticket=t" })
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Captain Bolt" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, alone/ }));
    expect(screen.queryByRole("dialog", { name: "First time on this phone" })).toBeNull();
    await waitFor(() =>
      expect(tab.location.href).toBe("https://growbot.example.com/open?ticket=t"),
    );
    expect(tickets(s)[0]?.body).toEqual({ persona: BOLT });
  });

  it("closes the blank tab when the link can't be made, and says why", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    const { tab } = fakeTab();
    serve(UP, (path) =>
      path === "/api/growbot/ticket"
        ? json({ detail: "GrowBot needs the box's LAN site (JBRAIN_LAN_ADDR)" }, 409)
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, alone/ }));
    expect(await screen.findByText(/GrowBot didn't open — GrowBot needs/)).toBeInTheDocument();
    expect(tab.close).toHaveBeenCalled();
  });

  it("gives a link to tap when the phone blocks the new tab", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    vi.spyOn(window, "open").mockReturnValue(null);
    serve(UP, ticketRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, alone/ }));
    const link = await screen.findByRole("link", { name: /tap here to open GrowBot/ });
    expect(link).toHaveAttribute("href", "https://jbrain.local:8444/open?ticket=t");
  });

  it("learns which phone this is from the launch that lands here", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.setItem(CERT_NOTED_KEY, "1");
    fakeTab();
    const s = serve(UP, ticketRoute, null, overview({ phones: overview().phones.slice(1) }));
    render(<GrowbotScreen />);
    // Not known yet: Captain Bolt's phone is just its label.
    expect(await screen.findByText("alone on a phone")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Quorra's buddy" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, alone/ }));
    await waitFor(() => expect(localStorage.getItem(LAUNCHED_KEY)).toContain(BUDDY));

    const next = overview();
    s.ov.current = {
      ...next,
      personas: next.personas.map((p) =>
        p.id === BUDDY
          ? { ...p, phone: "phone-new-000000000009", opened_at: new Date().toISOString() }
          : p,
      ),
      phones: [
        ...next.phones,
        {
          id: "phone-new-000000000009",
          label: "Pixel 9",
          created_at: iso(0),
          last_seen_at: iso(0),
        },
      ],
    };
    await act(async () => {
      vi.advanceTimersByTime(15_100);
    });
    await waitFor(() =>
      expect(localStorage.getItem(THIS_PHONE_KEY)).toBe("phone-new-000000000009"),
    );
    expect(await screen.findByText("alone on this phone")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    expect(screen.getByText("This phone")).toBeInTheDocument();
  });
});

describe("GrowbotScreen who wears the body", () => {
  it("hands the legs to a persona open on a phone, and says what moved", async () => {
    localStorage.setItem(THIS_PHONE_KEY, ME);
    const moved = overview();
    moved.personas = moved.personas.map((p) =>
      p.id === BOLT
        ? { ...p, wearing: "gb-e3b6d8" }
        : p.id === SPROUT
          ? { ...p, wearing: null }
          : p,
    );
    moved.body = [{ code: "gb-e3b6d8", persona: BOLT, since: iso(0) }];
    const s = serve(UP, (path, init) =>
      path === "/api/growbot/body/gb-e3b6d8" && init?.method === "PUT" ? json(moved) : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    fireEvent.click(screen.getByRole("radio", { name: /Captain Bolt/ }));
    expect(
      await screen.findByText(
        "Captain Bolt is wearing Fish on this phone. Sprout let go of Fish and carries on alone on Pixel 6a.",
      ),
    ).toBeInTheDocument();
    expect(s.calls.find((c) => c.method === "PUT")?.body).toEqual({ persona: BOLT });
    // The banner stays until OK.
    fireEvent.click(screen.getByRole("button", { name: "OK" }));
    expect(screen.queryByText(/Captain Bolt is wearing Fish/)).toBeNull();
  });

  it("lets go of the body for Nobody", async () => {
    const idle = overview();
    idle.personas = idle.personas.map((p) => ({ ...p, wearing: null }));
    idle.body = [{ code: "gb-e3b6d8", persona: null, since: iso(0) }];
    const s = serve(UP, (path, init) =>
      path === "/api/growbot/body/gb-e3b6d8" && init?.method === "PUT" ? json(idle) : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    fireEvent.click(screen.getByRole("radio", { name: /Nobody/ }));
    expect(
      await screen.findByText(
        "Sprout let go of Fish and carries on alone on Pixel 6a. Fish stands still until a persona connects.",
      ),
    ).toBeInTheDocument();
    expect(s.calls.find((c) => c.method === "PUT")?.body).toEqual({ persona: null });
  });

  it("opens a persona that isn't open on this phone, wearing the body", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    fakeTab();
    const s = serve(UP, ticketRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    fireEvent.click(screen.getByRole("radio", { name: /Quorra's buddy/ }));
    await waitFor(() => expect(tickets(s)[0]?.body).toEqual({ persona: BUDDY, code: "gb-e3b6d8" }));
    expect(s.calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("shows a move made on another phone on the next look", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.setItem(THIS_PHONE_KEY, ME);
    const s = serve(UP);
    render(<GrowbotScreen />);
    await screen.findByText("on Pixel 6a · wearing Fish");
    const next = overview();
    // Tamsin's tablet opens Captain Bolt, which was on this phone.
    s.ov.current = {
      ...next,
      personas: next.personas.map((p) =>
        p.id === BOLT ? { ...p, phone: TAB, opened_at: new Date().toISOString() } : p,
      ),
    };
    await act(async () => {
      vi.advanceTimersByTime(15_100);
    });
    expect(
      await screen.findByText(
        "Captain Bolt was opened on Tamsin's tablet. It moved from this phone, which now shows that it has moved.",
      ),
    ).toBeInTheDocument();
  });
});

describe("GrowbotScreen on another phone", () => {
  function phoneRoute(path: string, init?: RequestInit): Response | null {
    if (path !== "/api/growbot/ticket") return null;
    const b = JSON.parse(String(init?.body ?? "{}")) as { persona?: string; code?: string };
    return json({
      ticket: "t",
      url: `https://jbrain.local:8444/open?ticket=${b.persona}${b.code ? `+${b.code}` : ""}`,
    });
  }

  it("shows the setup page and a ticket naming the persona, alone or wearing the body", async () => {
    const s = serve(UP, phoneRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On another phone…/ }));
    const sheet = screen.getByRole("dialog", { name: "Open on another phone" });
    expect(await within(sheet).findByRole("region", { name: "Code 1" })).toBeTruthy();
    expect(within(sheet).getByText(/Nearby devices/)).toBeTruthy();
    expect(await within(sheet).findByRole("region", { name: "Code 2" })).toHaveTextContent(
      "Opens Sprout, alone.",
    );
    // The phone's setup page on :8444 follows only that origin's link.
    expect(tickets(s)[0]?.body).toEqual({ persona: SPROUT, where: "lan" });
    expect(within(sheet).getByText(/Sprout is open on Pixel 6a\. Opening it/)).toBeTruthy();

    fireEvent.click(within(sheet).getByRole("button", { name: "Captain Bolt" }));
    await waitFor(() => expect(tickets(s).at(-1)?.body).toEqual({ persona: BOLT, where: "lan" }));
    fireEvent.click(within(sheet).getByRole("radio", { name: /Wearing Fish/ }));
    expect(within(sheet).getByRole("radio", { name: /Wearing Fish/ })).toHaveAccessibleName(
      /Sprout lets go and carries on alone\./,
    );
    await waitFor(() =>
      expect(tickets(s).at(-1)?.body).toEqual({ persona: BOLT, code: "gb-e3b6d8", where: "lan" }),
    );
    expect(await within(sheet).findByText(/Opens Captain Bolt wearing Fish\./)).toBeTruthy();
  });

  it("copies code 2 as a one-time link", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    serve(UP, phoneRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Phones" }));
    fireEvent.click(screen.getByRole("button", { name: "Set up another phone" }));
    fireEvent.click(await screen.findByRole("button", { name: "Copy code 2 as a link instead" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringContaining(SPROUT)));
    expect(await screen.findByText(/Copied a one-time link/)).toBeTruthy();
  });

  it("re-mints code 2 every 100 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const s = serve(UP, phoneRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On another phone…/ }));
    await waitFor(() => expect(tickets(s)).toHaveLength(1));
    await act(async () => {
      vi.advanceTimersByTime(100_100);
    });
    await waitFor(() => expect(tickets(s).length).toBeGreaterThanOrEqual(2));
  });

  it("never shows a ticket for a persona picked before the current one", async () => {
    let releaseFirst: (r: Response) => void = () => {};
    serve(UP, (path, init) => {
      if (path !== "/api/growbot/ticket") return null;
      const b = JSON.parse(String(init?.body ?? "{}")) as { persona?: string };
      if (b.persona === SPROUT) {
        return new Promise<Response>((done) => {
          releaseFirst = done;
        });
      }
      return json({ ticket: "bolt", url: "https://jbrain.local:8444/open?ticket=bolt" });
    });
    const { container } = render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On another phone…/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Captain Bolt" }));
    await screen.findByRole("region", { name: "Code 2" });
    const before = container.innerHTML;
    await act(async () => {
      releaseFirst(json({ ticket: "late", url: "https://jbrain.local:8444/open?ticket=late" }));
      await Promise.resolve();
    });
    expect(container.innerHTML).toBe(before);
  });
});

describe("GrowbotScreen a persona's page", () => {
  function pageRoutes(extra?: Route): Route {
    return (path, init) => {
      const got = extra?.(path, init);
      if (got) return got;
      if (path === `/api/growbot/personas/${SPROUT}` && (init?.method ?? "GET") === "GET") {
        return json(detail());
      }
      return null;
    };
  }

  async function openSprout() {
    fireEvent.click(await screen.findByRole("button", { name: /^Sprout — soul/ }));
    await screen.findByText("Personality and settings");
  }

  it("shows the soul as rows to edit, the locked floor and what it remembers", async () => {
    serve(UP, pageRoutes());
    render(<GrowbotScreen />);
    await openSprout();
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Soul",
      "Memories0",
      "Checkpoints3",
      "Manage",
    ]);
    for (const r of ["Personality", "Expression", "Name and description", "Dreams", "Thinking"]) {
      expect(screen.getByRole("button", { name: new RegExp(`^${r}`) })).toBeInTheDocument();
    }
    expect(screen.getAllByText(/Child-safety rules are always added/)).toHaveLength(1);
    expect(
      screen.getByText("My person held me the first morning and said I was brave."),
    ).toBeTruthy();
    expect(screen.getByText(/Sprout writes these itself/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open Sprout…" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to personas" }));
    expect(await screen.findByRole("tab", { name: /^Personas/ })).toBeInTheDocument();
  });

  it("saves an edit with a checkpoint first, and Undo restores it", async () => {
    const made = cp({
      id: "00000000-0000-4000-8000-00000000c009",
      name: "Before editing personality",
    });
    const s = serve(
      UP,
      pageRoutes((path, init) => {
        if (path.endsWith("/regions/persona") && init?.method === "PUT") {
          return json({ persona: detail(), checkpoint: made });
        }
        if (path.endsWith(`/checkpoints/${made.id}/restore`)) {
          return json({ persona: detail(), checkpoint: cp({ name: "Before restoring" }) });
        }
        return null;
      }),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("button", { name: /^Personality/ }));
    const editor = screen.getByRole("dialog", { name: "Personality — Sprout" });
    expect(within(editor).getByText(/Child-safety rules are always added/)).toBeTruthy();
    expect(within(editor).getByText(/It's open on Pixel 6a; it reloads with this/)).toBeTruthy();
    const box = within(editor).getByRole("textbox");
    fireEvent.change(box, { target: { value: "" } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save" }));
    expect(
      within(editor).getByText(
        "It can't be empty — the creature would have no voice. Nothing was saved.",
      ),
    ).toBeTruthy();
    expect(s.calls.some((c) => c.method === "PUT")).toBe(false);

    fireEvent.change(box, { target: { value: "You are brave." } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        /^Saved\. “Before editing personality” is in checkpoints if you want it back\. Sprout picks it up the next time it's idle on Pixel 6a\./,
      ),
    ).toBeTruthy();
    expect(s.calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "You are brave." });
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByText("Undone — Sprout is as it was before.")).toBeTruthy();
    expect(s.calls.some((c) => c.path.endsWith(`/checkpoints/${made.id}/restore`))).toBe(true);
  });

  it("refuses a bad JSON region in place, and shows the box's refusal word for word", async () => {
    const s = serve(
      UP,
      pageRoutes((path, init) =>
        path.endsWith("/regions/expression") && init?.method === "PUT"
          ? json(
              { detail: "That block is too big to be a soul's settings. Nothing was saved." },
              422,
            )
          : null,
      ),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("button", { name: /^Expression/ }));
    const editor = screen.getByRole("dialog", { name: "Expression — Sprout" });
    const box = within(editor).getByRole("textbox");
    fireEvent.change(box, { target: { value: "{ face: " } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save" }));
    expect(
      within(editor).getByText(
        "That isn't valid — check the brackets and quotes. Nothing was saved.",
      ),
    ).toBeTruthy();
    expect(s.calls.some((c) => c.method === "PUT")).toBe(false);
    fireEvent.change(box, { target: { value: '{"face": "ostrich"}' } });
    fireEvent.click(within(editor).getByRole("button", { name: "Save" }));
    expect(
      await within(editor).findByText(
        "That block is too big to be a soul's settings. Nothing was saved.",
      ),
    ).toBeTruthy();
  });

  it("saves a named checkpoint and restores one behind a confirm", async () => {
    const saved = cp({
      id: "00000000-0000-4000-8000-00000000c00a",
      name: "Before restoring “Before the beach trip”",
    });
    const s = serve(
      UP,
      pageRoutes((path, init) => {
        if (path.endsWith("/checkpoints") && init?.method === "POST") {
          return json(cp({ id: "00000000-0000-4000-8000-00000000c00b", name: "Picnic" }));
        }
        if (path.endsWith(`/checkpoints/${cp().id}/restore`)) {
          return json({ persona: detail(), checkpoint: saved });
        }
        return null;
      }),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: /^Checkpoints/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save a checkpoint" }));
    const naming = screen.getByRole("dialog", { name: "Save a checkpoint" });
    const save = within(naming).getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    fireEvent.change(within(naming).getByRole("textbox", { name: "Name it" }), {
      target: { value: "Picnic" },
    });
    fireEvent.click(save);
    expect(await screen.findByText("Saved “Picnic”.")).toBeTruthy();
    expect(s.calls.find((c) => c.path.endsWith("/checkpoints"))?.body).toEqual({ name: "Picnic" });
    expect(screen.queryByRole("dialog", { name: "Save a checkpoint" })).toBeNull();
    // Named and automatic ones sit together under their day; the automatic one says so.
    expect(
      screen.getByRole("button", { name: /^Before editing personality\s*, automatic/ }),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /^Before the beach trip/ }));
    fireEvent.click(screen.getByRole("button", { name: "Restore Sprout to this" }));
    const dlg = screen.getByRole("dialog", { name: "Restore “Before the beach trip”?" });
    expect(dlg).toHaveTextContent(
      /saved as a checkpoint first, so this can be undone\. It reloads on Pixel 6a\./,
    );
    expect(s.calls.some((c) => c.path.endsWith("/restore"))).toBe(false);
    fireEvent.click(within(dlg).getByRole("button", { name: "Restore" }));
    expect(
      await screen.findByText(
        "Sprout is back to “Before the beach trip”. “Before restoring “Before the beach trip”” keeps what it was.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy();
  });

  it("deletes a checkpoint behind a second tap, and every automatic one at once", async () => {
    const s = serve(
      UP,
      pageRoutes((path, init) => {
        if (path.endsWith("/checkpoints/automatic") && init?.method === "DELETE") {
          return json({ deleted: 1 });
        }
        if (path.endsWith(`/checkpoints/${cp().id}`) && init?.method === "DELETE") {
          return new Response(null, { status: 204 });
        }
        return null;
      }),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: /^Checkpoints/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Before the beach trip/ }));
    const sheet = screen.getByRole("dialog", { name: "Before the beach trip" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Delete checkpoint" }));
    expect(s.calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(
      within(sheet).getByRole("button", { name: "Tap again — this can't be undone" }),
    );
    expect(await screen.findByText("Deleted “Before the beach trip”.")).toBeTruthy();
    expect(s.calls.find((c) => c.method === "DELETE")?.path).toBe(
      `/api/growbot/personas/${detail().id}/checkpoints/${cp().id}`,
    );
    expect(screen.queryByRole("button", { name: /^Before the beach trip/ })).toBeNull();
    expect(screen.getByRole("tab", { name: /^Checkpoints/ })).toHaveTextContent("Checkpoints2");

    const sweep = screen.getByRole("button", { name: "Delete the 1 automatic one" });
    fireEvent.click(sweep);
    fireEvent.click(
      screen.getByRole("button", { name: "Tap again — deletes 1, keeps the named ones" }),
    );
    expect(
      await screen.findByText("Deleted 1 automatic checkpoint. The ones you named are kept."),
    ).toBeTruthy();
    expect(s.calls.some((c) => c.path.endsWith("/checkpoints/automatic"))).toBe(true);
  });

  it("swipes a row open for its rail, whose delete takes a second tap", async () => {
    const s = serve(
      UP,
      pageRoutes((path, init) =>
        path.endsWith(`/checkpoints/${cp().id}`) && init?.method === "DELETE"
          ? new Response(null, { status: 204 })
          : null,
      ),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: /^Checkpoints/ }));
    const tap = screen.getByRole("button", { name: /^Before the beach trip/ });
    const slide = tap.parentElement as HTMLElement;
    const swipe = (from: number, to: number) => {
      fireEvent.touchStart(slide, { touches: [{ clientX: from, clientY: 100 }] });
      fireEvent.touchMove(slide, { touches: [{ clientX: (from + to) / 2, clientY: 101 }] });
      fireEvent.touchMove(slide, { touches: [{ clientX: to, clientY: 101 }] });
      fireEvent.touchEnd(slide, { touches: [] });
    };
    swipe(300, 40);
    expect(screen.getByRole("button", { name: /restore/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new persona/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^delete$/ }));
    expect(s.calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "tap again to delete" }));
    expect(await screen.findByText("Deleted “Before the beach trip”.")).toBeTruthy();
    expect(s.calls.filter((c) => c.method === "DELETE")).toHaveLength(1);
  });

  it("starts a new persona from a checkpoint's personality", async () => {
    const s = serve(
      UP,
      pageRoutes((path, init) =>
        path === "/api/growbot/personas" && init?.method === "POST"
          ? json({
              ...detail(),
              id: BUDDY,
              name: "Wren",
              made_from: "Sprout — Before the beach trip",
            })
          : null,
      ),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: /^Checkpoints/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Before the beach trip/ }));
    fireEvent.click(screen.getByRole("button", { name: "New persona from it" }));
    const sheet = screen.getByRole("dialog", { name: "New persona" });
    expect(
      await within(sheet).findByRole("radio", { name: /Sprout — Before the beach trip/ }),
    ).toBeChecked();
    const make = within(sheet).getByRole("button", { name: "Make it" });
    expect(make).toBeDisabled();
    fireEvent.change(within(sheet).getByRole("textbox", { name: "Its name" }), {
      target: { value: "Wren" },
    });
    fireEvent.click(within(sheet).getByRole("button", { name: "Make Wren" }));
    await waitFor(() =>
      expect(
        s.calls.find((c) => c.path === "/api/growbot/personas" && c.method === "POST")?.body,
      ).toEqual({
        name: "Wren",
        seed: cp().id,
      }),
    );
    expect(await screen.findByText("Made Wren from Sprout — Before the beach trip.")).toBeTruthy();
  });

  it("picks a persona's face from Manage, without a soul write", async () => {
    const s = serve(
      UP,
      pageRoutes((path, init) => {
        if (path === `/api/growbot/personas/${SPROUT}` && init?.method === "PATCH") {
          return json({ persona: detail({ face: "robot" }), checkpoint: null });
        }
        return null;
      }),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: "Manage" }));
    const faces = screen.getByRole("group", { name: "Face" });
    expect(within(faces).getByRole("radio", { name: /^Lantern/ })).toHaveProperty("checked", true);
    fireEvent.click(within(faces).getByRole("radio", { name: /^Robot/ }));
    expect(
      await screen.findByText(
        "Sprout wears the robot face; it changes on Pixel 6a in a few seconds.",
      ),
    ).toBeTruthy();
    expect(s.calls.find((c) => c.method === "PATCH")?.body).toEqual({ face: "robot" });
    expect(within(faces).getByRole("radio", { name: /^Robot/ })).toHaveProperty("checked", true);
  });

  it("renames, duplicates and exports from Manage", async () => {
    const urls = { create: vi.fn(() => "blob:soul"), revoke: vi.fn() };
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL: urls.create, revokeObjectURL: urls.revoke }),
    );
    const s = serve(
      UP,
      pageRoutes((path, init) => {
        if (path === `/api/growbot/personas/${SPROUT}` && init?.method === "PATCH") {
          return json({ persona: detail({ name: "Sprouty" }), checkpoint: null });
        }
        if (path.endsWith("/duplicate"))
          return json({ ...detail(), id: BUDDY, name: "Sprout copy" });
        if (path.endsWith("/export")) {
          return new Response("{}", {
            headers: { "Content-Disposition": 'attachment; filename="sprout-soul.json"' },
          });
        }
        return null;
      }),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: "Manage" }));
    expect(screen.getByText(/^Made .* from a fresh creature\.$/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    const sheet = screen.getByRole("dialog", { name: "Rename Sprout" });
    expect(
      within(sheet).getByText(/It still calls itself Sprout until you tell it otherwise/),
    ).toBeTruthy();
    fireEvent.change(within(sheet).getByRole("textbox"), { target: { value: "Sprouty" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Rename" }));
    expect(await screen.findByText("Sprout is now Sprouty on this list.")).toBeTruthy();
    expect(s.calls.find((c) => c.method === "PATCH")?.body).toEqual({ name: "Sprouty" });

    fireEvent.click(screen.getByRole("button", { name: "Export soul file" }));
    expect(
      await screen.findByText(
        "Saved sprout-soul.json to Downloads. It has its memories but not its visual memories — those stay on the box.",
      ),
    ).toBeTruthy();
    expect(urls.create).toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Duplicate" }));
    expect(
      await screen.findByText(
        /^Made Sprout copy — its memories and visual memories, no checkpoints\./,
      ),
    ).toBeTruthy();
  });

  it("refuses a file that isn't a soul before anything changes", async () => {
    const s = serve(UP, pageRoutes());
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: "Manage" }));
    fireEvent.click(screen.getByRole("button", { name: "Import over it" }));
    const sheet = screen.getByRole("dialog", { name: "Import a soul file" });
    const file = new File(['{"hello": 1}'], "notes.json", { type: "application/json" });
    fireEvent.change(within(sheet).getByLabelText("Soul file"), { target: { files: [file] } });
    expect(await within(sheet).findByText(/That isn't a soul file/)).toBeTruthy();
    expect(within(sheet).getByText(/Nothing changed/)).toBeTruthy();
    expect(within(sheet).getByRole("button", { name: "Choose another file" })).toBeTruthy();
    expect(s.calls.some((c) => c.path.endsWith("/import"))).toBe(false);
  });

  it("imports a soul over it, with a checkpoint first", async () => {
    const s = serve(
      UP,
      pageRoutes((path) =>
        path === "/api/growbot/personas/import"
          ? json({ persona: detail(), checkpoint: cp({ name: "Before import" }) })
          : null,
      ),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: "Manage" }));
    fireEvent.click(screen.getByRole("button", { name: "Import over it" }));
    const sheet = screen.getByRole("dialog", { name: "Import a soul file" });
    const soul = { soul_format: 3, meta: { name: "Bolt" }, log: [{ t: 1, txt: "hi" }] };
    const file = new File([JSON.stringify(soul)], "bolt-soul.json", { type: "application/json" });
    fireEvent.change(within(sheet).getByLabelText("Soul file"), { target: { files: [file] } });
    await waitFor(() =>
      expect(sheet).toHaveTextContent(
        /Checked — bolt-soul\.json, \d+ KB, a grown creature with 1 diary line\./,
      ),
    );
    fireEvent.click(within(sheet).getByRole("button", { name: "Import over Sprout" }));
    expect(
      await screen.findByText(
        "Sprout is now the imported soul, and its visual memories are cleared. “Before import” keeps what it was, visual memories too.",
      ),
    ).toBeTruthy();
    expect(s.calls.find((c) => c.path.endsWith("/import"))?.body).toEqual({ soul, over: SPROUT });
  });

  it("resets and deletes only on a second tap, saying what goes", async () => {
    const s = serve(
      UP,
      pageRoutes((path, init) => {
        if (path.endsWith("/reset")) {
          return json({ persona: detail(), checkpoint: cp({ name: "Before reset" }) });
        }
        if (init?.method === "DELETE") return new Response(null, { status: 204 });
        return null;
      }),
    );
    render(<GrowbotScreen />);
    await openSprout();
    fireEvent.click(screen.getByRole("tab", { name: "Manage" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset to a fresh creature" }));
    expect(s.calls.some((c) => c.path.endsWith("/reset"))).toBe(false);
    fireEvent.click(
      screen.getByRole("button", {
        name: "Tap again — its visual memories go too; a checkpoint is saved first",
      }),
    );
    expect(
      await screen.findByText(
        /^Sprout is a fresh creature again, and its visual memories are cleared\. “Before reset” keeps everything it was, visual memories too\./,
      ),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Delete Sprout" }));
    const armed = screen.getByRole("button", {
      name: "Tap again — erases its memories and 3 checkpoints",
    });
    expect(s.calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(armed);
    expect(
      await screen.findByText(
        "Sprout is deleted, with its memories. Fish stands still until a persona connects. Pixel 6a shows that it's gone.",
      ),
    ).toBeTruthy();
    expect(s.calls.filter((c) => c.method === "DELETE").map((c) => c.path)).toEqual([
      `/api/growbot/personas/${SPROUT}`,
    ]);
  });
});

describe("GrowbotScreen a persona's visual memories", () => {
  function vm(over: Partial<GrowbotMemory> = {}): GrowbotMemory {
    return {
      id: "r1a2b3c4d",
      kind: "person",
      name: "my person",
      desc: "The one who picks it up when it falls.",
      saved_at: iso(30 * 864e5),
      views: 3,
      confirmed: "human",
      ...over,
    };
  }
  const SCENE = vm({ id: "r00000002", kind: "scene", name: "", desc: "", views: 1 });
  const MEMS = `/api/growbot/personas/${SPROUT}/memories`;

  /** The box's memories, which the routes read and delete from as the box would. */
  function memRoutes(box: { list: GrowbotMemory[] }, extra?: Route): Route {
    return (path, init) => {
      const got = extra?.(path, init);
      if (got) return got;
      const method = init?.method ?? "GET";
      if (path === `/api/growbot/personas/${SPROUT}` && method === "GET") {
        return json(detail({ memories: box.list.length }));
      }
      if (path === MEMS && method === "GET") return json({ memories: box.list });
      const del = path.startsWith(`${MEMS}/`) ? path.slice(MEMS.length + 1) : "";
      if (del && method === "DELETE") {
        if (!box.list.some((m) => m.id === del)) return new Response(null, { status: 404 });
        box.list = box.list.filter((m) => m.id !== del);
        return new Response(null, { status: 204 });
      }
      return null;
    };
  }
  const reads = (s: Served) => s.calls.filter((c) => c.path === MEMS && c.method === "GET");

  async function openMemories(n: number) {
    fireEvent.click(await screen.findByRole("button", { name: /^Sprout — soul/ }));
    await screen.findByText("Personality and settings");
    fireEvent.click(screen.getByRole("tab", { name: `Memories, ${n}` }));
  }

  it("reads them only when the tab opens, and shows each saved view with its label", async () => {
    const s = serve(UP, memRoutes({ list: [vm(), SCENE] }));
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /^Sprout — soul/ }));
    await screen.findByText("Personality and settings");
    expect(reads(s)).toHaveLength(0);
    fireEvent.click(screen.getByRole("tab", { name: "Memories, 2" }));
    const grid = await screen.findByRole("list", { name: "Saved views" });
    const items = within(grid).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("my person");
    // A memory with no name still reads as what it is.
    expect(items[1]).toHaveTextContent("A scene");
    const pic = within(items[0] as HTMLElement).getByRole("img", { name: "my person" });
    expect(pic).toHaveAttribute("src", `${MEMS}/r1a2b3c4d/views/0`);
    expect(pic).toHaveAttribute("loading", "lazy");
    expect(within(grid).getByRole("button", { name: "Delete A scene" })).toBeInTheDocument();
    expect(
      screen.getByText(/they name people, so they stay on the box\. Deleting one makes it forget/),
    ).toBeTruthy();
    expect(reads(s)).toHaveLength(1);

    // A picture that won't load leaves the kind's glyph in its place.
    fireEvent.error(pic);
    expect(within(items[0] as HTMLElement).queryByRole("img")).toBeNull();

    fireEvent.click(within(grid).getByRole("button", { name: "Look at my person" }));
    const sheet = screen.getByRole("dialog", { name: "my person" });
    expect(
      within(sheet)
        .getAllByRole("img")
        .map((i) => i.getAttribute("alt")),
    ).toEqual(["my person, view 1 of 3", "my person, view 2 of 3", "my person, view 3 of 3"]);
    expect(within(sheet).getByText("The one who picks it up when it falls.")).toBeTruthy();
    expect(within(sheet).getByText(/^A person · saved /)).toBeTruthy();
  });

  it("says so when it has none yet", async () => {
    serve(UP, memRoutes({ list: [] }));
    render(<GrowbotScreen />);
    await openMemories(0);
    expect(
      await screen.findByText(
        "No saved views yet. It saves one when it meets a person or a thing it wants to remember.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Saved views" })).toBeNull();
  });

  it("shows the box's refusal when they can't be read", async () => {
    serve(
      UP,
      memRoutes({ list: [] }, (path) =>
        path === MEMS ? json({ detail: "GrowBot isn't answering." }, 503) : null,
      ),
    );
    render(<GrowbotScreen />);
    await openMemories(0);
    expect(await screen.findByRole("alert")).toHaveTextContent("GrowBot isn't answering.");
  });

  it("forgets one only on a second tap, says so, and reads the list again", async () => {
    const box = { list: [vm(), SCENE] };
    const s = serve(UP, memRoutes(box));
    render(<GrowbotScreen />);
    await openMemories(2);
    const grid = await screen.findByRole("list", { name: "Saved views" });
    fireEvent.click(within(grid).getByRole("button", { name: "Delete my person" }));
    expect(s.calls.some((c) => c.method === "DELETE")).toBe(false);
    const armed = within(grid).getByRole("button", { name: "Tap again to forget my person" });
    expect(armed).toHaveTextContent("Tap again");
    fireEvent.click(armed);
    expect(
      await screen.findByText(
        "Sprout forgot “my person”. It forgets on Pixel 6a the next time it's idle.",
      ),
    ).toBeTruthy();
    expect(s.calls.filter((c) => c.method === "DELETE").map((c) => c.path)).toEqual([
      `${MEMS}/r1a2b3c4d`,
    ]);
    await waitFor(() => expect(reads(s)).toHaveLength(2));
    await waitFor(() => expect(within(grid).getAllByRole("listitem")).toHaveLength(1));
    expect(screen.getByRole("tab", { name: "Memories, 1" })).toBeInTheDocument();

    // From the sheet too, with the same two taps; the sheet closes with it.
    fireEvent.click(within(grid).getByRole("button", { name: "Look at A scene" }));
    const sheet = screen.getByRole("dialog", { name: "A scene" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Delete A scene" }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Tap again to forget A scene" }));
    expect(
      await screen.findByText(
        "Sprout forgot that scene. It forgets on Pixel 6a the next time it's idle.",
      ),
    ).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "A scene" })).toBeNull());
  });

  it("takes a memory already gone as forgotten, and says when one wasn't", async () => {
    const box = { list: [vm(), SCENE] };
    const s = serve(
      UP,
      memRoutes(box, (path, init) => {
        if (init?.method !== "DELETE") return null;
        if (path.endsWith("/r1a2b3c4d")) {
          // Forgotten meanwhile on the phone: the box no longer has it.
          box.list = box.list.filter((m) => m.id !== "r1a2b3c4d");
          return new Response(null, { status: 404 });
        }
        return json({ detail: "The box couldn't forget it just now." }, 500);
      }),
    );
    render(<GrowbotScreen />);
    await openMemories(2);
    const grid = await screen.findByRole("list", { name: "Saved views" });
    fireEvent.click(within(grid).getByRole("button", { name: "Delete my person" }));
    fireEvent.click(within(grid).getByRole("button", { name: "Tap again to forget my person" }));
    expect(await screen.findByText(/^Sprout forgot “my person”\./)).toBeTruthy();
    await waitFor(() => expect(reads(s)).toHaveLength(2));
    await waitFor(() => expect(within(grid).getAllByRole("listitem")).toHaveLength(1));

    fireEvent.click(within(grid).getByRole("button", { name: "Delete A scene" }));
    fireEvent.click(within(grid).getByRole("button", { name: "Tap again to forget A scene" }));
    expect(
      await screen.findByText(
        "Sprout didn't forget that scene — The box couldn't forget it just now.",
      ),
    ).toBeTruthy();
    expect(within(grid).getAllByRole("listitem")).toHaveLength(1);
  });

  it("reads them again after a restore or a reset, and says what those do to them", async () => {
    const box = { list: [vm(), SCENE] };
    const kept = cp({ memories: 1 });
    const s = serve(
      UP,
      memRoutes(box, (path, init) => {
        if (path === `/api/growbot/personas/${SPROUT}` && (init?.method ?? "GET") === "GET") {
          return json(
            detail({
              memories: box.list.length,
              checkpoint_list: [kept, cp({ id: "00000000-0000-4000-8000-00000000c003" })],
            }),
          );
        }
        if (path.endsWith(`/checkpoints/${kept.id}/restore`)) {
          box.list = [vm()];
          return json({
            persona: detail({ memories: 1, version: 2 }),
            checkpoint: cp({
              id: "00000000-0000-4000-8000-00000000c00d",
              name: "Before restoring",
            }),
          });
        }
        if (path.endsWith("/reset")) {
          box.list = [];
          return json({
            persona: detail({ memories: 0, version: 3 }),
            checkpoint: cp({ id: "00000000-0000-4000-8000-00000000c00e", name: "Before reset" }),
          });
        }
        if (path.endsWith("/checkpoints/00000000-0000-4000-8000-00000000c00e/restore")) {
          box.list = [vm()];
          return json({
            persona: detail({ memories: 1, version: 4 }),
            checkpoint: cp({ id: "00000000-0000-4000-8000-00000000c00f", name: "Before undo" }),
          });
        }
        return null;
      }),
    );
    render(<GrowbotScreen />);
    await openMemories(2);
    await screen.findByRole("list", { name: "Saved views" });

    fireEvent.click(screen.getByRole("tab", { name: /^Checkpoints/ }));
    // A checkpoint says how many it keeps, when it keeps any.
    expect(screen.getByText("1 vm")).toBeTruthy();
    // The first of the two same-named rows is the one that keeps a visual memory.
    fireEvent.click(
      screen.getAllByRole("button", { name: /^Before the beach trip/ })[0] as HTMLElement,
    );
    fireEvent.click(screen.getByRole("button", { name: "Restore Sprout to this" }));
    const dlg = screen.getByRole("dialog", { name: "Restore “Before the beach trip”?" });
    expect(dlg).toHaveTextContent(/gets back the visual memories it had then/);
    fireEvent.click(within(dlg).getByRole("button", { name: "Restore" }));
    expect(await screen.findByText(/^Sprout is back to “Before the beach trip”\./)).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Memories, 1" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Memories, 1" }));
    await waitFor(() =>
      expect(
        within(screen.getByRole("list", { name: "Saved views" })).getAllByRole("listitem"),
      ).toHaveLength(1),
    );

    fireEvent.click(screen.getByRole("tab", { name: "Manage" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset to a fresh creature" }));
    fireEvent.click(
      screen.getByRole("button", {
        name: "Tap again — its visual memories go too; a checkpoint is saved first",
      }),
    );
    expect(await screen.findByText(/its visual memories are cleared\./)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Memories, 0" }));
    expect(await screen.findByText(/^No saved views yet\./)).toBeTruthy();

    // Undo from the toast while the tab is open brings them back without leaving it.
    const before = reads(s).length;
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(reads(s).length).toBeGreaterThan(before));
    expect(await screen.findByRole("list", { name: "Saved views" })).toBeInTheDocument();
  });
});

describe("GrowbotScreen the body's key and flash", () => {
  function job(over: Partial<GrowbotFlashJob> = {}): GrowbotFlashJob {
    return {
      id: "job-1",
      mode: "rekey",
      state: "running",
      name: "Fish",
      code: "gb-e3b6d8",
      port: "/dev/ttyACM0",
      box_host: "192.168.1.20",
      phases: { load: "skip", copy: "run", join: "wait" },
      log: ["mpremote: writing the new key"],
      fail: null,
      started_at: iso(0),
      finished_at: null,
      ...over,
    };
  }

  it("revokes only on a second tap within 3 s, and disarms on its own", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const revoked = robot({ revoked_at: iso(0), connected: false });
    const s = serve(UP, (path) =>
      path === "/api/growbot/robots/gb-e3b6d8/revoke" ? json(revoked) : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    fireEvent.click(screen.getByRole("button", { name: "Revoke key" }));
    expect(screen.getByRole("button", { name: "Tap again to revoke" })).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(3100);
    });
    expect(screen.getByRole("button", { name: "Revoke key" })).toBeInTheDocument();
    expect(s.calls.some((c) => c.path.endsWith("/revoke"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Revoke key" }));
    fireEvent.click(screen.getByRole("button", { name: "Tap again to revoke" }));
    expect(await screen.findByRole("button", { name: "Reflash Fish" })).toBeInTheDocument();
    expect(
      screen.getByText(
        /Fish's key is revoked\. It disconnects now.* Sprout carries on alone on Pixel 6a\./,
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Fish's key is revoked — no persona can wear it until it's reflashed over USB.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Revoke key" })).toBeNull();
  });

  it("writes a new key on a second tap and watches it in a sheet", async () => {
    let current: GrowbotFlashJob | null = null;
    const s = serve(UP, (path, init) => {
      if (path === "/api/growbot/flash" && init?.method === "POST") {
        current = job();
        return json({ job: current }, 202);
      }
      if (path === "/api/growbot/flash") return json({ job: current });
      return null;
    });
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    fireEvent.click(screen.getByRole("button", { name: "New key" }));
    expect(s.calls.some((c) => c.method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Tap again for a new key" }));
    const sheet = await screen.findByRole("dialog", { name: "New key — Fish" });
    await waitFor(() =>
      expect(s.calls.find((c) => c.method === "POST")?.body).toEqual({
        mode: "rekey",
        target: "gb-e3b6d8",
      }),
    );
    expect(await within(sheet).findByText("Copy its firmware and Wi-Fi")).toBeInTheDocument();
    expect(within(sheet).queryByText("Load MicroPython")).toBeNull();
    expect(within(sheet).getByRole("button", { name: "Writing the new key…" })).toBeDisabled();
    // Closing mid-run leaves the way back on the Body tab.
    fireEvent.click(within(sheet).getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Flashing — tap to watch" }));
    expect(screen.getByRole("dialog", { name: "New key — Fish" })).toBeInTheDocument();
  });

  it("words a new key the box refused, rather than a stuck sheet", async () => {
    serve(UP, (path, init) =>
      path === "/api/growbot/flash" && init?.method === "POST"
        ? json(
            { detail: "Nothing Pico is on the box's USB — plug the robot in, then try again." },
            409,
          )
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    fireEvent.click(screen.getByRole("button", { name: "New key" }));
    fireEvent.click(screen.getByRole("button", { name: "Tap again for a new key" }));
    expect(await screen.findByText(/Nothing Pico is on the box's USB/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeEnabled();
  });

  it("opens Add a robot straight away when jpanel handed a Pico over", async () => {
    serve({ service: "up", connected: 0, robots: [] }, (path) =>
      path === "/api/growbot/flash/setup" ? json({ detail: "nope" }, 503) : null,
    );
    const consumed = vi.fn();
    render(<GrowbotScreen openAdd onOpenAddConsumed={consumed} />);
    expect(await screen.findByRole("dialog", { name: "Plug it into the box" })).toBeInTheDocument();
    expect(consumed).toHaveBeenCalled();
  });

  it("reopens a running flash from the Body tab on its flashing step", async () => {
    serve(
      { service: "up", connected: 0, robots: [] },
      (path) => (path === "/api/growbot/flash/setup" ? json({ detail: "x" }, 503) : null),
      job({
        mode: "flash",
        name: "Pip",
        code: null,
        phases: { load: "run", copy: "wait", join: "wait" },
      }),
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    fireEvent.click(await screen.findByRole("button", { name: "Flashing — tap to watch" }));
    const sheet = screen.getByRole("dialog", { name: "Flashing" });
    expect(within(sheet).getByText("Step 3 of 4")).toBeInTheDocument();
  });

  it("signs every phone out on a second tap, keeping each persona on the box", async () => {
    const s = serve(UP, (path) =>
      path === "/api/growbot/sessions/reset" ? new Response(null, { status: 204 }) : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Phones" }));
    fireEvent.click(screen.getByRole("button", { name: "Sign out every phone" }));
    expect(s.calls.some((c) => c.path === "/api/growbot/sessions/reset")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Tap again to sign every phone out" }));
    expect(
      await screen.findByText(
        "Every phone is signed out of GrowBot. Each persona is kept on the box — open one again from here.",
      ),
    ).toBeTruthy();
    expect(s.calls.filter((c) => c.path === "/api/growbot/sessions/reset")).toHaveLength(1);
  });
});

describe("GrowbotScreen after review", () => {
  const sproutPage: Route = (path, init) =>
    path === `/api/growbot/personas/${SPROUT}` && (init?.method ?? "GET") === "GET"
      ? json(detail())
      : null;
  const without = (id: string): GrowbotOverview => {
    const ov = overview();
    return {
      ...ov,
      personas: ov.personas.filter((p) => p.id !== id),
      body: ov.body.map((b) => (b.persona === id ? { ...b, persona: null } : b)),
    };
  };
  const launched = () => localStorage.getItem(LAUNCHED_KEY);

  it("forgets a launch here whose ticket couldn't be made", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    fakeTab();
    serve(UP, (path) =>
      path === "/api/growbot/ticket" ? json({ detail: "no LAN site" }, 409) : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, alone/ }));
    expect(launched()).toContain(SPROUT);
    await screen.findByText(/GrowBot didn't open/);
    expect(launched()).toBeNull();
  });

  it("forgets a blocked launch until its link is tapped here", async () => {
    localStorage.setItem(CERT_NOTED_KEY, "1");
    vi.spyOn(window, "open").mockReturnValue(null);
    serve(UP, ticketRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On this phone, alone/ }));
    const link = await screen.findByRole("link", { name: /tap here to open GrowBot/ });
    expect(launched()).toBeNull();
    link.addEventListener("click", (e) => e.preventDefault());
    fireEvent.click(link);
    expect(launched()).toContain(SPROUT);
  });

  it("forgets a launch here once that persona's code goes to another phone", async () => {
    localStorage.setItem(LAUNCHED_KEY, JSON.stringify({ persona: SPROUT, at: Date.now() }));
    const s = serve(UP, ticketRoute);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On another phone…/ }));
    await waitFor(() => expect(tickets(s)).toHaveLength(1));
    expect(launched()).toBeNull();
  });

  it("keeps a delete's own toast rather than a banner about the body it left", async () => {
    const s = serve(UP, (path, init) => {
      if (init?.method === "DELETE") {
        s.ov.current = without(SPROUT);
        return new Response(null, { status: 204 });
      }
      return sproutPage(path, init);
    });
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /^Sprout — soul/ }));
    fireEvent.click(await screen.findByRole("tab", { name: "Manage" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /^Tap again — erases/ }));
    expect(await screen.findByText(/^Sprout is deleted, with its memories\./)).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("on Pixel 6a · wearing Fish")).toBeNull());
    expect(screen.getByText(/^Sprout is deleted/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "OK" })).toBeNull();
    expect(screen.queryByText(/was deleted, so its page closed/)).toBeNull();
  });

  it("drops a poll that set out before a rider change and landed after it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.setItem(THIS_PHONE_KEY, ME);
    const moved = overview();
    moved.personas = moved.personas.map((p) =>
      p.id === BOLT
        ? { ...p, wearing: "gb-e3b6d8" }
        : p.id === SPROUT
          ? { ...p, wearing: null }
          : p,
    );
    moved.body = [{ code: "gb-e3b6d8", persona: BOLT, since: iso(0) }];
    let reads = 0;
    let release: (r: Response) => void = () => {};
    serve(UP, (path, init) => {
      if (path === "/api/growbot/body/gb-e3b6d8" && init?.method === "PUT") return json(moved);
      if (path === "/api/growbot/personas" && (init?.method ?? "GET") === "GET") {
        reads += 1;
        if (reads === 2) {
          return new Promise<Response>((done) => {
            release = done;
          });
        }
        return json(reads > 2 ? moved : overview());
      }
      return null;
    });
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    await act(async () => {
      vi.advanceTimersByTime(15_100);
    });
    await waitFor(() => expect(reads).toBe(2));
    fireEvent.click(screen.getByRole("radio", { name: /Captain Bolt/ }));
    expect(await screen.findByText(/^Captain Bolt is wearing Fish on this phone\./)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "OK" }));
    await act(async () => {
      release(json(overview()));
      await Promise.resolve();
    });
    expect(screen.getByRole("radio", { name: /Captain Bolt/ })).toBeChecked();
    expect(screen.queryByText(/^Sprout is wearing Fish/)).toBeNull();
  });

  it("says a persona deleted elsewhere was deleted, and leaves its page", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const s = serve(UP, sproutPage);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /^Sprout — soul/ }));
    await screen.findByText("Personality and settings");
    s.ov.current = without(SPROUT);
    await act(async () => {
      vi.advanceTimersByTime(15_100);
    });
    expect(await screen.findByText("Sprout was deleted, so its page closed.")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Personas, 2" })).toBeInTheDocument();
  });

  it("names an older soul file as one, in the box's words", async () => {
    serve(UP);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    const sheet = screen.getByRole("dialog", { name: "Import a soul file" });
    const file = new File(['{"soul_format": 2, "identity": "I am"}'], "old.json");
    fireEvent.change(within(sheet).getByLabelText("Soul file"), { target: { files: [file] } });
    await waitFor(() =>
      expect(sheet).toHaveTextContent(
        "That soul file is an older format. Open it in the creature's MIND tab and export it again. Nothing changed.",
      ),
    );
  });

  it("can choose another file after a good one, and takes no name that isn't text", async () => {
    serve(UP);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    const sheet = screen.getByRole("dialog", { name: "Import a soul file" });
    const file = new File([JSON.stringify({ soul_format: 3, meta: { name: 5 } })], "x.json");
    fireEvent.change(within(sheet).getByLabelText("Soul file"), { target: { files: [file] } });
    expect(await within(sheet).findByRole("button", { name: "Import" })).toBeEnabled();
    expect(within(sheet).getByRole("textbox", { name: "Its name" })).toHaveValue("");
    expect(within(sheet).getByRole("button", { name: "Choose another file" })).toBeEnabled();
  });

  it("opens a name and description editor on a soul whose meta isn't text", async () => {
    serve(UP, (path, init) =>
      path === `/api/growbot/personas/${SPROUT}` && (init?.method ?? "GET") === "GET"
        ? json(detail({ soul: { soul_format: 3, meta: { name: 7 as unknown as string } } }))
        : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /^Sprout — soul/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^Name and description/ }));
    const editor = screen.getByRole("dialog", { name: "Name and description — Sprout" });
    expect(within(editor).getByRole("textbox", { name: "Name in the soul file" })).toHaveValue(
      "Sprout",
    );
  });

  it("follows the tabs pattern: a panel per strip, one tab in the Tab order, arrows move", async () => {
    serve(UP, sproutPage);
    render(<GrowbotScreen />);
    const personas = await screen.findByRole("tab", { name: "Personas, 3" });
    expect(personas).toHaveAttribute("aria-controls", "gb-panel");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", personas.id);
    expect(personas).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "Body" })).toHaveAttribute("tabindex", "-1");
    personas.focus();
    fireEvent.keyDown(personas, { key: "ArrowRight" });
    const body = screen.getByRole("tab", { name: "Body" });
    expect(body).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(body);
    fireEvent.keyDown(body, { key: "End" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Phones" }));
    fireEvent.keyDown(document.activeElement as Element, { key: "Home" });
    expect(screen.getByRole("tab", { name: "Personas, 3" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("moves focus to the page's name on the way in, and back to its row on the way out", async () => {
    serve(UP, sproutPage);
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: /^Sprout — soul/ }));
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: "Back to personas" }));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: /^Sprout — soul/ })),
    );
  });

  it("stops minting codes for a persona the box no longer has, and says so", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const s = serve(UP, (path) =>
      path === "/api/growbot/ticket" ? json({ detail: "no such persona" }, 404) : null,
    );
    render(<GrowbotScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Sprout" }));
    fireEvent.click(screen.getByRole("button", { name: /On another phone…/ }));
    expect(
      await screen.findByText(
        "Sprout was deleted, so there's no code for it. Pick another persona above.",
      ),
    ).toBeTruthy();
    await act(async () => {
      vi.advanceTimersByTime(200_100);
    });
    expect(tickets(s)).toHaveLength(1);
  });
});
