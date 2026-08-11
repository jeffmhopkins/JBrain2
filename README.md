# JBrain2

Personal knowledge system: notes in → RAG indexing → an LLM-maintained wiki
with notes as the sole sources of truth. Self-hosted on Ubuntu + Docker, private
by construction, and optionally fully offline on your own AI hardware.

- Docs map: [`docs/README.md`](docs/README.md)
- Design: [`docs/reference/ARCHITECTURE.md`](docs/reference/ARCHITECTURE.md)
- Services & components (the full inventory): [`docs/reference/SERVICES.md`](docs/reference/SERVICES.md)
- Phases: [`docs/ROADMAP.md`](docs/ROADMAP.md)
- Standards: [`docs/reference/DEVELOPMENT.md`](docs/reference/DEVELOPMENT.md)

## What it does

**Capture.** Write notes from your phone as an installable, offline-first PWA
(captures sync idempotently when you reconnect). Attach PDFs, images, audio, and
video — the box analyses them automatically (text/PDF extraction, OCR, image
captioning, and optional on-box speech-to-text and video understanding). A native
Android **owner app** wraps the same PWA with deterministic back and relays your
box's own notifications straight to your phone (self-hosted SSE, no Firebase).

**Organize & recall.** Every note is chunked, embedded, and searchable by meaning
*and* keyword (hybrid RAG). An LLM pipeline extracts **facts and entities** into a
citation-backed knowledge graph — a property-graph of typed, time-versioned facts —
resolves conflicts (newest-wins with a human **review inbox**), and canonicalizes
predicates into a registry. On top of it, a **machine-written wiki** maintains
itself: every claim cites a note, articles join the same search surface, a nightly
**health sweep** flags contradictions and stale claims, and you correct an article
by out-arguing it on its **Talk** board with a correction note — never by editing
prose.

**Ask & act — the Full Brain agent.** A tool-calling chat agent with a deep
toolbox: it searches your knowledge; manages lists and appointments; generates,
edits, analyses, compares, and OCRs images; transcribes audio and analyses video
and live streams; renders charts grounded in your data; answers location,
weather, climate-history, and storm questions; triages your Gmail; and reports on
the box's own health. On the open web it runs **deep research** — one question
becomes a structured, fully-cited report — fans out web-sandboxed sub-agents,
searches Grokipedia, and pulls **public records** (court, license, federal/state
registries). Reports land in a searchable **Research Library** you can revisit or
publish as a revocable public link. Personas are scoped: the knowledge persona is
firewalled to your data; the web persona has internet access and never sees your
knowledge base. Everything the agent (or the wiki) writes reads back to you, and
the PWA and wall can **read it aloud** with on-box neural voices.

**Structured records.** Lists, appointments (published as an **ICS calendar
feed** your phone subscribes to), and typed lab results — all tracing back to a
source note. (Full medical-record import is in progress; see the roadmap.)

**Automate & schedule.** A workflow engine (events → triggers → pipelines → runs)
drives ingestion and scheduled maintenance sweeps — all run-logged and fireable on
demand from the Ops screen. Saved-prompt **Tasks** re-run the agent on a schedule
or on demand, and owner-minted **guided-intake links** let other people contribute
notes through a guided interview you approve.

**Family & devices.** A companion Android app (**JBrain360**) reports device
location; the box keeps per-person trails, geofences, and presence on a live map.
A **wall display** turns the box's own monitor into a neural-vitals kiosk — and
into **JPet**, a Tron-styled 3D wireframe play-pet the kids drive and talk to from
their phones.

**Create & compute on the box.** Generate and edit images; run sandboxed on-box
**coding sessions** (a Grok CLI against local models, with a live preview and a
shareable read-only session view); and launch long **scientific computations**
from a self-serve **Math** launcher — a supervised job with a live terminal that
publishes a public results page when it finishes.

**Your hardware, your data.** One Docker stack on Ubuntu. Cloud LLMs by default,
or opt-in **on-box AI** on an AMD Strix Halo box: a catalogue of local LLMs
(llama.cpp/Vulkan, loaded and swapped on demand), image generation (ComfyUI /
Qwen-Image), transcription (whisper), and neural **read-aloud** (Kokoro), so
nothing leaves the machine. Self-hosted web search, page rendering, and OCR run in
their own containers too.

**Private by construction.** Postgres Row-Level Security enforces
health / finance / location **domain firewalls** at the database layer — app bugs
can't leak across them. The root credential is a single **owner key** printed once
(no accounts, no email recovery). Reach the box on your own domain, through a
Cloudflare Tunnel with no static IP or port-forwarding, or over the LAN when the
internet is down.

See [`docs/reference/SERVICES.md`](docs/reference/SERVICES.md) for the full
inventory of every container, service, and baked-in function.

## Install (fresh Ubuntu server)

```sh
git clone https://github.com/jeffmhopkins/JBrain2.git
cd JBrain2
sudo bash deploy/install.sh
```

The installer sets up Docker, asks for your domain and LLM API keys, builds
the images from source, and prints your **owner key** exactly once — copy it
to paper. Manage the stack with
`jbrain status | restart | logs | update | backup | restore | reset-owner-key`;
`jbrain update` pulls the latest main and rebuilds. Opt-in on-box features are
one command each — `jbrain enable-local-models`, `enable-whisper`,
`enable-lan`, `enable-jcode-preview`, `strix-halo-host-setup` — with image
generation and coding sandboxes enabled by their setup scripts under `scripts/`.

## Development

```sh
./scripts/dev-setup.sh   # installs backend, supervisor, and frontend deps
```

- Backend: `cd backend && uv run pytest` (RLS integration tests need Docker)
- Supervisor: `cd supervisor && uv run pytest`
- Frontend: `cd frontend && npm run test`

### Docs travel with the code

Documentation is a first-class deliverable, not an afterthought. Every PR
reconciles the docs it affects **in the same PR** — a plan's status flipped or
archived when its waves land, reference/runbook docs corrected when behaviour
changes, and each doc's `Last verified` stamp bumped. New docs are filed by kind
(`docs/reference/` · `docs/runbooks/` · `docs/plans/`), and volatile counters
(like a migration head) never get hardcoded in prose.

This is binding — see [`docs/DOC_LIFECYCLE.md`](docs/DOC_LIFECYCLE.md) (and
`CLAUDE.md` non-negotiable #9). The `docs` CI gate enforces the mechanical rules;
run it before a docs change:

```sh
bash scripts/docs-freshness.sh
```
