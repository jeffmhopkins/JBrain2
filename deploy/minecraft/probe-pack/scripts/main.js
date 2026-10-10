// M0 item 5 probe (docs/plans/MINECRAFT_BEDROCK_PLAN.md). Every line it logs starts
// with "[jbrain-probe]" so the wrapper's console can be grepped for the answers:
// does a stable-API pack load with no experiments, does console output reach the
// server console, does `scriptevent` arrive, can a custom command be registered, is
// there a stable chat event, and is a dead player's inventory still readable.
import { system, world, CommandPermissionLevel, CustomCommandParamType,
  CustomCommandStatus } from "@minecraft/server";

// Home-test kit (docs/runbooks/MINECRAFT_HOME_TEST.md): the custom items and recipe from
// this pack, with icons from the server-pushed probe resource pack. Each finding is a
// `[jbrain-probe]` line, logged once per player, so the owner just plays and the
// assistant reads the answers off the server log.

const say = (msg) => console.log(`[jbrain-probe] ${msg}`);

say("loaded");
say(`chatSend after-event available: ${Boolean(world.afterEvents.chatSend)}`);

system.beforeEvents.startup.subscribe((ev) => {
  try {
    ev.customCommandRegistry.registerCommand(
      {
        name: "jb:dave",
        description: "Ask Dave (probe)",
        permissionLevel: CommandPermissionLevel.Any,
        optionalParameters: [{ name: "question", type: CustomCommandParamType.String }],
      },
      (origin, question) => {
        say(`command from ${origin.sourceEntity?.name ?? "?"}: ${question ?? ""}`);
        return { status: CustomCommandStatus.Success, message: "Dave heard you (probe)" };
      },
    );
    say("custom command jb:dave registered");
  } catch (e) {
    say(`custom command registration failed: ${e}`);
  }
});

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

// Who has had each test item, so each finding is logged once rather than every tick.
const seen = new Set();
const once = (key, msg) => {
  if (seen.has(key)) return;
  seen.add(key);
  say(msg);
};

// The arrow the tricorder (M9) will draw, aimed at world spawn for the test: the bearing to
// the target minus where the player faces, in eight steps.
const ARROWS = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"];
const arrowTo = (player, tx, tz) => {
  const { x, z } = player.location;
  const view = player.getViewDirection();
  const target = Math.atan2(tx - x, -(tz - z));
  const facing = Math.atan2(view.x, -view.z);
  const turn = (((target - facing) * 180) / Math.PI + 720) % 360;
  return { arrow: ARROWS[Math.round(turn / 45) % 8], blocks: Math.round(Math.hypot(tx - x, tz - z)) };
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
        once(`tc:${player.name}`, `tricorder in inventory: ${player.name}`);
      }
    }
    const held = inv.getItem(player.selectedSlotIndex);
    if (held?.typeId === "jbrain:tricorder") {
      once(`hold:${player.name}`, `tricorder held: ${player.name} (actionbar arrow on)`);
      const { arrow, blocks } = arrowTo(player, 0, 0);
      player.onScreenDisplay.setActionBar(`${arrow} World spawn · ${blocks} blocks`);
    }
  }
}, 5);
