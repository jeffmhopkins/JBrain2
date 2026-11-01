# The house's DNS (AdGuard Home on the box)

> **Status:** Living · **Last verified:** 2026-11-01

The box runs AdGuard Home as the `adguard` service. Every device whose lookups reach it
gets ads and smart-TV telemetry blocked, and the box keeps a week's log of which device
looked up what. That log is how you see what the TVs send. Behind Spectrum's router a
device reaches it only when its DNS is set by hand (see "What Spectrum's DNS setting
actually does" below).
The build plan is `../archive/HOME_DNS_PLAN.md`.

It is part of the stock stack, so **Ops → Update** installs it and nothing goes in `.env`.
Until a device is pointed at it, it logs nothing.

## Turning it on (the router, from the phone)

This box sits behind a Spectrum router (Advanced WiFi, `SAX2V1R`), managed only through
the **My Spectrum** app:

1. **Reserve the box's address**, so it never moves. In the app, find the box under
   **Devices** and reserve its IP. The address is the one in `GET
   /api/debug/home-network` → `dns_addresses`.
2. **Services → Router → DNS Server → Manage DNS Servers.** Set
   - **Primary:** the box's LAN address.
   - **Secondary:** `94.140.14.14` (AdGuard's public filtering DNS). Spectrum's router
     falls through to it when the Primary does not answer, which on this router is
     always (below), so this choice is what filters the whole house.
3. **Point the devices you want to see at the box** by hand: in a TV's network settings
   (manual or advanced), set DNS to the box's address. Each one then appears on the
   Home network screen.

Phones with **Private DNS** set (Android → Network → Private DNS) skip the router's DNS
entirely, at home and away. That is fine: keep `dns.adguard-dns.com` on a phone that
leaves the house.

## What Spectrum's DNS setting actually does

Answered on the live box, 2026-11-01: **the SAX2V1R keeps DNS for itself, and never
reaches a server on the LAN.** Devices are handed the router (`192.168.1.1`, plus its IPv6
address) as DNS. With the box as Primary, the box logged not one lookup, not even from
the router, and the router fell through to the Secondary. So with Spectrum's router:

- **Blocking works for every device anyway**, through the Secondary (`94.140.14.14`,
  AdGuard's public filtering DNS). That is why the Secondary must be a filtering one.
- **The box sees only devices whose DNS is set by hand.** On a TV: network settings →
  manual / advanced → DNS = the box's address (some TVs also want a fixed IP for that).
  Each such device then appears on the Home network screen by itself. A phone's Private
  DNS skips this too.
- **Every device at once** needs your own router behind Spectrum's modem, handing out the
  box as DNS (a GL.iNet Flint 2 or a Firewalla; see the plan, §4).

The Primary can stay pointed at the box: it costs nothing, and a router firmware that
starts honouring it will route the house through the box with no further change.

## What it blocks

The first start seeds `deploy/adguard/AdGuardHome.yaml`:

- **AdGuard DNS filter**: general ads and trackers.
- **HaGeZi Pro**: ads, trackers and device telemetry (TVs and phones among them), from
  AdGuard's own list registry. A box first seeded with the old HaGeZi Native Tracker URL
  (jsDelivr `@latest`, which answers 403) is moved to it at its next start.
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

- **The PWA:** the **Home network** launcher tile. Blocking on or off (with a pause for 10
  minutes, an hour, or until turned back on), every device ranked by how much of its recent
  traffic was blocked, and one device's names it tried to reach and its recent lookups. Tap
  a name to allow it or block it for every device (a TV menu that broke behind a list is
  fixed by allowing its name), and the pencil names a device. The blocked share is read
  from each device's last 200 lookups, because AdGuard counts lookups per device but not
  blocks; the week's totals in the status card are AdGuard's own.
- **The debug API** (`DEBUG_ACCESS.md`): `GET /api/debug/home-network` for the summary
  and `GET /api/debug/home-network/queries?client=<ip>&blocked=true` for one device's
  blocked lookups.
- **The owner API:** the same reads at `/api/home-network`, plus `POST
  /api/home-network/protection {"enabled": false, "minutes": 10}` to pause blocking for a
  site that breaks behind a list. It comes back on by itself. `GET
  /api/home-network/devices/<ip>` is one device; `GET`/`POST /api/home-network/rules`
  (`{"domain": …, "action": "block"|"allow"|"clear"}`) are the one-name rules, written as
  `||name^` / `@@||name^` beside anything else in AdGuard's custom rules, which they never
  touch; `POST /api/home-network/devices/<ip>/name {"name": …}` names a device.

## How it is wired, and why

- **Private sources only, no rate limit.** `allowed_clients` is re-pinned to the private
  ranges every start, so a box in the router's DMZ or behind a forwarded :53 never answers
  the internet. With that in place AdGuard's default limit (20/s per /24, the whole
  house's budget) is turned off.
- **Device names from the router.** Reverse lookups go to the router (the
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

- **A device set to the box cannot resolve names.** The box is down: set that device's DNS
  back to automatic. The rest of the house goes through the router, which falls through
  to the Secondary on its own; if it does not, set the router's Primary to the
  Secondary's address in the Spectrum app.
- **`/api/home-network` says 503 "unreachable".** The container is down or restarting.
  Restart `adguard` from **Ops**. Its log (`/api/debug/logs/adguard`) starts with the addresses it
  bound, or with why it could not (`no adguard.token`, `no default route`).
- **502 "refused the api's password".** The secrets volume was re-minted while `adguard`
  ran, and an update does not necessarily restart it. Restart `adguard` from **Ops**.
- **`adguard` keeps restarting** with "not a private address" in its log: the box's
  default route gives it a public address, and serving DNS there would be an open
  resolver for the internet. It refuses on purpose.
