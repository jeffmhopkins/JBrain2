# Home network — the house's DNS

> **Status:** **Approved by the owner** (2026-11-01): **A, "Devices first"** is the binding spec · **Last verified:** 2026-11-01

Open `index.html`, then each variant. Every page is standalone and clickable on a phone: devices
open, names open a sheet that allows or blocks them, and the dashed strip at the top switches
between working, router-forwards, not-set-up and DNS-down states.

- **`a-devices.html` — chosen.** Each device ranked by how much of its traffic is blocked; a
  device shows the names it tried to reach and its recent lookups. Built as
  `frontend/src/screens/HomeNetworkScreen.tsx`; the settled rules are in
  `../../reference/DESIGN.md` "Home network".
- `b-companies.html` — leads with the reports stopped this week, grouped by the company
  receiving them. Not built: AdGuard knows names and lists, not who owns a name.
- `c-live-log.html` — a running feed, blocked-only by default, one chip per device. Not built;
  the device screen's Recent lookups is its useful half.

The data is a made-up week for the house; kid devices use placeholder names.
