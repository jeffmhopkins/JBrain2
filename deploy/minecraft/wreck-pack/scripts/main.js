// The spawn wreck's pack (docs/plans/MINECRAFT_BEDROCK_PLAN.md §SW1–SW2). The wrapper
// installs it only into a world the box generated itself. This first slice only reports
// that it loaded, so the wrapper can tell the pack reached a brand-new world; the build
// lands in SW2. Stable @minecraft/server only.
import { world } from "@minecraft/server";

const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);

world.afterEvents.worldLoad.subscribe(() => {
  emit({ ev: "wreck", phase: "loaded" });
});
