# JBrain2

Personal knowledge system: notes in → RAG indexing → an LLM-maintained wiki
with notes as the sole sources of truth. Self-hosted on Ubuntu + Docker.

- Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Phases: [`docs/ROADMAP.md`](docs/ROADMAP.md)
- Standards: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)

## Install (fresh Ubuntu server)

```sh
git clone https://github.com/jeffmhopkins/JBrain2.git
cd JBrain2
sudo bash deploy/install.sh
```

The installer sets up Docker, asks for your domain and LLM API keys, builds
the images from source, and prints your **owner key** exactly once — copy it
to paper. Manage the stack with
`jbrain status | restart | logs | reset-owner-key | update | backup`;
`jbrain update` pulls the latest main and rebuilds.

## Development

```sh
./scripts/dev-setup.sh   # installs backend, supervisor, and frontend deps
```

- Backend: `cd backend && uv run pytest` (RLS integration tests need Docker)
- Supervisor: `cd supervisor && uv run pytest`
- Frontend: `cd frontend && npm run test`
