// Ops' shortcut into the Minecraft screen (DESIGN.md "Minecraft server screen"): a plain list
// row with a trailing chevron, not an OpsCard caret — it goes somewhere rather than opening in
// place. Its one line is the glance: who's on, or the lockout while an update waits.

import type { MinecraftStatus, MinecraftVersion } from "../api/client";
import { glanceOf } from "../minecraft";
import { ChevronRightIcon, CubeIcon } from "./icons";

export function MinecraftOpsRow({
  status,
  version,
  error,
  onOpen,
}: {
  status: MinecraftStatus | null;
  version: MinecraftVersion | null;
  error: string | null;
  onOpen: () => void;
}) {
  const g = glanceOf(status, version, error);
  return (
    <section className="ops-card mc-entry">
      <button
        type="button"
        className="mc-navrow"
        onClick={onOpen}
        aria-label={`Open Minecraft — ${g.word}, ${g.meta}`}
      >
        <span className="mc-navico" aria-hidden="true">
          <CubeIcon size={20} />
        </span>
        <span className="mc-navtx">
          <span className="ops-card-title">Minecraft</span>
          <span className={`mc-navmeta${g.tone ? ` ${g.tone}` : ""}`}>{g.meta}</span>
        </span>
        <span className={`mc-navstate mc-word-${g.level}`}>
          <span className={`mc-dot mc-dot-${g.level}`} />
          {g.word}
        </span>
        <ChevronRightIcon size={18} />
      </button>
    </section>
  );
}
