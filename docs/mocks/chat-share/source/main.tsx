import { type ReactNode, StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/styles/tokens.css";
import "../src/styles.css";
import "./mock.css";
import { AgentTranscript } from "../src/agent/FullBrainSurface";
import type { TranscriptMessage } from "../src/agent/transcript";
import type { FullBrain } from "../src/agent/useFullBrain";

const noop = () => {};
const T0 = Date.parse("2026-10-12T17:40:00");
const URL_TXT = "jbrain.app/s/c/7Kq2-mVw9-tRb4";

const user = (text: string): TranscriptMessage => ({
  role: "user",
  text,
  tools: [],
  views: [],
  streaming: false,
  reasoning: "",
  thinking: false,
});

const MESSAGES: TranscriptMessage[] = [
  user(
    "Where are SpaceX secondary shares trading right now, and how does that compare to the last tender?",
  ),
  {
    role: "assistant",
    streaming: false,
    thinking: false,
    views: [],
    startedAt: T0,
    elapsedMs: 41_000,
    reasoning:
      "The owner wants a current secondary-market read and the last company tender for comparison. Search for recent indications first, then read the two most direct pages rather than relying on snippets.",
    text: "Secondary marketplaces are showing indications **above the last company-run tender price**[^1], but the bid/ask spread is wide, so no single number is a real “price”[^2].\n\n### How to read it\n\n- **Tender price**: what insiders actually sold at. Treat it as the floor.\n- **Marketplace band**: indications of interest, not fills. Volume is thin.\n- **Direction**: indications have drifted up since the tender, in line with the reported valuation step-up[^2].\n\nIf you want a single figure to anchor on, use the tender and treat anything above it as optimism you can't yet sell into.",
    tools: [
      {
        id: "t1",
        name: "web_search",
        ok: true,
        textOffset: 0,
        args: { query: "SpaceX secondary market share price tender offer" },
        result: "8 results",
        webSources: [
          { url: "https://forgeglobal.com/spacex_stock/", title: "SpaceX stock price · Forge" },
          { url: "https://www.cnbc.com/spacex-tender-offer/", title: "SpaceX tender offer values company higher · CNBC" },
        ],
        summary: "8 results for SpaceX secondary market share price tender offer",
      },
      {
        id: "t2",
        name: "web_fetch",
        ok: true,
        textOffset: 0,
        args: { url: "https://forgeglobal.com/spacex_stock/" },
        webSources: [{ url: "https://forgeglobal.com/spacex_stock/", title: "SpaceX stock price · Forge" }],
        summary: "Forge Price indications and recent secondary activity for SpaceX.",
      },
      {
        id: "t3",
        name: "web_fetch",
        ok: true,
        textOffset: 0,
        args: { url: "https://www.cnbc.com/spacex-tender-offer/" },
        webSources: [{ url: "https://www.cnbc.com/spacex-tender-offer/", title: "SpaceX tender offer values company higher · CNBC" }],
        summary: "Report on SpaceX's most recent insider tender offer.",
      },
    ],
  },
  user("Compare that to what I paid in my pretrade allocation."),
  {
    role: "assistant",
    streaming: false,
    thinking: false,
    views: [],
    startedAt: T0 + 300_000,
    elapsedMs: 18_000,
    reasoning:
      "The allocation price lives in the owner's notes, not on the web. Find the confirmation note and read the figure from it.",
    text: "Your **March 4** pretrade confirmation records the allocation price[^1]. Against the marketplace band from the last turn, the position is up on paper.\n\nThere is no liquid exit until the next tender window, so the gain is unrealised and the spread above is the honest range for it.",
    tools: [
      {
        id: "t4",
        name: "search",
        ok: true,
        textOffset: 0,
        args: { query: "SpaceX pretrade allocation price" },
        result: "2 notes",
        sources: [
          { noteId: "n1", domain: "finance", text: "Pretrade confirm — SpaceX allocation, settled Mar 4" },
          { noteId: "n2", domain: "general", text: "Call with broker about secondary windows" },
        ],
      },
      {
        id: "t5",
        name: "read_note",
        ok: true,
        textOffset: 0,
        args: { note_id: "n1" },
        sources: [{ noteId: "n1", domain: "finance", text: "Pretrade confirm — SpaceX allocation, settled Mar 4" }],
        summary: "Pretrade confirm — SpaceX allocation, settled Mar 4.",
      },
    ],
  },
];

const FB = {
  active: {
    id: "s1",
    title: "SpaceX Pretrade",
    status: "active",
    agent: "jerv",
    domain_scopes: ["general", "finance"],
    subject_ids: [],
    created_at: "2026-10-12T17:40:00Z",
    last_active_at: "2026-10-12T17:47:00Z",
  },
  sessions: [],
  agentOptions: [],
  proposals: [],
  reloadProposals: noop,
  reloadSessions: noop,
  panel: "none",
  setPanel: noop,
  openProposal: null,
  setOpenProposal: noop,
  messages: MESSAGES,
  busy: false,
  activeTurn: null,
  canSend: false,
  usage: null,
  stop: noop,
  send: async () => false,
  openQuestions: [],
  answers: {},
  setAnswer: noop,
  requestOpen: noop,
} as unknown as FullBrain;

// ---------- glyphs (owner side of the mock) ----------
const sv = (d: ReactNode) => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    {d}
  </svg>
);
const G = {
  link: sv(
    <>
      <path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11.5 4.4" />
      <path d="M14 11a5 5 0 0 0-7.07 0L4.1 13.83a5 5 0 0 0 7.07 7.07l1.33-1.33" />
    </>,
  ),
  unlink: sv(
    <>
      <path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11.5 4.4" />
      <path d="M14 11a5 5 0 0 0-7.07 0L4.1 13.83a5 5 0 0 0 7.07 7.07l1.33-1.33" />
      <path d="M3 3l18 18" />
    </>,
  ),
  pencil: sv(<path d="M4 20h4L19 9l-4-4L4 16zM13 7l4 4" />),
  scope: sv(
    <>
      <path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h10M18 18h2" />
      <circle cx="16" cy="6" r="2" />
      <circle cx="8" cy="12" r="2" />
      <circle cx="16" cy="18" r="2" />
    </>,
  ),
  archive: sv(
    <>
      <rect x="3" y="4" width="18" height="4" rx="1" />
      <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8M10 12h4" />
    </>,
  ),
  trash: sv(<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" />),
  back: sv(<path d="M15 5l-7 7 7 7" />),
  chev: sv(<path d="M9 5l7 7-7 7" />),
  search: sv(
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20l-4-4" />
    </>,
  ),
  plus: sv(<path d="M12 5v14M5 12h14" />),
  bot: sv(
    <>
      <rect x="4" y="8" width="16" height="12" rx="2" />
      <path d="M12 4v4M9 13h.01M15 13h.01" />
    </>,
  ),
  copy: sv(
    <>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h8" />
    </>,
  ),
  out: sv(<path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7M12 3v12M8 7l4-4 4 4" />),
  eye: sv(
    <>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </>,
  ),
  lock: sv(
    <>
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </>,
  ),
  shield: sv(<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" />),
  reload: sv(<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />),
};

type Screen = "rail" | "sheet" | "public" | "revoked";

const ROWS: [string, string, boolean?][] = [
  ["SpaceX Pretrade", "2 turns"],
  ["Desk panel", "8 turns"],
  ["Desk panel", "1 turn"],
  ["Weather", "1 turn", true],
  ["News", "1 turn"],
  ["Email overview", "1 turn"],
  ["This week in space flight", "1 turn"],
];

function ChatsList({
  railOpen,
  shared,
  onToggleRail,
  onShare,
}: {
  railOpen: boolean;
  shared: boolean;
  onToggleRail: () => void;
  onShare: () => void;
}): ReactNode {
  return (
    <>
      <div className="csm-apphead">
        <span className="t">Research</span>
        <span className="p">0 %</span>
      </div>
      <div className="csm-chatsbar">
        <span className="back">{G.back}</span>
        <span className="t">Chats</span>
        <span className="hint">tap one, or start fresh</span>
      </div>
      <div className="csm-listpad">
        <div className="csm-newchat">{G.plus} New chat</div>
        <div className="csm-tabs">
          <div className="on">
            Today <span className="n">9</span>
          </div>
          <div>
            Older <span className="n">1036</span>
          </div>
          <div>
            Archived <span className="n">13</span>
          </div>
        </div>
        <div className="csm-search">{G.search} Search chats</div>
        <div className="csm-rows">
          {ROWS.map(([t, meta, isShared], i) => (
            <div className="csm-row" key={`${t}-${i}`}>
              {i === 0 && (
                <div className="csm-rail">
                  <button type="button" className="csm-rbtn r-edit">
                    {G.pencil}rename
                  </button>
                  <button type="button" className="csm-rbtn r-share" onClick={onShare}>
                    {G.link}share
                  </button>
                  <button type="button" className="csm-rbtn r-scope">
                    {G.scope}scope
                  </button>
                  <button type="button" className="csm-rbtn r-archive">
                    {G.archive}archive
                  </button>
                  <button type="button" className="csm-rbtn r-delete">
                    {G.trash}delete
                  </button>
                </div>
              )}
              <button
                type="button"
                className="csm-slide"
                style={{ transform: `translateX(${i === 0 && railOpen ? -240 : 0}px)` }}
                onClick={i === 0 ? onToggleRail : undefined}
              >
                <span className="dot" />
                <span className="tt">{t}</span>
                <span className="meta">
                  {(isShared || (i === 0 && shared)) && <span className="lk">{G.link}</span>}
                  {meta}
                </span>
                <span className="chev">{G.chev}</span>
              </button>
            </div>
          ))}
        </div>
      </div>
      <div className="csm-omni">
        <div className="segs">
          <div>{G.plus} Entry</div>
          <div className="on">{G.search} Research</div>
          <div>{G.bot} Brain</div>
        </div>
        <div className="in">Ask the web, or work a problem…</div>
      </div>
    </>
  );
}

function ShareSheet({
  open,
  onClose,
  onRevoke,
  toast,
}: {
  open: boolean;
  onClose: () => void;
  onRevoke: () => void;
  toast: (m: string) => void;
}): ReactNode {
  const copy = () => {
    try {
      navigator.clipboard.writeText(`https://${URL_TXT}`).catch(noop);
    } catch {
      /* the frame may refuse the clipboard; the toast still confirms the mock */
    }
    toast("Link copied");
  };
  return (
    <>
      <button type="button" aria-label="Close" className={`csm-scrim${open ? " on" : ""}`} onClick={onClose} />
      <div className={`csm-sheet${open ? " on" : ""}`} role="dialog" aria-label="Share chat">
        <div className="grip" />
        <div className="csm-sh-title">
          <span className="disc">{G.link}</span>
          <div>
            <h3>Share chat</h3>
            <div className="chat">SpaceX Pretrade · 2 turns · Research</div>
          </div>
        </div>
        <p className="csm-sub">
          Anyone with the link can read the whole chat, tool steps included. Each time they open it,
          they see it as it is then, so turns you add later show up on their next load.
        </p>
        <div className="csm-warn">
          {G.shield}
          <span>
            This chat read your <b>Financial</b> notes. Turn 2 quotes one, and anyone with the link
            will see it.
          </span>
        </div>
        <div className="csm-linkbox">
          <span className="url">{URL_TXT}</span>
          <button type="button" className="csm-btn steel" onClick={copy}>
            {G.copy}Copy
          </button>
        </div>
        <button type="button" className="csm-btn ghost full" onClick={copy}>
          {G.out}Send with…
        </button>
        <div className="csm-stats">
          {G.eye}Opened 3 times · last 2 h ago
        </div>
        <button type="button" className="csm-action danger" onClick={onRevoke}>
          {G.unlink}Revoke link<span className="r">stops working at once</span>
        </button>
      </div>
    </>
  );
}

function PublicView(): ReactNode {
  return (
    <>
      <div className="csm-urlbar">
        {G.lock}
        {URL_TXT}
      </div>
      <div className="csm-pub">
        <div className="csm-band">
          <div className="kick">
            {G.link}Shared chat · read-only
          </div>
          <h2>SpaceX Pretrade</h2>
          <div className="who">
            As of Oct 12, 5:47 PM · <span className="mode">Research</span> · 2 turns
            <span className="csm-reload">
              {G.reload}Reload for newer turns
            </span>
          </div>
        </div>
        <div className="fb-shell csm-shell">
          <AgentTranscript fb={FB} />
        </div>
        <div className="csm-pubfoot">
          {G.lock}
          <span>
            Shared from <b className="csm-wm">JBrain<span>.</span></b> by its owner. You can read
            this chat; you can’t reply, open its notes, or see anything else in their account.
          </span>
        </div>
      </div>
    </>
  );
}

function Revoked(): ReactNode {
  return (
    <>
      <div className="csm-urlbar">
        {G.lock}
        {URL_TXT}
      </div>
      <div className="csm-revoked">
        <span className="disc">{G.unlink}</span>
        <h3>This link no longer works</h3>
        <p>The owner stopped sharing this chat. Ask them for a new link if you still need it.</p>
      </div>
    </>
  );
}

function Mock(): ReactNode {
  const [screen, setScreen] = useState<Screen>("public");
  const [railOpen, setRailOpen] = useState(true);
  const [theme, setTheme] = useState<"dark" | "light">(
    window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark",
  );
  const [toastMsg, setToastMsg] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
  useEffect(() => {
    if (!toastMsg) return;
    const t = setTimeout(() => setToastMsg(null), 1500);
    return () => clearTimeout(t);
  }, [toastMsg]);

  const go = (s: Screen) => {
    setScreen(s);
    setRailOpen(s === "rail");
  };

  return (
    <div className="csm-page">
      <div className="csm-head">
        <h1>Chat share links · B, live</h1>
        <p>
          The public page renders the app&rsquo;s own transcript component and stylesheet: tap
          <b> Thought</b>, <b>Worked</b> and any step row to expand it, exactly as in the app. The
          page has no top bar and no omnibox, and each load shows the whole chat as it is at that
          moment. Sample chat content.
        </p>
        <div className="csm-controls">
          <div className="csm-seg" role="group" aria-label="Screen">
            {(
              [
                ["rail", "Swipe rail"],
                ["sheet", "Share sheet"],
                ["public", "Public view"],
                ["revoked", "Revoked"],
              ] as [Screen, string][]
            ).map(([k, l]) => (
              <button key={k} type="button" aria-pressed={screen === k} onClick={() => go(k)}>
                {l}
              </button>
            ))}
          </div>
          <div className="csm-seg theme" role="group" aria-label="Theme">
            {(["dark", "light"] as const).map((t) => (
              <button key={t} type="button" aria-pressed={theme === t} onClick={() => setTheme(t)}>
                {t === "dark" ? "Dark" : "Light"}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="csm-phone">
        {screen === "public" ? (
          <PublicView />
        ) : screen === "revoked" ? (
          <Revoked />
        ) : (
          <>
            <ChatsList
              railOpen={railOpen}
              shared={screen === "sheet"}
              onToggleRail={() => setRailOpen((o) => !o)}
              onShare={() => {
                setScreen("sheet");
                setRailOpen(false);
                setToastMsg("Link created");
              }}
            />
            <ShareSheet
              open={screen === "sheet"}
              onClose={() => go("rail")}
              onRevoke={() => go("revoked")}
              toast={setToastMsg}
            />
          </>
        )}
        <div className={`csm-toast${toastMsg ? " on" : ""}`}>{toastMsg}</div>
      </div>
    </div>
  );
}

const root = document.getElementById("root");
if (root)
  createRoot(root).render(
    <StrictMode>
      <Mock />
    </StrictMode>,
  );
