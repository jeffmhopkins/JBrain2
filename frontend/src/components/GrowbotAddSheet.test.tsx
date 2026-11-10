import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  GrowbotFlashJob,
  GrowbotFlashRequest,
  GrowbotFlashSetup,
  GrowbotRobot,
} from "../api/client";
import { FAIL_COPY } from "../growbot";
import { GrowbotAddSheet } from "./GrowbotAddSheet";

// Add a robot is one primary action per step, and every failure says what happened and the
// recovery (DESIGN.md "GrowBot — the desk robot's screen").

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setup(over: Partial<GrowbotFlashSetup> = {}): GrowbotFlashSetup {
  return {
    flasher: true,
    usb: [{ kind: "boot", port: null, label: "RP2350 bootloader" }],
    networks: [
      { ssid: "Home-2G", remembered: true },
      { ssid: "Workshop", remembered: true },
    ],
    box_host: "192.168.1.20",
    lan: true,
    ca: true,
    micropython: { version: "1.28.0", ready: true },
    firmware_ready: true,
    ...over,
  };
}

const QUORRA: GrowbotRobot = {
  code: "gb-3fa91c",
  name: "Quorra",
  created_at: "2026-11-01T09:00:00Z",
  rotated_at: null,
  revoked_at: null,
  last_seen_at: null,
  connected: false,
};

function job(over: Partial<GrowbotFlashJob> = {}): GrowbotFlashJob {
  return {
    id: "job-1",
    mode: "flash",
    state: "running",
    name: "Pip",
    code: null,
    port: null,
    box_host: "192.168.1.20",
    phases: { load: "run", copy: "wait", join: "wait" },
    log: ["picotool: loading MicroPython 1.28.0"],
    fail: null,
    started_at: "2026-11-10T09:00:00Z",
    finished_at: null,
    ...over,
  };
}

function serveSetup(s: GrowbotFlashSetup | Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input) === "/api/growbot/flash/setup"
        ? s instanceof Response
          ? s
          : json(s)
        : new Response(null, { status: 404 }),
    ),
  );
}

function sheet(props: Partial<Parameters<typeof GrowbotAddSheet>[0]> = {}) {
  const onStart = vi.fn(async (_b: GrowbotFlashRequest) => job());
  const all = {
    robots: [QUORRA],
    initialTarget: "new",
    job: null,
    onStart,
    onOpen: vi.fn(),
    onClose: vi.fn(),
    ...props,
  };
  const view = render(<GrowbotAddSheet {...all} />);
  return { ...all, ...view };
}

async function toWifi(name = "Pip") {
  fireEvent.change(await screen.findByLabelText("Its name"), { target: { value: name } });
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  return screen.findByRole("dialog", { name: "Wi-Fi it should join" });
}

describe("GrowbotAddSheet", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows what is on the box's USB and asks for a name before Next", async () => {
    serveSetup(setup());
    sheet();
    expect(await screen.findByText("RP2350 bootloader")).toBeInTheDocument();
    expect(screen.getByText("A new Pico — MicroPython goes on first.")).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 4")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByText(/Give it a name/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Its name"), { target: { value: "Pip" } });
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("says nothing is plugged in, and Rescan looks again", async () => {
    serveSetup(setup({ usb: [] }));
    sheet();
    expect(await screen.findByText(/Nothing on the box's USB/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Rescan USB" }));
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2));
  });

  it.each([
    [{ flasher: false }, /No flasher on this box/],
    [{ lan: false }, /needs the box's LAN site/],
    [{ ca: false }, /won't flash one that would dial in unverified/],
    [{ micropython: { version: "1.28.0", ready: false } }, /MicroPython 1.28.0 isn't on the box/],
    [{ firmware_ready: false }, /firmware isn't on the box yet/],
  ])("refuses up front, in a sentence, when the box can't flash (%o)", async (over, words) => {
    serveSetup(setup(over as Partial<GrowbotFlashSetup>));
    sheet();
    expect(await screen.findByText(words)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("shows the box's own refusal sentence when setup can't be read", async () => {
    serveSetup(json({ detail: "No flasher on this box — JBRAIN_ENDPOINT_URL is empty." }, 503));
    sheet();
    expect(await screen.findByText(/JBRAIN_ENDPOINT_URL is empty/)).toBeInTheDocument();
  });

  it("offers a reflash that keeps the code, and Reinstall only on a running Pico", async () => {
    serveSetup(
      setup({ usb: [{ kind: "micropython", port: "/dev/ttyACM0", label: "MicroPython" }] }),
    );
    const { onStart } = sheet({ initialTarget: QUORRA.code });
    expect(await screen.findByText("/dev/ttyACM0")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Reflash Quorra/ })).toBeChecked();
    expect(screen.queryByLabelText("Its name")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /Reinstall MicroPython too/ }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(await screen.findByRole("button", { name: "Flash GrowBot" }));
    await waitFor(() =>
      expect(onStart).toHaveBeenCalledWith({
        mode: "flash",
        target: "gb-3fa91c",
        networks: [{ ssid: "Home-2G" }],
        reinstall: true,
      }),
    );
  });

  it("sends the networks in order, a new one with its password and Remember", async () => {
    serveSetup(setup());
    const { onStart } = sheet();
    const wifi = await toWifi();
    expect(within(wifi).getByRole("checkbox", { name: /Home-2G/ })).toBeChecked();
    expect(within(wifi).getByRole("checkbox", { name: /Workshop/ })).not.toBeChecked();
    expect(within(wifi).getByText(/reaches the box at/)).toHaveTextContent("192.168.1.20");

    fireEvent.click(within(wifi).getByRole("button", { name: "Add another network" }));
    fireEvent.change(within(wifi).getByLabelText("Network name"), { target: { value: "Barn" } });
    fireEvent.change(within(wifi).getByLabelText("Password"), { target: { value: "short" } });
    expect(within(wifi).getByText(/8 to 63 ordinary characters/)).toBeInTheDocument();
    expect(within(wifi).getByRole("button", { name: "Add network" })).toBeDisabled();
    fireEvent.change(within(wifi).getByLabelText("Password"), {
      target: { value: "hunter22hunter" },
    });
    fireEvent.click(within(wifi).getByRole("button", { name: "Add network" }));
    expect(within(wifi).getByRole("checkbox", { name: /Barn/ })).toBeChecked();

    fireEvent.click(within(wifi).getByRole("button", { name: "Flash GrowBot" }));
    await waitFor(() =>
      expect(onStart).toHaveBeenCalledWith({
        mode: "flash",
        target: "new",
        name: "Pip",
        networks: [
          { ssid: "Home-2G" },
          { ssid: "Barn", password: "hunter22hunter", remember: true },
        ],
      }),
    );
    expect(await screen.findByRole("dialog", { name: "Flashing" })).toBeInTheDocument();
  });

  it("won't flash a robot with no network", async () => {
    serveSetup(setup());
    sheet();
    const wifi = await toWifi();
    fireEvent.click(within(wifi).getByRole("checkbox", { name: /Home-2G/ }));
    expect(within(wifi).getByText(/Pick at least one/)).toBeInTheDocument();
    expect(within(wifi).getByRole("button", { name: "Flash GrowBot" })).toBeDisabled();
  });

  it("asks for the box's address when the box can't tell its own", async () => {
    serveSetup(setup({ box_host: null }));
    const { onStart } = sheet();
    const wifi = await toWifi();
    const flash = within(wifi).getByRole("button", { name: "Flash GrowBot" });
    expect(flash).toBeDisabled();
    fireEvent.change(within(wifi).getByLabelText("The box's address on your network"), {
      target: { value: "192.168.1.9" },
    });
    fireEvent.click(flash);
    await waitFor(() =>
      expect(vi.mocked(onStart).mock.calls[0]?.[0]).toMatchObject({ box_host: "192.168.1.9" }),
    );
  });

  it("shows the box's refusal of a flash as its sentence and stays on Wi-Fi", async () => {
    serveSetup(setup());
    const onStart = vi.fn(async () => {
      const { ApiError } = await import("../api/client");
      throw new ApiError(422, 'The box doesn\'t remember "Workshop" — type its password.');
    });
    sheet({ onStart });
    const wifi = await toWifi();
    fireEvent.click(within(wifi).getByRole("button", { name: "Flash GrowBot" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/doesn't remember "Workshop"/);
    expect(screen.getByRole("dialog", { name: "Wi-Fi it should join" })).toBeInTheDocument();
  });

  it("runs three named phases with the log behind Show the log", async () => {
    serveSetup(setup());
    const { rerender, onStart, onOpen, onClose } = sheet();
    await toWifi();
    fireEvent.click(screen.getByRole("button", { name: "Flash GrowBot" }));
    const flashing = await screen.findByRole("dialog", { name: "Flashing" });
    const props = { robots: [QUORRA], initialTarget: "new", onStart, onOpen, onClose };
    rerender(
      <GrowbotAddSheet
        {...props}
        job={job({ phases: { load: "wait", copy: "wait", join: "wait" }, log: [] })}
      />,
    );
    const rows = within(flashing).getByRole("list", { name: "Flash progress" });
    expect(
      within(rows)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([
      "Load MicroPythonwaiting",
      "Copy its firmware and Wi-Fiwaiting",
      "Join Wi-Fi and reach the boxwaiting",
    ]);
    rerender(<GrowbotAddSheet {...props} job={job()} />);
    expect(within(flashing).getByText("running")).toBeInTheDocument();
    expect(within(flashing).queryByLabelText("Flash log")).toBeNull();
    fireEvent.click(within(flashing).getByRole("button", { name: "Show the log" }));
    expect(within(flashing).getByLabelText("Flash log")).toHaveTextContent("picotool");
    expect(within(flashing).getByRole("button", { name: "Flashing…" })).toBeDisabled();

    rerender(
      <GrowbotAddSheet
        {...props}
        job={job({
          state: "ok",
          code: "gb-7c20be",
          phases: { load: "done", copy: "done", join: "done" },
          finished_at: "2026-11-10T09:01:00Z",
        })}
      />,
    );
    const paired = await screen.findByRole("dialog", { name: "Pip is paired" });
    expect(within(paired).getByText("gb-7c20be")).toBeInTheDocument();
    fireEvent.click(within(paired).getByRole("button", { name: "Open GrowBot" }));
    expect(onOpen).toHaveBeenCalledWith("gb-7c20be");
  });

  it.each(Object.keys(FAIL_COPY))(
    "words a %s failure as what happened and the recovery, with the log open",
    async (code) => {
      serveSetup(setup());
      const { rerender, onStart, onOpen, onClose } = sheet();
      await toWifi();
      fireEvent.click(screen.getByRole("button", { name: "Flash GrowBot" }));
      await screen.findByRole("dialog", { name: "Flashing" });
      rerender(
        <GrowbotAddSheet
          robots={[QUORRA]}
          initialTarget="new"
          onStart={onStart}
          onOpen={onOpen}
          onClose={onClose}
          job={job({
            state: "fail",
            phases: { load: "done", copy: "done", join: "fail" },
            log: ["FAILED: something"],
            fail: { code: code as keyof typeof FAIL_COPY, message: "raw" },
          })}
        />,
      );
      const failed = await screen.findByRole("dialog", { name: "It didn't finish" });
      const copy = FAIL_COPY[code as keyof typeof FAIL_COPY];
      expect(within(failed).getByRole("alert")).toHaveTextContent(copy.title);
      expect(within(failed).getByRole("alert")).not.toHaveTextContent(/see the log/i);
      expect(within(failed).getByLabelText("Flash log")).toHaveTextContent("FAILED");
      expect(within(failed).getByRole("button", { name: "Try again" })).toBeEnabled();
      const back = code === "wrong_robot" ? "Back to which robot" : "Back to Wi-Fi";
      expect(within(failed).getByRole("button", { name: back })).toBeInTheDocument();
    },
  );

  it("tries again with the same request, never reinstalling twice", async () => {
    serveSetup(
      setup({ usb: [{ kind: "micropython", port: "/dev/ttyACM0", label: "MicroPython" }] }),
    );
    const { rerender, onStart, onOpen, onClose } = sheet({ initialTarget: QUORRA.code });
    fireEvent.click(await screen.findByRole("checkbox", { name: /Reinstall MicroPython too/ }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(await screen.findByRole("button", { name: "Flash GrowBot" }));
    await screen.findByRole("dialog", { name: "Flashing" });
    rerender(
      <GrowbotAddSheet
        robots={[QUORRA]}
        initialTarget={QUORRA.code}
        onStart={onStart}
        onOpen={onOpen}
        onClose={onClose}
        job={job({ state: "fail", fail: { code: "wifi_failed", message: "x" } })}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    await waitFor(() => expect(onStart).toHaveBeenCalledTimes(2));
    expect(vi.mocked(onStart).mock.calls[1]?.[0]).toEqual({
      mode: "flash",
      target: "gb-3fa91c",
      networks: [{ ssid: "Home-2G" }],
    });
  });
});
