// "Discuss this article" (docs/mocks/wiki-talk-b-topics.html): the only way to correct a
// machine-written article — file an OWNER CORRECTION note (CLAUDE.md #7: humans never edit the
// wiki directly). The note out-argues the graph (force-supersedes + pins the conflicting fact,
// Wave A+) and the article rebuilds. Built on the shared <Sheet>; submits via api.fileCorrection.

import { useState } from "react";
import { api } from "../../api/client";
import { Sheet } from "../../components/Sheet";
import { DOMAIN_TITLE } from "../../notes/modes";

type Phase = "edit" | "sending" | "done" | "error";

export function DiscussSheet({
  articleId,
  domains,
  onClose,
}: {
  articleId: string;
  /** The article's section domains — the correction routes to one of them. */
  domains: string[];
  onClose: () => void;
}) {
  const [body, setBody] = useState("");
  const [domain, setDomain] = useState(domains[0] ?? "general");
  const [phase, setPhase] = useState<Phase>("edit");

  async function submit() {
    const text = body.trim();
    if (!text || phase === "sending") return;
    setPhase("sending");
    try {
      await api.fileCorrection(articleId, { body: text, domain });
      setPhase("done");
    } catch {
      setPhase("error");
    }
  }

  return (
    <Sheet title="Discuss this article" onClose={onClose}>
      {phase === "done" ? (
        <p className="wiki-discuss-note">
          Filed your correction as a note. It out-argues the conflicting fact, and the article will
          rebuild from the corrected graph — the wiki stays machine-written.
        </p>
      ) : (
        <>
          <textarea
            className="wiki-discuss-ta wiki-discuss-input"
            aria-label="What's wrong, and what it should say"
            placeholder="Describe what's wrong and what it should say…"
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
          <div className="wiki-discuss-row">
            <select
              className="wiki-discuss-domain"
              aria-label="Domain"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
            >
              {domains.map((d) => (
                <option key={d} value={d}>
                  {DOMAIN_TITLE[d] ?? d}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="wiki-discuss-submit"
              disabled={phase === "sending" || body.trim() === ""}
              onClick={() => void submit()}
            >
              {phase === "sending" ? "Filing…" : "File correction"}
            </button>
          </div>
          {phase === "error" && (
            <p className="wiki-discuss-note wiki-discuss-err">Couldn't file it — try again.</p>
          )}
          <p className="wiki-discuss-note">
            Filed as an owner correction note in the chosen domain — the wiki stays machine-written;
            facts are never edited directly.
          </p>
        </>
      )}
    </Sheet>
  );
}
