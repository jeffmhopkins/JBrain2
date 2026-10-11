// M0 item 5 probe (docs/plans/MINECRAFT_BEDROCK_PLAN.md). Every line it logs starts
// with "[jbrain-probe]" so the wrapper's console can be grepped for the answers:
// does a stable-API pack load with no experiments, does console output reach the
// server console, does `scriptevent` arrive, is there a stable chat event, and is a
// dead player's inventory still readable. What it once carried has moved to the
// always-installed jbrain pack, because two packs defining one name collide: `/jb:dave`
// (the in-game door, §M5), and the Tricorder, the Power Pack, its recipe and the
// waypoint arrow (§M9, §M10). The kit now only watches, so the owner just plays and the
// assistant reads the answers off the server log (docs/runbooks/MINECRAFT_HOME_TEST.md).
import { system, world } from "@minecraft/server";

const say = (msg) => console.log(`[jbrain-probe] ${msg}`);

say("loaded");
say(`chatSend after-event available: ${Boolean(world.afterEvents.chatSend)}`);

system.afterEvents.scriptEventReceive.subscribe((ev) => {
  if (ev.id.startsWith("jb:")) say(`scriptevent ${ev.id} ${ev.message}`);
});

if (world.afterEvents.chatSend) {
  world.afterEvents.chatSend.subscribe((ev) => {
    say(`chat from ${ev.sender.name}: ${ev.message}`);
  });
}

world.afterEvents.entityDie.subscribe((ev) => {
  const dead = ev.deadEntity;
  if (dead.typeId !== "minecraft:player") return;
  try {
    const inv = dead.getComponent("minecraft:inventory")?.container;
    const used = inv ? inv.size - inv.emptySlotsCount : -1;
    say(`player died: ${dead.name}; inventory slots still filled: ${used}`);
  } catch (e) {
    say(`player died: ${dead.name}; inventory unreadable: ${e}`);
  }
});

// Who has had each of the crew's items, so each finding is logged once rather than
// every tick: the item reached the player, with its lock and keep-on-death flags.
const seen = new Set();
const once = (key, msg) => {
  if (seen.has(key)) return;
  seen.add(key);
  say(msg);
};

system.runInterval(() => {
  for (const player of world.getAllPlayers()) {
    const inv = player.getComponent("minecraft:inventory")?.container;
    if (!inv) continue;
    for (let i = 0; i < inv.size; i++) {
      const item = inv.getItem(i);
      if (item?.typeId === "jbrain:power_pack") {
        once(`pp:${player.name}`, `power_pack in inventory: ${player.name} (x${item.amount})`);
      } else if (item?.typeId === "jbrain:tricorder") {
        once(
          `tc:${player.name}`,
          `tricorder in inventory: ${player.name} (${item.nameTag}; lock ${item.lockMode}; keepOnDeath ${item.keepOnDeath})`,
        );
      }
    }
  }
}, 20);
