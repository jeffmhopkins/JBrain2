# GUI gate — image-model settings (Wave G6)

Three interactive mocks of the **image service in the Settings → LLM screen** —
where the G5 backend (`GET /api/settings/image`: service status, real VRAM
total/free, the catalog with provisioned/disk/footprint, and start/stop/free) is
surfaced. Open each in a browser; the Start/Stop/Free controls are live.

The owner picks one; the chosen mock becomes the binding spec for the G6 build.

| Variant | File | Idea | Trade-off |
|---|---|---|---|
| **A — Sibling drawer** | `image-models-a-sibling-drawer.html` | A second collapsible drawer below "Local models", same chrome: own VRAM meter, a service Start/Stop/Free row, per-model rows. | Most consistent + lowest-risk to build; two separate memory bars (LLM + image). |
| **B — Unified meter** | `image-models-b-unified-meter.html` | One "On-box models" drawer with a **single** unified-memory bar showing LLM + image together; LLM and image subsections beneath. | Truest "shared 128 GB budget" picture (the locked intent); more layout work, couples the two surfaces. |
| **C — Service card** | `image-models-c-service-card.html` | An appliance-style ComfyUI **service** card: prominent VRAM ring + big Start/Stop/Free, then a tight model list. | Clearest as a "device"; diverges most from the existing drawer styling. |

All three read the same real data: ComfyUI reachable + **real VRAM total/free**
(`/system_stats`), each model's kind / on-disk size / resident estimate /
provisioned state, and the owner-only **start / stop / free** actions. Provisioning
(the weight download) stays the on-box `comfyui-setup.sh` step — these surfaces are
status + runtime control, not a downloader.

Earlier locked intent (the image UI shares the LLM drawer / a shared RAM meter)
maps most directly to **B**; **A** is the lighter consistent option; **C** is the
service-first reframe.
