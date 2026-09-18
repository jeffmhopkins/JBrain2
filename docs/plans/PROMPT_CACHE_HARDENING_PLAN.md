# Prompt cache hardening — what the instrumentation found

> **Status:** In progress · **Last verified:** 2026-09-18 ·
> **Waves:** P0✅ P1✅ P2◻️ P3◻️ P4◻️ P5◻️

The jerv prompt cache (`llm/kv_prefix.py` + `llm/warm_keeper.py` + the `--slot-save-path`
half of `llm/llama_swap_config.py`) works, and now says so. This plan carries what an
eight-way parallel review of it found, ranked by what each defect costs the owner.

## Measured on the box, 2026-09-18

The numbers every item below is weighed against, taken through
`GET /api/debug/llm/kv-prefix` and `POST …/prime` after P0 shipped:

| | |
|---|---|
| Prefix | **30,546 tokens** (45 tools, 30,095-char persona) |
| Cold load, prompt tokens **processed** | **11** — the restore spared the rest |
| Reuse rate, warm prime | **1.0** (30,545 cached / 1 processed) |
| Restore | **94 ms** page-cache-warm, **547 ms** cold off NVMe |
| Slot file | **1.13 GB** + its `.ckpt` sidecar |
| Store | 23.6 GiB of 25 GiB — **94% of budget**, 18 files |

Two corrections to figures repeated across the corpus, now that they are measured: the
file is **~1.1 GB, not ~2.2 GB**, and the headline "~60 s prefill" is the low end of a
range the code itself records as 118 s / 204 s / ~101 s at 32k / ~220 s at 262k.

## P0 ✅ — the cache says whether it is working (#1412)

Every outcome counted, a bounded ring of recent ones, `kv_prefix_missed` box events, and
`GET /api/debug/llm/kv-prefix` resolving the fingerprint a turn WOULD ask for against what
is on disk. `POST …/prime` returns the prompt-cache delta across the prime.

Why first: the store was correct and unobservable — only its two SUCCESS paths wrote a box
event, so a healthy store and a store that had not restored since boot produced identical
output everywhere, which is how this feature shipped inert twice.

## P1 ✅ — the five that cost the most (#1413, and this PR)

- **Scheduled tasks, plan continuations and sub-agents sent a divergent tool array.**
  `tasks/runner.py` and `agent/spawn.py` built an `AgentLoop` with no
  `hidden_tools_provider`. Pinned by an AST scan over every construction, so the next
  caller fails until it is wired deliberately.
- **An eviction left the store believing it had restored into a slot that was gone.**
  `residency._prefix_lost` told the keeper and not the store.
- **A restore failing at the transport left a poisoned file** nothing repaired.
- **A cold prime could not report the reuse it had just won** — found on the live box.
- **The keeper's memo omitted the reasoning effort** that the fingerprint includes, so a
  Settings change meant no re-prime, no save, and a permanent re-prefill.
- **`_prime_tokens` was keyed by served model, not fingerprint**, so one identity's count
  condemned and DELETED another's good file with no re-save path.
- **The restore lock sat below the memo and file checks**, so two callers both restored.
- **Evictions left no trace at all**, and the 25 GiB budget had "no knob" on a box with no
  terminal. Now `DELETE /llm/kv-prefix` and `PUT /llm/kv-prefix/budget`.

## P2 ◻️ — the slot story is not what the UI says

- **There is no slot pinning.** No `--slot-prompt-similarity`, no slot-id routing, no
  reservation. llama.cpp picks the longest-prefix slot, else the **LRU** slot — and the idle
  jerv slot IS the LRU slot. `llama_swap_config.py:401-407` and `LLMSettingsScreen.tsx:1893`
  ("background jobs and chat-titling can't evict it") both assert otherwise.
- **`-cram 0` removes the recovery path** that would make a steal cheap, justified by a
  comment written for the single-slot world.
- **Enabling the interactive slot on a qwen3.8 hybrid strips `--slot-save-path`.** The strip
  is correct (a plain-recurrent restore is unsound); the bug is that it is silent. Now
  visible as `no_disk_layer`, but the PWA still offers the toggle without saying so.
- Decide alongside: whether `auto_restore` should default on. Measured OFF on the box today,
  so the RAM tier is doing nothing and the disk layer carries everything.

## P3 ◻️ — identity and lane

- **`agent.turn` is worn by background traffic** — `daily_briefing.py:47`,
  `deep_research.py:115`, `spawn.py:81`. The label the KV machinery keys on.
- **The restore gate is `any()` across ALL slots**, a single-slot argument in a two-slot
  world: it refuses to restore into an empty idle slot because another is busy, and a test
  currently pins that as correct.
- **`--port` is a model INDEX and is in the fingerprint** — installing a model from the PWA
  orphans every later model's file.
- **The chat template's CONTENT is not hashed**, only its path; nor is the llama-server build.
- **A per-conversation effort or model pick can never have a file saved for it.**
- **Residency evicts biggest-first** with no protection for the interactive model, which on
  this box IS the biggest — including mid-turn on `analyze_image`.
- **Stop mid-stream skips all post-turn bookkeeping** (`router.py:942-961` sits after the
  yield loop), stranding `_restored_unused`.

## P4 ◻️ — the keeper

No backoff or ceiling on prime failure (~17k log lines/day, and a 5 s evict/reload fight with
the worker); `note_prefix_lost` cannot wake the 60 s sleep and is lost if a prime is in
flight; shutdown cancels without draining, so a cancel mid-save can leave a partial file;
`--keep` / context-shift are unset, inheriting an upstream default that could silently drop
the prefix head.

## P5 ◻️ — tests and docs

- **No KV test uses a recorded `/slots` body.** Every slot dict is hand-written with the three
  keys the code reads; a real body has ~20. The docstring's claims about llama.cpp semantics
  are encoded into the fakes rather than tested against them — and `test_prefill.py` already
  carries verbatim live captures precisely because three earlier attempts shipped dead against
  a guessed shape. This is the highest-value test work left.
- `max_tokens=1` — the whole basis of the exact-integer save gate — is asserted nowhere.
- The busy-slot-freed-into-a-live-conversation branch is uncovered, and is the most
  safety-critical one in the module.
- **No reference doc exists**: the design lives in `kv_prefix.py`'s docstring, with the fullest
  prose account buried in a hardware-setup runbook. `MODEL_ACCESS_INVENTORY.md`'s KV rows carry
  nine rotted `file:line` citations, and four code sites point readers at an archived v1 plan
  whose central claims are now false.
