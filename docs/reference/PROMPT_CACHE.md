# The jerv prompt cache

> **Status:** Living · **Last verified:** 2026-10-05

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

**Flash-Next** (a hybrid, catalog `kv_restore_needs_patch`) is admitted because its image
builds the checkpoint-sidecar patch in (`deploy/patches/0001`, `PATCH_RESTORE_CHECKPOINT=1`
since F4) — and the store proves it on every save: the patched server writes a `.ckpt` sidecar
beside each slot file, so a save without one removes the file and takes the model out of the
disk layer until the next api start (`patch_absent` in the state read). A file without its
sidecar is never restored.

**The restore gate.** On Flash-Next nothing is restored — no role prefix, no conversation —
until `POST /api/debug/llm/slot-probe` has **passed** (next-token probabilities within tolerance, restore
effective, and the save's checkpoint sidecar present) against the server running now. The
probe writes its verdict as `restore-gate.json` beside the slot files, keyed by a fingerprint
of the launch line (minus `--port`) and the llama.cpp build from `/props`; a new image or launch
line reads as `awaiting_probe` until the probe runs again, and a failed probe keeps restores off.
Saves are not gated. The state read and Ops show `awaiting_probe | passed | failed`.

## On Flash-Next: one prefix per role, and conversation files (F4)

Flash-Next serves eight role-pinned slots over one shared KV pool
(`../plans/FLASH_NEXT_ENGINE_PLAN.md` §4a), so the store works per **role**:

- jerv's prime is saved from slot 0 and restored into slot 0. The keeper also restores the
  same file into the **scheduled-task** slot (2) when it is empty — scheduled turns send jerv's
  persona, tools and effort, so it is their identity too. Ingest, research and the pet are not
  primed: their stable prefixes are a few hundred tokens or do not exist. Any role's turn still
  restores into its own slot on demand if a file of its identity exists.
- A restore goes into the role's own slot only while that slot is idle and **empty**, never
  over an occupied one, and only when the restored tokens fit the pool — judged by the router's
  pool guard under its own lock, with its pending calls, so a restore can never push the pool
  past its cells under a busy request (a full pool fails every busy request). The guard also
  charges what a restore put in a slot that never ran a request (`/slots` reports no size for
  one) until a request uses it or the guard erases it, and tells the store when it does.
- Memos and drift are per (model, role). A restored-but-unused slot is not restored again until
  a request uses it, the model reloads, or this process's pool guard erases it. (The worker's
  guard can erase it unseen; that role's next turn then pays one prefill and resets the memo.)
- Every save deletes the old `.ckpt` sidecar first, so a sidecar after the save proves the
  running build is patched; a patch-gated role prefix is re-saved once per api start for the
  same reason, even when its file exists.
- No save leaves the models volume with less than 20 GiB free, or less than twice the file
  about to be written (`save_skipped_low_disk`); saves and prunes run one at a time.

The interactive slot also carries **conversation files** (`llm/kv_conversation.py`, toggle
*Keep chats on disk*, default ON) — for **research-type chats only**. A slot file holds the
conversation's token ids on disk, outside Postgres, where the domain firewalls (health, finance,
location) cannot reach it, so a chat gets one only when it cannot hold firewalled or private
data:

- its persona reads no knowledge base and holds no mail tools (jerv and the other
  `reads_knowledge_base=False` agents; never the curator, never the archivist);
- its session names no domain but `general` (an unknown domain counts as firewalled) and no
  subject — and never did: a chat re-scoped away from a firewalled domain (or one with a
  subject) is recorded at the re-scope in the owner setting `llm_kv_conversation_excluded_sessions`
  and stays off disk for good, since its history can carry what the old scope let it read;
- no location, mail or records tool (`current_location`, `where_was_i`, `weather`, `gmail_*`,
  `read_labs`, … — `kv_conversation.EXCLUDED_TOOLS`) has run in ANY of its turns. The chat reads
  that off the transcript before each turn; a turn that runs one deletes the conversation's
  files at once and it is never saved again.

**Brain/curator chats never get a conversation file.** Role prefix files hold only the system
prompt and tool schemas, no owner data, and are unaffected. Deleting a chat, or changing its
scope, deletes its files (`forget_conversation`); turning the toggle off deletes them all. None
of these can be undone by a save already under way: a forgotten conversation is marked before
its files are deleted, a clear bumps an epoch every earlier claim carries, and every save checks
both under the same lock the deletion takes — including a claim made by a turn that was still
streaming when the chat was deleted. A restore's pool cells are reserved in the pool guard's own
locked decision and held while the file streams, so no placement can count them free. One window stays open: a save interrupted after llama-server wrote the slot file but before
the store wrote its claim (the api cancelled or killed mid-save) leaves a conversation file with
no `.meta`. It can never be restored — a file without a readable claim is never trusted — and
it is the first thing the budget evicts; any forget or clear also deletes it, since a claimless
conversation file cannot say whose it is.

When another conversation, or the keeper's prime, is about to take slot 0, the conversation it
holds is saved first — only if `/slots` still reads as that conversation's cache (between its
last prompt and prompt + answer) and the server saves exactly that many tokens. The save streams
outside the store's main lock. A conversation idle for 10 minutes is saved by the keeper's tick.
When a conversation speaks again and slot 0 does not hold it, its file is restored before the
request on **identity alone** — same conversation key, same base identity (launch line, persona,
tools, effort). A chat request's message list is not stable from turn to turn (a timestamped
`now` block, resume/plan/artifact context, per-turn hints, the turn's own tool steps with their
replayed thinking), so no message comparison could hold; llama-server compares TOKENS after the
restore and re-evaluates from the first divergence, reusing up to the nearest context checkpoint
before it — a restore is never wrong, only more or less useful. How useful is **measured**: the
first request after a restore reports `cached_tokens`, judged a hit (at least half the restored
tokens reused), partial (at least 4,096) or miss. A conversation whose restores miss three times
in a row loses its file and is not saved again until the api restarts. A request that another
interactive request superseded never claims the slot. File names are hashes, and the `.meta`
claim holds only the key's hash, the base identity, counts and the miss streak.

Both kinds share the **disk budget** (default 40 GiB, Ops → *Prompt cache disk*), which is
**per engine**: Flash-Next's files and the standard engine's are each held to it separately,
so one engine's parked prefixes never evict the other's (owner, 2026-10-05; `store.by_engine`
in the state read). Within an engine every conversation file is evicted before any role
prefix, oldest first. A Flash-Next 29k-token prefix
is ~0.55 GiB; a conversation file grows with its length from there.

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

The owner's two knobs are in the PWA, Ops → Server update: **Keep chats on disk** (the
conversation cache) and **Prompt cache disk** (the budget). Both apply at once.

Everything else is the owner debug API (`runbooks/DEBUG_ACCESS.md` has the full reference):

- `GET /api/debug/llm/kv-prefix` — **the question "is it working?"**. Counters for every
  outcome since the api started, a hit/miss `summary`, per-model state (`file_present`,
  `restored_unused`, `cold_no_file`, `no_disk_layer`, `ineligible`) resolved against what is
  actually on disk, per-role rows and conversation state on Flash-Next, disk usage against the
  budget, and llama-server's own reuse ratio.
- `POST /api/debug/llm/local-models/{id}/prime` — run the real prime and time it. `reuse_rate`
  near 1.0 proves a restore landed; `elapsed_ms` alone only implies it.
- `DELETE /api/debug/llm/kv-prefix` — clear the store, or one model's files.
- `PUT /api/debug/llm/kv-prefix/budget?gb=N` — the disk allowance, 2..500 GiB, live.
- `PUT /api/debug/llm/kv-prefix/conversations?enabled=` — the conversation cache, live.
- `POST /api/debug/llm/slot-probe` — save a slot, restore it into another, compare next-token
  next-token probabilities within a tolerance (0.01 by default — in probability, because a
  same-slot re-read already moves deep-tail logprobs by over a nat); `passed` and `sidecar`
  are Flash-Next's F4 gate.

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
- **There is no slot pinning on the standard engine.** A second slot buys one dissimilar
  request of headroom, not immunity; see `llm/llama_swap_config.py`'s `-np` comment for what
  llama.cpp actually does. Flash-Next pins every call to its role's slot.
- **Conversation restores are unmeasured on the box** until F4's on-box sitting: whether the
  previous answer's re-render leaves a checkpoint close enough to the divergence to be worth
  the restore is a measurement, not a guarantee.

## Where the code is

| | |
|---|---|
| `llm/kv_prefix.py` | the disk store: fingerprint, save gate, restore gate, per-role slots, conversations, LRU budget |
| `llm/kv_conversation.py` | conversation file names, claims, the privacy rule, the restore decision and its judging |
| `llm/warm_keeper.py` | the keep-warm loop: prime, re-prime, the edge triggers |
| `agent/priming.py` | the prime's (system, tools) — the same call a real turn makes |
| `llm/llama_swap_config.py` | `--slot-save-path`, `-np`, `--cache-reuse`, `-cram` |
| `llm/router.py` | restore-before-dispatch, and the post-turn identity note |
| `api/llm_settings.py`, `api/debug.py` | the operator surface above |
