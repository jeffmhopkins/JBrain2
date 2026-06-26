from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: an unset compose var arrives as "" (e.g. ${FOO:-});
    # treat that as absent so it falls back to the default rather than failing to
    # parse — load-bearing for the wipe one-shot's off-by-default bool guard.
    model_config = SettingsConfigDict(
        env_prefix="JBRAIN_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    database_url: str = "postgresql+asyncpg://jbrain_app:jbrain_app@localhost:5432/jbrain"
    supervisor_url: str = "http://supervisor:9000"
    supervisor_token: str = ""
    session_cookie: str = "jbrain_session"
    blob_dir: str = "/data/blobs"
    backups_dir: str = "/data/backups"
    embed_url: str = "http://embed:80"
    embed_model: str = "BAAI/bge-small-en-v1.5"

    # MQTT secure spine (JBrain360, opt-in `mqtt` compose profile). The broker
    # (Mosquitto + go-auth) calls the API's /internal/mqtt-* endpoints; the ingest
    # consumer connects to the broker as a server-side subscriber authenticated by
    # `mqtt_ingest_secret` (a shared service secret, NOT a device key) and is granted
    # read-only `owntracks/#`. Empty secret disables that identity (fail-closed: the
    # endpoints reject it and the consumer does not start).
    mqtt_broker_host: str = "mqtt"
    mqtt_broker_port: int = 1883
    mqtt_ingest_username: str = "jbrain-ingest"
    mqtt_ingest_secret: str = ""
    # Device-facing broker coordinates returned in the OwnTracks pairing config (the
    # public hostname/port a phone connects to). Empty host until a deploy sets it.
    mqtt_public_host: str = ""
    mqtt_public_port: int = 8883
    # The dashboard URL the forked app's WebView loads; set when M4 lands.
    dashboard_url: str = ""
    # The public, internet-reachable base URL of this box (e.g. the Cloudflare
    # Tunnel host). Embedded in a minted debug-token payload so a handed-off token
    # points an EXTERNAL assistant at the public host — even when the token is
    # minted from the LAN PWA. The LAN-only web console ignores this (it calls the
    # API same-origin); only off-box clients use it. Empty = fall back to the
    # request origin (dev / single-host installs).
    public_base_url: str = ""
    # The Origins allowed to open the live WebSocket (CSWSH defense, plan B8): a
    # comma-separated allow-list. A browser always sends `Origin` on the WS
    # handshake, so a cross-site page on a victim's machine is rejected. Empty =
    # unset (dev / native clients that send no Origin); when set, a present Origin
    # MUST match. See `allowed_ws_origins`.
    dashboard_allowed_origins: str = ""

    @property
    def allowed_ws_origins(self) -> frozenset[str]:
        return frozenset(o.strip() for o in self.dashboard_allowed_origins.split(",") if o.strip())

    # Map basemap tiles, served through the server-side proxy/cache (api/tiles.py)
    # so the phone fetches tiles only from this box, never a third-party tile host.
    # A DELIBERATE relaxation of the location plan's L1 ("no tiles leave the box"):
    # the server fetches-and-caches upstream tiles, so the upstream learns the
    # coarse map areas the owner browses (tied to the server IP, never the device).
    #
    # Two selectable schemes — `dark` and `light` — each a separate upstream with its
    # OWN on-disk cache namespace, so the app's tile toggle never serves one scheme's
    # cached z/x/y under the other. The endpoint takes the scheme as a path segment
    # (/api/tiles/{scheme}/{z}/{x}/{y}.png); the default is what a request to an
    # unknown/legacy path resolves to. Empty disables THAT scheme (its requests 404
    # and the map degrades to the on-box schematic); an empty default scheme disables
    # tiles for clients that don't pin a scheme.
    # Defaults: CARTO "Dark Matter" / "Positron" — clean, minimal basemaps. Keyless;
    # © OpenStreetMap © CARTO. Swap either to any {z}/{x}/{y}.png raster style; the
    # cache namespaces by URL, so a change re-fetches cleanly (no stale-style tiles).
    tile_upstream_url: str = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
    tile_upstream_url_light: str = "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"
    tile_default_scheme: str = "dark"
    # Sent on every upstream tile fetch — OSM's tile policy requires an honest UA.
    tile_user_agent: str = "JBrain2 self-hosted personal instance"
    tile_cache_dir: str = "/data/tiles"
    tile_max_zoom: int = 19
    # Pinned, owner-configured base URLs for the egress connectors (#9). Free,
    # no-auth NLM services; the egress guard fills only typed slots, never a URL.
    rxnav_url: str = "https://rxnav.nlm.nih.gov"
    medlineplus_url: str = "https://connect.medlineplus.gov"
    # The self-hosted SearXNG metasearch instance backing the jerv chatbot's
    # web_search/web_fetch tools (docs/ASSISTANT.md "Agent selection"). On-box, so
    # a jerv search leaves the box only via SearXNG's own upstreams — the same
    # local-first posture as the geocoder. The compose service is part of the stock
    # stack, so this default points at a running instance; empty disables web search
    # (the tool returns "not configured") but the sidecars still load so jerv always
    # has its handlers.
    searxng_url: str = "http://searxng:8080"
    # The external reverse-geocoder fallback (Phase 7 Wave 4b), Nominatim-compatible.
    # DEFAULT OFF: empty means the connector is never registered, so there is no
    # off-box geocoding path at all. When set, a lookup still leaves the box only on
    # an owner-approved egress Proposal (coordinates only — no free-text slot).
    external_geocoder_url: str = ""
    # Cookies require HTTPS in production; tests and local dev run plain HTTP.
    secure_cookies: bool = True

    # One-time install reset (W3.3 cutover): when set, the `jbrain.install_wipe`
    # one-shot drops + rebuilds the schema, clears blob/backup storage, and
    # enables the v3 pipeline — then writes a sentinel so it NEVER runs twice.
    # Off by default; a deliberate, destructive opt-in for a fresh install.
    wipe_on_first_deploy: bool = False

    anthropic_api_key: str = ""
    xai_api_key: str = ""
    # Self-hosted local models are an OFF-BY-DEFAULT opt-in: the stock deploy
    # routes everything to the cloud providers, and the settings screen offers no
    # local options until an operator turns this on (deploy/install.sh prompt →
    # the `local-llm` compose profile + scripts/local-llm-setup.sh). When false the
    # `local` provider client is still wired but nothing routes to it.
    local_llm_enabled: bool = False
    # OPT-IN owner debug console (docs/DEBUG_ACCESS.md): the gate for the
    # capability-token surface (/api/debug/*) the owner uses to let an external
    # assistant run prompt iteration, read-only SQL, logs, and live LLM routing.
    # OFF by default — when false the debug router is not mounted and minting is
    # refused, so the feature adds zero surface unless the owner turns it on.
    debug_access_enabled: bool = False
    # Per-note pipeline flow trace (jbrain.analysis.flow_trace): integrate_note
    # emits one structured INFO event per seam — extract → integrate → recover →
    # plan → per-fact commit decision — each keyed by note_id, so an operator
    # tailing the worker logs can watch a single note's facts flow end to end and
    # see exactly where an edge is dropped, refreshed, or superseded. Pure
    # observability: it changes no disposition. AUTO-ARMS when debug_access_enabled
    # is on (an enabled console is the debugging session this is for); this flag is
    # an explicit override to trace WITHOUT the console. Read once per process, so
    # flip the env and restart the worker.
    analysis_trace: bool = False
    # Future-GPU escape hatch: any OpenAI-compatible server (the llama-swap gateway
    # the local-llm profile runs, or an Ollama default).
    local_llm_url: str = "http://localhost:11434/v1"
    # OPT-IN on-box image generation: a ComfyUI service (Qwen-Image on the owner's
    # Strix Halo box) JBrain manages through the `comfyui` compose profile, the
    # sibling of the local-llm gateway (docs/IMAGE_GEN_SERVICE_PLAN.md). EMPTY URL
    # DISABLES the feature: main.py wires no client and the tools never reach the
    # registry — graceful degrade, mirroring a provider hidden when unkeyed. The URL
    # is the functional gate; `comfyui_enabled` mirrors the install-time choice for
    # parity with local_llm_enabled. scripts/comfyui-setup.sh sets both.
    comfyui_url: str = ""
    comfyui_enabled: bool = False
    # Catalog ids (jbrain.image_gen.catalog) the operator has provisioned and wants
    # offered in settings (Wave G5/G6). Set by the setup path alongside the weights.
    comfyui_models: list[str] = []
    # Read-only mount of the provisioned image weights (scripts/comfyui-setup.sh's
    # ./comfyui-models), so the settings screen can report each model's real on-disk
    # footprint for the shared RAM meter. The API only stats files here — host/infra
    # files, not application blobs, so the read sits outside the storage abstraction
    # (same rationale as local_models_dir / host_metrics' /proc read).
    comfyui_models_dir: str = "/data/comfyui-models"
    # Overall budget for ONE render (cold model load + sampling + tiled VAE decode).
    # On the iGPU a large/high-step image — 1536x1536 at 45 steps — plus a cold model
    # load (we free ComfyUI between renders) runs well past the 1024x1024/20-step base,
    # so this is generous: it's the ceiling for a render that genuinely hung, not the
    # expected duration. Raise it for even larger jobs.
    comfyui_timeout: float = 1800.0
    # Local models on one box are far slower than the cloud APIs — a 30B+ doing a
    # long OCR/extraction at a few dozen tok/s can run for minutes. The 120s cloud
    # default would time out mid-generation and the job would retry-loop, never
    # finishing. Give the local client a generous ceiling; queue backoff still
    # covers a genuinely wedged server.
    local_llm_timeout: float = 600.0
    # The model name the bare `local` provider spec resolves to (local:<model>)
    # when no curated catalog model is selected — the local server's served model.
    # A plain default so the spec is always concrete.
    local_llm_model: str = "local"
    # Catalog ids (jbrain.llm.local_catalog) the operator has provisioned and wants
    # offered in the settings screen. Empty + enabled falls back to the single
    # generic `local_llm_model` escape-hatch choice. Set by the install/update path
    # (JBRAIN_LOCAL_MODELS) alongside the downloaded weights.
    local_models: list[str] = []
    # Read-only mount of the provisioned weights (scripts/local-llm-setup.sh's
    # ./local-models), so the settings screen can report each model's REAL on-disk
    # footprint instead of the catalog's nominal estimate. The API only stats files
    # here — host/infra files, not application blobs, so the read sits outside the
    # storage abstraction (same rationale as host_metrics' /proc read).
    local_models_dir: str = "/data/local-models"
    # Whether the gateway keeps the recommended models co-resident (a non-swapping
    # llama-swap group) rather than swapping one at a time. Mirrors the install-time
    # LOCAL_LLM_RESIDENT_GROUP so a runtime config regeneration (after a
    # context-window edit) reproduces the same group the setup script wrote.
    local_llm_resident_group: bool = False
    # OPT-IN on-box speech-to-text: whisper.cpp served by the same llama-swap
    # gateway the local-llm profile runs (docs/WHISPER_TRANSCRIPTION_PLAN.md), so
    # it loads on first request and the gateway frees it when idle — and the
    # transcribe job/tool additionally unload it the moment they finish. Audio (and,
    # fast-follow, video) attachments transcribe through it, and jerv gets a
    # transcribe tool. EMPTY URL DISABLES the feature: no client is wired, audio
    # attachments extract to nothing, and the tool reports "not configured" — the
    # same graceful degrade as comfyui_url. `whisper_enabled` mirrors the
    # install-time choice for parity with local_llm_enabled / comfyui_enabled.
    whisper_url: str = ""
    whisper_enabled: bool = False
    # The served-model name the gateway resolves to a loaded whisper.cpp model
    # (and the name LocalGateway.unload() evicts). A plain default so the request
    # is always concrete; the setup script writes the provisioned name.
    whisper_model: str = "whisper"
    # Generous ceiling for one transcription: a long clip on a cold model load
    # (reading weights, then decoding at on-box speeds) can run for minutes, and a
    # too-tight timeout would retry-loop mid-decode. Queue backoff still covers a
    # genuinely wedged server.
    whisper_timeout: float = 300.0
    # Per-attachment size budget (the docs/ANALYSIS.md "Dispatcher-level policy"
    # cap, OCR's MAX_OCR_BYTES sibling): ingest skips enqueueing transcription for
    # larger files, with a logged warning and no cache row, so a smaller re-upload
    # transcribes normally. 100 MB ~ a long lossy recording.
    whisper_max_bytes: int = 100 * 1024 * 1024

    # OPT-IN Gmail access for the `archivist` persona (docs/EMAIL_ARCHIVIST_PLAN.md):
    # OAuth2 client credentials + a long-lived refresh token, minted once by
    # scripts/gmail-oauth-bootstrap.py and pasted here. No token table, no DB — a
    # single-owner box, so a config secret mirrors `mqtt_ingest_secret`. EMPTY
    # `gmail_refresh_token` DISABLES the feature (fail-closed): the client is not
    # wired and the gmail_* tools drop from the registry, the same graceful degrade
    # as comfyui_url/whisper_url. The scope minted is `gmail.modify` (read + label +
    # archive, never delete); `gmail_api_url`/`gmail_token_url` are pinned, never
    # model-supplied.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_api_url: str = "https://gmail.googleapis.com/gmail/v1"
    gmail_token_url: str = "https://oauth2.googleapis.com/token"

    # OPT-IN code mode (docs/proposed/JCODE_PLAN.md): a sandboxed coding-session
    # SIDECAR running Claude Code's agent engine against an on-box coder model, fronted
    # by the PWA. NOT a knowledge agent — it reads no notes and is not in the agent
    # loop; the api only PROXIES its control surface to the owner (Wave J2). EMPTY
    # `jcode_url` DISABLES the feature (fail-closed): no jcode routes, no launcher tile
    # — the same graceful degrade as comfyui_url/whisper_url. `jcode_token` is the
    # shared bearer the api presents to the internal control server; `jcode_enabled`
    # mirrors the install-time choice for parity with the other opt-in services.
    jcode_url: str = ""
    jcode_enabled: bool = False
    jcode_token: str = ""
    jcode_model: str = "qwen3-coder-next"
    # When a code-mode session opens, warm its coder model into the gateway: evict the
    # other resident models (llama-swap then gives the coder the whole box) and load it.
    # It is NOT unloaded when the session ends — it stays resident until another JBrain
    # task loads a different model, at which point the gateway swaps it out naturally.
    jcode_warm_on_create: bool = True

    # JSON object of per-task "provider:model" overrides, merged over the
    # adapter defaults — see jbrain.llm.router.TASK_DEFAULTS.
    llm_tasks: dict[str, str] = {}
    # JSON object of capability-tier "provider:model" overrides (high/low/vision),
    # merged over jbrain.llm.router.TIER_DEFAULTS. A prompt file declares the tier
    # it needs (strength:); the router resolves that tier to a model here — unless
    # the task is explicitly pinned in llm_tasks, which wins.
    llm_tiers: dict[str, str] = {}
    # "provider:model" -> $/M tokens, applied at query time over llm_usage —
    # docs/ANALYSIS.md "Cost estimates" (grok-4.3 rates, xAI docs June 2026).
    llm_prices: dict[str, dict[str, float]] = {
        "xai:grok-4.3": {"input_per_m": 1.25, "output_per_m": 2.50}
    }


def get_settings() -> Settings:
    return Settings()
