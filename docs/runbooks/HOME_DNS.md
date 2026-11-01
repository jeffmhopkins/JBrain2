# The house's DNS (AdGuard Home on the box)

> **Status:** Living · **Last verified:** 2026-11-01

The box runs AdGuard Home as the `adguard` service. Once the router hands the box out as
DNS, every device in the house gets ads and smart-TV telemetry blocked, and the box keeps
a week's log of which device looked up what. That log is how you see what the TVs send.
The build plan is `../plans/HOME_DNS_PLAN.md`.

It is part of the stock stack, so **Ops → Update** installs it and nothing goes in `.env`.
Until the router points at it, it answers nothing.

## Turning it on (the router, from the phone)

This box sits behind a Spectrum router (Advanced WiFi, `SAX2V1R`), managed only through
the **My Spectrum** app:

1. **Reserve the box's address**, so it never moves. In the app, find the box under
   **Devices** and reserve its IP. The address is the one in `GET
   /api/debug/home-network` → `dns_addresses`.
2. **Services → Router → DNS Server → Manage DNS Servers.** Set
   - **Primary:** the box's LAN address.
   - **Secondary:** `94.140.14.14` (AdGuard's public filtering DNS). Devices fall back to
     the secondary when the box does not answer, for example while it reboots, so this
     choice keeps them filtered instead of falling back to Google.
3. Devices pick the change up when they renew their lease. Toggling Wi-Fi off and on
   forces it.

Phones with **Private DNS** set (Android → Network → Private DNS) skip the router's DNS
entirely, at home and away. That is fine: keep `dns.adguard-dns.com` on a phone that
leaves the house.

## What Spectrum's DNS setting actually does

Spectrum's app does not say whether its DNS field is handed to each device or used only
by the router itself, which then forwards everything to the box. Blocking works either
way, but per-device visibility differs:

- **Handed to devices:** `GET /api/debug/home-network` lists each device's own IP under
  `devices`. Everything works.
- **Forwarded by the router:** `devices` is a single IP, the router's. Every device's
  lookups are still filtered, but the log cannot tell the TV from a laptop. The fix is
  hardware: your own router behind Spectrum's modem, handing out the box as DNS (see the
  plan, §4).

## What it blocks

The first start seeds `deploy/adguard/AdGuardHome.yaml`:

- **AdGuard DNS filter**: general ads and trackers.
- **HaGeZi Native Tracker**: device and OS telemetry by vendor (Samsung, LG, Roku, Amazon,
  Apple, Xiaomi, ...).
- **Perflyst Smart TV**: smart-TV ad and telemetry hosts.

Upstream lookups go out encrypted (DNS-over-HTTPS to Quad9 and Cloudflare, in parallel),
so the ISP sees neither the names nor which ones were blocked. Lists and rules added
later through AdGuard's API survive restarts and updates. Only the listening addresses
and the UI password are re-set at each start.

Two limits no DNS blocker gets past:

- **TVs that hardcode their DNS** (`8.8.8.8`, or DNS-over-HTTPS on 443) never ask the box.
  In the log they look quiet. Catching them needs a router that can redirect port 53 and
  block those resolvers, which Spectrum's router cannot do.
- **Ads served from the same name as the content** (YouTube, some TV home screens) cannot
  be told apart by name. Blocking a TV maker's own ad domains can also blank its home
  screen; if a TV's menus break, check the TV's blocked names in the log.

## Reading it

- **The PWA:** the Home network screen is wave B of the plan. It is not built yet.
- **The debug API** (`DEBUG_ACCESS.md`): `GET /api/debug/home-network` for the summary
  and `GET /api/debug/home-network/queries?client=<ip>&blocked=true` for one device's
  blocked lookups.
- **The owner API:** the same reads at `/api/home-network`, plus `POST
  /api/home-network/protection {"enabled": false, "minutes": 10}` to pause blocking for a
  site that breaks behind a list. It comes back on by itself.

## How it is wired, and why

- **No rate limit, and device names from the router.** AdGuard's default limit (20/s per
  /24) is the whole house's budget, so it is off. Reverse lookups go to the router (the
  default gateway, re-pinned each start) because the host's resolver forwards to the box.
- **Host network, port 53 on the LAN address only.** Ubuntu's systemd-resolved holds
  `127.0.0.53:53`, so a wildcard bind (and any published `53:53`) fails. Freeing it is a
  host step, which the owner cannot run (CLAUDE.md #10). The entrypoint finds the LAN
  address from the default route at each start, so a new lease is picked up by a restart.
- **The UI and REST API are on the docker bridge gateway only** (`:3053`), the address the
  api reaches as `host.docker.internal`. No LAN device is told that address, though a host that
  adds a route to it could still knock, so the password is the real barrier. The one user is
  `jbrain`, and its password is `adguard.token`, which the api mints into
  `internal_secrets`. The entrypoint bcrypts it into the config at every start, so a
  re-minted token is picked up by a restart of `adguard`.
- **Kept up through an update.** `update-inner.sh` keeps `adguard` running through the
  quiesce, and the update's recreate restarts it only when its image or config changed (a
  few seconds, with devices on the secondary meanwhile).
- **The config is AdGuard's.** It rewrites the file whenever a setting changes, so the box
  never regenerates it. `deploy/adguard/pin.awk` re-pins three values (the DNS address,
  the UI address, the user) and leaves the rest as found.

## When it goes wrong

- **The house cannot resolve names.** Devices should fall back to the secondary on their
  own. If they do not, set the router's Primary back to the secondary's address from the
  Spectrum app; that is the whole rollback.
- **`/api/home-network` says 503 "unreachable".** The container is down or restarting.
  Restart `adguard` from **Ops**. Its log (`/api/debug/logs/adguard`) starts with the addresses it
  bound, or with why it could not (`no adguard.token`, `no default route`).
- **502 "refused the api's password".** The secrets volume was re-minted while `adguard`
  ran, and an update does not necessarily restart it. Restart `adguard` from **Ops**.
- **`adguard` keeps restarting** with "not a private address" in its log: the box's
  default route gives it a public address, and serving DNS there would be an open
  resolver for the internet. It refuses on purpose.
