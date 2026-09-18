# The jerv prompt cache

> **Status:** Living · **Last verified:** 2026-09-18

How the interactive agent's ~30k-token prefix is kept ready, what it costs when it is not,
and — the part that did not exist until 2026-09-18 — how to tell which of those is happening.

The design narrative and both post-mortems live in `backend/src/jbrain/llm/kv_prefix.py`'s
module docstring, which is the authority on WHY each rule is shaped the way it is. This doc
is the map: the three layers, the identity that ties them together, the operator surface, and
the numbers measured on the box.

## The problem

A jerv turn's prompt opens with a persona + tool-schema prefix that does not change between
turns. On this box that prefix is **30,546 tokens** (a 30,095-character persona and 45 tool
schemas). Processing it from cold is the single longest silence the box produces.

## Three layers, each covering the one below

| Layer | Holds the prefix in | Survives | Costs when it misses |
|---|---|---|---|
| llama.cpp slot + `--cache-reuse` | a server slot (RAM) | nothing — a restart or an eviction clears it | full prefill |
| `WarmKeeper` | the same slot, re-primed | a gateway restart, not an api restart | one prime |
| `KvPrefixStore` | a file under `/models/.kvslots/` | restarts, evictions, updates | one prefill, once |

`--cache-ram` is deliberately **0** (`llama_swap_config.py`): upstream's in-RAM prompt cache
would make an evicted slot cheap to recover, but it costs 16 GB of the pool this box has
frozen on three times. The disk store is the replacement, paid in disk instead of memory.

## Identity — the thing that makes a file reusable

A saved slot is only meaningful to a prompt that begins with the same tokens, so every file
is named by a fingerprint over the inputs that decide those tokens:

- the **launch line** (`-c`, `-np`, every server flag, the model path) — minus `--port`,
  which is only the model's index in the installed set
- the **system prompt** text
- the **tool schemas**, serialized in sorted order
- the **reasoning effort**, because the template renders it into the leading tokens

Anything that moves one of those is a different prefix and gets a different file. Two things
deliberately stay OUT: the GGUF's bytes and the rendered date. The date is handled at the
template layer instead — `deploy/chat-templates/gpt-oss-120b.jinja` moves harmony's
`Current date` to the *tail* of the developer block, so midnight does not invalidate the
prefix. That vendored template exists for that one reason and applies to gpt-oss only.

Known gaps, with reasons, in `../archive/PROMPT_CACHE_HARDENING_PLAN.md`: the chat template's
*content* and the llama-server build are not in the fingerprint, because neither is reachable
from a synchronous hash.

## Eligibility

Not every model can restore a slot. Plain attention models can. A **recurrent** model cannot
— the restore path clears the context checkpoints that are its only prefix-reuse mechanism —
and an **external-draft speculative** entry carries draft state no slot file captures. The
qwen3.8 MTP hybrids are admitted only when the owner's Fast-Qwen-loads patch setting is on,
because the patched engine supplies the checkpoint restore the stock server lacks.

One consequence worth knowing before you touch Settings: raising a hybrid's slot count to 2
strips `--spec-type`, which withholds `--slot-save-path`, which turns the disk layer off for
that model. Correct, and the screen now says so.

## What it is worth (measured on the box, 2026-09-18)

| | |
|---|---|
| Cold load, prompt tokens **processed** | **11** of 30,546 |
| Reuse rate on a warm prime | **1.0** |
| Restore | **94 ms** page-cache-warm, 547 ms cold off NVMe |
| Full prefill, for comparison | 118 s measured; 204 s contended; ~220 s at a 262k window |
| Slot file | ~1.1 GB plus a `.ckpt` sidecar |

The often-repeated "~60 s prefill" is the low end of that range, not its centre.

## Operating it, with no terminal

Everything here is the owner debug API (`runbooks/DEBUG_ACCESS.md` has the full reference):

- `GET /api/debug/llm/kv-prefix` — **the question "is it working?"**. Counters for every
  outcome since the api started, per-model state (`file_present`, `restored_unused`,
  `cold_no_file`, `no_disk_layer`, `ineligible`) resolved against what is actually on disk,
  disk usage against the budget, and llama-server's own reuse ratio.
- `POST /api/debug/llm/local-models/{id}/prime` — run the real prime and time it. `reuse_rate`
  near 1.0 proves a restore landed; `elapsed_ms` alone only implies it.
- `DELETE /api/debug/llm/kv-prefix` — clear the store, or one model's files.
- `PUT /api/debug/llm/kv-prefix/budget?gb=N` — the disk allowance, 2..500 GiB.

A miss also writes a `kv_prefix_missed` box event, so it reaches the PWA's vitals.

**Read the counters before believing anything else.** This feature shipped inert twice — once
on a read-only mount, once on a flag/eligibility split — and both times the reason it survived
was that a healthy store and a dead one produced identical output: no rows at all.

## Known gaps

Carried deliberately out of the 2026-09 hardening (`../archive/PROMPT_CACHE_HARDENING_PLAN.md`
has each one's full reasoning). None is silent any more — the counters and the state read will
show you when one bites.

- **The chat template's CONTENT and the llama-server build are not in the fingerprint.** Ship
  an edited `.jinja` under an unchanged path, or repin the engine, and a stale file still
  matches. Fail-soft — a wrong restore trips the `n_restored` check and the file is deleted —
  except on the FIRST restore after a boot, which adopts whatever count comes back with no
  comparison, and that is exactly when a stale file is most likely. Neither input is reachable
  from a synchronous hash: the template lives on the gateway container, the build only over
  HTTP.
- **A per-conversation model or effort pick has no save path.** The fingerprint moves with it
  (correctly), but only the keeper and the load-time warm ever save, and both use the STORED
  effort. So a conversation pinned to a non-default effort re-prefills every turn, forever.
- **Residency evicts biggest-first**, and the interactive model is usually the biggest — so it
  is the first victim, including mid-turn when a vision tool needs room. Protecting it means
  refusing loads that fit today: a budget policy decision, not a cache fix.
- **`n_keep` is 0 and context shift is unconfigured.** Measured, not assumed. If shift ever
  engages on an overrun, nothing pins the prefix head — and the slot still reports a large
  `n_prompt_tokens` afterwards, so the restore gate would read "something prefix-sized is
  cached" and decline. Silent and self-concealing if it ever fires.
- **There is no slot pinning.** A second slot buys one dissimilar request of headroom, not
  immunity; see `llm/llama_swap_config.py`'s `-np` comment for what llama.cpp actually does.

## Where the code is

| | |
|---|---|
| `llm/kv_prefix.py` | the disk store: fingerprint, save gate, restore gate, LRU budget |
| `llm/warm_keeper.py` | the keep-warm loop: prime, re-prime, the edge triggers |
| `agent/priming.py` | the prime's (system, tools) — the same call a real turn makes |
| `llm/llama_swap_config.py` | `--slot-save-path`, `-np`, `--cache-reuse`, `-cram` |
| `llm/router.py` | restore-before-dispatch, and the post-turn identity note |
| `api/llm_settings.py`, `api/debug.py` | the operator surface above |
