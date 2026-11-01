# The house's DNS: block ads and TV telemetry, and see who sends what

> **Status:** In progress · **Last verified:** 2026-11-01 · **Waves:** A✅ B◻️

Owner, 2026-11-01: *"do research on what container we can make that will monitor my local
network for those two of things. I really don't like what tvs sending businesses too much
info etc."* The "two things" are blocking smart-TV ads and tracking, and seeing what each
device on the network talks to. The owner's phone keeps its own Private DNS
(`dns.adguard-dns.com`); this plan is for everything else in the house.

## 1. The choice

AdGuard Home in a container on the box, with the router handing out the box as DNS. It
blocks by name for every device and logs every lookup per device. One container covers
both asks.

Rejected:

- **Pi-hole and Blocky.** Blocking is the same lists either way. AdGuard Home is one Go
  binary with a complete REST API (status, stats, per-client query log, protection, rules)
  that the api can read, and encrypted upstreams built in.
- **Traffic analysis (ntopng, Zeek, Suricata).** These see traffic only if the box is in
  the path or on a mirrored switch port. Spectrum's router offers neither, and DNS is what
  names the companies a TV talks to anyway.
- **NetAlertX** (new-device alerts). Useful, but a separate ask. AdGuard's client list
  already names every device that looks something up.

## 2. Constraints the design follows from

- **Spectrum's router** (`SAX2V1R`) exposes only a Primary/Secondary DNS pair in the My
  Spectrum app: no DHCP options, no port-53 redirect, no firewall rules. Whether that pair
  reaches devices or only the router's own resolver is not documented. §4 covers both.
- **Port 53.** systemd-resolved's stub holds `127.0.0.53:53` on Ubuntu, so a wildcard bind
  fails. Disabling the stub is a host step (CLAUDE.md #10). So the container is
  host-networked and binds the LAN address only.
- **No terminal, no `.env` edit.** It is stock stack, and Ops → Update is the whole
  install.
- **The UI must not be on the LAN.** AdGuard's UI and API can change the house's DNS. They
  listen on the docker bridge gateway, which the api reaches and the LAN does not, behind a
  password only the api holds.

## 3. Waves

### A — the container and the api's reads ✅

- `adguard` service (`deploy/Dockerfile.adguard`, digest-pinned `adguard/adguardhome`),
  host network, `adguard_conf`/`adguard_work` volumes, kept up through an update's
  quiesce.
- `deploy/adguard/entrypoint.sh`: waits for `adguard.token`, finds the LAN and bridge
  addresses, seeds the config on first start (`AdGuardHome.yaml`: AdGuard DNS filter,
  HaGeZi Native Tracker and Perflyst Smart TV; DoH upstreams; a week of log), and re-pins
  the addresses and the one user at each start (`pin.awk`).
- The api mints `adguard.token` (`internal_auth.ADGUARD`) and reads AdGuard through
  `jbrain.homenet.adguard`: owner `GET /api/home-network`, `GET
  /api/home-network/queries`, `POST /api/home-network/protection`, and the two reads under
  `/api/debug/home-network`.
- Owner step after merge: the router settings in `../runbooks/HOME_DNS.md`.

### B — the Home network screen (GUI gate)

The PWA screen: blocking on/off with a pause, each device with how much it asked and how
much was blocked, a device's log (blocked-only by default), naming a device, and blocking
or allowing a name in one tap. Needs owner-side routes the api does not have yet:
`POST /api/home-network/devices` (name a device; AdGuard persistent clients) and `POST
/api/home-network/rules` (add or remove one `||name^` / `@@||name^` rule). Per
`../reference/PROCESS.md`, three interactive mocks go to the owner before any of it is
built.

## 4. Open: what Spectrum's DNS field does

Wave A's summary answers it on the live box: one device in `devices` (the router) means the
router forwards. If so, blocking still works for the whole house, but per-device logs need
the owner's own router behind Spectrum's modem (a GL.iNet Flint 2 or a Firewalla). That
router would hand out the box as DNS, redirect stray port-53 traffic to it, and block the
public DoH resolvers hardcoded TVs use. That is an owner purchase, not a wave.
