import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { type ReportDetail, api } from "../api/client";
import { ResearchShareApp } from "./ResearchShareApp";

// The app reads the live location; pin the share token so the boot path runs.
const share = vi.hoisted(() => ({ token: "tok" as string | null }));
vi.mock("../research/share", async (orig) => ({
  ...(await orig<typeof import("../research/share")>()),
  parseResearchSharePath: () => share.token,
}));

// The report renderer is heavy (Markdown/KaTeX); stub it to assert wiring only.
vi.mock("./ResearchDetailScreen", () => ({
  ReportDetailBody: ({ report }: { report: { id: string } }) => (
    <div data-testid="report">{report.id}</div>
  ),
}));

const REPORT = {
  id: "r1",
  question: "Q",
  report_md: "body",
  sources: [],
} as unknown as ReportDetail;

describe("ResearchShareApp", () => {
  beforeEach(() => {
    share.token = "tok";
  });

  it("renders a single-report share", async () => {
    vi.spyOn(api, "researchShareView").mockResolvedValue({
      kind: "report",
      label: null,
      report: REPORT,
      reports: null,
    });
    render(<ResearchShareApp />);
    expect(await screen.findByTestId("report")).toHaveTextContent("r1");
  });

  it("renders a folder index and opens a member on tap", async () => {
    vi.spyOn(api, "researchShareView").mockResolvedValue({
      kind: "group",
      label: "Medical",
      report: null,
      reports: [{ id: "r1", question: "First", title: null, created_at: null }],
    });
    const fetchReport = vi.spyOn(api, "researchShareReport").mockResolvedValue(REPORT);
    render(<ResearchShareApp />);
    fireEvent.click(await screen.findByText("First"));
    expect(await screen.findByTestId("report")).toHaveTextContent("r1");
    expect(fetchReport).toHaveBeenCalledWith("tok", "r1");
  });

  it("keeps the folder index when a member fetch fails", async () => {
    vi.spyOn(api, "researchShareView").mockResolvedValue({
      kind: "group",
      label: "Medical",
      report: null,
      reports: [{ id: "r1", question: "First", title: null, created_at: null }],
    });
    vi.spyOn(api, "researchShareReport").mockRejectedValue(new Error("boom"));
    render(<ResearchShareApp />);
    fireEvent.click(await screen.findByText("First"));
    // The index survives (the card is still there) and a notice appears — not a dead-end error.
    expect(await screen.findByText(/couldn't open that report/i)).toBeInTheDocument();
    expect(screen.getByText("First")).toBeInTheDocument();
  });

  it("shows an invalid/revoked message when the token doesn't resolve", async () => {
    vi.spyOn(api, "researchShareView").mockRejectedValue(new Error("404"));
    render(<ResearchShareApp />);
    expect(await screen.findByText(/invalid or has been revoked/i)).toBeInTheDocument();
  });
});
