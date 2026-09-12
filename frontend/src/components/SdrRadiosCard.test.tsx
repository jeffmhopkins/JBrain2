// Settings → Radios, and the two fields the owner asked for: tuner gain, and an
// upconverter's offset. Binding spec: docs/mocks/radio-settings/a-two-more-fields.html.
//
// What is worth testing here is not that a control renders. It is that the card cannot
// tell the owner something the radio will not do: that UNSET stays unset rather than
// reading as 0 dB (a real setting, and the deaf one), that turning the converter on
// stores an offset in Hz rather than MHz, and that the note beside each control changes
// with the choice — because that note is the only place the consequences of the setting
// are stated at all.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { SdrRadio } from "../sdrRadios";
import { SdrRadiosCard } from "./SdrRadiosCard";

const WIRE = "77192819";

function radio(over: Partial<SdrRadio> = {}): SdrRadio {
  return {
    serial: WIRE,
    name: "Long wire",
    description: "9:1 unun, inline LNA",
    role: "general",
    attached: true,
    gain: "",
    upconverter_hz: 0,
    ...over,
  };
}

function served(over: Partial<SdrRadio> = {}) {
  return { radios: [radio(over)], conflicts: {}, scan_ok: true };
}

beforeEach(() => {
  vi.spyOn(api, "getSdrRadios").mockResolvedValue(served() as never);
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function shown(over: Partial<SdrRadio> = {}) {
  vi.spyOn(api, "getSdrRadios").mockResolvedValue(served(over) as never);
  const describe_ = vi.spyOn(api, "describeSdrRadio").mockResolvedValue(served(over) as never);
  render(<SdrRadiosCard />);
  await screen.findByRole("group", { name: /gain/i });
  return describe_;
}

describe("gain", () => {
  it("offers the measured rungs and auto, and nothing between them", async () => {
    // Nothing between them was MEASURED. 0/10/20/30/40 gave 20.2/40.5/41.4/39.7/32.8 dB
    // SNR at 162.550 through the fixed listen chain; a slider would imply readings
    // nobody took and would make 0 look like the safe end of a range.
    await shown();

    const group = screen.getByRole("group", { name: /gain/i });
    expect([...group.querySelectorAll("button")].map((b) => b.textContent)).toEqual([
      "0 dB",
      "10",
      "20",
      "30",
      "40",
      "Auto",
    ]);
  });

  it("starts with nothing chosen, and says what unset does", async () => {
    // UNSET IS A STATE, and it is the one every box has been in. Drawing it as 0 dB
    // would report a choice nobody made — and the one rung the measurements call deaf.
    await shown();

    for (const button of screen.getByRole("group", { name: /gain/i }).querySelectorAll("button")) {
      expect(button.getAttribute("aria-pressed")).toBe("false");
    }
    expect(screen.getByText(/Unset — listening keeps the radio's own loop/)).toBeInTheDocument();
  });

  it("saves the chosen rung against the serial", async () => {
    const describe_ = await shown();

    fireEvent.click(screen.getByRole("button", { name: "20" }));
    fireEvent.click(screen.getByRole("button", { name: /^Save/ }));

    await waitFor(() => expect(describe_).toHaveBeenCalled());
    expect(describe_.mock.calls[0]?.[1]).toMatchObject({ gain: "20", upconverter_hz: 0 });
  });

  it("lets the chosen rung be tapped off again, back to unset", async () => {
    // Without this the control could reach every value except the one it started at,
    // and a radio could never be put back to what the box did before this shipped.
    await shown({ gain: "30" });

    fireEvent.click(screen.getByRole("button", { name: "30" }));

    expect(screen.getByText(/Unset — listening keeps/)).toBeInTheDocument();
  });

  it("says auto is worse, in place", async () => {
    await shown();

    fireEvent.click(screen.getByRole("button", { name: "Auto" }));

    expect(screen.getByText(/Auto moves the gain while you watch/)).toBeInTheDocument();
    expect(screen.getByText(/162\.35 that does not exist/)).toBeInTheDocument();
  });

  it("says a gain cannot apply below 24 MHz with no converter, and is stored anyway", async () => {
    // The recurring failure in this subsystem is a gain that reads back as a number no
    // signal passed through. Direct sampling powers the tuner down; `-g` there writes
    // to a chip that is not listening.
    await shown({ gain: "20" });

    expect(screen.getByText(/the tuner is powered down/)).toBeInTheDocument();
    expect(screen.getByText(/stored and applies the moment it can/)).toBeInTheDocument();
  });
});

describe("the upconverter", () => {
  it("is off by default and says the dongle sees the antenna", async () => {
    await shown();

    expect(screen.getByText(/Off — the dongle sees the antenna directly/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/offset in megahertz/i)).not.toBeInTheDocument();
  });

  it("offers the Ham It Up's number as a starting point, editable", async () => {
    // 125 MHz is THIS unit's number, not every unit's, which is the whole reason the
    // field exists rather than a constant.
    await shown();

    fireEvent.click(screen.getByRole("button", { name: "Inline" }));

    expect(screen.getByLabelText(/offset in megahertz/i)).toHaveValue(125);
  });

  it("saves the offset in Hz, and states the shift in the owner's direction", async () => {
    const describe_ = await shown();

    fireEvent.click(screen.getByRole("button", { name: "Inline" }));
    // The arithmetic is worked in the direction the owner feels it: they ask for a
    // frequency and the dongle is told a higher one.
    expect(screen.getByText(/7\.200 MHz is heard at 132\.200/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Save/ }));

    await waitFor(() => expect(describe_).toHaveBeenCalled());
    expect(describe_.mock.calls[0]?.[1]).toMatchObject({ upconverter_hz: 125_000_000 });
  });

  it("promises that every frequency shown stays the owner's", async () => {
    // The single highest-risk property of the whole feature, said where the owner turns
    // it on: one layer that forgot to leave the offset off would label a picture 125 MHz
    // wrong, and nothing in the picture would look unusual.
    await shown({ upconverter_hz: 125_000_000 });

    expect(screen.getByText(/stays the real one/)).toBeInTheDocument();
  });

  it("warns about VHF and names the edge the owner gave for it", async () => {
    await shown({ upconverter_hz: 125_000_000 });

    const note = screen.getByText(/passes HF only/);
    expect(note).toBeInTheDocument();
    expect(note.textContent).toMatch(/300 Hz to 65 MHz/);
  });

  it("takes the direct-sampling caveat off the gain note once a converter is inline", async () => {
    // Because it is no longer true: with a converter the dongle tunes above 24 MHz, the
    // R820T2 is back in circuit, and gain applies on HF.
    await shown({ gain: "20", upconverter_hz: 125_000_000 });

    expect(screen.queryByText(/the tuner is powered down/)).not.toBeInTheDocument();
  });
});
