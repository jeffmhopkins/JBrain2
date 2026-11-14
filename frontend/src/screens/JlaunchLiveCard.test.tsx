import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import type { JlaunchJob } from "../jlaunch/types";
import { LiveCard } from "./JlaunchScreen";

const JOB: JlaunchJob = {
  id: "j1",
  spec_name: "growbot_heading_walk",
  title: "GrowBot heading walk",
  repo: "https://github.com/jeffmhopkins/GrowBot_Training",
  state: "running",
  phase: "train",
  artifact_name: "growbot_heading_walk_artifacts.tar.gz",
  started_at: "2026-11-14T00:00:00+00:00",
  finished_at: null,
  verify_ok: null,
  headline: [],
  machine: {},
  phase_times: [],
  error: null,
  artifact_present: false,
  artifact_bytes: null,
  console_tail: "",
};

const FILES = [
  { name: "progress.json", bytes: 900, modified_at: "2026-11-14T01:00:00+00:00" },
  { name: "replay.html", bytes: 400000, modified_at: "2026-11-14T01:00:00+00:00" },
  { name: "policy_latest.json", bytes: 30000, modified_at: "2026-11-14T01:00:00+00:00" },
];

afterEach(() => vi.restoreAllMocks());

describe("LiveCard", () => {
  it("shows progress, the replay link and downloads for the rest", async () => {
    vi.spyOn(api, "jlaunchLiveFiles").mockResolvedValue(FILES);
    vi.spyOn(api, "jlaunchProgress").mockResolvedValue({
      title: "Training heading_walk",
      percent: 42.4,
      eta_s: 4800,
      summary: [{ label: "Heading error", value: "54°" }],
    });

    render(<LiveCard job={JOB} />);

    await waitFor(() => expect(screen.getByText("42%")).toBeInTheDocument());
    expect(screen.getByText("about 1 h 20 min left")).toBeInTheDocument();
    expect(screen.getByText("54°")).toBeInTheDocument();
    const replay = screen.getByRole("link", { name: /Watch the latest walk/ });
    expect(replay).toHaveAttribute("href", "/api/jlaunch/jobs/j1/live/replay.html");
    expect(replay).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "/api/jlaunch/jobs/j1/live/policy_latest.json?download=1",
    );
  });

  it("renders nothing for a job without a live folder", async () => {
    const spy = vi
      .spyOn(api, "jlaunchLiveFiles")
      .mockRejectedValue(new ApiError(404, "this job has no live folder"));
    const { container } = render(<LiveCard job={{ ...JOB, spec_name: "erdos_straus_1e12" }} />);
    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
