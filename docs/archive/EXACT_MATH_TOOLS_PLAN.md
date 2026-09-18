# Exact-math tools — build plan (`calculate` + `run_python`)

> **Status:** Shipped 2026-09 · `calculate` (in-process, sympy) + `run_python` (the `pysandbox` sidecar) · no migration · **Waves:** W1✅ W2✅ W3✅ W4✅

A mechanical backstop for arithmetic. The agent gains two tools: `calculate`, which
evaluates one expression exactly, and `run_python`, which runs a short program in a
sandboxed container. Every persona but the non-owner `intake` interviewer holds both.

## Why this exists

A language model does arithmetic by predicting digits. `DEEP_RESEARCH_SCRATCHPAD_PLAN.md`
(lines 46–49, 147–150) documents number-invention as a **recurring failure class, still
unfixed after three prompt versions** — in the same model that `render_chart`'s description
invites to plot "a count you tallied." Prompt discipline has owned this problem for the whole
lineage and has not closed it, because the instruction "don't make numbers up" cannot be
followed by a system whose only arithmetic is recall.

Two prompts in the tree said the quiet part out loud: `research.prompt` and `review.prompt`
each told their persona *"don't spend a tool call on arithmetic you can do in your head."*
That was correct advice when the alternative was web-searching a calculator. It is the wrong
advice once a deterministic evaluator exists, and both lines are rewritten here.

## The decision this reverses, and on what grounds

Two documents refuse what this plan builds, and neither is overruled lightly.

**`docs/reference/ASSISTANT.md`** lists among its refusals: *"no code execution in the
agent."* That refusal stands, unamended. `run_python` does not execute code **in the agent** —
the api POSTs a snippet to a separate container, exactly as it posts a render to `htmlrender`
and proxies a session to `jcode`. `JCODE_PLAN.md` settled this reconciliation already, in its
own words: *"jcode does not violate that, because it is not the agent."* The same argument,
and the same containment, applies here. `calculate` does not touch the refusal at all: it is
not code execution by any reading — a closed expression grammar with no names, attributes,
assignment or calls outside a fixed table.

**`docs/proposed/JERV_CONTEXT_BUDGET_PLAN.md` §5** rejected a "Python + file-execution
sandbox" — and its rejection is the more interesting one, because it was right about the
costs and explicitly named what would reopen it:

> **Revised trigger:** the only surviving argument is the *deterministic-arithmetic* one —
> revisit if a mechanical backstop is ever chosen over the prompt-discipline lineage that
> owns number-invention today.

That is this plan. The owner chose the mechanical backstop. The two costs §5 priced are
answered rather than waved away:

| §5's cost | How it is paid |
|---|---|
| **CLAUDE.md #2** — an exec tool is a by-construction hole in the storage abstraction | The sandbox has no storage to bypass. No blob volume, no database URL, no owner data, a read-only root filesystem and a tmpfs scratch that dies with the call. The storage abstraction governs *owner content*; the sandbox holds none, so it is correctly outside that chokepoint — the same reasoning `JCODE_PLAN.md` recorded for its own checkout. |
| **CLAUDE.md #10** — its failure modes are undebuggable without a terminal | Nothing about it needs a terminal. It is stock-stack (no compose profile), so a plain `up -d` — what **Ops → Update** runs from the PWA — brings it up. Its failures surface as the tool's own one-line errors in the chat, and as `agent.tool_call` / `agent.run_python` lines in the same container logs the debug API already serves. There is no new host step and no new `.env` flag required to run it. |

§5's *other* half stands and is not disturbed: the missing **chart shapes** (scatter,
non-date x-axis, correlation, histogram, pie) remain `htmlrender`'s to serve, not an
interpreter's. `run_python` returns text; it draws nothing.

## What shipped

### W1 — `calculate` (in-process)

`backend/src/jbrain/agent/mathtools.py` + `tools/calculate.tool`. New runtime dependency:
`sympy`.

- The expression is parsed by the **stdlib** `ast` and walked against a closed allowlist.
  Deliberately **not** sympy's `sympify`/`parse_expr`, which `eval()` their input and are a
  full escape surface. `__import__('os')` is not a blocked call — it is a name that was never
  in the table.
- Literals enter as **exact rationals** via `Decimal(repr(v)).as_integer_ratio()`, so `0.1`
  is `1/10` rather than the binary float's `3602879701896397/36028797018963968`. `0.1 + 0.2`
  is `3/10`.
- Irrational results stay symbolic with a decimal alongside (`sqrt(2)/3` →
  `exact: sqrt(2)/3`, `decimal: 0.471404520791032`). A whole number gets no decimal line.
- Static cost bounds (expression length, AST size and depth, integer-power digits, factorial
  argument) refuse the shapes that hang **before** evaluating them.
- **Oversized results are refused, not truncated** — the deliberate asymmetry with
  `run_python`. The first 4,000 digits of a 31,000-digit integer is not a shortened answer,
  it is a different number, and a model handed one quotes it as the result. The cap also sits
  under CPython's own 4,300-digit int-to-string ceiling, so rendering can never surface an
  error about `sys.set_int_max_str_digits`.

### W2 — the `pysandbox` sidecar

`deploy/pysandbox/{server.py,runner.py}`, `deploy/Dockerfile.pysandbox`, a `pysandbox`
compose service on a new `sandbox` network declared `internal: true`.

Containment, outermost first — each layer assumes the ones inside it will be defeated:

1. **The container.** `internal: true` network (no route off the box, and only `api` on it to
   reach), `read_only` root filesystem, tmpfs `/tmp`, `cap_drop: ALL`,
   `no-new-privileges`, non-root user, `mem_limit`, `pids_limit`. It holds nothing: no owner
   data, no credentials, no blob volume, no Docker socket.
2. **The process.** One child per call, in its own session, with rlimits applied between fork
   and exec (address space, CPU seconds, file size, process count). A wall-clock timeout
   `SIGKILL`s the whole process **group** — not `SIGTERM`, because a snippet that has wedged
   the interpreter cannot be relied on to run a handler.
3. **The runner.** Import guard (networking + escape modules refused by name), filesystem
   guard (`open` confined to the scratch directory, realpath-resolved), process guard
   (`os.system`/`os.fork`/`exec*`/`spawn*`).

Layer 3 is the weakest and is documented as such: a determined escape through a raw syscall
gets past it. It exists so that the ordinary case — a model reflexively writing
`import requests` — becomes an immediate, actionable error rather than a mysterious timeout.
The guarantee lives in layers 1 and 2.

**A bug worth recording.** The first implementation reported errors through
`traceback.extract_tb`, which resolves each frame's source line via `linecache` — which opens
the file — which the filesystem guard refuses. So *reporting* an error raised an error, and
every refusal the guards produced died on the way out instead of reaching the model. The fix
is `traceback.walk_tb`, which reads only code objects already in memory. It is the shape of
bug a sandbox produces: a control that works, and silently breaks the channel that reports it.

### W3 — the agent surface

- `backend/src/jbrain/pysandbox.py` (client, mirroring `HtmlRenderClient`),
  `agent/pythontools.py` (handler), `tools/run_python.tool`, `pysandbox_url` config,
  `main.py` wiring. Absent sidecar ⇒ the `.tool` is dropped from the registry entirely, so no
  persona is offered a tool that would only ever report itself unavailable.
- **Permission classes differ on purpose.** `calculate` is `read` — it reads nothing and runs
  nothing, so curator's `tools=None` wildcard holding it is right. `run_python` is `web`, and
  *not because it egresses* (it cannot): `web` is the opt-in, never-in-the-wildcard gate, and
  a tool that executes model-authored code must be granted deliberately per persona. This is
  the reasoning `contracts.py` already records for the radio tools — *the class is the GATE,
  not the egress*. curator holds `run_python` through `extra_tools`, as it holds
  `deep_produce`.
- Every persona gains both, via `MATH_TOOLS`, **except `intake`** — a non-owner stranger
  drives that one and its empty allowlist is a security boundary, not an oversight.
- **`teacher` is the deliberate reversal**, owner-decided: its empty allowlist was the
  design. A Socratic tutor that cannot check a learner's arithmetic must either trust it or
  assert its own. Its prompt confines the tools to *checking* — never running the problem for
  the learner, never pasting a tool's answer back as the solution.
- **`summarize` is the other**: a pure transform gains two tools because rolling findings
  into a summary is exactly where a total or a percentage gets invented.

### W4 — prompts and the tool-call log

- Guidance added to `system.prompt` (v8→v9), `jerv.prompt` (v48→v49), `archivist.prompt`
  (v6→v7), `teacher.prompt` (v1→v2, tailored), `summarize.prompt` (v2→v3, whose "You have NO
  tools" claim had become false). `research.prompt` (v17→v18) and `review.prompt` (v8→v9) had
  their "arithmetic you can do in your head" lines rewritten rather than deleted — the advice
  against web-searching a calculator is still right.
- **Per-tool-call logging.** `AgentLoop._dispatch` now times every handler and emits one
  `agent.tool_call` line (tool, ok, `duration_ms`, argument **names**, sizes), and
  `duration_ms` rides the `tool_result` event into the persisted `agent_turns` step — so
  "which call was the slow one" is answerable from a stored turn, not only from the run total.
  Before this, per-call duration was recorded nowhere: `run_steps` carries
  `idx/kind/name/ok/cost_tokens` and the run row carries one total.
- **Arguments are summarized in the log, never quoted.** A tool call's arguments routinely
  contain what the owner just said, and the container log is not RLS-scoped. A domain
  firewall enforced in Postgres is worth nothing if the same text lands in a log line. The
  verbatim arguments are persisted on the turn's own row, under the owner's scope.

## What this deliberately does not do

| Not built | Why |
|---|---|
| **Packages in the sandbox** (numpy, pandas) | Every added package is another import surface inside the thing whose whole job is to be boring, and the stdlib covers what the tool is for. If one ever earns a place it is a reviewed line in `Dockerfile.pysandbox`. |
| **State between `run_python` calls** | Each call is a fresh process and a fresh scratch directory. A session-scoped kernel is a second lifecycle to reason about, and the tool's value is that each call is independently checkable. |
| **Plotting from `run_python`** | `htmlrender` is the sanctioned path for visual output (`AGENT_CANVAS_PLAN` W1b), and §5's rejection of an interpreter-for-charts stands. |
| **Sending owner data to the sandbox** | The handler passes the model's snippet and nothing else. The sandbox cannot fetch anything, so the only way owner data reaches it is a caller putting it there. |
| **A `seccomp`/`unshare` network namespace per child** | Would need `CAP_SYS_ADMIN` or user namespaces in the container, which is a larger privilege than the thing it defends. The `internal: true` topology is the real network guarantee; the runner's import guard is the usability layer over it. |

## Residual / open

- **`teacher` and `summarize` gaining tools is a behaviour change worth watching** in live
  use: both prompts now carry restraint instructions that were previously enforced by an
  empty allowlist, which is a weaker guarantee.
- **`MAX_RESULT_DIGITS = 4,000` is a judgement, not a measurement.** It is where refusal
  starts being more honest than truncation; if a real question ever needs a bigger exact
  integer, the number moves and the CPython ceiling has to be handled explicitly.

## Verification

`backend/tests/unit/test_agent_mathtools.py` (44) — exactness, the closed grammar against
fifteen escape shapes, cost bounds, error legibility.
`backend/tests/unit/test_pysandbox_server.py` (36) — the real runner in real subprocesses: no
network, no filesystem outside scratch, no other processes, truncation, and an infinite loop
killed at the wall clock.
`backend/tests/unit/test_agent_pythontools.py` (22) — the client sends only the snippet, a
failing snippet is a successful call, rendering.
`backend/tests/unit/test_agent_loop.py` — the tool-call log carries the duration and the
argument names but not their values, and a refused tool still leaves a line.

The compose topology the containment rests on is asserted too, in
`test_pysandbox_server.py`: `sandbox` is `internal: true`, **exactly** `api` and `pysandbox`
join it, the service mounts no volumes and carries no credential-shaped environment, and it
is not profile-gated. Membership is the real egress-free claim — a network with no gateway
still reaches everything else on it — and it is a one-word change in a 900-line file that
nothing else would catch.
