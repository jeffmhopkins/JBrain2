// The omnibox plan popover (docs/archive/JERV_PLANNING_TOOL_PLAN.md): the in-work plan's
// live status — checklist, auto-resume countdown, Continue now / Stop — in the shared
// bottom Sheet, opened from the composer-foot plan pill. Keeps the plan OUT of the chat
// transcript once approved so it doesn't crowd deep-research and other tool views; the
// owner taps the pill to see status, then dismisses. Renders the shared PlanBody off a
// PlanStateHook owned by the parent, so the pill and the popover reflect one live state.

import type { ReactNode } from "react";
import { Sheet } from "../components/Sheet";
import { PlanBody, type PlanStateHook } from "./views/registry";

export function PlanSheet({
  st,
  onClose,
}: {
  st: PlanStateHook;
  onClose: () => void;
}): ReactNode {
  return (
    <Sheet title="Plan" onClose={onClose}>
      <div className="plan-sheet">
        <PlanBody st={st} />
      </div>
    </Sheet>
  );
}
