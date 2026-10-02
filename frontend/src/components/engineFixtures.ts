// Shared EngineState fixtures for the engine-switch tests (card, banner, store).

import type { EngineState, EngineSwitchStatus } from "../api/client";

export function engineState(over: Partial<EngineState> = {}): EngineState {
  return {
    desired: "standard",
    effective: "standard",
    services: {
      standard: { service: "local-llm", state: "running" },
      "flash-next": { service: "flash-next", state: "exited" },
    },
    running: ["standard"],
    consistent: true,
    installed: { standard: true, "flash-next": true },
    oneshot: null,
    perplexity_running: false,
    switching: false,
    admission: { closed: false, reason: null, until: null },
    memory: {
      gtt_used_gb: 81.9,
      gtt_total_gb: 120,
      gtt_free_gb: 38.1,
      host_total_gb: 121.2,
      host_used_gb: 94.9,
    },
    guard: null,
    switch: null,
    ...over,
  };
}

export function switchStatus(over: Partial<EngineSwitchStatus> = {}): EngineSwitchStatus {
  return {
    id: "sw-1",
    source: "owner",
    target: "flash-next",
    previous: "standard",
    force: false,
    stage: "stopping",
    reason: null,
    started_at: "2026-10-02T09:41:05Z",
    updated_at: "2026-10-02T09:41:20Z",
    ended_at: null,
    stages: [
      { stage: "draining", at: "2026-10-02T09:41:05Z" },
      { stage: "stopping", at: "2026-10-02T09:41:14Z" },
    ],
    model: null,
    smoke: [],
    notes: [],
    ...over,
  };
}
