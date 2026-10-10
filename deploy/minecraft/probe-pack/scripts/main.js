// M0 item 5 probe (docs/plans/MINECRAFT_BEDROCK_PLAN.md). Every line it logs starts
// with "[jbrain-probe]" so the wrapper's console can be grepped for the answers:
// does a stable-API pack load with no experiments, does console output reach the
// server console, does `scriptevent` arrive, can a custom command be registered, is
// there a stable chat event, and is a dead player's inventory still readable.
import { system, world, CommandPermissionLevel, CustomCommandParamType,
  CustomCommandStatus } from "@minecraft/server";

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
