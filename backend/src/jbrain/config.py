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
    # Future-GPU escape hatch: any OpenAI-compatible server (the llama-swap gateway
    # the local-llm profile runs, or an Ollama default).
    local_llm_url: str = "http://localhost:11434/v1"
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
