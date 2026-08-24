# Running JBrain's local models on an AMD Strix Halo box

> **Status:** Living · **Last verified:** 2026-08-24

End-to-end runbook for self-hosting the optional local models (docs/reference/ANALYSIS.md,
"Self-hosted local models") on a **Ryzen AI Max+ 395 / 128 GB** (gfx1151,
Radeon 8060S) system. Path: **Ubuntu → kernel ≥ 6.18.4 → Vulkan → JBrain base
install → host tuning + reboot → enable local models → route in the UI.** On
**26.04 LTS** the stock kernel already clears the Phase 2 floor, so it's **one
reboot** (host tuning); an older release that needs a mainline kernel is two.

Local hosting is opt-in; the stock deploy is cloud-only. Nothing here runs
automatically — every step is a deliberate command.

---

## Phase 0 — BIOS
- **Secure Boot.** Leave it **on** if you'll run the stock signed kernel (the
  default on 26.04 LTS, whose 7.0 kernel already clears the Phase 2 floor).
  **Disable it** only if you install an unsigned mainline kernel (Phase 2) — on
  an older release, or to get a newer kernel than your distro ships.
- **Resizable BAR / "Above 4G decoding": Enabled.**
- **GPU/UMA memory:** set the iGPU to a **small fixed** dedicated allocation, not
  `Auto`. The iGPU borrows the shared pool dynamically via `amdgpu.gttsize`
  (Phase 5), so the carve-out only needs to be tiny.
  - ⚠️ **Avoid `Auto`.** On a 128 GB box `Auto` (`UMA_AUTO`) silently carves out
    ~50% of RAM (64 GB) as fixed VRAM — the OS then sees only 64 GB and the
    ~91 GB resident set can't fit.
  - On the AMI BIOS in the GMKtec EVO-X2 the control is **Advanced → GFX
    Configuration**: set **`iGPU Configuration` = `UMA_SPECIFIED`** and
    **`UMA Frame buffer Size` = `2G`** (the smallest offered). Other boards label
    it "UMA Mode" / "UMA Frame Buffer Size" — same idea, pick the smallest.
  - **Sanity check after Phase 5's reboot** that the carve-out is actually small
    (the `Auto` trap is invisible until you look):
    ```bash
    free -h                                              # MemTotal ~125 GB (not ~64)
    cat /sys/class/drm/card*/device/mem_info_vram_total  # ~2 GB carve-out (not 64 GiB)
    cat /sys/class/drm/card*/device/mem_info_gtt_total   # ~124 GB — the pool models use
    ```
    A `vram_total` of ~64 GiB and `MemTotal` of ~64 GB means the iGPU is still on
    `Auto`/a large fixed UMA — go back into BIOS and set the small carve-out.

## Phase 1 — Install Ubuntu
- **Ubuntu 26.04 LTS** is the pick: it ships **Linux 7.0** (well above the
  Phase 2 gfx1151 floor) and a recent Mesa, so gfx1151 works on the **stock
  signed kernel** — no mainline `.deb`, no Secure-Boot-off dance — and it's the
  long-support LTS.
  - 25.10 also works but is a 9-month interim release (EOL ~mid-2026). On it,
    and on 24.04 LTS, the stock kernel predates the 6.18.4 floor, so you must
    add a mainline kernel in Phase 2 (24.04 also needs a newer-Mesa PPA).
  - **Upgrading an existing 25.10 box in place** (`do-release-upgrade`) is
    supported and lands you on the stock 7.0 kernel: at each dpkg config prompt,
    **keep your version (`N`)** for any file you customized per this runbook —
    `/etc/default/grub`, `/etc/systemd/journald.conf`, `/etc/default/earlyoom` —
    then re-run the verification below.
- Install normally, then: `sudo apt update && sudo apt full-upgrade -y`

## Phase 2 — Kernel ≥ 6.18.4  (hard requirement)
gfx1151 has a stability bug below 6.18.4. Check:
```bash
uname -r
```
**On 26.04 LTS the stock kernel is 7.0 — this requirement is already met, so
skip the rest of this phase (no mainline install, no reboot #1).** On 25.10 /
24.04 the stock kernel is older. If **< 6.18.4**, install a mainline kernel (6.18.7+ is the community-tested one)
from <https://kernel.ubuntu.com/mainline/> — download the `linux-headers`,
`linux-modules`, and `linux-image-unsigned` **amd64** `.deb`s for the chosen
6.18.x, then:
```bash
sudo dpkg -i linux-*.deb
sudo reboot
```
- ⚠️ Secure Boot must be **off** (Phase 0) or the unsigned image won't boot.
- ⚠️ Avoid `linux-firmware` **20251125** (breaks ROCm on Strix Halo).

After reboot, confirm `uname -r` ≥ 6.18.4.

## Phase 3 — Vulkan stack + verify the GPU
We default to **Vulkan/RADV** (needs only `/dev/dri`; no ROCm setup):
```bash
sudo apt install -y mesa-vulkan-drivers vulkan-tools
vulkaninfo --summary | grep -i deviceName
```
✅ **Checkpoint:** you must see **Radeon 8060S**. The RADV device string is
`RADV STRIX_HALO` on recent Mesa (26.04) and `RADV GFX1151` on older Mesa — both
are correct. (An `llvmpipe` entry alongside it is the software fallback; ignore
it.) If the Radeon device is absent, Mesa is too old (on 24.04 add
`ppa:kisak/kisak-mesa`) or the kernel is too old — fix before continuing.

## Phase 4 — Install JBrain (cloud stack first)
Installs Docker, clones to `/opt/jbrain2/src`, brings up the stack:
```bash
curl -fsSL https://raw.githubusercontent.com/jeffmhopkins/JBrain2/main/deploy/install.sh | sudo bash
```
Prompts:
- **Domain** — the name you'll use to reach this box.
- **Access mode** — **1) Cloudflare Tunnel** (default, recommended for a home box
  on a dynamic IP / behind CGNAT: no static IP, no port-forwarding; full
  walkthrough in `CLOUDFLARE_TUNNEL.md`) or **2) Direct** (the box has a public
  name resolving to it with inbound 80/443 open, and Caddy fetches Let's Encrypt).
- **Anthropic / xAI keys** — paste, or leave blank to run fully local.
- **"Enable self-hosted local models?"** → **N** for now (host tuning + reboot
  comes first; you'll enable them in Phase 6).

✅ **Checkpoint:** `jbrain status` shows `api`/`db`/`proxy` healthy (plus
`cloudflared` in tunnel mode). In direct mode the site loads as soon as DNS +
Let's Encrypt resolve; in tunnel mode it loads once you've finished the
Cloudflare side (`CLOUDFLARE_TUNNEL.md`).

## Phase 5 — Host tuning + reboot (#2)
```bash
sudo jbrain strix-halo-host-setup
```
Idempotently (confirming before the GRUB edit):
- kernel params `amd_iommu=off` plus a **derived** `ttm.pages_limit` — MemTotal
  minus a 16 GiB host reserve, so the GTT ceiling is a genuine backstop instead
  of ~100% of RAM (which disables it; see "Reading the memory instruments
  correctly" below for the mechanism). `amdgpu.gttsize` is deprecated and no
  longer written; the script prints the derived pages figure it will set.
- adds you to `video`/`render`,
- installs a `tuned` accelerator-performance profile.

Then:
```bash
sudo reboot
```
✅ **Checkpoint:** `cat /proc/cmdline` contains `amd_iommu=off` and a
`ttm.pages_limit=` whose GiB value (pages / 262144) is MemTotal minus ~16 GiB —
**not** the old hardcoded `32505856` (124 GiB), which on a 128 GB box means the
backstop is off.

> **26.04 note — `crashkernel`.** Ubuntu 26.04 adds a `crashkernel=…:4096M`
> reservation on a 128 GB box (it shows up in `/proc/cmdline`, and it's why
> `free` reports ~121 GB rather than ~125). kdump can't survive the
> reclaim-livelock freeze this hardware is prone to — the box locks before it
> runs — so on a memory-tight local set you can reclaim that ~4 GB of headroom:
> add `crashkernel=0` to `GRUB_CMDLINE_LINUX_DEFAULT`, `sudo update-grub`,
> reboot. Optional — a headroom-vs-crash-diagnostics trade.

## Phase 6 — Enable the local models
```bash
sudo jbrain enable-local-models
```
Builds the gateway (the community-maintained gfx1151 llama.cpp image +
llama-swap), downloads the recommended set — **Qwen3-VL-30B-A3B Q8 (~32 GB)** +
**gpt-oss-120b MXFP4 (~59 GB)**, ~91 GB total — generates the llama-swap config,
and starts the gateway.

**The app is the box's sole model evictor, and it restores.** Every model is a llama-swap
non-swapping group member, so the gateway never auto-evicts anyone — instead, before a
model loads, the app (`jbrain.llm.residency`) frees the **fewest** resident models needed to
keep **≥15% of RAM free** after it's resident, evicting biggest-first. Since 2026-08-23 the
**reservation ledger is the authority** for that arithmetic (`jbrain.llm.ledger`): the plan
runs the same `admission.admit` the load's charge applies, over the ledger's own pools and
rows, so the evict verdict and the admission verdict cannot disagree; a live `/proc/meminfo`
reading survives as the ledger's *measured* term — it is what sees consumers the ledger
didn't create (image-gen, whisper, OS pressure) — and the whole-box measured+predicted
planner is now only the fallback for a build (or a moment) with no ledger read. So you can
**load any model**: a small model (Qwen3.5-0.8B/4B) stays hot beside gpt-oss-120b, requesting
the coder evicts the *big* model — not the tiny one — and a model too large to co-reside
evicts everything and takes the box. Whatever a *transient* eviction removed (an image render
freeing the LLMs, or a code session giving the coder the box) is **remembered and restored at
end of turn**, so the box drifts back to its prior steady state instead of cold-loading on
demand. This replaced the old all-or-nothing pin that co-resided ~91 GB with no headroom and
drove kernel-reclaim hard-freezes (see "Stability — hard-freeze / OOM hardening" below); the
budget is what makes it safe. The 15% floor is tunable **live from Settings → LLM → On-box
memory** (the "Free-RAM headroom" control, 5–50%, read by the evictor in both the api and
worker on the next load with no restart) or, at deploy time, via `LOCAL_LLM_FREE_RAM_FRACTION`;
the stored control value overrides the env default. Lower it to co-reside more (e.g. keep the
Q8 vision model hot beside gpt-oss-120b) at a thinner margin; raise it for more freeze safety.
The Settings → On-box models screen exposes the same rule: **Staging** an available model is a
transient *preview* — it dry-runs the eviction (`plan-load`) so you can see what loading it
would evict before you commit, and **Load** applies exactly that. (There is no persisted
keep-hot pin: models you use stay warm on their own via the restore above.) The **Catalogue**
tab also carries an **Available** switch per installed model: making one *unavailable* takes it
out of the router's swap roster (and unloads it) without deleting the weights, so it flips back
instantly — distinct from **Uninstall**, which prunes the weights. A model that
**can't fit the box at all** — its footprint exceeds total RAM even after evicting everything —
is **refused, not attempted**, on every path (the manual Load 409s, an auto-load fails the
completion), and nothing is evicted for it: loading it would only OOM-crash the box, so the
app declines rather than trying. That guard is the app's; earlyoom below is the OS-level
backstop for the rest.

**The interactive model is kept resident AND primed across restarts (`jbrain.llm.warm_keeper`).**
The slow bit of a first chat turn isn't the weight-load — it's the **prompt prefill**: the
model reading the whole persona + tool schemas before it emits token one. **Measured
2026-08-17 on gpt-oss-120b: 56s for jerv's 22,704-token prefix, ~416 tok/s prompt
processing** (three cold runs within 0.6s of each other; a warm `--cache-reuse` hit on the
same payload returns in ~1.0s, and a 280-token control in 1.5s, so ~54.6s of that is the
tool block itself). That 55s gap is what everything below exists to keep off the owner's
first message. The gateway runs `--cache-reuse`, so a turn can reuse a matching leading prefix
instead of re-prefilling it — but only if that prefix was primed first, and nothing did that
after a restart (residency's restore only undoes *same-process* evictions; its keep-hot set is
empty on a fresh boot, and an on-demand load is bare). The **WarmKeeper** fills the gap: a
detached boot + interval reconciler that keeps the model `agent.turn` routes to (when it's
local) resident **and primed**. It primes by issuing a throwaway turn down the **same path a
real turn takes** — `router.converse("agent.turn", …)` with jerv's persona + tools + the
resolved effort — so the primed KV prefix is byte-identical to what a real turn sends and the
reuse actually lands. That call also loads the model on demand through residency, so one prime
both resides and warms it.

> **The disk KV cache is BACK (v2, `jbrain.llm.kv_prefix`) — rebuilt 2026-08-23 against the
> reasons v1 was removed.** v1 (removed 2026-08-21) never worked on either family this box
> serves: on a HYBRID (every Qwen3.8 27B entry) the restore path calls `prompt.clear()`,
> wiping the context checkpoints that are a recurrent model's only prefix-reuse mechanism —
> inert by construction — and on gpt-oss it wrote whatever held the single slot at save time
> (measured **2,164 tokens** against a ~36k prefix, because the analysis tasks routed there
> and evicted jerv between the prime and the save). v2 answers each: hybrids and speculative
> entries are refused up front (`--slot-save-path` is only rendered for attention models;
> the catalog's `kv_slot_restorable` flag can override that per entry but **no shipped
> model sets it** — see the box below);
> a save happens ONLY when a slot's `n_prompt_tokens` exactly equals the prime's own
> `usage.input_tokens`, read in the same breath as the prime, and the server's `n_saved`
> must agree or the file is deleted; a restore's `n_restored` is verified against the same
> count and floor before it is trusted, falling back to the prefill otherwise. Files are
> fingerprinted over the model's rendered launch line + persona + tool schemas + the
> agent task's reasoning effort (any change that could stale a slot moves the filename —
> the effort included, because the chat template renders it into the prompt's leading
> tokens: gpt-oss writes a literal "Reasoning: low" header), and the store holds the
> whole tree to a
> 25 GiB budget by evicting least-recently-USED files (a restore refreshes its file's
> clock) — so each config the owner flips between (interactive slot on/off, window) keeps
> its own ~2 GiB file and flips restore in ~100 ms instead of re-paying a ~2 min prefill.
> Files live under
> `/models/.kvslots/<model>/` — the one writable subtree in the local-llm service's
> otherwise read-only weights mount (a nested rw bind in docker-compose; the weights
> themselves stay untouchable by the inference process). The keeper restores before it primes (a cold prime becomes a
> ~1 s cache hit instead of a ~60 s prefill), saves after, and probes every settled tick so
> a single-slot clobber heals off-turn; the router restores inline before an agent turn
> that would otherwise re-prefill; the load-time warm also SAVES the slot after it returns
> (the only save moment a non-agent model gets); and the gateway's load-time warm restores first and
> renders the SAME prompt a routed turn sends (system + tools + the reasoning effort) —
> until 2026-08-23 it omitted the effort, primed a "Reasoning: medium" variant no turn
> ever used, and burned a full ~62 s prefill on every load while clobbering the freshly
> restored cache on a 1-slot server. Saves and restores land in Vitals as
> `kv_prefix_saved` / `kv_prefix_restored` rows with token counts and elapsed ms.
>
> **Restore does not reuse on a hybrid — the disk cache is gpt-oss-only in practice.**
> The qwen3.8 twins were opted in on 2026-08-23 and reverted on 2026-08-24 after a live
> A/B: a qwen reload took **176 s WITH the restore, identical to without**. The restore
> itself is fine — `kv_prefix_restored` fires with the correct token count in ~130 ms —
> but llama-server then re-prefills the whole prompt. Root cause (traced through
> `tools/server/server-context.cpp`): the restore sets `slot.prompt.tokens`, so
> `get_common_prefix` matches — but for a **SWA / hybrid / recurrent** model, reusing that
> state is gated on a **context checkpoint** (`slot.prompt.checkpoints`), and a slot-restore
> rebuilds the raw memory state WITHOUT registering one. The reuse path finds no checkpoint
> and hits `do_reset` (server log: *"forcing full prompt re-processing … likely due to SWA
> or hybrid/recurrent memory"*), so `n_past = 0`. gpt-oss (pure attention) needs no
> checkpoint, so its restore reuses directly — reload **43 s** (weights + a 1-token warm),
> turns restore in ~90 ms. In-session qwen reuse still works (checkpoints ARE created live
> during a conversation — that is why `f_keep ≈ 0.99` turn-to-turn); only the cross-load
> disk restore is inert. THE FIX is a llama-server patch that carries the **checkpoints
> themselves across save/restore** in a sidecar file (`<slot file>.ckpt`): the save handler
> writes the slot's live checkpoints, the restore handler reloads them, and the reuse path
> then finds exactly what a live session had. (A first attempt SEEDED a checkpoint at
> restore time instead; the engine's own trace showed it checked and rejected — the reuse
> bound is strict `pos_min < pos_min_thold` and the restored state is after-N, so a seeded
> tip checkpoint structurally can never qualify. Validated locally on a live hybrid
> (LFM2-350M) before shipping: a 2820-token restored prompt re-processed 4 tokens.) It
> can't ride a JBrain update — `llama-server` is the community
> `kyuz0/amd-strix-halo-toolboxes` binary, not a local build.
>
> **The patched-build path is committed, OPT-IN, default OFF — activated from the PWA.**
> `deploy/patches/` holds the patch (anchored, applied by `deploy/apply-llama-patches.sh`),
> and `Dockerfile.local-llm` has a builder stage that rebuilds llama-server with the patch
> applied, INSIDE the base image so it reuses the proven gfx1151 Vulkan toolchain. With the
> flag unset the image is byte-for-byte the stock base — zero change. **The builder checks
> out the commit the base image's own binary reports** (`llama-server --version`, read at
> build time; the pinned base is `b10615`/`f280b2698`, the digest validated live
> 2026-08-24 — smoke passed and the patched qwen reload measured ~14 s): the base links
> `libggml*.so`/`libllama.so` dynamically and we overlay only the `llama-server` binary, so
> any other commit ships an ABI-incompatible binary that crashes at model load. Both
> 2026-08-24 smoke failures were hand-pinned commits: `c060ca974` against a `571d0d540` base
> (with the compose-side default silently overriding a later correction), then a stale
> fallback against the auto-updated base after upstream changed its `--version` format to
> `version: 0.2.0-dev (build NNNN, commit sha)`. The builder parses both known formats; an
> unparseable version FAILS the build fast (a guessed commit just wastes a compile before
> the smoke test catches it), and `LLAMA_CPP_COMMIT` / `LOCAL_LLM_LLAMA_COMMIT` is empty by
> default — set it only to deliberately force a commit for an exotic base variant. The
> builder also repairs the base's own toolchain before compiling: the rolling image has
> shipped with its RPM DB claiming binutils installed while `/usr/bin/ld` was missing
> ("collect2: cannot find 'ld'"), so missing tools get a dnf install *and* a reinstall
> pass, hard-failing up front if still absent. The patch is applied by anchor and the apply
> script knows each historical wording of the restore path as an alternative anchor; if
> upstream rewords it again the build fails loudly and the update flow rolls back. The
> whole stage was validated off-box by running it verbatim (same script, same rolling
> image, x86_64) before it ever reached the box. **To activate: Ops → "Fast Qwen loads" toggle**, then Ops → Update — the
> owner runs this box with no terminal (CLAUDE.md #10), so the switch is a PWA setting
> (`local_llm_patch_restore_checkpoint`), not an `.env` edit. The setting drives the build:
> `update-inner.sh` reads it (`jbrain.cli local-llm-patch-restore-checkpoint`) and exports
> `LOCAL_LLM_PATCH_RESTORE_CHECKPOINT` for the compose build arg, so the toggle alone
> triggers the rebuild even when auto-update is off (rebuild-needed = auto-update OR patch).
> The update flow rebuilds llama-server (~20-30 min under `PULL_TIMEOUT_S`), and its smoke
> test rolls back to the stock base if the build or the binary fails — and on that failure
> the script clears the toggle back off (`set-local-llm-patch-restore-checkpoint off`), so a
> bad build turns its own switch off rather than rebuilding a broken engine on every future
> Update. Built and smoke-tested on the box 2026-08-24 (engine `b10612-758443071`, at the
> base's own commit). **The setting is also the qwen eligibility gate:** `KvPrefixStore`
> reads it at api startup (`patch_active`) and admits the qwen3.8 MTP-hybrids to the disk
> restore ONLY when it is on — no `kv_slot_restorable=True` flip in `local_catalog.py` is
> needed (the static flag stays off; the setting is the gate). Because the value is read
> once at startup, toggling it takes effect on the container recreate the same Update
> already performs. The launch line's side of the plumbing is STATIC, not toggled:
> `llama_swap_config` renders `--slot-save-path` for recurrent+MTP entries unconditionally
> (the flag alone causes no saves — the store's gate does), because the store resolves its
> save dir off the rendered launch line and, without the flag, silently declines — the gap
> that made the toggle inert on its first working engine (2026-08-24). Upstreaming the patch to ggml-org/llama.cpp
> remains the long-term home (retires the local build on the digest bump that carries it).
>
> **The in-RAM prompt cache is off too** (`-cram 0`). llama.cpp defaults `--cache-ram` to
> 8192 MiB, so every resident model silently reserved 8 GiB of host memory — and on Strix Halo
> host and device draw on one pool. The goal it lost to is CO-RESIDENCY: gpt-oss-120b and a
> Qwen3.8 27B held together with no swapping. MEASURED 2026-08-21 against the 124.0 GB GTT cap
> with the guard holding 6 GB back:
>
> | | resident | headroom |
> |---|---|---|
> | `--cache-ram` at its 8 GiB default | 109.3 GB | 8.7 GB |
> | `-cram 0` | 93.3 GB | 24.7 GB |
>
> 8.7 GB is one wrong estimate from the freeze this box has taken three times. The cost is
> real and accepted: an evicted conversation re-prefills instead of being restored from host
> RAM. Co-residency is what pays for it — a model that is never swapped out is a model whose
> slots are not being fought over. `local_catalog.CACHE_RAM_GB` is 0.0 to match, and the flag
> is off the operator allowlist so it cannot be turned back on without the budget following.

> **Model loads are a QUEUE — one at a time, across the whole box.** The runaway watchdog
> anchors its ceiling to a GTT baseline sampled when a load starts (`gpu_guard.guarded_load`),
> which only means anything if nothing else is still allocating. MEASURED 2026-08-21:
> gpt-oss-120b was 30 s into a reload — GTT at 36.4 GB on its way to a measured 69.24 — when a
> staged qwen3.8-27b-abliterated took that 36.4 as *its* baseline and set a ceiling of
> `36.4 + 24.1 x 1.75 = 78.6`. GTT then reached 79.9 — gpt-oss finishing plus abliterated
> starting — and the guard blamed the whole climb on abliterated and aborted it. Nothing ran
> away; the previous model was still arriving. The per-model lock does not cover this (the two
> models differ), so there is a second, global one.
>
> A false abort is not cheap: it unloads a healthy model **and** strands the weights it had
> already read in the page cache, which `read_memory_gb` counts as used — so the next load sees
> less headroom and is likelier to abort in turn. That ratchet is why the fix is serialisation
> rather than a wider multiple: the ceiling exists to catch an order-of-magnitude balloon, and
> loosening it enough to absorb a second model's allocation would blind it to exactly that.
>
> It is a queue and not a silent gate on purpose. A waiting load records
> `queued — waiting for <model> to finish loading` to `box_events` before it blocks, so the
> vitals surface names what is ahead of it. Staging a second model right after a first IS the
> co-residency workflow, the wait behind a 120B is minutes, and the owner drives this box
> through the PWA — a Load button that does nothing for that long is indistinguishable from a
> broken one. A queued load costs latency, never correctness: the pre-flight re-samples once
> the lock frees, so it is admitted against the box as it actually is after the model ahead of
> it has landed.

> **`--swa-full` on gpt-oss.** It was introduced as the precondition for a KV-slot restore
> doing anything on an interleaved sliding-window model; that feature was removed in v1 and
> rebuilt in v2 **for attention models** (see "The disk KV cache is BACK" above), and the flag
> stays because full history on those layers is what a long conversation needs — and it
> remains what lets a v2 restore land on gpt-oss. The original
> measurement, kept because it is what the `kv_full_history` budget doubling is derived from:
> a restore into a windowed cache reports full success and is then discarded — same
> token count, same bytes, same 0.3s, and llama-server re-prefills anyway. Measured A/B on the
> box: **69,373 ms without the flag, 194 ms with it.** The catalog carries it as
> `kv_full_history=True` on gpt-oss-120b, and the shared `_kv_gb` term doubles that model's KV
> to match — since the 2026-08-23 four-window remeasure the base coefficient is 5.01, so the
> doubled term is **≈10.0 GB per 128k** (it was 4.5 → 9.0 when this note was first written) —
> so the memory meter, the eviction budget **and the load pre-flight** stay honest. The
> pre-flight used to skip the doubling: against a model that measured 69.24 GB it reserved
> 64.0 GB, corrected to 68.55 (2026-08-18 figures at the old 4.5 coefficient; the 5.01
> remeasure lifts the reservation ~1 GB further). The
> lever that pays for it is the **context window**: KV scales linearly with `-c`, so serving at
> 64k costs exactly what 128k did before the flag.

> **The numbers above are gpt-oss. Here are the MEASURED ones for a hybrid** (Qwen3.8-27B
> `qwen35`, 262k window, on this box, 2026-08-18). Qwen3.8 runs 48 of its 65 layers as Gated
> DeltaNet — linear attention carrying a recurrent state — and the Nemotron Lightning entry is
> a Mamba-2 hybrid of the same shape.
>
> | what | measured |
> |---|---|
> | Warm prime (checkpoint hit) | **0.99 s**, 32,485 of 32,489 prompt tokens reused |
> | Cold prime (no checkpoint yet) | **~101 s** at 32k window, ~220 s at 262k |
> | Prefill throughput | **~243 tok/s** |
>
> **Prompt caching WORKS on a hybrid.** Steady state is a second and ~99.99% reuse. An earlier
> version of this section claimed the opposite; it was wrong, and the two mistakes behind it are
> worth keeping because both are easy to repeat:
>
> - **`n_prompt_tokens_cache` in `/slots` is zeroed when the slot is released** (llama.cpp
>   `server-context.cpp`, `reset()` does `stats = {}`). It is a snapshot of the RUNNING task, so
>   polling it after a request always reads 0 — whether reuse was total or nonexistent. Use
>   **`timings.cache_n` in the completion response body** (copied before release) or the
>   cumulative `llamacpp:prompt_tokens_cached_total` from `/metrics`. This is the same trap as
>   `/props`'s dead `speculative.types` field, one layer along.
> - **~243 tok/s prefill is this hardware, not a fault.** Published figures for a 27B-class Q4 on
>   Strix Halo Vulkan are 250–330 tok/s, and 243 tok/s is ~44% of the 8060S's 29.7 TFLOPS FP16
>   peak — high utilisation for llama.cpp on an iGPU. A cold 24.5k-token prefill costing ~100 s is
>   arithmetic, not a bug. Linear attention is also FLATTER with depth than dense attention
>   (263→260 tok/s from pp2048 to pp8192, against a dense model's 884→490), so the hybrid helps
>   here rather than hurting.
>
> **Context checkpoints are the prefix-reuse mechanism, and for a hybrid they are the ONLY one.**
> Not a rollback feature that happens to exist. A recurrent model's `pos_min` is always ≈ the end
> of the sequence, so llama.cpp enters the checkpoint branch on every request; with no matching
> checkpoint it logs `forcing full prompt re-processing due to lack of cache data` and reprocesses
> from zero (upstream: discussion #19264, closed "It's already implemented"; PR #20288 exists so
> hybrids get "near-zero prompt re-eval", citing Qwen3.5-35B).
>
> Consequences that are easy to get backwards:
>
> - **Sweeping `--ctx-checkpoints` proves nothing on its own — RESOLVED, and the reason matters.**
>   An earlier sweep found 2 and 8 identical (101.26 s vs 101.08 s) and concluded nothing was
>   being restored. The count was never the whole setting: `--checkpoint-min-step` defaults to
>   **8192**, so 8 checkpoints forced 8192 tokens apart still could not cover a 33k conversation
>   densely enough to catch a divergence near its end. Raising the count while leaving the step
>   alone is inert *by construction*. **Raise them together.**
> - **We now serve `--ctx-checkpoints 16` + `--checkpoint-min-step 1024` — on MEASURED models only**
>   (llama.cpp's own
>   defaults are 32 and 8192). MEASURED on the box against a real 33k-token conversation: every
>   turn used to re-prefill the whole prompt — 33,648 tokens, 232 s, repeatedly — and now
>   processes only its delta, 814 tokens in 8.4 s and 1,084 in 15.9 s. `erasing old context
>   checkpoint` went from 12 occurrences to 0. First-response time went from ~4 minutes to
>   10-25 s.
> - **Checkpoints are HOST RAM, not device memory.** `common_prompt_checkpoint` holds
>   `std::vector<uint8_t>` buffers. Confirmed here: going from 2 to 16 left GTT at 26.21 GiB,
>   unchanged. On unified memory it still comes out of the one pool and the budget counts it, but
>   it does **not** add to the GTT-cap pressure that is this box's hang mode — so the count is a
>   cheaper knob than it was documented to be. Size, measured: **275-284 MiB** each for the
>   hybrid 27B, against a catalog figure of 150 MiB taken from upstream #27211 for a different
>   quant.
> - **The raise is gated on that measurement, not applied flat.** Checkpoints are created for a
>   hybrid, or an SWA model served without `--swa-full` (that flag zeroes `n_swa`, so gpt-oss
>   creates none at all). The Nemotron hybrid (`nemotron-3.5-lightning-30b`, the only one left
>   in the catalog) qualifies and is still unmeasured (`checkpoint_gb=0.0`), and it runs
>   **two slots** — so a flat raise would have put 32 checkpoints of unknown size on the box
>   against a budget of zero. A model earns the higher count by having its cost measured;
>   everything else stays at 2. Measure the Nemotron and it picks up the raise with no code
>   change beyond the catalog number.
> - **Both flags ARE settable live**, via `PUT /api/debug/llm/local-models/{id}/extra-args` — no
>   deploy. That is how the above was measured before it became the default.
> - **`--cache-reuse` is not merely a no-op on a hybrid — it is a hazard.** Its partial-range
>   `seq_rm` returns false for recurrent memory, which reaches `GGML_ABORT`, i.e. the server dies.
>   On an identical prompt the reuse loop never executes, which is why we have not seen it.
> - **A slot restore could never deliver prefix reuse on a hybrid** — one of the reasons the
>   v1 disk KV cache was removed, and why the v2 rebuild refuses hybrids up front (see "The
>   disk KV cache is BACK" above). Restore calls
>   `prompt.clear()`, which clears the checkpoints, so a disk-restored slot full-reprocesses its
>   next request. The KV-slot cache is a gpt-oss win; on a hybrid it loads bytes that buy nothing.
> - **`--swa-full` is inert here** — llama.cpp auto-disables it when `n_swa == 0`, which holds for
>   `qwen35`. Correctly unset on every hybrid entry.
>
> **What actually costs you a cold prefill, then, is anything that INVALIDATES the checkpoint.**
> Known candidates on this box, unproven in ranking: a background task landing in the same slot
> (a request for a second slot now drops speculation instead of being clamped away, so there IS
> one to absorb it if the operator asks; the chat auto-titler that used to be the loudest such
> task is gone — jerv names its own chat in-turn via `name_session`); an aborted request, since a prefill cut short by a client
> timeout appears to leave no committed checkpoint and the next turn starts cold again; and a
> prompt whose leading bytes moved.
>
> ⚠️ **The debug console cannot observe a cold prime.** The reverse proxy in front of this box
> times out around 100 s and returns 524, and `/upstream/…` has its own hard 180 s ceiling
> (observed: `POST …/v1/chat/completions 502` at exactly `3m0.003s`). A cold prime exceeds both,
> so the client gives up while the server keeps working — and on a one-slot model the next request
> queues behind a task that may already have been dropped. Measure cold behaviour from the gateway
> log and `/metrics` deltas, never from the debug client's own wall time.

The cache is keyed by FILENAME — the digest described above (prompt, tool schemas,
`build_info`, chat template, `n_ctx`/`n_slots`, the launch flags, and the UTC date only for a
template that renders one). Anything that changes the bytes changes the name, the restore simply
misses, and the keeper falls through to a normal prefill.

It also listens to the residency coordinator: an eviction, or the bare
reload the end-of-turn restore does, reports the dropped prefix so the keeper re-primes on its
eager cadence. Without that edge it only noticed a lost prime when a tick happened to *observe*
the model missing — so an evict and its restore that both landed inside one interval (an image
render, a code-mode toggle) left the keeper reporting settled while the next chat turn paid the
full cold prefill in the foreground. It only ever *adds* that one model, under the same free-RAM floor
and code-mode hold as everything else, and reconciles on an interval so it self-heals after an
app restart, an update (fresh container), or a standalone gateway (llama-swap) restart.

**Installing a model from the PWA makes it the active model.** When an update installs a
model the operator queued, the model sync re-points `agent.turn` at it (`jbrain.cli
local-activate`, called from `deploy/local-models-sync.sh`), so the just-installed model
becomes the box's active chat model and the WarmKeeper above keeps *it* hot — instead of
leaving whatever the post-update smoke-test probe loaded (e.g. gpt-oss-120b) resident. It
activates the most-recently-queued installed model, with the same reasoning-effort gating the
Settings screen applies; a routine or uninstall-only update leaves routing untouched. Change
your active model any time in **Settings → LLM routing** (or the debug console's `llm-set`).

Two subtleties the prime must respect, both learned the hard way:
- **Match the tools exactly.** Under the gateway's `--jinja` the chat template renders the tool
  definitions into the prompt's *leading* tokens, so a persona-only warm diverges from a real
  tool-carrying turn before the reusable prefix ends and `--cache-reuse` salvages almost
  nothing. The prime sends the same tool schemas a real turn does.
- **Re-prime on a liveness flip.** The primed tool set depends on ComfyUI liveness (the
  image-gen tools are hidden when it's down). A prime taken at boot while ComfyUI was still
  unreachable hides those tools, but a real turn once ComfyUI is up shows them — a mismatch that
  silently defeats the reuse. So the keeper keys its "already primed" state on
  `(model, hidden-tool-set)` and re-primes when the hidden set changes, self-correcting once
  liveness settles. The manual **Load** button primes the same persona+tools shape too.

**Per-model context windows survive a deploy (the deploy re-stamp applies overrides; boot reconcile is the backstop).**
Each on-box model has a **context-window** picker (Settings → LLM); the chosen value is persisted
(`app.settings → llm_local_context_windows`) and stamped into the gateway's `-c`. The **deploy**
re-stamp (`deploy/local-models-sync.sh` → `python -m jbrain.llm.llama_swap_config`) used to
regenerate `llama-swap.yaml` from the **base catalog** with no overrides — so every update silently
reset a raised window to its catalog default. That bit twice: Nemotron 3.5 Lightning (32k base,
raised to 500k) reset to 32k, and Qwen3.8-27B-Q4 (32k base, raised to 128k) reset to 32k while the
meter still showed 128k — the model then overflowed ("this model ran out of context") at a
displayed ~25%, because the agent's persona + ~39 tool schemas is already ~33k tokens. Now
`llama_swap_config._main` **loads the saved windows/slots from the settings store (`_saved_overrides`)
and applies them itself**, so the deploy re-stamp is correct at write time — it no longer depends on
the api restarting (a model-only sync's `up -d` often doesn't restart it). `reconcile_gateway_windows_on_boot`
remains as a **backstop**: idempotent (a no-op when the config already matches, so a plain restart
keeps its warm model), best-effort (a missing weight or down gateway never blocks startup), and it
evicts the affected resident models to reload at the corrected `-c` only when the served config
actually changed.

**Recommended on gpt-oss: a dedicated interactive slot (Settings → LLM → On-box models).** This
used to read "optional", on the reasoning that small background completions don't evict a large
prefix. MEASURED 2026-08-21, and they do: on a single-slot gpt-oss the primed slot was found
holding **2,164 tokens** against a ~36k jerv prefix, because `note.extract`,
`entity.disambiguate` and `fact.adjudicate` all route to that model and take the one slot in
turn. It mattered less while `--cache-ram` was on, which restored the evicted conversation from
host RAM; with the prompt cache off (above) an evicted prefix is a re-prefill. The 16 GB the
cache used to take across a co-resident pair is roughly what a second gpt-oss slot costs, so
this is the same budget spent on the thing that actually helps. Each on-box model has an **interactive
slot** toggle beside its context-window picker: turning it on gives that model llama-server
`-np 2` (two KV slots). llama-server routes each request to the slot with the longest matching
prefix, so jerv turns keep their primed KV in one slot while title/background traffic uses the
other — neither can evict the other's cache. The cost is KV RAM: a second slot **doubles** the
model's KV (the meter and the residency eviction budget both account for it), so `-c` is set to
`window × slots` to keep each slot at the full window. Editable only while the model isn't
resident (a running process can't add a slot live); the change unloads it so the next request
reloads with the new `-np`.

**Code mode reserves the box (exclusive while ON).** Two ~60 GB models — gpt-oss-120b and the
Qwen3-Coder-Next coder — can't safely co-reside on 128 GB, and the swap between them has a
load/unload memory transient the 15% floor doesn't cover. A background deep-research load
contending with a freshly-opened jcode session drove the box past physical RAM and earlyoom
took down a process. So while **code mode is ON**, it reserves the box for code mode's own
models: the jcode power toggle writes a `code_mode_hold_name` flag holding the served names of
**both** the coder (executor) **and** the plan subagent's model (so jcode's own plan↔execute
swap isn't refused). While it is set (1) `residency.ensure_room` **refuses to load any model
outside that set** unless already resident — nothing evicts code mode's models or co-loads a
second large model — and (2) the **worker pauses** its job loop and scheduler tick, so no
autonomous research/ingestion starts and contends. Chat, vision, and background jobs are
declined with "code mode is holding the box — turn it off" until you toggle code mode OFF, which
clears the flag and re-warms the general hot set (gpt-oss + vision). This makes the
contention/OOM impossible by construction rather than relying on the swap transient staying
under the floor. The flag is **cleared at API startup** so a crash/reboot mid-session (earlyoom,
a redeploy) can't strand it — a stale hold would otherwise wedge the box (all loads refused,
worker paused) while the launcher, which reads live service state, shows code mode OFF. Both the
residency and worker reads **fail open** (a settings-read hiccup lifts the reservation rather
than wedging on it), so a full DB outage — which also stops the job queue — is the only case
that drops protection.

The reservation stops *new* contention, but a report can already be **mid-generation** on
gpt-oss when you open code mode. Warming the coder unloads gpt-oss out from under it, so the
launcher surfaces that turn instead of killing it silently: `/jcode/power` reports `active_turn`
— true when a worker job is **running** AND a non-coder model is **resident** (precisely the turn
the coder swap would end; a job on the separate embed/TEI service leaves nothing reasoning-resident
and isn't flagged). When it's set, toggling code mode **on** holds at a confirm gate ("a
`<kind>` turn is running … activating code mode ends it") before starting anything. On confirm,
`set_power` **cancels** that turn — `queue.cancel_running` marks it terminally failed with
`attempts` forced to the ceiling, so the worker's own mid-call failure handling can't requeue it
(`queue.fail` has no `status='running'` guard). A cancelled report does **not** resurrect (matching
the deliberate "end it" choice); self-healing kinds (ingest/embed/integration) are re-enqueued by
their reconcile sweeps regardless. Cancel is scoped to the disruptive turn only, so a background
embed/ingest job on the TEI service is left to finish.

✅ **Checkpoint:** `jbrain status` shows `local-llm` running; `jbrain logs
local-llm` shows llama-swap listening and the resident models loaded.

> **Known caveat — gpt-oss-120b on Vulkan.** MXFP4 is supported on the Vulkan
> backend, but gpt-oss-120b has a reported Vulkan KV-cache OOM
> ([llama.cpp #15120](https://github.com/ggml-org/llama.cpp/issues/15120)) on
> some setups despite free memory. If it fails to load, either reduce its
> context in the generated `local-models/llama-swap.yaml` (add `-c 8192`), or
> switch the gateway to the ROCm fp4 base (below), which is the better fp4 path.
>
> **Known caveat — gpt-oss tool-calling grammar (JSON-Schema `enum`).** gpt-oss's
> harmony tool path (llama.cpp `--jinja`) builds a GBNF grammar over the tool union,
> and a JSON-Schema `enum` on a property of a many-optional-property tool object
> deterministically segfaults the upstream (turn returns HTTP 500). Bisected via the
> debug `tool-probe` (`docs/runbooks/DEBUG_ACCESS_SESSION_GUIDE.md`) as the
> enum × full-optional-field-set interaction — not tool count, byte size, or
> non-ASCII text. **When authoring a `.tool` sidecar that may be served by gpt-oss,
> keep allowed values in the description and validate them in the handler rather than
> using `enum`** (there is a regression test pinning `analyze_stream` enum-free).
> Cloud models and Qwen are unaffected.

## Phase 7 — Route tasks to local (in the UI)
Open your domain → paste the owner key (`jbrain reset-owner-key` to mint a new
one) → **Settings → LLM**:
- **Vision** → `Qwen3-VL 30B` (OCR/captions run on-box; text-only models are
  filtered out of this tier).
- **High-stakes reasoning** → `GPT-OSS 120B`.
- Leave the rest on cloud or go fully local — per task, your call.

### Adding / removing models later — from the PWA, no shell
Once hosting is on, **Settings → LLM → On-box models** lists the whole catalog,
not just what's provisioned. Each un-provisioned model (e.g. **Qwen3.8 27B** at
Q8, ~28 GB) has an **Install** button. Tapping it **starts the download
immediately** — a dedicated weight-sync one-shot, **not** a system update: it
checks free disk, pulls the queued weights, adds them to `LOCAL_MODELS`, re-stamps
the gateway config, and restarts the gateway — the same provisioning `enable-local-models`
does, but with no `git pull` or image rebuild. The **disk check refuses** rather than warns
(catalog `size_gb` for the queued models plus a 10 GB margin) and leaves the queue intact,
so freeing space and waiting for the next sync is the whole retry. It had no check at all
until 2026-08-19: the guarded path was `scripts/local-llm-setup.sh`, a shell script the owner
cannot run, so the product pointed them at the unguarded one — queueing the ~85 GB Q8 coder
with 40 GB free filled the filesystem that also holds the database, the blobs and the
backups, and surfaced only as `hf` errors in the provision log after the fact. The drawer follows it live (a
per-model GB bar reading the bytes on disk); the coarse phase and the verbose
per-model download log stream into the queue banner. **Removing** is symmetric:
an installed model's **Uninstall** button (on the Installed or Catalog tab) applies
through the same sync one-shot, dropping it from `LOCAL_MODELS` and pruning its
weights. A large model that can't co-reside (e.g. the ~85 GB Q8 coder) evicts
down to at most a small low-tier model when loaded — it runs effectively
standalone with the box to itself. Going the other way,
**Qwen3-VL 30B at Q4_K_M (~18 GB)** sits in the catalog beside the recommended Q8 vision
model as a memory-saver twin: half the weights, so it co-resides with gpt-oss-120b under the
free-RAM floor instead of evicting it — at some OCR-fidelity cost on dense/small text (its
vision projector stays F16, so the fine-text hit is limited). Install it when co-residence
headroom matters more than the last bit of transcription accuracy. First-time host prep (GPU GIDs, the gateway image,
kernel params) still needs Phases 1–6 on the box; the PWA path only *adds/removes
models* on an already-enabled stack.

**Red-team probe — `Qwen3.8 27B · abliterated`.** An opt-in catalog entry (`qwen3.8-27b-abliterated`,
Q4_K_M, ~16.5 GB) carrying an *abliterated* build of the same Qwen3.8-27B the aligned twins serve:
the refusal directions are edited out of the residual stream, so it answers prompts the aligned
model declines. It is here to **exercise the sandbox's own controls** — checking what the guardrails
around the model catch when the model itself catches nothing — not to do work. Install and uninstall
it from the same **On-box models** drawer as anything else; nothing routes to it until you point a
task at it, and it is never in the recommended set.

Two things to know before you point anything at it:
- **It ships its own system prompt, and you cannot turn it off.** The chat template baked into the
  GGUF prepends a "task-execution machine / never refuse, no pushback" block **above** whatever
  system message JBrain sends, on every turn. There is no API flag to suppress it — it is in the
  weights file. So (a) selecting this model for a real task silently displaces JBrain's own
  prompting, and (b) the vendor's 2.4% residual-refusal figure is measured *with* that prompt in
  place, not for the weights alone. Read any result you get as "model + jailbreak prompt".
- **Vendor-labelled experimental.** Same serving shape as `qwen3.8-27b-q4` (dense 27B, text +
  vision, MTP self-speculation, hybrid thinking, 262k native window), so it co-resides beside
  gpt-oss-120b — but it is a modified research checkpoint, not a released model. If a load
  misbehaves, fall back to `qwen3.8-27b-q4` and compare.

Its thinking level is not optional here the way it is elsewhere: this template **raises** on a
level outside `low` / `medium` / `xhigh` rather than ignoring it, and defaults to `xhigh` when sent
none. The catalog's level map already sends exactly the three it accepts.

**MTP (faster generation) is a serving MODE, not a separate entry.** There is no
`Qwen3.8 27B · MTP` to select: that entry was retired (it was a byte-identical duplicate of the
Q4 twin differing only in flags), and every Qwen3.8 entry — Q8, Q4 and the abliterated probe —
now serves with llama.cpp multi-token prediction (`--spec-type draft-mtp`), self-speculation off
the MTP head the GGUF already bakes in. Published Strix Halo measurements put it at **1.8–2.4×
decode** on a dense 27B; measured here, 22.41 t/s against ~11–12 unspeculated.

**Why self-speculation and not a draft model.** Every published "use a small drafter" recipe
assumes a discrete GPU. Here the drafter and the target share ONE memory bus, so a separate
drafter reads its own weights across the exact resource the target is already bandwidth-bound
on. Measured on this hardware class, MTP beats a separate-drafter method (DFlash) by 30–67%
*despite* DFlash being the better algorithm on paper. Prefer serving modes that add no weights.

Three things to know:
- **Single-slot.** llama.cpp's speculative path serves one sequence, and draft acceptance
  collapses as concurrent sequences rise (reported on this exact gfx1151 SoC). The gateway
  **clamps `-np` to 1** for any `--spec-type` model, so a saved slot override cannot break it —
  which also means this entry can never hold the interactive keep-warm second slot.
- **Decode only, and it needs length.** Prompt processing is a touch slower, and the gain needs
  a few hundred output tokens to pay for its overhead — a short tool call sees little or none.
- **`--spec-draft-p-min` is UNSET, deliberately.** The catalog pins only `--spec-draft-n-max 3`
  (llama.cpp's default and the Strix Halo consensus). This runbook previously said the entry
  pinned `p-min 0.6`; it never did after that value was removed, because the source recommending
  it said in its own words "sweep empirically; do not adopt without testing". So the gate runs at
  llama.cpp's `0.00` (ungated). Gating is specifically a bandwidth-starved-machine win and is
  worth sweeping — it is on the extra-args allowlist for exactly that — but treat any acceptance
  or throughput number taken before the sweep as an UNGATED measurement.

> ### ⚠️ Reading the memory instruments correctly (this cost three power cycles)
>
> **`mem_info_gtt_used` is DEVICE-WIDE, not per-process.** It reads
> `ttm_resource_manager_usage()` and sums every client on the card. Attributing it to one model
> is wrong, and it produced two false conclusions here: a load was read as costing 86 GiB when
> the real figure was ~19 GiB and the rest was another model the residency restore had put back
> underneath the measurement. For per-model attribution use `/proc/<pid>/fdinfo/<drm-fd>` →
> **`drm-resident-gtt`**, or `amdgpu_top`'s process view.
>
> **What the freezes actually were — REWRITTEN, because the first explanation was wrong.**
> Three power cycles. The story told here for a while was memory arithmetic: a
> projector-carrying model loaded onto a box whose headroom looked fine because the estimate
> left out a "~33 GiB GTT balloon". That number came from back-solving one freeze against
> llama.cpp **#27146** — a report on a **different GPU** (Radeon 890M / gfx1150 / 32 GB, not
> this box) quoting **`total-vm`**, i.e. virtual address space, with `anon-rss: 4 kB`. It was
> never a measurement of resident memory, and the arithmetic built on it never added up: with
> the real load-time cost (~4 GiB of warmup buffer), freeze #2 was ~90 GiB against a ~124 GiB
> pool, which does not freeze anything.
>
> **The mechanism is the host's GTT configuration, not any single model.** Phase 5 used to set
> `ttm.pages_limit=32505856` — **124 GiB, i.e. ~100% of system RAM.** That matters because of
> how the kernel behaves (all verified in v6.18 source):
>
> - GTT pages are allocated `GFP_HIGHUSER` — unmovable, on no LRU, **not reclaimable**.
> - TTM registers **no shrinker for live BO pages**, only for its free-page cache. The kernel
>   cannot reclaim a loaded model's GTT no matter how much pressure it is under.
> - `amdgpu_bo_create()` sets `.gfp_retry_mayfail = true` — *"We opt to avoid OOM on system
>   pages allocations"*. Per the kernel's own memory-allocation guide, that flag means **the
>   OOM killer is not called.**
>
> So when GTT allocation exhausts RAM, every task enters direct reclaim, finds nothing
> reclaimable, and cannot escalate to an OOM kill. Nothing dies; the machine simply stops. The
> one mechanism that prevents this is `ttm.pages_limit`: exceeding `gtt_total` returns a clean
> `-ENOSPC` **before** the pages are requested. Setting it to 100% of RAM removes that
> backstop, and on kernel 6.18 nothing caps it for you (the physical-RAM sanity cap first
> appears in v7.2).
>
> **Action:** `ttm.pages_limit` should be about `MemTotal − 16 GiB`, not 100%.
> `scripts/strix-halo-host-setup.sh` now DERIVES it that way rather than hardcoding the 124 GiB
> that disabled the backstop. ⚠️ But it is a kernel command-line parameter, and the script
> respects an existing value rather than overwriting it — so **a box already carrying the old
> `ttm.pages_limit=32505856` keeps it until someone edits `/etc/default/grub` and reboots.**
> That is a HOST step the owner cannot perform (CLAUDE.md #10), and it remains the gap to
> design out: the update path should set and verify it, and Ops should surface the live value
> so a wrong setting is visible rather than latent.
>
> **Expected footprint is the check to apply.** A 15.9 GiB Q4 model at 32k should land near
> 19–20 GiB (weights + ~2 GiB KV across the 16 attention layers + ~150 MiB recurrent state +
> ~1 GiB compute). Published runs put this same model at 262k context inside 24 GB. If a
> reading is multiples of that, suspect the instrument before the model. The gateway tracks llama.cpp master (see "Tracking newest llama.cpp" below), so after an
update confirm it loads and generates; on a bad build fall back to `qwen3.8-27b-q4`.

> **Reading what the engine actually did.** llama-swap's own log is a ~100 KB ring buffer the
> access log floods within minutes, and `logs local-llm` is that same stream — but
> llama-server's stdout now **has** a debug surface: `GET /api/debug/llm/upstream-logs`
> (`debug-connect.sh upstream-logs`) reads the history burst llama-swap replays on
> `/logs/stream/{stream}` — the startup banner, per-request prompt-eval throughput,
> context-checkpoint evictions, a failed load's reason. Caveat: at the default verbosity 3
> (we pass no `-lv`) the model LOADER prints nothing — a load is a ~1.4 s silent gap with no
> per-buffer memory breakdown; a load's memory is measured by the device delta instead. For
> structured reads use the passthroughs below, all of which refuse a non-resident
> model (a diagnostic must never trigger a load — that is what froze the host):
>
> | want | command |
> |---|---|
> | build id, real `-c`, `total_slots` | `debug-connect.sh props <id>` |
> | is speculation actually drafting | `debug-connect.sh slots <id>` |
> | tokens per forward pass, decode t/s | `debug-connect.sh spec-metrics <id>` |
>
> The `--slots` and `--metrics` flags the config always passes exist for the last two. For a
> long stretch the flags were passed but the ROUTES were missing, so MTP could only be judged
> by wall-clock timings — which is how an entire investigation concluded MTP was off from a
> field (`/props`'s `speculative.types`) that reads "none" on every build.

### Vision + MTP in one model — measured, and it works

`qwen3.8-27b-q4` serves the projector and the MTP head together (MTP is a serving mode
of that entry, not a separate model). Measured on a genuinely
empty box (GTT floor 0.14 GiB, nothing else resident, auto-reload off):

| | measured | note |
|---|---|---|
| load | **19.07 GiB** | predicted 20.59 — over by 1.5, the safe direction |
| image encode (2.1 MB) | **+0.11 GiB** | identical to the q4 twin |
| peak GTT across the test | 19.19 GiB | flat, no balloon |
| speculation | 1.8 tok/step, 17.54 t/s | unchanged by the projector |

So one model gives vision AND ~2x decode in the memory the q4 twin uses for vision alone.

**This entry was text-only for a long time on a belief that turned out to be false** — that an
mmproj beside the MTP head balloons GTT (llama.cpp #27146). It does not, on this box, in any
configuration. Every observation behind that belief was a MEMORY COLLISION misread as an
interaction: two freezes with ~67 GiB of gpt-oss still resident, and later a guard abort
reporting "GTT 59.8 GB" that was the primary model reloading *underneath* the measurement.

That last one is the trap worth remembering: **`gpu_guard` reads GTT device-wide, not
per-process**, so anything loading concurrently lands in the reading and is attributed to the
model being loaded. A measurement taken while the box is not exclusively yours is not a
measurement of your model. None of the earlier attempts ever had the box to themselves, and
holding it empty only became possible once the warm keeper started honouring the auto-reload
switch.

### Two things auto-load models, and the switch governs both

Settings → LLM's automatic-reload switch has to be read as covering **two** independent paths,
because for a long time it only covered one:

| path | when it fires |
|---|---|
| residency restore | end of turn, putting back what a displacement evicted |
| **WarmKeeper** | every 5s while the primary local model is wanted but not resident |

The keeper used to ignore the setting entirely. Turning auto-reload off stopped restores while
the keeper went on reloading the router's primary model on its eager cadence — so an operator
who unloaded a 68 GiB model watched it reappear within five seconds, repeatedly, with the UI
insisting automatic reloading was off. It also made the box impossible to hold empty, which
matters because "load one model on an otherwise empty box" is the shape of every memory
measurement here.

Both now read the same switch. The keeper's gate is on LOADING only: a model that is already
resident still gets primed, because holding a warm prefix costs no memory and dropping it would
make the first turn slow for nothing.

To hold the box genuinely empty — for a load measurement, or to test a model in isolation —
turn automatic reloading OFF first, then unload. Otherwise the keeper wins.

### Tuning how much of an image the model actually sees

The floor is a catalog FIELD (`image_min_tokens`), set per model like `context_window` and
overridable per model in the PWA. `--image-max-tokens` is the ceiling
(llama.cpp defaults to 4096 for this projector family; the catalog pins only the floor, at
2048). Both are on the `extra-args` allowlist, so they tune live:

Measured on a bottle label carrying fine print, three reads per floor:

| floor | result |
|---|---|
| 1024 | 1 of 3 usable; the others invented company names (`FANTASY SODA CO.`) |
| **2048** (catalog default) | 3 of 3 read the core label — volume, units, product name |
| 4096 | 3 of 3 read the core label AND promotional small print the lower floors could not resolve at all |

Higher was strictly better on this image: 4096 lost nothing and added real text. The shipped
default is **2048** — the point where the core label became reliable rather than lucky. 4096 is
left as a per-model opt-in: it read more, but the evidence is one photo and the extra prefill
would be spent on every image turn. The consistency is the
tell — at 1024 the wrong reads DIVERGED (a different invented company each time), which is what
confabulation looks like; at 4096 three independent reads AGREED on the same fine print, which is
what reading looks like. Judge a vision change by whether repeats converge, never by one sample.

One artifact no floor fixes: every read across all three floors dropped a leading letter
(`ATURALLY FLAVORED`, nine times of nine). Stable misreads are not a resolution problem and
will not tune away.

**In the PWA:** Settings → LLM, the **image detail** control on any vision model. It sits
beside the context window, takes effect on the model's next load, and shows the catalog's own
floor marked `(default)` so picking that stores no override. Text-only entries have no such
control — a floor there would never be read.

For a quick sweep without touching saved settings, `extra-args` does the same thing and clears
in one call:

```
debug-connect.sh extra-args qwen3.8-27b-q4 --image-min-tokens 2048
debug-connect.sh vision <attachment_id> --task vision.ocr --max-tokens 600
debug-connect.sh extra-args qwen3.8-27b-q4          # no args = back to the catalog
```

Raise the floor when small text in a photo comes back garbled — a curved bottle label, a
receipt, a screenshot of a table. The cost is prefill time and KV, not weights, so it does not
move the load footprint; it shows up as a slower first token on image turns.

Two cautions when reading the result. Setting `extra-args` RESTARTS the model, so the prefix
cache is cold and the first call afterwards is slow for reasons unrelated to the flag. And
these are hybrid-thinking models: a caption that comes back EMPTY usually means `max_tokens`
was spent on the reasoning block, not that vision failed — check `reasoning_chars` in the
`llm.complete` log line before concluding anything, and give it 600 tokens rather than 150.

### What the speculation numbers mean, and what to optimise

This build of llama-server exposes **no draft/accept counters at all** — the metric set is
prompt/predict/decode totals and nothing else. The acceptance rate cannot be read directly, so
`spec-metrics` derives it:

`tokens_per_step` = `tokens_predicted_total` / `n_decode_total`. A forward pass on this box
costs one full read of the weights, so tokens emitted per pass IS the speedup. 1.0 means
speculation is doing nothing.

Both figures are **process-lifetime totals**. For one request, read before and after and divide
the deltas — otherwise warm-up and every earlier request are folded in.

**Optimise `tokens_per_second`, never `tokens_per_step`.** They disagree, and following the
wrong one leads the wrong way. Measured on an uncontended box, 400 output tokens, `pet.thought`:

| `--spec-draft-n-max` | tok/step @ ~30 tok ctx | t/s | tok/step @ ~8.6k ctx | t/s |
|---|---|---|---|---|
| 1 | 1.608 | 8.46 | 1.747 | 18.25 |
| **3 (default)** | 2.157 | 9.98 | 2.454 | **20.86** |
| 5 | 2.075 | 13.51 | **2.685** | 19.12 |
| 7 | 1.954 | 5.29 | 2.381 | 8.38 |

Read the long-context columns: those runs all hit the 400-token cap, so they share a
denominator. The short runs stopped naturally at 192-249 tokens, which smears per-request
fixed cost and deflates their t/s — do not rank on them.

Three things this settles:

- **n-max 7 is a 60% throughput loss** (8.38 vs 20.86 t/s) and is the one result far outside
  run-to-run noise. Whatever community reports say about drafting seven tokens ahead, it is
  wrong for this hardware.
- **n-max 5 drafts best and is still slower** — 2.685 tokens/step against 3's 2.454, at 19.12
  t/s against 20.86. Verifying a longer draft costs more per pass than the extra accepted
  token returns. This is why tokens/step is a trap.
- **Longer context HELPS acceptance**, at every setting (1.608→1.747, 2.157→2.454,
  2.075→2.685, 1.954→2.381). More context makes the next token more predictable. The intuition
  that speculation decays as the window fills is backwards here.

3 and 5 differ by ~9%, which is at the edge of the spread seen between repeat runs at a fixed
setting (1.81-2.02 tok/step), so treat them as tied and keep the default. Single samples per
cell; re-measure before acting on any difference this small.

The context effect keeps going, and it does not separate 3 from 5. At ~25k tokens — most of
the 32k window — both land in the same place:

| `--spec-draft-n-max` | tok/step @ ~25k | t/s |
|---|---|---|
| 3 (default) | 3.150 | 20.10 |
| 5 | 3.226 | 20.35 |

Acceptance rises monotonically with context at every setting tested (2.075 → 2.685 → 3.150 for
the default, at ~30 / ~8.6k / ~25k), but the two settings stay within 1% of each other at the
long end. Nothing here argues for moving off the default.

Measure with a WARM prefix. `n_decode_total` counts every `llama_decode()` call, prompt
processing included, so a cold 25k prefill adds ~20 batches to the denominator and understates
tokens/step. Send the request once to populate the prefix cache, then measure the second one.
A cold 25k prefill also takes ~270s at ~92 t/s prompt throughput — long enough that a client
timeout will report zero tokens against a nonzero decode count, which looks like a generation
failure and is not one.

### Tuning MTP without a release

Every step below runs from the PWA, the debug console, or an update — no shell (CLAUDE.md #10).
The flags are on the extra-args allowlist precisely so this loop exists.

| Step | How |
|---|---|
| Ship the catalog defaults + download the projector | **Ops → Update** (the sync detects the newly-required file and pulls it) |
| Confirm the build, real `-c`, and `total_slots` | `debug-connect.sh props <id>` — **on an ALREADY-RESIDENT model** (see the warning below) |
| Point a task at it | PWA **Settings → LLM**, or `debug-connect.sh llm-set <task> qwen3.8-27b-q4 <effort>` |
| Try a different draft setting | `debug-connect.sh extra-args qwen3.8-27b-q4 --spec-draft-p-min 0.75` |
| Measure it | `debug-connect.sh prime qwen3.8-27b-q4` → `elapsed_ms` |
| **See whether it is drafting** | `debug-connect.sh slots qwen3.8-27b-q4` → the per-slot `speculative` object |
| **See the speedup** | `debug-connect.sh spec-metrics qwen3.8-27b-q4` → `tokens_per_step`, `tokens_per_second` |
| Revert to the catalog | `debug-connect.sh extra-args qwen3.8-27b-q4` (no args) |
| Check vision still works | `debug-connect.sh vision <attachment_id> --task vision.caption` against **`qwen3.8-27b-q4`** — vision and MTP coexist, measured (see above); the old "never the MTP entry" caution referred to a retired entry and a belief since disproved |

> ### `props` reads, it no longer loads
>
> `props` reaches llama-server through llama-swap's `/upstream/<model>/` passthrough, and that
> path used to trigger **llama-swap's own on-demand load** — outside `jbrain.llm.residency`,
> which is the box's sole evictor and the only thing that checks whether a load fits. Calling
> it on a cold model beside a large resident one **froze the host to a power cycle**.
>
> It now refuses a model that isn't already resident, so the order is explicit:
>
> ```bash
> debug-connect.sh unload <the big resident model>   # goes through residency
> debug-connect.sh load <the model you want>         # goes through residency + the GPU guard
> debug-connect.sh props <that model>                # a pure read
> ```

### The device-memory guard

The free-RAM budget counts **system RAM**. A model's device buffers are **GTT** — system pages
the amdgpu driver pins — capped separately by `amdgpu.gttsize`/`ttm.pages_limit`. The two are
accounted apart and drift, which is how a load with 105 GiB free and a 21 GiB catalog footprint
still took the host down. `jbrain.llm.gpu_guard` closes that:

- **Pre-flight** — a load is refused when the device pool can't hold it while keeping
  `MIN_FREE_GTT_GB` (6 GB) back for the host.
- **Watchdog** — GTT is sampled every second *during* the load; a climb past
  `RUNAWAY_MULTIPLE` × the predicted footprint, or free GTT hitting the floor, **cancels the
  load and unloads the model**. This is the part that protects a model nobody has characterized:
  an estimate can only be wrong in ways we've already seen, and the first load of anything is a
  guess.
- **Post-load** — one more sample after the load returns, because a fast load can finish between
  two samples and an allocation can still be settling.
- **Measurement** — the real GTT delta is logged (`gpu_guard.measured_footprint`, and
  `residency.load_measured` beside the prediction). ⚠️ It is currently a DEVICE-WIDE delta, so a
  model restored concurrently by the residency coordinator is counted into it; it needs moving
  to per-process `drm-resident-gtt` before it can be trusted or used to replace the catalog
  estimate.

### Rewriting llama-swap.yaml kills EVERY resident model

Not the model being edited — **all of them**. DIAGNOSED 2026-08-20, after the owner reported
several times that staging a model in the PWA unloaded `gpt-oss-120b` and was several times
told it was a display artifact. It was not.

```
a settings PUT (context window / image floor / slots / extra args)
  -> api.llm_settings._try_regenerate()
  -> llama_swap_config.write() lands a fresh mtime, even byte-identical
  -> llama-swap --watch-config polls MTIME + SIZE every 2 s, so it fires
  -> llama-swap reload(): builds a new server, then old.Shutdown()
  -> every running llama-server process dies
```

**Why it stayed invisible.** The kill happens inside llama-swap, so nothing writes an
`app.box_events` row and the vitals surface says nothing — the app does not know it happened.
Meanwhile `_unload_if_loaded`, sitting on the line right after the regen call, unloads only the
model named in the PUT, so the code reads as though a settings edit touches one model.

**How it was finally caught.** Three consecutive manual loads of `gpt-oss-120b` with **zero**
`model_unload` rows between them — proof the app never asked — and then llama-swap's own log:

```
<gpt-oss-120b> Health check passed
reloading configuration
configuration reloaded          <- model dies here
<gpt-oss-120b> Health check passed
reloading configuration
configuration reloaded
```

**Fixed** by making `llama_swap_config.write` compare rendered content against the file and
no-op when unchanged. The boot reconciler (`llm_settings.reconcile_gateway_config`) already did
exactly this, with the comment *"leave any resident model warm"* — the knowledge existed and had
only ever been applied to the boot path.

A PUT that genuinely changes a served command still writes, still reloads, and still costs the
resident set — and *that* is what the content compare could not fix, because a real edit has to
reach the file eventually. **The re-stamp was therefore moved off the PUT entirely and onto the
load** (`llm_settings.regen_gateway_config`, called by `local_gateway` immediately before it
starts a model). Safe because the PWA only allows a model's flags to be edited while it is NOT
resident (`editable = !m.loaded`), so at edit time there is no process the new flags could apply
to. Editing now costs the resident set nothing at all; the reload is charged to the load that
actually needs it.

**The deferral had a second-order race, and the first version shipped with it.** Writing the
config and then immediately loading means llama-swap's 2 s watcher fires *while the load is
still in flight* — so the load is killed by its own config change, bystander included:

```
after edit: reloads=0 resident=gpt-oss-120b     <- editing is now free, as designed
after load: reloads=1 resident=NONE             <- ...and the load killed both
```

```
<gpt-oss-120b> Health check passed
<qwen3-vl-30b-a3b-q4> Health check passed
reloading configuration                          <- lands ~2 s late, kills both
```

`app.box_events` caught both halves of it: a `model_unload` for the edited model (*"its context
window changed"*, the app's own eviction, correct), then a **failed** `model_load` —
`Server error '500 Internal Server Error' for url .../upstream/qwen3-vl-30b-a3b-q4/health` —
and, for the bystander that also died, **no row at all**.

`regen_gateway_config` now compares the config's mtime across the write and, **only when the
file actually changed**, waits `_GATEWAY_RELOAD_SETTLE_S` before returning to the caller. The
unchanged case still returns immediately, which is nearly every load.

**Why 4 s and not 30.** llama-swap's `reload()` swaps the server *before* it kills anything:

```go
activeSrv = newSrv        // the SWAP
old.Shutdown(30s)         // the old llama-servers are killed here
```

Only the window before the swap can hurt a load — one that starts after it is served by the new
server and cannot be killed by that reload. That window is the watcher's 2 s poll plus a
`LoadConfig` and a `server.New()`, neither of which loads a model. The 30 s `shutdownTimeout` is
spent *after* the swap, on processes the load no longer races, so it is not the number to size
against. (First read of this code suggested the opposite and nearly bought a streaming
log-watcher; the ordering is the whole answer.)

A sleep rather than a readiness probe because llama-swap exposes no "reload done" signal a client
can poll: `/logs` carries `configuration reloaded`, but only after the shutdown that we do not
need to wait for, and `/running` is answered by the new server from the moment of the swap.

**Not covered, deliberately:** the old llama-servers can still be dying as the new load starts,
so both may hold GTT briefly. That inflates the `gpu_guard` baseline sampled just after the wait,
which errs toward *refusing* a load — the safe direction on a box that freezes when GTT is
exhausted.

**The eviction is still real, and is now NARRATED.** A genuine flag change has to reach the
file eventually, so loading an edited model still costs whatever else was resident — that part
is llama-swap's semantics and cannot be engineered away. What was wrong was the silence.
`local_gateway._narrate_reload_casualties` samples `/running` either side of the re-stamp (the
settle wait means the casualties are observed fact, not a prediction) and writes a
`model_unload` row per model that died, reading *"the gateway reloaded to apply changed settings
for <model>"*. So the vitals surface now explains the eviction the owner kept reporting instead
of showing nothing.

### NEGATIVE RESULT: auto-restoring the reload's casualties (tried, reverted)

The obvious next step after narrating the casualties is to put them back, and it was built and
merged (#1174) before being reverted. **Do not rebuild it without reading this.** It handed the
casualties to the residency coordinator as an external displacement (`note_evicted` +
`schedule_restore`), reusing the budget-aware restore rather than growing a second one. It
passed seven mutation-checked tests. On the box it did nothing useful.

MEASURED 2026-08-20, `gpt-oss-120b` resident, editing then loading `qwen3-vl-30b-q4`:

```
13:43:53  I load q4
13:44:00  model_unload  gpt-oss-120b  "the gateway reloaded to apply changed settings for q4"
13:44:17  model_unload  q4            FAILED (502 — llama-swap still settling)
13:44:17  model_load    gpt-oss-120b  ok, detail EMPTY          <- NOT the casualty restore
13:44:23  resident: gpt-oss-120b, qwen3-vl-30b-q4               <- briefly both
13:44:24  model_load    q4  FAILED "putting back what a displacement took / device memory ran
                                    away while loading: GTT 40.4 GB past the 36.1 GB ceiling"
13:44:26  model_unload  q4            ok
13:45:07  agent.turn on gpt-oss-120b, slot restored
```

Two things to read off it:

- **The casualty came back on its own, and not from the restore.** The 13:44:17 load carries an
  EMPTY detail; the restore stamps `putting back what a displacement took`. `gpt-oss-120b`
  returned because a real turn wanted it. On a box whose keeper primes a primary model, ordinary
  turn traffic already reinstates it — the feature is redundant there and inert everywhere the
  end-of-turn restore switch is off (which is how this box runs).
- **The `putting back` rows are about q4, not the casualty** — pre-existing displacement
  bookkeeping reacting to `ensure_room`'s eviction, and it hit the device-memory runaway abort.
  Not caused by the reverted feature, but it shows the window is already contended; adding a
  third actor to it bought nothing.

**The problem actually worth solving, which this mis-framed:** a turn wanting the primary model
runs `ensure_room`, which evicts the model the operator just deliberately loaded. That is the
churn behind the long-standing "I stage a model and gpt-oss-120b comes back" report — operator
intent versus keeper intent, not reload collateral.

**Generalisable:** moving a side effect off the writer and onto the reader does not remove it,
it re-times it. Ask what else is in flight at the new moment — here, the load the caller is
about to start.

**Diagnostic recipe, reusable.** `app.box_events` records every unload the APP performs, with a
distinct `detail` per path (`to make room for X, which you loaded`, `its context window changed`,
`you unloaded it`, …). A model that vanishes with **no row at all** was not unloaded by us —
look at the gateway, not the app. `POST /api/debug/sql` reaches that table.

### The gateway (llama-swap) is pinned to a commit — currently v250

`deploy/Dockerfile.local-llm` pins `LLAMA_SWAP_VERSION` to a full commit SHA, because
llama-swap tags releases `vNNN` and Go module resolution rejects that. A test
(`test_llama_swap_pin.py`) keeps it a 40-char SHA and keeps the comment naming the release,
since a bare SHA is unreviewable and a floating ref would let the gateway change under the box
on any rebuild.

Moved from v228 to **v250** on 2026-08-20 for two upstream fixes that both bite this box:

- **#875 / #878** — a browser tab left open on llama-swap's **own** web UI falls behind on
  llama.cpp's verbose output; the bounded event bus fills, `Broadcast` blocks `Write`, the
  child's stdout pipe backs up, and **llama.cpp stalls** until the tab is closed.
- **#946 / #949** — *"a request arriving during a process stop would hang forever"*: the router
  decided start/stop from a snapshot that could go stale. That is this box's ordinary
  workload — the residency evictor unloads models while requests keep arriving — and it is the
  best candidate for the upstream timeouts and the wedged-after-unload state seen on
  2026-08-20.

**Verified before the bump rather than after:** the config this repo generates
(`llama_swap_config.render` over the whole catalog) was parsed with v250's own
`internal/config.LoadConfig` — the full catalog of the day (16 models, 2 groups, at the
2026-08-20 bump; the roster drifts — 15 models as of 2026-08-23), no error — and every route the app calls
(`/running`, `/api/models/unload/{model}`, `/logs`, `/logs/stream/*`, `/upstream/{model}/…`)
still exists at that tag. Do the same before the next bump; the gateway is the single process
serving every model, so a config-schema change is an outage.

### The weights are resident twice — MEASURED 2026-08-19, after the box died for it

The gateway serves `--no-mmap`, so llama.cpp READS each GGUF rather than mapping it.

> ⚠ **`--no-mmap`'s justification is a phrase, not evidence — and it is the cause of everything
> in this section.** The only rationale recorded anywhere (here and in
> `llama_swap_config.py`) is "a gfx1151 stability flag": no measurement, no upstream issue, no
> date. It appears to be inherited from the strix-halo-toolbox recipe — the same source as the
> kernel settings this runbook already documents as wrong for this box.
>
> Three things say it deserves re-testing rather than repetition:
> - **llama.cpp deprecated it.** Every load logs `DEPRECATED: --mmap and --no-mmap are
>   deprecated. use --load-mode mmap instead`.
> - **The engine already handles the case the flag presumably exists for.** `--load-mode`
>   defaults to `auto` = "mmap, unless a device does not support it". Our blanket flag overrides
>   llama.cpp's own detection.
> - **`dio` exists.** `--load-mode dio` uses DirectIO, bypassing the page cache entirely — which
>   would remove the second copy at its source instead of reclaiming it afterwards.
>
> MEASURED 2026-08-20, and the reason this matters beyond tidiness: loading `gpt-oss-120b` on an
> idle box took `used` to **115 GB of 121** — cache climbing to 49 GB while 67 GB was already
> pinned in GTT. The drop cannot help during that window: llama.cpp has not returned yet. Cache
> topped out below the full 59 GB only because the kernel was *already reclaiming under GTT
> pressure*, which is the livelock mechanism itself. The admission guard does not model this
> transient at all — it projected 68.55 and measured 69.26 resident, both correct, both about
> the steady state.
>
> **How to test it, no deploy needed.** `--load-mode` / `-lm` is on the extra-args allowlist and
> supersedes the hardcoded `--no-mmap` (exactly one reaches the command line):
>
> ```
> scripts/debug-connect.sh   # PUT /api/debug/llm/local-models/<id>/extra-args
> #   {"args":["--load-mode","mmap"]}   then load and watch Cached
> #   {"args":["--load-mode","dio"]}    then load and watch Cached
> #   {"args":[]}                       clears back to the hardcoded default
> ```
>
> Read `Cached` from `GET /api/debug/host/metrics` during and after the load. A working mmap or
> dio run should show the transient largely gone. **Not changed by default**: the flag may have
> been added for a real crash nobody wrote down, and finding out the hard way costs a power
> cycle on a box with no terminal. Record the result here either way.
 The weights then exist **twice**: once in GTT, once in the page cache
the read filled. Sampling a single `gpt-oss-120b` load on the idle box:

| | avail | MemFree | Cached | GTT |
|---|---|---|---|---|
| baseline | 105.4 | 109.9 | 5.2 | 0.0 |
| mid-load | 46.0 | **8.4** | 49.1 | 57.4 |
| steady | 35.7 | 8.0 | 39.4 | 67.6 |
| **after unload** | 103.8 | **76.0** | **39.4** | **0.0** |

Unload frees the GTT copy perfectly and **never** frees the cache copy. So one 68 GiB model
occupies ~107 GiB of the 121 GiB pool while loaded and leaves ~39 GiB behind when it goes.

Three things follow, all now fixed in code:

- **`local_weights.drop_weights_page_cache`** fadvises `DONTNEED` over the model's GGUFs as
  each load returns, from the load chokepoint. `drop_image_model_page_cache` does the same for
  ComfyUI's weights after a render — that path re-reads ~58 GB cold every time, because the
  render deliberately unloads the model afterwards.
- **`host_metrics.read_memory_gb` stopped believing `MemAvailable`.** It now reports
  `MemTotal − MemFree − SReclaimable`: page cache counts as USED. Reclaiming it while the iGPU
  pins most of RAM as GTT is the livelock, not the escape from it.
- **`gpu_guard.refuse_if_no_device_room` is bound by BOTH pools.** It used to use
  `gtt_total − gtt_used` alone — but `amdgpu.gttsize` is 124 GiB on a 121 GiB box, so that
  "device pool" is essentially all of RAM and counts page cache as room the GPU could take. At
  the fatal load it read **50.4 GB of headroom over 8.0 GB of free pages**.

**How the box actually died (2026-08-19).** `agent.turn` moved off the abliterated 27B onto
gpt-oss at 02:26. The old model's GTT was freed; its ~16 GiB cache shadow stayed. gpt-oss then
pulled 59 GiB of weights through a cache that was already holding it, `MemFree` collapsed, and
the kernel pushed all 17 containers' anonymous pages to swap at once. Swap never drains, so
every service ran from swap for the next seven hours — which is why the 2-second vitals sampler
kept timing out and a `/props` metadata read later took 31 seconds. The morning tasks were the
load that finished it, not the cause.

**Where the guard lives, and why that is the whole point.** It is inside
`LocalGatewayClient.load` — the single chokepoint every path to committing device memory passes
through. It used to sit on the residency coordinator, which was **one of six callers**, and all
three freezes arrived through the other five (the settings screen's deliberate load, the debug
console's, and the coordinator's own end-of-turn restore). A check a caller can route around is
not a check. A unit test (`tests/unit/test_llm_load_guard_chokepoint.py`) walks the AST of
`src/jbrain/` and fails CI if anyone ever constructs a local-model gateway without the probe.

**What the pre-flight reserves.** `local_catalog.load_footprint_gb` — weights + KV + a runtime
term (recurrent state, compute/output buffers, and the MTP draft context on a speculative
entry). For a vision model it adds the CLIP attention buffer at its **load-time warmup size**
only. It does **not** reserve that buffer's peak, because the peak does not exist at load.

**It reserves at the window the box actually serves, not the catalog default.** KV is *linear*
in `-c`, so a guard that sizes off the catalog's `context_window` while llama-swap serves an
operator override is not sized for the load it is guarding. Measured 2026-08-19: the abliterated
27B ships a 32768 catalog window, is served at `-c 262144`, and the pre-flight reserved
**20.29 GB** for a load that measured **36.92 GB** — 1.8× light on the one code path whose job is
refusing a load rather than freezing the host. `LocalGatewayClient` now reads the live
per-model window and slot overrides (the same `settings_store` loaders the eviction budget
uses) and passes them in; at 262144 the reservation is **34.29 GB** against 36.92 measured,
which the guard's 6 GB held-back headroom covers.

Two paths deliberately keep the catalog fallback: `jbrain local-llm smoketest`, which runs
under `docker compose run --no-deps` with **no DB** to read overrides from, and any gateway
built without the loaders (the tests). Both under-reserve rather than over-reserve; the load
**watchdog** measures real growth mid-flight and aborts, which is what makes that survivable.

**Where the vision cost really is.** The large allocation behind #27146 is the CLIP/mtmd
encoder's attention matrix — F32 `[n_patches, n_patches, n_head]`, materialised when flash
attention is off in the CLIP graph. Two properties decide where it belongs in the budget:

- **It is not a load-time cost.** llama.cpp warms the projector at a capped 46×46 = 2116 image
  tokens (`set_warmup_n_tokens`); the full-resolution buffer appears on the **first real
  image**, which may be much later or never.
- **It is not transient.** `ggml_gallocr_reserve_n_impl` only ever grows the buffer — there is
  no shrink path — and it is freed at model unload. A smaller later image releases nothing.

So it lives in `footprint_gb` (resident, drives eviction), not in the load reservation. It
scales with the **square** of the image token ceiling: `n_patches = max_image_tokens ×
n_merge²`, buffer = `n_patches² × n_head × 4` bytes.

| `--image-max-tokens` | buffer (CLIP FA **off**) |
|---|---|
| 4096 (llama.cpp's default for this projector family — what we inherit) | **16.0 GiB** |
| 1024 | 1.0 GiB |
| 512 | 0.25 GiB |

✅ **MEASURED on this box: CLIP flash attention is ON**, so the workspace is the LINEAR branch
(~0.47 GiB at the 4096-token ceiling), not the quadratic one. The catalog briefly assumed OFF
and over-reserved every vision entry by ~145x. How it was settled, with auto-restore switched
off so nothing could reload underneath the samples:

| step | GTT (GiB) | Δ |
|---|---|---|
| baseline, `gpt-oss-120b` resident only | 67.71 | — |
| after loading `qwen3.8-27b-q4` (served at `-c 131072`) | 93.73 | **+26.02** |
| after a full-resolution 2.1 MB image encode | 93.84 | **+0.11** |

The load delta discriminates on its own: 25.60 predicted with flash attention on versus 29.62
with it off, against 26.02 measured. The image encode settles it — with flash attention off a
full-resolution image allocates up to 16 GiB; it allocated 0.11. `-fa 1` reaches the CLIP
graph. The corrected model predicts 25.82 resident at that window against 26.02 measured, a
0.8% error.

⚠️ **This is contingent on `-fa`.** Anything that drops it from the served flags, or a build
where it silently does not apply to the CLIP graph, puts the quadratic branch back in play —
`vision_attn_buffer_gb(flash_attention=False)` keeps it for exactly that case.

> **The startup banner is readable today via `GET /api/debug/llm/upstream-logs`.** It was not
> when this was measured — `warmup: flash attention is enabled|disabled` never reaches
> `gateway-logs` or `logs local-llm`, both llama-swap's own HTTP access log — which is why
> the question had to be answered by GTT deltas at the time. The verbosity caveat stands (the
> default `-lv` prints the banner and per-request lines, not a load's per-buffer memory
> breakdown), but "did `-fa` apply?" is now one `upstream-logs` read, not a GTT experiment.

Note we pass `--image-min-tokens` (2048 by default, per model), which is the **floor**. Nothing caps the ceiling, so
4096 is the exposure. Pinning `--image-max-tokens 1024` would cut the buffer quadratically at
some cost to grounding accuracy on small text.

### Stopping the box loading models on its own

**Settings → LLM → Auto-restore models** (under the memory meter). ON — the default and the
long-standing behaviour — means that after a displacement (an image render, a code session, a
big one-off) the box puts the displaced models back once the turn ends, so it settles at its
steady state rather than cold-loading next turn. OFF means it stops: a model comes back only
when a turn actually needs one.

Turn it off while diagnosing the box, or while deliberately holding it near empty — a restore is
a model load nobody asked for at that moment, and it has repeatedly appeared *underneath* a
measurement and confused it. It is a **surprise** control, not a safety one: every load, restore
included, goes through the device-memory guard either way. Read live, so the flip applies to the
next turn with no restart.

### Is MTP actually drafting?

`/props`'s `speculative.types` **cannot answer this** — the server builds that object from a
`task_params` it never populates, so it reads `"none"` on every build whether or not speculation
is running. An entire investigation here concluded MTP was off from that field. The real signals,
both of which need flags the config now always passes (`--slots`, `--metrics`):

| Signal | Where |
|---|---|
| `speculative: true` | `GET /slots` — a real bool from `can_speculate()` |
| draft / accept counters | `GET /metrics` → `llamacpp:spec_decode_num_draft_tokens_total`, `…_accepted_tokens_total` |
| `draft_n`, `draft_n_accepted` | a completion's `timings` — present only when tokens were drafted |
| `draft acceptance = 0.72 ( 210 accepted / 290 generated)` | gateway log, per request |

Note also that llama.cpp **hard-fails** at load when `--spec-type draft-mtp` is given and the
model has no nextn tensors (`llama_init_from_model` returns null → the server aborts). So a
server that starts at all has the MTP head loaded — "it started" is evidence of loading, never
of drafting.

Readings come from the supervisor's `/metrics` → `gpu_mem`, which reads
`/sys/class/drm/card*/device/mem_info_*`. A box that can't read them (no amdgpu, supervisor
down) degrades to the old unguarded behaviour rather than refusing to serve.

Two things deliberately have no remote path. The **`-np` slot count** is owner-authenticated
(PWA only) — but a speculative model is clamped to one slot in the config generator regardless,
so a stale override there cannot break it. And **promoting a setting that measures well** still
takes a catalog edit and a release: the live path is for finding the value, not for keeping it,
so the box never drifts from what the repo says it serves.

> **When a download fails**, the banner shows the reason (last log line). For the
> full verbose log — the resolved repo, include globs, and the hf error (404 / auth
> / disk / network) — read `GET /api/debug/provision/status` via a debug token
> (docs/runbooks/DEBUG_ACCESS.md), or the Ops update log.

## Phase 8 — Confirm it's really local
- Add a note with a photo → it should OCR locally; watch `jbrain logs local-llm`.
- Ops screen → AI usage card shows the local model serving those tasks ($0 cost,
  since local isn't in the price table).

The gateway has **no published port** (internal network only) — verify via the
app and `jbrain logs`, not `curl localhost:8080`.

---

## Image generation — ComfyUI + Qwen-Image (optional, opt-in)
Powers jerv's `generate_image` / `edit_image` tools
(`docs/archive/IMAGE_GEN_SERVICE_PLAN.md`): text→image via **Qwen-Image** (native bf16),
a near-instant **fast** path via the **Qwen-Image 4-step Lightning** LoRA (`generate_image`
`speed: fast`), and image→image via **Qwen-Image-Edit**, served by a **ROCm ComfyUI JBrain manages
as a compose service** — the sibling of the local-LLM gateway. Like that
gateway, it is **opt-in**: a stock deploy never starts it, and JBrain only ever
**POSTs a workflow graph** to it over HTTP (no new backend dependency). Leave it
unprovisioned to keep the feature (and both tools) off.

Prereqs are the same gfx1151 floor as the rest of this runbook: **kernel ≥
6.18.4** (Phase 2) and a working GPU stack. Unlike the Vulkan LLM path, ComfyUI's
**ROCm** stack needs **both** `/dev/kfd` and `/dev/dri` and
`HSA_OVERRIDE_GFX_VERSION=11.5.1` (the `comfyui` compose service sets this) so
ROCm treats the iGPU as gfx1151 — without it the stack silently CPU-falls-back.

**One command provisions and enables it:**
```bash
sudo bash scripts/comfyui-setup.sh             # the recommended set: Qwen-Image generate +
                                               # edit and both 4-step Lightning fast siblings
sudo bash scripts/comfyui-setup.sh qwen-image  # or explicit catalog ids
sudo bash scripts/comfyui-setup.sh dreamshaper # add the lightweight SDXL model (~7 GB)
```
The recommended set covers the `fast` and `quality` paths of both `generate_image`
and `edit_image`: the generate + edit base models plus their 4-step Lightning LoRA
siblings (the LoRA is shared, ~0.85 GB on top of the base weights). Models are
additive: provisioning `dreamshaper` downloads only its ~7 GB checkpoint and leaves
an already-installed Qwen-Image in place.
The script (the sibling of `local-llm-setup.sh`) downloads the weight files named
by the catalog (`jbrain.image_gen.catalog`) into `./comfyui-models/<subdir>`,
writes `JBRAIN_COMFYUI_*` into `.env`, and starts the `comfyui` profile. The api
reaches the service at `http://comfyui:8188` over the internal network — **no
published host port**, mirroring the LLM gateway. The model catalog is the single
source of truth for repos/filenames; add a model by adding a catalog entry, not by
editing the script.

Once image generation is enabled, **`jbrain update` re-syncs the models for you**:
after rebuilding it re-runs the provisioning step for the union of your current
selection and the recommended set, so an update that introduces a new model (or new
weight file) downloads it automatically — no manual re-run. It's idempotent, so an
unchanged catalog is a no-op; it never drops a model you provisioned, and a sync
failure is logged without aborting the update.

- **Validated on-box.** A 1328×1328, 20-step Qwen-Image renders on the iGPU from
  **native bf16** weights (~58 GB resident, the 2512 checkpoint). The renders
  **time-share** the unified memory — the local LLMs are unloaded before a render
  and ComfyUI's model is freed after — so the diffusion model has the box to itself
  and bf16 costs no more RAM than the old fp8 build (gfx1151 upcast fp8 to bf16 at
  load anyway), minus the quantization loss. The `qwen-image-edit` model ships
  **recommended** (part of the default provisioned set) — its graph is validated
  structurally (exported from the box).
- **Fast path — Qwen-Image 4-step Lightning.** `generate_image` with `speed: fast`
  routes to the Qwen-Image base model driven through the shared 4-step Lightning
  LoRA at CFG 1 — the same ~58 GB Qwen family as the quality path, so it returns in
  a fraction of the quality render time without a second large checkpoint.
  **DreamShaper XL Lightning** is a separate opt-in tier (`speed: dreamshaper`): a
  single all-in-one SDXL checkpoint (~6.7 GB, baked VAE) driven by the stock SDXL
  graph at 4–8 steps that renders in **seconds** — lower fidelity than Qwen, but a
  tiny standalone for quick or exploratory requests. It ships **non-recommended**
  (opt in with `comfyui-setup.sh dreamshaper`; its standard SDXL graph is authored,
  not yet box-exported); a first on-box render is the final confirmation.
- **JBrain owns the graph, not the model.** The backend POSTs the workflow JSON in
  `backend/src/jbrain/image_gen/workflows/` (`qwen_image.json`,
  `qwen_image_edit.json`, `dreamshaper_xl.json`), filling typed slots (prompt,
  seed, steps, dims, and — for edit — the uploaded input image). The driver picks the
  graph from the requested model, so a model is a JSON + binding pair, not a code path.

✅ **Checkpoint:** after `comfyui-setup.sh`, ask jerv to generate an image; the
result streams back inline in the chat turn (a chat-only artifact — never a note,
never RAG-indexed). Watch the `comfyui` service logs (`docker compose logs
comfyui`) for the submitted graph.

---

## Expected performance
~31 tok/s on gpt-oss-120b, ~30–45 tok/s on Qwen3-VL. Models co-reside up to the RAM
budget: as many stay hot as fit under the ≥15%-free floor — judged by the reservation
ledger's admission arithmetic, with `/proc/meminfo` as its measured term and fallback — and
a load evicts the fewest
others needed to make room (so a text↔vision switch only cold-loads if both don't fit).
Tune the headroom from **Settings → LLM → On-box memory** (live) or with
`LOCAL_LLM_FREE_RAM_FRACTION` (deploy default).

## Switching to ROCm (optional, faster)
The ROCm/rocWMMA path is often faster on gfx1151 and is the better route for
gpt-oss's fp4. To use it, set the base image and add the extra device/permission
the ROCm runtime needs, then rebuild:
```bash
# in /opt/jbrain2/.env
LOCAL_LLM_BASE=docker.io/kyuz0/amd-strix-halo-toolboxes:rocm-7.2.4
```
and in `docker-compose.yml` under the `local-llm` service add `- /dev/kfd:/dev/kfd`
to `devices:` and `security_opt: [seccomp:unconfined]`, then
`jbrain enable-local-models` (or rebuild). Benchmark before committing.

## Stability — hard-freeze / OOM hardening
The recommended local set keeps **~91 GB resident** in the 130 GB unified pool,
leaving only ~30 GB for the OS, containers, and KV-cache. A sudden allocation on top
of that — a `jbrain update` recreating containers, a model swap, an image render, or
context growth — can push the box to its memory ceiling. On this hardware the kernel
does **not** always OOM-kill cleanly: it can enter a **reclaim livelock** where every
core spins in page reclaim and the whole machine hard-locks — USB keyboard and mouse
included — until a power cycle. The log signature is a burst of *missed kernel
messages* ending mid `Mem-Info`/slab dump, and Postgres recovering with "database
system was not properly shut down" on the next boot.

Update-time allocation is handled for you, and the handling is structural rather than
best-effort — see "An update never competes with itself for memory" below for the
sequence. The short version: an update quiesces the stack, hardens *before* the heavy
phase, drops the page cache, and refuses a model load it cannot afford.

The host OS hardening is **applied automatically** by `deploy/oom-hardening.sh` — run
from `scripts/local-llm-setup.sh` (install and every `jbrain enable-local-models`) **and**
from the host `jbrain update` path when local hosting is on, so an existing box gets
hardened on its next update with no manual step. A **PWA** update cannot run that script
(earlyoom needs `apt`, the persistent file needs `/etc/sysctl.d`), but it does apply the
reclaim-headroom knobs in step 2 for the current boot through a privileged one-shot — the
`vm.*` knobs are not namespaced, so a container writes the same values the host would. The
steps below are what the hardening does and how to verify:

1. **earlyoom** — kill the biggest hog on memory pressure *before* the kernel stalls.
   Installed by the setup script (it needs `apt`), but its **thresholds are applied by an
   ordinary Ops → Update too**: `update-inner.sh` writes `/etc/default/earlyoom` through the
   same privileged one-shot it uses for the `vm.*` knobs and restarts the unit via `nsenter`.
   That closes a real gap — the arguments used to come only from the host script, so a box
   updated from the PWA kept whatever a past host install wrote. Verify:
   ```bash
   systemctl is-active earlyoom && cat /etc/default/earlyoom
   # EARLYOOM_ARGS="-r 60 -m 30 -m 20 -s 10 -s 5 --prefer ^llama-server$ --avoid ^(sshd|systemd|systemd-.*|dockerd|containerd|postgres|supervisor)$"
   ```
   > **Why 30/20 and not the 10/5 this used to say.** earlyoom fires only when memory **and**
   > swap are both under their limits, and its `-m` reads **MemAvailable** — which counts page
   > cache as free. During the 2026-08-19 livelock swap was 100% consumed for seven hours (so
   > `-s` was satisfied throughout) while MemAvailable held at **29%**. `-m 10` was never
   > approached, the AND never closed, and this backstop did not fire once. Raising the memory
   > limit is safe *because* of that AND: swap is ~100% free on a healthy box, so `-s 10` gates
   > everything. Together they mean "swap is gone AND memory is tight" — the livelock, and not
   > a state this box reaches in health.
> **Ops → Host settings shows all of this without a shell.** `/api/ops/host-settings`
> (`jbrain.host_settings`) reads the live `ttm.pages_limit`, the `vm.*` knobs and MemTotal
> and reports which assumptions actually hold, flagging separately the ones an Update cannot
> fix. It exists because the one that mattered was invisible: `ttm.pages_limit` sat at 124 GiB
> on a 121 GiB box — **disabled**, since the over-commit it refuses can never occur above
> MemTotal — and nothing said so until a freeze was traced back to it weeks later.

2. **Reclaim headroom (sysctl)** — start reclaiming earlier, thrash into swap less.
   Written to `/etc/sysctl.d/99-jbrain-oom.conf` by the setup script; verify:
   ```bash
   sysctl vm.min_free_kbytes vm.watermark_scale_factor vm.swappiness
   # vm.min_free_kbytes = 2097152 · vm.watermark_scale_factor = 200 · vm.swappiness = 10
   ```
3. **The app evicts to a RAM budget, so nothing over-commits the box** — every load frees
   the fewest resident models needed to keep ≥15% of RAM free (`LOCAL_LLM_FREE_RAM_FRACTION`)
   before it loads, and nothing is ever pinned beyond that floor. There is no keep-hot pin to
   over-commit: a manual **Load** (Settings → LLM → On-box models) evicts to the same budget —
   the **Stage** preview shows what it will evict first — and models you use stay warm via the
   end-of-turn restore, not a held pin. **The reservation ledger is the authority**
   (`jbrain.llm.ledger`, since 2026-08-23): the eviction plan runs `admission.admit` over the
   ledger's own pools and rows — the same arithmetic the load's charge applies — so the evict
   verdict and the admission verdict cannot disagree; the older whole-box measured+predicted
   planner survives only as the fallback for a failed ledger read. **Cross-process
   serialization:** the `api` and the `worker` each run their own `ResidencyCoordinator`, so
   to stop two processes co-loading past the floor, a **Postgres advisory box lock**
   (`pg_box_lock`, `jbrain.llm.residency` / `ledger.charge`) covers the **decision and the
   evictions only** — not the load itself. Every load, the manual operator **Load** included,
   then **charges** the ledger: decide-and-insert in one transaction under that same lock
   with a bounded `lock_timeout`, and the charge row is what protects the load once the lock
   releases — the next process's plan sees the charged reservation. A DB hiccup at charge
   time **refuses transiently** (the settings screen 409s, the worker defers) rather than
   loading unguarded. earlyoom (steps 1–2) stays the OS-level backstop.
4. **Persistent logs** so the next event's full dump survives the freeze:
   ```bash
   sudo mkdir -p /var/log/journal
   sudo sed -i 's/^#\?Storage=.*/Storage=persistent/' /etc/systemd/journald.conf
   sudo systemctl restart systemd-journald
   ```

> **Swap is deliberately small (8 GB).** Do **not** enlarge it to "fix" the freezes —
> on a reclaim livelock a large swap just prolongs the thrash instead of breaking it.
> The levers above (kill fast, keep headroom) are the real fix; swap is only a shallow
> cushion for brief spikes.

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Whole box hard-freezes (kbd/mouse dead), needs a power cycle | Unified-memory OOM / reclaim livelock — see "Stability — hard-freeze / OOM hardening" above. |
| `vulkaninfo` shows no device | Mesa or kernel too old (Phases 2–3). |
| Unsigned kernel won't boot | Secure Boot still on (Phase 0). |
| Gateway crash-loops | `jbrain logs local-llm`; missing GGUF shard (setup validates this) or config path. |
| gpt-oss OOMs on load | Vulkan KV-cache bug — add `-c 8192` in `llama-swap.yaml`, or use the ROCm fp4 base. |
| Model loads but slow / OOM | GTT param didn't take — re-check `/proc/cmdline` (Phase 5). |
| `/dev/dri` permission denied in container | host render GID not written — check `.env` `RENDER_GID`, re-run `enable-local-models`. |

## Reproducibility / trust
The gateway base is a **community** image, pinned by digest in
`deploy/Dockerfile.local-llm` (`LOCAL_LLM_BASE`): the rolling `:vulkan-radv` tag tracks
llama.cpp master, and a floating build once let a gpt-oss tool-call grammar regression
ride in unnoticed, so a known-good digest is the reproducible baseline. That pinned
digest is now the **rollback target** for the auto-update below, not a frozen ceiling —
override it in `.env` (`LOCAL_LLM_BASE=…@sha256:<digest>`) to move the baseline, e.g. a
`rocm-*` variant.

### Tracking newest llama.cpp automatically (default-on)
A **just-released** model can need a llama.cpp newer than the pinned base — its
architecture won't load (llama-server exits at model-load: `upstream command exited
prematurely` in the gateway log; the Nemotron-3.5 hybrid-Mamba arch is the canonical
case). So on a box with local hosting enabled, **every `jbrain update` (host or PWA)
auto-updates the gateway** — no flag, no shell step (the owner drives this box from the
PWA, no terminal): it rebuilds on the **floating** tag with `docker compose build --pull`
(a cheap manifest check when llama.cpp hasn't moved; a real rebuild when it has), then
**smoke-tests** the build — loads the smallest installed model and, when gpt-oss is
installed, runs one tool-carrying probe (the exact surface the past regression broke).
**If the smoke test fails, the update rolls the gateway back to the pinned
`LOCAL_LLM_BASE`** and keeps serving, logging a `WARNING` to the update log. That
rollback net is what makes tracking master safe by default: a bad upstream build never
leaves the box unable to serve, and a good one lands automatically.

**There is ONE update script.** `deploy/update-inner.sh` is the implementation; the PWA
runs it as a detached one-shot and `jbrain update` runs it directly with
`JBRAIN_HOST_UPDATE=1`. It used to be two hand-maintained copies, and they drifted in
exactly the way that matters: the host copy never rebuilt the gateway on the floating
llama.cpp tag or loaded a model to smoke-test it, and the containerized copy never
re-applied the OOM hardening — so the PWA path did the memory-heavy work *without* the
protection against precisely that, and hard-locked the box. Steps that genuinely need the
real host (mDNS via systemd/avahi, on-box image models, `earlyoom` via `apt`) are gated on
that flag inside the one script rather than living in a second one — and the list is kept
as short as it can honestly be: the reclaim sysctls left it once a privileged one-shot
turned out to reach them.

**Before any of the churn, the gateway is emptied and removed.** An update asks it to
release every loaded model (`jbrain.cli local-llm-unload`), then removes the container
rather than merely stopping it, then waits for it to actually be gone. Stopping alone
leaves the kernel reclaiming tens of gigabytes exactly as the build starts allocating —
the race that drove a reclaim livelock and hard-locked the host, keyboard included — and a
merely-stopped container is one stray `up -d` away from reloading the weights mid-update.
`local-models-sync.sh` honours `JBRAIN_SKIP_GATEWAY_START` for the same reason: its own
restart used to land squarely in that window. The gateway comes back once, deliberately,
after the rebuild.

### An update never competes with itself for memory
Emptying the gateway was not enough on its own. Updates kept hard-locking the box, and the
kernel trace named the culprit exactly: `llama-server` failing an **order:0** allocation —
a single 4 KB page — inside `amdgpu_ttm_tt_populate` during a GEM buffer create, with
`__GFP_RETRY_MAYFAIL` in the flags. That flag tells the kernel to reclaim hard and then
*fail* rather than invoke the OOM killer, which is why nothing was killed, nothing showed
up in the app's metrics, and the host simply froze in reclaim. earlyoom and the sysctls
above were already in place when it happened, so hardening alone does not cover this.

The fix is that `deploy/update-inner.sh` stops doing several memory-heavy things at once.
Its phase order is deliberate:

1. Backup, source refresh, `.env` backfills — stack up, cheap.
2. **Quiesce.** Empty and remove the gateway, then stop every service except the control
   plane (`db`, `api`, `supervisor`, `proxy`, `cloudflared`). Those stay up so the PWA can
   still stream the update log — going fully dark would free about a gigabyte and cost you
   any way to tell a slow build from a wedged one.
3. Reclaim hardening and the image build — against the quiesced stack.
4. **Pause the `api` too**, rebuild the gateway on the floating tag, **drop the page
   cache**, then run the smoke test — which loads the **smallest installed model** (~1 GB
   with a tiny Qwen present) and probes that same model, so this phase does not allocate
   tens of gigabytes at all.
5. Empty the gateway again, migrate, recreate the stack, restore everything the quiesce
   stopped.
6. Model syncs, gateway restart, prune.

**The smoke test does not load a big model.** It used to run its tool probe against
gpt-oss-120b, because the regression it guards against was a *harmony* tool-grammar
segfault and harmony is gpt-oss. But loading ~59 GB is precisely what the kernel traces
caught freezing this box mid-update — and a frozen box never reaches the rollback the smoke
test exists to trigger. So the probe now runs against the model already loaded in step 1,
and gpt-oss is probed **only when something else has already made it resident**, where it
costs nothing.

> **This is a deliberate coverage trade, not a free win.** Harmony has its own chat template
> and its own grammar path in llama.cpp, so a harmony-specific regression can now ship
> unnoticed. The asymmetry is the argument: a missed harmony regression breaks gpt-oss tool
> turns, which you can route around from Settings in seconds, while the load that would have
> caught it can hard-lock a box you operate remotely with no terminal. If you ever want the
> old behaviour back, the probe target is `smoketest.TOOL_PROBE_MODEL_ID`.

**Why the `api` is paused for step 4 anyway.** It is deliberately kept up through the build
so you can watch the log — but it runs the keep-warm prime, which retries every five seconds
and loads the chat model the instant the gateway answers. That is ~60 GB allocated mid-update
by a process nobody asked to do it, and now that the smoke test is cheap it is the *largest*
allocation this phase could make. So the PWA goes dark for the gateway rebuild and smoke
test — minutes, and only on an update where the gateway image actually changed — and comes
back with the full log intact.

**Recovering a quiesce that never finished.** The `EXIT` trap restores the stopped services
on an ordinary failure or a caught signal, but it cannot cover a `SIGKILL` (the supervisor's
stale-one-shot reaper uses one) or a power cut — and every service is `restart:
unless-stopped`, which by definition does *not* restart something explicitly stopped. So the
quiesced set is also written to `.jbrain-quiesced` in the install dir before the first stop,
and the **next update restores anything left in it before doing anything else**. That matters
most for `comfyui` and the mqtt pair: no `up -d` in the update names them, so without the file
nothing would ever bring them back.

Two details matter more than they look. The **page-cache drop** comes immediately before
the load because `MemAvailable` counts reclaimable cache as free — and reclaiming it under
pressure is the very thing that livelocks this box, so after a build that just wrote tens
of GB of layers the number reads fine and cannot be realised in time. Dropping first turns
the reading into real free pages; if the drop doesn't move the number, the update log says
so rather than letting the check quietly measure cache. And the smoke test then **refuses a
load it cannot afford**: it checks `MemAvailable` against the model's resident cost (weights
**plus KV cache**) and `smoketest.LOAD_HEADROOM_GB`, and if short it reports the shortfall
and fails. With a tiny model installed that check now passes trivially — it stays because it
is the guard for a box whose *smallest* installed model is a big one — which routes into the existing rollback, so a box without the room keeps the
pinned, known-good base instead of betting the host on a verification step. In the update log
that reads as `NOT ENOUGH MEMORY to load … REFUSED`. A model the gateway already holds is
never charged for, since loading it allocates nothing.

That headroom is **the app's own floor, not a smaller one invented for the update**: 20 GB,
matching the ~19.5 GB that `LOCAL_LLM_FREE_RAM_FRACTION` (0.15) keeps free on this 130 GB
box for every ordinary turn. The update path is the one that has actually frozen this
hardware; it would be indefensible for it to be the loosest load path on the machine. A unit
test pins the two together.

**Every one-off `compose run` in an update is bounded.** The toggle read gets 120s and the
smoke test 600s, and a timeout is treated as a FAILURE — so a hung smoke test rolls the
gateway back to the pinned base rather than stopping the update dead. This exists because
an update stalled twice at `jbrain-api-run-… Created`: a container compose created and
never started, with `set -e` and no ceiling meaning the update simply stopped there
forever, stack half recreated. The gateway client's own httpx timeouts cannot help when
the process never starts. A killed run also sweeps the orphaned one-off container, which
`--rm` never cleans up because the container belongs to the daemon rather than to
`compose run`.

**Turning it off is a PWA toggle**, not a file edit: **Ops → Update → "Track newest
llama.cpp"**. The owner runs this box remotely with no terminal (CLAUDE.md #10), and this
is the one switch that decides whether an update loads a model into the iGPU at all — the
heaviest thing an otherwise-routine update does — so it has to be reachable from the
surface they actually operate. The stored setting is read by the update one-shot; an
`.env` opt-out still wins, so an existing frozen box stays frozen.

The smoke test is also **skipped when the rebuild produces the identical image**: it
exists to vet a new upstream build, and re-loading a model to re-verify a byte-identical
gateway was pure cost on every no-op update.

Knobs (all optional, `.env` — the first is the same switch as the PWA toggle above):

```
# Freeze the gateway on the pinned base instead of tracking newest (reproducible builds):
LOCAL_LLM_AUTO_UPDATE=false
# The rolling tag to float onto (default below); rocm users point at a rocm-* tag:
LOCAL_LLM_BASE_FLOATING=docker.io/kyuz0/amd-strix-halo-toolboxes:vulkan-radv
```

Gated on `LOCAL_LLM_ENABLED`, so a stock cloud stack is never touched. Refreshes on
**update**, not on a bare reboot — a reboot restarts the existing gateway image
unchanged, so **run an update from the PWA (Ops → Update) to pick up a newer llama.cpp**.
A still-unsupported architecture (the community image hasn't caught up to a day-old
model yet) is NOT caught by the smoke test — it checks the smallest model + gpt-oss, not
every model — so the floated build is kept and that one model simply keeps failing to
load until upstream lands the arch; the next update retries automatically.
