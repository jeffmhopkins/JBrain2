# probe-pack — the home-test kit

A behavior pack that only **watches**. Every line it prints starts with `[jbrain-probe]`,
so the assistant can read the answers off the server log while the owner plays. It was
the M0 probe (plan §M0, item 5) and answered these questions:

- Does a stable-API pack load with no experiments?
- Does script output reach the server console?
- Does `scriptevent` arrive?
- Is there a stable chat event?
- Is a dead player's inventory still readable?

It now also logs, once per player, when the crew's items (the Tricorder, the Power Pack)
first reach them, with the Tricorder's lock and keep-on-death flags.

It is **not** installed by default. The assistant turns it on and off over the debug
console:

```
POST /api/debug/minecraft/probe-pack {"install": true}    # or false
```

That restarts the server with the pack installed in (or removed from) the loaded world.
The whole sitting is in `docs/runbooks/MINECRAFT_HOME_TEST.md`.

What the kit used to carry (`/jb:dave`, the Tricorder, the Power Pack and its recipe, the
waypoint arrow) has moved to the always-installed [`../jbrain-pack/`](../jbrain-pack/README.md).
Two packs that define the same name collide, so **don't add items, blocks or commands
here**. Keep it to logging.
