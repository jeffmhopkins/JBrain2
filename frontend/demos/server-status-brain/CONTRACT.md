# Server-Brain status visualization — shared data contract

All variants implement the **same** public interface so a real backend can be
wired in later by swapping the simulator for a WebSocket/SSE/poll source. Do not
deviate from these field names or ranges.

## Public API (attach to `window.ServerBrain`)

```js
// Push a fresh stats snapshot. Variant reacts/animates toward it.
window.ServerBrain.update(stats: ServerStats): void

// Built-in fake-data source. ON by default so the file is alive on open.
window.ServerBrain.startSimulator(): void
window.ServerBrain.stopSimulator(): void

// External tool calls reaching IN from the web -> reach-out tendrils.
window.ServerBrain.webSearch(): void     // cyan probe fan, one returns a result
window.ServerBrain.webFetch(): void      // amber filament, sustained inward stream

// Demo helpers the on-screen controls call (so reactivity is visible):
window.ServerBrain.spikeGPU(): void     // force a GPU saturation burst
window.ServerBrain.burstLLM(): void      // force an LLM inference burst
window.ServerBrain.injectError(): void   // force an API error blip
```

## `ServerStats` shape (all numbers; fractions are 0..1)

```js
{
  ts: 1719772800000,          // epoch ms
  health: 'ok',               // 'ok' | 'warn' | 'crit'  -> overall mood/bloom tint
  gpu: {
    util:      0.0..1.0,      // utilization fraction  (HEADLINE signal)
    vram:      0.0..1.0,      // VRAM used fraction
    tempC:     30..95,
    powerW:    0..600,
    powerMaxW: 600
  },
  llm: {
    active:       true|false,
    model:        'qwen3-30b',
    tokensPerSec: 0..120,     // generation speed
    queue:        0..32,      // pending requests
    ctxUsed:      0.0..1.0    // context window fill
  },
  api: {
    reqPerSec: 0..50,
    p95Ms:     5..2000,
    errorRate: 0.0..1.0,
    inflight:  0..64
  },
  db: {
    qps:       0..300,        // queries/sec
    poolUsed:  0.0..1.0,      // connection pool saturation
    slowQueries: 0..20
  },
  net: {                      // box network throughput -> rim/edge aura
    inRate:  0.0..1.0,        // ingress (download) -> BLUE rim
    outRate: 0.0..1.0         // egress  (upload)   -> CORAL rim
  },
  disk: {
    readRate: 0.0..1.0        // disk read throughput -> VIOLET rim
  },
  events: [                   // external tool calls since the last push; drained
    { kind: 'web_search', ts: 1719772800000 },  // -> ServerBrain.webSearch()
    { kind: 'web_fetch',  ts: 1719772800000 }   // -> ServerBrain.webFetch()
  ]
}
```

## Reactivity expectations (the 40% "functional" half)

- **GPU util** is the headline: it should visibly drive the densest/brightest
  part of the animation (energy, bloom intensity, node firing rate).
- **LLM inference** bursts should read as waves/cascades of activity; tokens/sec
  ~ flow speed; an active model should feel "thinking."
- **API + DB** are the I/O periphery: request rate = inbound pulses; DB qps =
  steady background throughput; errors flash a warning accent (rose `#CF8A8F`).
- **net + disk** glow the outer-shell **rim aura** (subtle): net in = blue, net
  out = coral, disk read = violet, intensity ~ throughput.
- **web search / fetch** events draw **reach-out tendrils** from a peripheral
  neuron out to a distant point, then a packet returns inward and seeds a cascade
  (search = cyan fan of probes; fetch = single amber sustained inward stream).
- **health** tints the global bloom: ok = cool/steel-green, warn = amber, crit = rose.
- Correlated bursts are realistic: an LLM burst raises GPU util, temp, power.

## Palette (JBrain2 brand — dark-first)

```
--bg      #0E0F11   (app background, near-black)
--surface #17181B
--steel   #7FA7C9   (brand / info / healthy-cool)
--green   #8FBC9A   (healthy / success)
--amber   #C9A36A   (warning / pending)
--rose    #CF8A8F   (error / critical)
--teal    #6FB6B1
--violet  #A493C9
--text    #E6E7E9
--text-2  #9A9DA3
```

You may push the palette toward something more luminous/artistic for the glow —
but stay dark-mode and keep these hues recognizable as the signal accents.

## Build constraints

- **Single self-contained `.html` file.** Inline all CSS/JS. CDN imports for a
  3D lib (e.g. three.js) are acceptable; note the dependency in a comment.
- 60% artistic / 40% functional. It must look like the gorgeous neural-brain
  server animations people post on Twitter/X — dark, dense nodes, glowing bloom,
  slight 3D depth — AND it must legibly convey server status at a glance.
- Include a small, unobtrusive on-screen control strip (pause sim / spike GPU /
  burst LLM / inject error) and a compact legend mapping visuals → signals.
- 60fps target; degrade gracefully; pause animation when tab hidden.
