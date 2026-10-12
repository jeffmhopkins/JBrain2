import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReportPlayer, type ReportReadAloud } from "./ReportPlayer";
import { reportSections } from "./reportSections";

const MD = "## Headlines\nCongress passed a bill.\n## Space\nA launch is set.\n## Weather\nSunny.";
const MDS = reportSections(MD).map((s) => s.md);

function ra(over: Partial<ReportReadAloud> = {}): ReportReadAloud {
  return { playing: null, onToggle: vi.fn(), playSections: vi.fn(), progress: null, ...over };
}

describe("ReportPlayer", () => {
  it("starts folded to one big Play button that plays from the first story", () => {
    const readAloud = ra();
    render(<ReportPlayer playKey="k" reportMd={MD} readAloud={readAloud} />);
    expect(screen.queryByRole("button", { name: "Next story" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Play brief/ }));
    expect(readAloud.playSections).toHaveBeenCalledWith("k", MDS, 0);
  });

  it("shows the current story and skips forward a whole story", () => {
    const readAloud = ra({ playing: "k", progress: { key: "k", section: 1, fraction: 0.5 } });
    render(<ReportPlayer playKey="k" reportMd={MD} readAloud={readAloud} />);
    expect(screen.getByText("Story 2 of 3")).toBeInTheDocument();
    expect(screen.getByText("Space")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next story" }));
    expect(readAloud.playSections).toHaveBeenLastCalledWith("k", MDS, 2);
  });

  it("goes back a story right after one starts, and jumps by tapping a segment", () => {
    const readAloud = ra({ playing: "k", progress: { key: "k", section: 1, fraction: 0 } });
    render(<ReportPlayer playKey="k" reportMd={MD} readAloud={readAloud} />);
    fireEvent.click(screen.getByRole("button", { name: "Prev story" }));
    expect(readAloud.playSections).toHaveBeenLastCalledWith("k", MDS, 0);
    fireEvent.click(screen.getByRole("button", { name: "Play story 3: Weather" }));
    expect(readAloud.playSections).toHaveBeenLastCalledWith("k", MDS, 2);
  });

  it("restarts the current story when Prev is tapped well into it", () => {
    vi.useFakeTimers();
    try {
      const readAloud = ra({ playing: "k", progress: { key: "k", section: 1, fraction: 0.2 } });
      render(<ReportPlayer playKey="k" reportMd={MD} readAloud={readAloud} />);
      vi.advanceTimersByTime(5000);
      fireEvent.click(screen.getByRole("button", { name: "Prev story" }));
      expect(readAloud.playSections).toHaveBeenLastCalledWith("k", MDS, 1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("pauses through the keyed toggle and resumes the same story", () => {
    const readAloud = ra({ playing: "k", progress: { key: "k", section: 1, fraction: 0.5 } });
    const { rerender } = render(<ReportPlayer playKey="k" reportMd={MD} readAloud={readAloud} />);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(readAloud.onToggle).toHaveBeenCalledWith("k", "");

    const paused = { ...readAloud, playing: null };
    rerender(<ReportPlayer playKey="k" reportMd={MD} readAloud={paused} />);
    expect(screen.getByText("Story 2 of 3 · paused")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(readAloud.playSections).toHaveBeenLastCalledWith("k", MDS, 1);
  });

  it("disables Next on the last story and folds back once the brief has finished", () => {
    const playing = ra({ playing: "k", progress: { key: "k", section: 2, fraction: 0 } });
    const { rerender } = render(<ReportPlayer playKey="k" reportMd={MD} readAloud={playing} />);
    expect(screen.getByRole("button", { name: "Next story" })).toBeDisabled();
    rerender(
      <ReportPlayer
        playKey="k"
        reportMd={MD}
        readAloud={ra({ progress: { key: "k", section: 2, fraction: 1 } })}
      />,
    );
    expect(screen.getByRole("button", { name: /Play brief/ })).toBeInTheDocument();
  });

  it("Stop silences it and folds the player away", () => {
    const readAloud = ra({ playing: "k", progress: { key: "k", section: 0, fraction: 0 } });
    const { rerender } = render(<ReportPlayer playKey="k" reportMd={MD} readAloud={readAloud} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(readAloud.onToggle).toHaveBeenCalledWith("k", "");
    rerender(
      <ReportPlayer playKey="k" reportMd={MD} readAloud={{ ...readAloud, playing: null }} />,
    );
    expect(screen.getByRole("button", { name: /Play brief/ })).toBeInTheDocument();
  });

  it("ignores another key's progress", () => {
    render(
      <ReportPlayer
        playKey="k"
        reportMd={MD}
        readAloud={ra({ playing: "other", progress: { key: "other", section: 2, fraction: 0.5 } })}
      />,
    );
    expect(screen.getByRole("button", { name: /Play brief/ })).toBeInTheDocument();
  });

  it("renders nothing for a report with nothing to speak", () => {
    const { container } = render(
      <ReportPlayer playKey="k" reportMd="## Sources\n1. x" readAloud={ra()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
