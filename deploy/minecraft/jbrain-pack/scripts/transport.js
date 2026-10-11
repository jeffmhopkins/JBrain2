// Transport on request (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M10, "Teleport to a known
// place, one Power Pack per trip"; canon: Bleepbloop_City notes/lore.md, "Transport on
// request"). Dave's mc_transport sends `scriptevent jb:transport {…}` with one of the
// asker's own saved places; the pack checks their Tricorder and pack, asks Yes or No on a
// server form, and only on Yes, with the landing clear, teleports them and burns one pack.
// Every step answers on the console as `[jbrain] transport <id> <outcome> {json}`, which
// the tool reads back and logs. Not a JSON event line: the wrapper's event parser only
// takes `[jbrain] {…}`. Stable @minecraft/server and @minecraft/server-ui only.
import { system, world } from "@minecraft/server";
import { FormCancelationReason, MessageFormData } from "@minecraft/server-ui";
import { POWER_PACK, packToBurn } from "./rules.js";
import { DIMS, DIM_OF, expired, landing, offerText, parseTransport, parseTransportSwitch,
  parseVisited, refusal, switchIsOn, visitedAfter } from "./transport_rules.js";

const SWITCH = "jb:power_pack_transport";
const visitedKey = (id) => `jb:visited:${id}`;
// Vanilla's enderman teleport, at both ends.
const SOUND = "mob.endermen.portal";
const PARTICLE = "minecraft:portal_reverse_particle";
// A form can't open over the chat the player just typed /jb:dave in: try again shortly.
const BUSY_RETRY_TICKS = 20;
// The destination is usually far off and unloaded; a ticking area loads it, checked
// this often, this many times, before the trip is refused with the pack kept.
const LOAD_EVERY_TICKS = 10;
const LOAD_TRIES = 10;

const transportOn = () => switchIsOn(world.getDynamicProperty(SWITCH));

const alive = (player) => {
  try {
    return typeof player.isValid === "function" ? player.isValid() : player.isValid !== false;
  } catch {
    return false;
  }
};

const blockAt = (dim, loc) => {
  try {
    return dim.getBlock(loc);
  } catch {
    return undefined; // an unloaded chunk throws on some versions, undefined on others
  }
};

const effects = (dim, loc) => {
  try {
    dim.playSound(SOUND, loc);
    for (let i = 0; i < 3; i++) dim.spawnParticle(PARTICLE, { x: loc.x, y: loc.y + i * 0.6, z: loc.z });
  } catch {
    // Sound and sparkle are flavour; a trip never fails over them.
  }
};

export const initTransport = ({ carried, tricorderOn, say }) => {
  // One open form per player, by their id.
  const pending = new Map();

  const tell = (id, outcome, detail = {}) =>
    say(`transport ${id} ${outcome} ${JSON.stringify(detail)}`);

  const packStacks = (player) => carried(player).filter((s) => s.item.typeId === POWER_PACK);
  const packCount = (stacks) => stacks.reduce((n, s) => n + s.item.amount, 0);

  const visitedBy = (player, offer) => [
    ...new Set([...parseVisited(world.getDynamicProperty(visitedKey(player.id))), ...offer.visited]),
  ];

  const why = (player, offer) =>
    refusal({
      switchOn: transportOn(),
      tricorder: tricorderOn(player),
      packs: packCount(packStacks(player)),
      hereDim: DIM_OF[player.dimension.id],
      target: offer,
      visited: visitedBy(player, offer),
    });

  const burnOne = (stacks) => {
    const pick = packToBurn(stacks.map((s, i) => ({ i, amount: s.item.amount })));
    const { item, put } = stacks[pick.i];
    if (item.amount > 1) {
      item.amount -= 1;
      put(item);
    } else {
      put(undefined);
    }
  };

  const go = (player, offer) => {
    const dim = world.getDimension(DIMS[offer.dim]);
    const spot = { x: offer.x + 0.5, y: offer.y, z: offer.z + 0.5 };
    const area = `jbt${offer.id}`;
    try {
      dim.runCommand(`tickingarea add circle ${offer.x} ${offer.y} ${offer.z} 1 ${area} true`);
    } catch {
      // No free ticking area: a destination already loaded still works.
    }
    const done = (outcome, detail = {}) => {
      try {
        dim.runCommand(`tickingarea remove ${area}`);
      } catch {
        // Never added.
      }
      tell(offer.id, outcome, { player: player.name, label: offer.label, ...detail });
    };
    let tries = 0;
    const attempt = () => {
      try {
        if (!alive(player)) return done("offline");
        const feet = blockAt(dim, { x: offer.x, y: offer.y, z: offer.z });
        const head = blockAt(dim, { x: offer.x, y: offer.y + 1, z: offer.z });
        const bad = landing(feet, head);
        if (bad === "unloaded" && ++tries < LOAD_TRIES) {
          system.runTimeout(attempt, LOAD_EVERY_TICKS);
          return;
        }
        if (bad) return done(bad);
        const again = why(player, offer);
        if (again) return done("refused", { why: again });
        const stacks = packStacks(player);
        const before = packCount(stacks);
        const fromDim = player.dimension;
        const from = { ...player.location };
        if (!player.tryTeleport(spot, { dimension: dim, checkForBlocks: true, keepVelocity: false })) {
          return done("blocked");
        }
        burnOne(stacks);
        effects(fromDim, from);
        effects(dim, spot);
        done("moved", {
          from: {
            x: Math.floor(from.x),
            y: Math.floor(from.y),
            z: Math.floor(from.z),
            dim: DIM_OF[fromDim.id] ?? fromDim.id,
          },
          left: before - 1,
        });
      } catch (e) {
        done("failed", { error: String(e) });
      }
    };
    attempt();
  };

  const ask = (player, offer) => {
    const form = new MessageFormData()
      .title("Dave")
      .body(offer.text)
      .button1("Yes")
      .button2("No");
    form
      .show(player)
      .then((resp) => {
        const busy = resp.canceled && resp.cancelationReason === FormCancelationReason.UserBusy;
        if (busy && !expired(offer.at, Date.now()) && alive(player)) {
          system.runTimeout(() => ask(player, offer), BUSY_RETRY_TICKS);
          return;
        }
        pending.delete(player.id);
        if (busy) return tell(offer.id, "expired");
        if (resp.canceled || resp.selection !== 0) return tell(offer.id, "declined");
        if (expired(offer.at, Date.now())) return tell(offer.id, "expired");
        go(player, offer);
      })
      .catch((e) => {
        pending.delete(player.id);
        tell(offer.id, "failed", { error: String(e) });
      });
  };

  const offer = (message) => {
    const t = parseTransport(message);
    const player = world
      .getAllPlayers()
      .find((p) => p.name.toLowerCase() === t.player.toLowerCase());
    if (!player) return tell(t.id, "refused", { why: "offline" });
    const open = pending.get(player.id);
    if (open && !expired(open.at, Date.now())) return tell(t.id, "refused", { why: "busy" });
    const no = why(player, t);
    if (no) return tell(t.id, "refused", { why: no });
    const packs = packCount(packStacks(player));
    const here = { ...player.location, dim: DIM_OF[player.dimension.id] };
    const o = { ...t, at: Date.now(), text: offerText({ label: t.label, here, target: t, packs }) };
    pending.set(player.id, o);
    // Said before the form opens, so the tool's read-back sees it at once.
    tell(t.id, "offered", { text: o.text, packs });
    ask(player, o);
  };

  system.afterEvents.scriptEventReceive.subscribe((ev) => {
    if (ev.id === "jb:transport") {
      try {
        offer(ev.message);
      } catch (e) {
        say(`transport refused: ${e}`);
      }
    } else if (ev.id === "jb:config") {
      try {
        const cfg = parseTransportSwitch(ev.message);
        if ("on" in cfg) world.setDynamicProperty(SWITCH, cfg.on);
        say(`config power_pack_transport=${transportOn()}`);
      } catch {
        // main.js already said the config was refused.
      }
    }
  });

  // The dimensions each player has stood in, for "only to a dimension you've visited".
  system.runInterval(() => {
    for (const player of world.getAllPlayers()) {
      try {
        const key = visitedKey(player.id);
        const before = parseVisited(world.getDynamicProperty(key));
        const after = visitedAfter(before, DIM_OF[player.dimension.id]);
        if (after !== before) world.setDynamicProperty(key, JSON.stringify(after));
      } catch {
        // Mid-join or mid-portal: next second.
      }
    }
  }, 20);
};
