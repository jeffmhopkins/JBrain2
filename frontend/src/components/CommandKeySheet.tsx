// The one-time reveal of a radio command's shared secret (docs/plans/APRS_CONTROL_PLAN.md
// P4). The box generates the key and shows it exactly here, once: nothing on this screen
// can fetch it again, and a rotate is what produces a new one. That is deliberate — a key
// a screen can re-display is a key a borrowed phone can read.
//
// The owner runs this box remotely with no terminal (CLAUDE.md #10), so copying the key
// into the sending side has to be a tap, never a file on the host.

import { useState } from "react";
import type { CommandKey } from "../api/client";
import { Sheet } from "./Sheet";
import { CheckIcon } from "./icons";

interface CommandKeySheetProps {
  keyed: CommandKey;
  onClose: () => void;
}

export function CommandKeySheet({ keyed, onClose }: CommandKeySheetProps) {
  const [copied, setCopied] = useState(false);

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(keyed.key);
      setCopied(true);
    } catch {
      // A clipboard the browser refuses is not an error worth a dialog: the key is on
      // screen and can be typed. Saying "copied" when it was not would be the failure.
      setCopied(false);
    }
  }

  return (
    <Sheet title={`Key for ${keyed.word}`} onClose={onClose}>
      <p className="cks-lead">
        Copy this into whatever sends the command. It is shown once — close this and the only way to
        see a key again is to make a new one, which stops this one working.
      </p>
      <output className="cks-key">{keyed.key}</output>
      <button type="button" className="cks-copy" onClick={() => void copy()}>
        {copied ? <CheckIcon size={16} /> : null}
        {copied ? "Copied" : "Copy key"}
      </button>
      <p className="cks-note">
        The key stays on the box and in your sender. What goes over the air is the word and a
        five-character code — <b>{keyed.word} 7K2M9</b> — which is spent the moment it is used, so
        anyone who hears it can only replay something already done.
      </p>
    </Sheet>
  );
}
