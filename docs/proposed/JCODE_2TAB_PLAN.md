# jcode → two tabs (Terminal · Preview) — build plan

Binding plan for gutting code mode (jcode) down to a terminal-first session, per the
multi-wave process (`docs/PROCESS.md`). The GUI gate is settled: three interactive
mocks were presented and **Variant A — full-bleed terminal**
(`docs/mocks/jcode-session-2tab-a-fullbleed.html`) is the binding spec.

## Goals (owner request, decisions settled)

1. **Two tabs only: Terminal · Preview.** Remove the PWA chat, the diff placeholder,
   and the read-only terminal-log tab. The interactive shell becomes the single
   primary surface; Variant A maximizes its space (slim 1-line header, owner actions
   in a `⋯` menu, terminal fills the rest).
2. **Terminal = plain login shell** in the checkout (run a coding CLI yourself —
   `claude` or `grok`), model pinned as today. Both CLIs are installed in the image and
   pinned to the on-box coder: `claude` via the `claude-shim` Anthropic↔OpenAI
   translator, `grok` (`@vibe-kit/grok-cli`, the Node-native build) straight at the
   gateway's OpenAI `/v1` (no shim — it's OpenAI-compatible and the gateway is
   OpenAI-native).
3. **Terminal exit pauses the session.** Exiting the shell (Ctrl-D / `exit`) kills the
   session's processes and marks it **`stopped`**, but **keeps the on-disk checkout**
   (uncommitted work preserved). It can be **restarted from the session manager** (or
   the in-screen Restart). Distinguish a real shell exit (PTY child EOF) from a mere
   socket drop (tab switch / background), which must NOT stop the session.
4. **Full gut of the now-dead chat machinery** (backend + control server): the SSE
   turn endpoints, the headless `CodingAgent`, and the per-turn frame buffer.
5. **Coder model on warm:** serve it at its **full native context (262144)** and
   **never evict/reload it when it is already resident**.
6. **Keep** the session manager, share links, and external-LLM endpoints. Share
   recipients get **both** tabs (terminal included).

## Non-negotiables touched

- No new DB table (`jcode_sessions.status` is free-form `Text`; `stopped` is a new
  value) → no migration, no new RLS test. Every changed route stays `owner_only`
  except the existing `JcodeAccessDep` share routes.
- Tests land with the code (80% gate; the removed turn paths drop with their tests).
- All LLM/gateway access stays behind the existing adapters.

## Wave 1 — backend & control-server gut + lifecycle + model

**T1.1 — control server (`jcode/`).** Remove `agent.py` (`CodingAgent`,
`ClaudeCodeAgent`, `FakeCodingAgent`, `TurnEvent`) and the `/sessions/{sid}/turn` +
`/sessions/{sid}/cancel` routes. `SessionManager` drops its `agent` dependency,
`run_turn`, `cancel`, and the turn-concurrency counter; `delete` no longer calls the
agent. Add:
- `stop(sid)` — kill the session's terminal PGIDs + `kill_processes_in_dir`, set
  `status="stopped"`, **keep the checkout**.
- `restart(sid)` — set `status="ready"` (checkout already present); 404 if unknown.
- `Status` gains `"stopped"`. `idle_sessions` **excludes** `stopped` sessions so a
  paused checkout is not reaped (the owner chose "keep checkout").
- `terminal.py`: `serve_terminal` gains an `on_shell_exit` callback fired **only** on
  PTY child EOF (real exit), distinct from `on_close` (socket drop). `app.py`'s
  terminal route wires `on_shell_exit → sessions.stop(sid)`.
- `app.py` adds `POST /sessions/{sid}/restart`; `main.py` stops constructing the agent.

**T1.2 — api proxy (`backend/jbrain/api/jcode.py`).** Remove `_JcodeTurn`, `_drive`,
`run_turn`, `reconnect`, `cancel_turn`, `_run_or_403`, `_frame`, `_turns`, and the
`jcode_turns` app-state wiring. `JcodeApi`/`JcodeClient`/`FakeJcodeClient` drop
`stream_turn` + `cancel`, gain `stop` + `restart`. Add owner routes
`POST /jcode/sessions/{sid}/stop` and `/restart` (proxy + mirror status). `delete` no
longer cancels a turn (no turns); it still tears down via the control server.

**T1.3 — model (`backend/jbrain/api/jcode.py` + catalog/setup).** `_warm_model`
short-circuits when `served` is already in `gateway.running()` (no eviction, no load
probe). The coder serves at full native context: set the coder catalog entries'
`context_window` to `native_context_window` (262144) so `llama_swap_config` stamps the
full `-c`; `jcode-setup.sh` regenerates the gateway config. `_model_payload` reports the
served window so the UI/meter reflect it.

Local verify: `ruff` + `pyright` + the touched unit tests (`test_jcode_api`,
`test_jcode_client`, `test_jcode_terminal`, jcode `test_app`/`test_sessions`/
`test_reaper`/`test_terminal`, `test_llama_swap_config`, `test_llm_local_catalog`).

## Wave 2 — frontend (Variant A) + manager restart + docs

**T2.1 — api client/types/mock.** Remove `jcodeTurn`, `jcodeResume`, `cancelJcodeRun`,
and `jcode/stream.ts` (+ its test) if unused elsewhere. Add `jcodeStopSession` /
`jcodeRestartSession`. Drop the now-unused `JcodeEvent`/`Item` turn types. Update
`api/mock.ts`.

**T2.2 — `JcodeSessionScreen.tsx` (Variant A).** Rewrite to two tabs (Terminal,
Preview). Keep `JcodeCli`, `JcodeKeys`, `JcodeShareManager`, the model load
prompt/loading bar (now a terminal overlay), and preview. Remove the chat transcript,
`JcodeBubble`, `applyEvent`/`send`/`stop`, the composer, the status line, and the
diff/log panels. Slim 1-line header + `⋯` menu (Reset · Share · Stop · Delete). On
PTY/socket close that corresponds to a shell exit, show the **stopped** overlay with
**Restart**; Restart calls `jcodeRestartSession` then remounts the terminal.

**T2.3 — `JcodeScreen.tsx` (manager).** Render `stopped` status (grey dot); a stopped
row's tap restarts then opens; the swipe rail offers Restart. External-LLM section
unchanged.

**T2.4 — docs.** Update `docs/DESIGN.md` jcode section to the 2-tab Variant A spec
(supersede the 4-view variant C); keep the chosen mock canonical under `docs/mocks/`.
`dev-setup.sh` unaffected (no new dep).

Local verify: `biome` + `tsc` + `vitest` (`JcodeSessionScreen.test`,
`JcodeScreen`-adjacent, `JcodeShareApp.test`).

## Gates

Per-task: independent adversarial review (reviewer ≠ builder) before merge to the wave
branch. Per-wave: a second wave-level review of the whole diff. CI green before any
merge. PRs are opened only on explicit owner request.
