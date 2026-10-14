# jbrain-sim — node tests for the jbrain pack

`node --test` suites for [`../jbrain-pack/`](../jbrain-pack/README.md), plus stand-ins for
the game API that let the pack's real scripts run outside Minecraft.

You don't run these directly. The supervisor's pytest copies the pack into a temp
directory, installs the fakes as `node_modules/@minecraft/server` and
`node_modules/@minecraft/server-ui`, and runs `node --test` there. CI runs them the same
way:

```sh
(cd supervisor && uv run pytest tests/test_minecraft_jbrain_pack.py \
  tests/test_minecraft_replicator.py tests/test_minecraft_wormhole.py \
  tests/test_minecraft_beacon.py)
```

Node comes from `scripts/dev-setup.sh`. If node is missing, the tests fail with a clear
message; they are never skipped.

## The fakes

| File | Stands in for |
| --- | --- |
| `fake_server.mjs` | `@minecraft/server`: players, inventories, equipment, dynamic properties, scriptevents, custom commands, ticks. It is strict where the game is strict: an item read from a container is a copy, so a change only sticks once it is written back. |
| `fake_server_ui.mjs` | `@minecraft/server-ui`: action and modal forms. A test picks each answer with `forms.answer`. |
| `replicator_fake.mjs` | `fake_server.mjs` plus placed blocks with states, block containers (chests, hoppers), stack sizes and loaded chunks, for the Replicator. It is installed with `fake_server.mjs` beside it as `base.mjs`. |

The beacon, spawn-point and wormhole tests add the few extra game pieces they need in
their own file, so the shared fake stays small.

## The suites

| Suite | Covers | Run by |
| --- | --- | --- |
| `rules.test.mjs` | `rules.js`'s pure rules: the arrow, ownership, the death table, drop fates, unlocks, parsers, and Dave's lines. | `test_minecraft_jbrain_pack.py` |
| `pack.test.mjs` | `main.js` end to end: the Tricorder beamed and bound, the owner-only arrow, no Tricorder means no Dave, the Power Pack charm's deaths, `/dave` and its menu. | `test_minecraft_jbrain_pack.py` |
| `welcome.test.mjs` | Dave's welcome and the first-time beats, played through `main.js`. | `test_minecraft_jbrain_pack.py` |
| `scan.test.mjs` | The Tricorder's live scan. | `test_minecraft_jbrain_pack.py` |
| `transport.test.mjs` | Transport on request: the form, the landing check, the burned pack. | `test_minecraft_jbrain_pack.py` |
| `spawnpoint.test.mjs` | Respawn points and their kinds. | `test_minecraft_jbrain_pack.py` |
| `beacon.test.mjs` | The No-Spawn Beacon's decision table and sweep, and its unlock. | `test_minecraft_beacon.py` |
| `replicator.test.mjs` | The Replicator's rules and its script-run furnace. | `test_minecraft_replicator.py` |
| `wormhole_rules.test.mjs`, `wormhole.test.mjs` | Gate rules, and gates through `main.js`. | `test_minecraft_wormhole.py` |

A new suite only runs once a supervisor test copies and runs it. Add it to the right
`test_minecraft_*.py` in the same PR.
