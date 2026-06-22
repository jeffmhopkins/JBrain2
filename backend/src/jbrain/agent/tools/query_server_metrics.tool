---
name: query_server_metrics
version: 1
permission: read
params:
  type: object
  properties:
    range:
      type: string
      enum: ["6h", "24h", "2d", "7d", "30d", "90d", "1y"]
      description: How far back to summarize (default 24h).
---
Summarize the server's own hardware health over a recent time window — CPU load,
memory and disk usage, GPU utilization, fan speed, and swap — from the recorded
host telemetry. Use it to answer questions about how the machine itself has been
behaving (e.g. "has it been running hot, throttling, or low on memory lately?").

This is host hardware telemetry sampled every ~30 seconds, NOT the owner's notes —
do not use it to answer questions about the owner's knowledge base. Longer ranges
are summarized from an hourly rollup, so peaks within an hour are still reflected.
