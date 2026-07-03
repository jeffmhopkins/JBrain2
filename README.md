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
captioning, and optional on-box speech-to-text and video understanding).

**Organize & recall.** Every note is chunked, embedded, and searchable by meaning
*and* keyword (hybrid RAG). An LLM pipeline extracts **facts and entities** into a
citation-backed knowledge graph, resolves conflicts (newest-wins with a human
**review inbox**), and maintains a **machine-written wiki** — every claim cites a
note, and you correct it by out-arguing it with a correction note, never by
editing prose.

**Ask & act — the Full Brain agent.** A tool-calling chat agent that searches
your knowledge, manages lists and appointments, generates and edits images,
answers location and weather/storm questions, triages your Gmail, and fans out
web-sandboxed research sub-agents. Personas are scoped: the knowledge persona is
firewalled to your data; the web persona has internet access and never sees your
knowledge base.

**Structured records.** Lists, appointments (published as an **ICS calendar
feed** your phone subscribes to), and typed lab results — all tracing back to a
source note.

**Family location (JBrain360).** A companion Android app reports device location;
the box keeps per-person trails, geofences, and presence on a live map.

**Automation.** A workflow engine (events → triggers → pipelines → runs) drives
ingestion and scheduled maintenance sweeps — all run-logged and fireable on
demand from the Ops screen.

**Your hardware, your data.** One Docker stack on Ubuntu. Cloud LLMs by default,
or opt-in **on-box AI** on an AMD Strix Halo box — local LLMs (llama.cpp), image
generation (ComfyUI / Qwen-Image), and transcription (whisper), so nothing leaves
the machine. Optional sandboxed on-box coding sessions, too.

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
`jbrain status | restart | logs | reset-owner-key | update | backup | restore`;
`jbrain update` pulls the latest main and rebuilds.

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
