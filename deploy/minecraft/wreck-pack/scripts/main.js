// The spawn wreck's pack (docs/plans/MINECRAFT_BEDROCK_PLAN.md §SW1–SW2). The wrapper
// installs it only into a world the box generated itself. This first slice builds
// nothing: a world it reaches will have been played by the time the build ships, and
// building then would land on whatever its players made at spawn. So it records the
// world as passed over (SW2's build honours that record) and reports done, which also
// tells the wrapper the pack reached the world. Stable @minecraft/server only.
import { world } from "@minecraft/server";

const KEY = "jbrain:wreck";
const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);

world.afterEvents.worldLoad.subscribe(() => {
  emit({ ev: "wreck", phase: "loaded" });
  if (world.getDynamicProperty(KEY) === undefined) {
    world.setDynamicProperty(
      KEY,
      JSON.stringify({ stage: "done", skipped: true, cause: "armed before the build shipped" })
    );
  }
  emit({ ev: "wreck", phase: "done" });
});
