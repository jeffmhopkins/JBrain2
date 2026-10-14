# jbrain-rp — the jbrain pack's resource pack

The art for [`../jbrain-pack/`](../jbrain-pack/README.md)'s custom items and blocks. The
wrapper installs it into every world beside the behavior pack. BDS pushes it to each
player on join, and `texturepacks-required=true` (pinned by `../install.py`) makes
accepting it a condition of joining. Nobody installs anything on their client, and
nobody sees the Tricorder as a missing-texture square (plan §3c, rule 2).

| File | Holds |
| --- | --- |
| `textures/items/*.png` | Item icons: Tricorder, Power Pack, Wormhole Seed, and the Replicator's plain and programmed placers. |
| `textures/item_texture.json` | The `atlas.items` names (`jbrain_tricorder`, …) that `../jbrain-pack/items/*.json` refer to. |
| `textures/blocks/*.png` | Block faces: the No-Spawn Beacon (top, side, bottom), the Replicator (front, top, side, each with programmed and working variants), and the wormhole surface. |
| `textures/terrain_texture.json` | The block atlas names that `../jbrain-pack/blocks/*.json` refer to. |
| `textures/flipbook_textures.json` | The animated faces: the wormhole's swirl, and the Replicator's working front and top. |

The look of the items was chosen in `docs/mocks/minecraft-items/` (set A). The look of
the wormhole and the beacon is described in the canon, Bleepbloop_City's
[`notes/lore.md`](https://github.com/jeffmhopkins/Bleepbloop_City/blob/main/notes/lore.md).

## Changing it

- A new texture needs its atlas entry here **and** the item or block that names it in
  `../jbrain-pack/`, in the same PR. The supervisor's pack tests
  (`test_minecraft_jbrain_pack.py`, `_replicator.py`, `_wormhole.py`, `_beacon.py`) check
  that every name resolves to a PNG that exists.
- When this pack's `manifest.json` version changes, update `../jbrain-pack/manifest.json`'s
  dependency on it to match. The tests check that too.
- Real clients cache resource packs by version, so a changed PNG under an unchanged
  version may not reach players who already have the pack.
