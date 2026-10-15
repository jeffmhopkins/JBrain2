# cast — the house's Chromecast TVs

> **Status:** Living · **Last verified:** 2026-10-15

The `cast` sidecar is the first build of jpanel Cast (`docs/mocks/jpanel-cast/README.md`): a
spike that answers what each real TV reports before any notice is cast to one. It finds every
Cast device on the LAN by mDNS, holds a connection to each, and logs every status change, so
the owner can switch TVs between states and the timeline is read back afterwards. It casts two
things, and only to a TV that is idle or off: the spike's test card, and a notice's own frame,
which the api draws and posts here (`POST /tvs/<id>/cast`, plan K1,
`docs/plans/JPANEL_CAST_PLAN.md`). Either is quit when its time is up, or on `POST
/tvs/<id>/stop`, but only while it is still ours on the screen.

| File | What it is |
| --- | --- |
| `gate.py` | The owner's rule as a pure function (`idle` / `standby` cast; `busy` / `unknown` never), and the per-TV timeline. |
| `frame.py` | The 1920×1080 test card, drawn with the stdlib. |
| `tvs.py` | The only module that imports PyChromecast: discovery, connections, readings, the test cast, a notice's cast and its stop. |
| `server.py` | The HTTP surface, bearer-gated except `/healthz` and the card's one-time `/frame/<nonce>.png`. |

**Host network**, like `minecraft`: mDNS never reaches a bridge network, and a TV fetches the
card from the box's own LAN address. The control port (`CAST_PORT`, default 19190) is therefore
on the LAN and takes `cast.token`, which the api mints into the `internal_secrets` volume this
container mounts read-only. The api reaches it through `host.docker.internal`; the routes are
`/api/debug/cast*` (`docs/runbooks/DEBUG_ACCESS.md`) and the owner's `/api/cast/*`.

Tested and typechecked by `supervisor` (its pytest loads these files by path, its pyright
includes this directory, and PyChromecast is in its dev extra at the image's pin). Nothing
lints this directory, so keep it to 88 columns by hand.
