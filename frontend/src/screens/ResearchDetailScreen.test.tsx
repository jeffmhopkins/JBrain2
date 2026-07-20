import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ReportDetail, type VideoDetail, api } from "../api/client";
import { ResearchDetailScreen } from "./ResearchDetailScreen";

const REPORT: ReportDetail = {
  id: "r1",
  question: "How was the 1918 flu toll estimated?",
  report_md: "## Summary\n\nEstimates range 17M to 100M.",
  complexity: "deep",
  rounds: 2,
  sub_agents: 6,
  analyzed: true,
  revised: false,
  coverage_limited: true,
  truncated: false,
  sources: [{ url: "https://example.org", title: "A" }],
  created_at: "2026-07-18T00:00:00Z",
};

const VIDEO: VideoDetail = {
  source_id: "src1",
  video_id: "v1",
  provider: "youtube",
  title: "Strix Halo deep research",
  channel_name: "Donato Capitella",
  url: "https://youtu.be/x",
  transcript_source: "captions",
  summary: "A local deep-research agent on Strix Halo.",
  duration_s: 1694,
  duration_ms: 1694000,
  published_at: "2026-07-17T00:00:00Z",
  windows: [{ t_ms: 0, text: "intro line" }],
  frames: [{ t_ms: 0, caption: "title" }],
  cued_transcript: null,
};

afterEach(() => vi.restoreAllMocks());

describe("ResearchDetailScreen", () => {
  it("renders a report's provenance strip and markdown body", async () => {
    vi.spyOn(api, "researchReport").mockResolvedValue(REPORT);
    render(<ResearchDetailScreen kind="report" id="r1" syncStatus="synced" onClose={() => {}} />);
    expect(await screen.findByText("How was the 1918 flu toll estimated?")).toBeInTheDocument();
    expect(screen.getByText("cross-checked")).toBeInTheDocument();
    expect(screen.getByText("coverage limited")).toBeInTheDocument();
    // The report_md renders through the shared Markdown path.
    expect(screen.getByText(/Estimates range 17M to 100M/)).toBeInTheDocument();
  });

  it("renders a video via the VideoAnalysis card", async () => {
    vi.spyOn(api, "researchVideo").mockResolvedValue(VIDEO);
    render(<ResearchDetailScreen kind="video" id="v1" syncStatus="synced" onClose={() => {}} />);
    expect(await screen.findByText("Strix Halo deep research")).toBeInTheDocument();
    expect(screen.getByText(/A local deep-research agent/)).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    vi.spyOn(api, "researchReport").mockRejectedValue(new Error("boom"));
    render(<ResearchDetailScreen kind="report" id="r1" syncStatus="synced" onClose={() => {}} />);
    expect(await screen.findByText(/Couldn't load this/)).toBeInTheDocument();
  });
});
