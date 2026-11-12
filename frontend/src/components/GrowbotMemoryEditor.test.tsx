import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type GrowbotPersona, type GrowbotWritten, api } from "../api/client";
import { GrowbotMemoryEditor } from "./GrowbotMemoryEditor";

const p = { id: "p1", name: "Sprout", version: 3 } as GrowbotPersona;
const soul = { soul_format: 3, log: [{ t: 1_700_000_000_000, txt: "Saw the mug." }] };

describe("GrowbotMemoryEditor", () => {
  afterEach(() => vi.restoreAllMocks());

  it("saves against the version it opened on, though the screen's poll re-read the persona", async () => {
    const put = vi
      .spyOn(api, "editGrowbotRegion")
      .mockResolvedValue({ persona: p, checkpoint: null } as unknown as GrowbotWritten);
    const props = {
      p,
      soul,
      which: "log" as const,
      openOn: null,
      onClose: () => {},
      onSaved: () => {},
    };
    const { rerender } = render(<GrowbotMemoryEditor {...props} version={3} />);
    rerender(<GrowbotMemoryEditor {...props} version={4} />);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalled());
    expect(put).toHaveBeenCalledWith("p1", "log", [soul.log[0]], 3);
  });

  it("leaves out a diary line whose time a Date cannot hold", () => {
    render(
      <GrowbotMemoryEditor
        p={p}
        soul={{ log: [{ t: 1e300, txt: "far future" }] }}
        version={3}
        which="log"
        openOn={null}
        onClose={() => {}}
        onSaved={() => {}}
      />,
    );
    expect(screen.getByText("No diary lines yet.")).toBeTruthy();
  });
});
