import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type CastNotice, type CastTv, api } from "../api/client";
import { CastScreen } from "./CastScreen";

const TVS: CastTv[] = [
  {
    id: "a",
    name: "Big TV",
    model: null,
    state: "idle",
    why: "Ambient",
    castable: true,
    reading: { app_id: null, display_name: null, connected: true },
  },
  {
    id: "b",
    name: "Parents",
    model: null,
    state: "standby",
    why: "off",
    castable: true,
    reading: { app_id: null, display_name: null, connected: true },
  },
  {
    id: "c",
    name: "Twins room",
    model: null,
    state: "busy",
    why: "Netflix · playing",
    castable: false,
    reading: { app_id: "Netflix", display_name: "Netflix", connected: true },
  },
];

function sent(over: Partial<CastNotice> = {}): CastNotice {
  return {
    id: "n1",
    sender: "owner",
    title: "Dinner's ready",
    body: "",
    seconds: 30,
    created_at: new Date().toISOString(),
    stopped_at: null,
    tvs: [
      { tv_id: "a", name: "Big TV", outcome: "shown", why: "Ambient" },
      { tv_id: "b", name: "Parents", outcome: "woke", why: "was off" },
    ],
    ...over,
  };
}

beforeEach(() => {
  vi.spyOn(api, "castTvs").mockResolvedValue({ tvs: TVS });
  vi.spyOn(api, "castPreview").mockResolvedValue(new Blob(["png"], { type: "image/png" }));
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }),
  );
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("Cast", () => {
  it("lists each TV with what it is doing, in words", async () => {
    render(<CastScreen />);
    expect(await screen.findByText("3 found on the network")).toBeInTheDocument();
    expect(screen.getByText("Big TV")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Off · will turn on")).toBeInTheDocument();
    expect(screen.getByText("In use")).toBeInTheDocument();
    expect(screen.getByText("Netflix · playing")).toBeInTheDocument();
  });

  it("says when the box can't reach its TV finder", async () => {
    vi.spyOn(api, "castTvs").mockRejectedValue(new ApiError(503, "no sidecar"));
    render(<CastScreen />);
    expect(
      await screen.findByText("The box can't reach its TV finder right now."),
    ).toBeInTheDocument();
  });

  it("sends to the TVs that would take it, then shows how each took it", async () => {
    const send = vi.spyOn(api, "castSend").mockResolvedValue({ sent: sent() });
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));

    // The ready and off TVs are picked to start with; the busy one is not.
    const picks = within(screen.getByRole("group", { name: "TVs" }));
    expect(picks.getByRole("button", { name: /Big TV/ })).toHaveAttribute("aria-pressed", "true");
    expect(picks.getByRole("button", { name: /Twins room/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    const cast = screen.getByRole("button", { name: /Cast to 2 TVs/ });
    expect(cast).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Dinner's ready" } });
    await waitFor(() => expect(api.castPreview).toHaveBeenCalled(), { timeout: 2000 });
    expect(await screen.findByAltText("What the TV will show")).toBeInTheDocument();

    // Picking a TV that is in use says so before sending.
    fireEvent.click(picks.getByRole("button", { name: /Twins room/ }));
    expect(
      screen.getByText("Twins room is netflix · playing and will be skipped."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 of 3 TVs/ }));

    await screen.findByText("Cast to 2 of 2");
    expect(send).toHaveBeenCalledWith({
      tv_ids: ["a", "b", "c"],
      title: "Dinner's ready",
      body: "",
      picture_b64: null,
      seconds: 30,
    });
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("On screen")).toBeInTheDocument();
    expect(sheet.getByText("Turned on")).toBeInTheDocument();
  });

  it("says a refusal in words", async () => {
    vi.spyOn(api, "castSend").mockResolvedValue({
      refused: { status: 415, reason: "bad_picture" },
    });
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Look" } });
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 TVs/ }));
    expect(await screen.findByText("That picture couldn't be read.")).toBeInTheDocument();
  });

  it("says when the box can't reach the TVs to send", async () => {
    vi.spyOn(api, "castSend").mockRejectedValue(new ApiError(503, "cast sidecar unreachable"));
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Look" } });
    fireEvent.click(screen.getByRole("button", { name: /Cast to 2 TVs/ }));
    expect(await screen.findByText("The box can't reach the TVs right now.")).toBeInTheDocument();
  });

  it("says when the preview couldn't be drawn", async () => {
    vi.spyOn(api, "castPreview").mockRejectedValue(new ApiError(415, "bad picture"));
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Look" } });
    expect(
      await screen.findByText("Couldn't draw the preview", undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
  });

  it("refuses a title too long for the screen before sending", async () => {
    render(<CastScreen />);
    await screen.findByText("3 found on the network");
    fireEvent.click(screen.getByRole("button", { name: /Send to TVs/ }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "x".repeat(121) } });
    expect(screen.getByText("That title is too long for the screen.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Cast to 2 TVs/ })).toBeDisabled();
  });

  it("lists what was sent and takes one down from its sheet", async () => {
    vi.spyOn(api, "castSent").mockResolvedValue({ notices: [sent()], kept_s: 604800 });
    const stop = vi
      .spyOn(api, "castStop")
      .mockResolvedValue({ id: "n1", stopped: { a: true, b: false } });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    fireEvent.click(await screen.findByRole("button", { name: /Dinner's ready/ }));
    expect(screen.getByText("Cast to 2 of 2")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Take it down" }));
    });
    expect(stop).toHaveBeenCalledWith("n1");
    expect(screen.getByText("Taken down on 1 TV.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Take it down" })).toBeNull();
  });

  it("says when nothing was cast this week", async () => {
    vi.spyOn(api, "castSent").mockResolvedValue({ notices: [], kept_s: 604800 });
    render(<CastScreen />);
    fireEvent.click(screen.getByRole("button", { name: "Sent" }));
    expect(await screen.findByText("Nothing cast this week.")).toBeInTheDocument();
  });
});
