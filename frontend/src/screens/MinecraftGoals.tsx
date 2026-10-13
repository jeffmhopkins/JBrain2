// Players' goals and their progress (plan §M24; GUI gate docs/mocks/minecraft-goals/,
// owner's pick 2026-10-13: variant B, "expandable if clicked", look-only). The Players table
// gains a glance line under each player with goals; tapping it opens all that player's goals
// in place, and tapping a goal opens its detail in place too (DESIGN "Surface paradigms":
// row-level detail is an inline expansion, and one modal at a time). The player Sheet shows
// the same list and the latest journal. Nothing here edits: players change goals with Dave.

import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  type MinecraftGoal,
  type MinecraftGoalCount,
  type MinecraftGoalProgress,
  type MinecraftGoals,
  type MinecraftJournalEntry,
  type MinecraftPlayerGoals,
  api,
} from "../api/client";
import {
  ArchiveIcon,
  BanIcon,
  BotIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  CubeIcon,
  FlagIcon,
  GamepadIcon,
  InfoIcon,
  PinIcon,
  RefreshIcon,
  UserIcon,
} from "../components/icons";
import { clockOf, dayOf, plural, shortDate } from "../minecraft";
import {
  NB,
  asOfText,
  finishedGoals,
  finishedText,
  glanceGoal,
  isCounted,
  isReached,
  itemWords,
  journalOf,
  latestJournal,
  moreText,
  needsCount,
  openGoals,
  placeWhere,
  sourceText,
  whyParts,
} from "../minecraftGoals";

export interface GoalCountState {
  busy: boolean;
  data: MinecraftGoalProgress | null;
  error: string | null;
}

export interface GoalsState {
  goals: MinecraftGoals | null;
  error: string | null;
  counts: Record<string, GoalCountState>;
  /** Count one player's goals: `flush` is Count again, a fresh save of the running world. */
  count: (xuid: string, flush: boolean) => void;
}

/** An error as a sentence: its own full stop unless it already ends with one. */
function sentence(text: string): string {
  return /[.?!]$/.test(text) ? text : `${text}.`;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** The goals, read once, and each player's count, made as soon as their goals are shown:
 *  one request per player (all their goals at once), from the files as they are. */
export function useMinecraftGoals(): GoalsState {
  const [goals, setGoals] = useState<MinecraftGoals | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [counts, setCounts] = useState<Record<string, GoalCountState>>({});

  const count = useCallback((xuid: string, flush: boolean) => {
    setCounts((c) => ({ ...c, [xuid]: { data: null, error: null, ...c[xuid], busy: true } }));
    api.minecraftGoalProgress(xuid, flush).then(
      (data) => setCounts((c) => ({ ...c, [xuid]: { busy: false, data, error: null } })),
      (err: unknown) =>
        setCounts((c) => ({
          ...c,
          [xuid]: { busy: false, data: c[xuid]?.data ?? null, error: errorText(err) },
        })),
    );
  }, []);

  useEffect(() => {
    let live = true;
    api.minecraftGoals().then(
      (g) => {
        if (!live) return;
        setGoals(g);
        for (const p of g.players) if (needsCount(p)) count(p.xuid, false);
      },
      (err: unknown) => live && setError(errorText(err)),
    );
    return () => {
      live = false;
    };
  }, [count]);

  return { goals, error, counts, count };
}

// ---- the bar ----

function GoalBar({
  goal,
  c,
  big = false,
}: {
  goal: MinecraftGoal;
  c: MinecraftGoalCount | null;
  big?: boolean;
}) {
  const need = goal.item_count ?? 1;
  const cls = `mc-gbar${big ? " lg" : ""}`;
  if (!c || c.have === null) {
    return <div className={`${cls} unknown`} role="img" aria-label="Not counted" />;
  }
  const pct = (v: number) => Math.min(100, (v / need) * 100);
  const carried = pct(c.carried ?? 0);
  const chests = Math.min(100 - carried, pct(c.have - (c.carried ?? 0)));
  return (
    <div
      className={`${cls}${isReached(c) ? " reached" : ""}`}
      role="img"
      aria-label={`${c.have} of ${need}`}
    >
      <i className="c" style={{ width: `${carried}%` }} />
      <i className="h" style={{ width: `${chests}%` }} />
    </div>
  );
}

function countOf(
  state: GoalCountState | undefined,
  goal: MinecraftGoal,
): MinecraftGoalCount | null {
  return state?.data?.goals.find((g) => g.id === goal.id) ?? null;
}

function CountFigure({
  goal,
  c,
  busy,
}: { goal: MinecraftGoal; c: MinecraftGoalCount | null; busy: boolean }) {
  const have = c && c.have !== null ? String(c.have) : busy && !c ? "…" : "—";
  return (
    <span className="mc-gn">
      {have}
      <small>
        {NB}/{NB}
        {goal.item_count}
      </small>
    </span>
  );
}

/** The words under a goal: what it counts and where from, or why it can't be counted now. */
function WhyLine({
  goal,
  c,
  state,
  tag,
  nowMs,
}: {
  goal: MinecraftGoal;
  c: MinecraftGoalCount | null;
  state: GoalCountState | undefined;
  tag: string;
  nowMs: number;
}) {
  if (!isCounted(goal)) {
    return (
      <span className="mc-gs">No count · made {shortDate(goal.created_at).replace(" ", NB)}</span>
    );
  }
  if (!c) {
    const text =
      state?.error != null
        ? `Can't count right now — ${state.error}`
        : state?.busy
          ? "counting…"
          : "not counted yet";
    return <span className="mc-gs">{text}</span>;
  }
  const notLoaded = !c.loaded && c.chests_why !== "no_world";
  return (
    <span className="mc-gs">
      {isReached(c) && (
        <>
          <span className="mc-pill reached">
            <CheckIcon size={12} />
            reached
          </span>{" "}
        </>
      )}
      {notLoaded && (
        <>
          <span className="mc-pill later">not loaded</span> In <b>{c.world_name ?? c.world}</b>,
          which isn&apos;t the loaded world ·{" "}
        </>
      )}
      {whyParts(c, tag, nowMs).join(" · ")}
      {isReached(c) && ` · ${tag} can tell Dave it's done`}
    </span>
  );
}

// ---- a goal opened in place ----

function SourceRow({
  icon,
  head,
  sub,
  value,
  far = false,
}: {
  icon: ReactNode;
  head: string;
  sub: string;
  value: number | null;
  far?: boolean;
}) {
  return (
    <li className="mc-wr">
      <span className="mc-wi" aria-hidden="true">
        {icon}
      </span>
      <span className="mc-wt">
        {head}
        {far && <span className="mc-chip-far">far</span>}
        {sub && <small>{sub}</small>}
      </span>
      <span className={`mc-wn${value === null ? " none" : ""}`}>{value ?? "—"}</span>
    </li>
  );
}

function carriedSub(c: MinecraftGoalCount, tag: string): string {
  if (c.carried_why === "offline") return `counted only while ${tag} is on`;
  if (c.carried_why === "not_loaded") {
    return `counted only while ${c.world_name ?? "that world"} is loaded`;
  }
  if (c.carried_why === "failed") return "wasn't counted — the server didn't answer";
  const boxes = c.boxes
    ? ` · ${plural(c.boxes, "shulker box", "shulker boxes").replace(/ /g, NB)} carried: what's inside isn't counted`
    : "";
  return `inventory, armour and off-hand${boxes}`;
}

function GoalDetail({
  pg,
  goal,
  c,
  state,
  nowMs,
  onCountAgain,
}: {
  pg: MinecraftPlayerGoals;
  goal: MinecraftGoal;
  c: MinecraftGoalCount | null;
  state: GoalCountState | undefined;
  nowMs: number;
  onCountAgain: () => void;
}) {
  const tag = pg.gamertag;
  const need = goal.item_count ?? 0;
  const anyFar = c?.chests.some((ch) => ch.far) ?? false;
  return (
    <div className="mc-goal-open">
      {isCounted(goal) ? (
        <>
          <p className="mc-goal-big">
            {c && c.have !== null ? (
              <>
                {c.have}
                <small>
                  of {need} —{" "}
                  {isReached(c)
                    ? `reached; ${tag} can tell Dave it's done`
                    : `${need - c.have} to go`}
                </small>
              </>
            ) : (
              <>
                —<small>of {need}</small>
              </>
            )}
          </p>
          <GoalBar goal={goal} c={c} big />
          {c && c.have !== null && (
            <p className="mc-key">
              {c.carried !== null && (
                <span>
                  <i className="c" />
                  carried
                </span>
              )}
              <span>
                <i className="h" />
                in chests near their places
              </span>
            </p>
          )}
          {c && (
            <>
              {!c.loaded && c.chests_why !== "no_world" && (
                <p className="mc-note">
                  <span className="mc-pill later">not loaded</span> In{" "}
                  <b>{c.world_name ?? c.world}</b>, which isn&apos;t the loaded world: its chests
                  are read from its saved files.
                </p>
              )}
              <ul className="mc-where" aria-label="Where the count comes from">
                <SourceRow
                  icon={<UserIcon size={16} />}
                  head={`On ${tag}`}
                  sub={carriedSub(c, tag)}
                  value={c.carried}
                />
                {c.chests.map((ch) => (
                  <SourceRow
                    key={ch.place}
                    icon={<ArchiveIcon size={16} />}
                    head={`Near ${ch.place}`}
                    sub={["chests within 48 blocks", placeWhere(ch.distance, ch.far, tag)]
                      .filter(Boolean)
                      .join(" · ")}
                    value={c.chests_why === null ? ch.count : null}
                    far={ch.far}
                  />
                ))}
                {c.here !== null && (
                  <SourceRow
                    icon={<PinIcon size={16} />}
                    head={`Near ${tag}`}
                    sub="chests within 48 blocks of where they stand"
                    value={c.here}
                  />
                )}
              </ul>
              {c.chests_why === "no_places" && (
                <p className="mc-note">
                  {tag} has no saved places in this world, so no chests count. They save one by
                  asking Dave where they stand.
                </p>
              )}
              {c.chests_why === "failed" && (
                <p className="mc-note">Chests weren&apos;t counted — {c.detail}</p>
              )}
              {c.chests_why === "no_world" && (
                <p className="mc-note">This goal&apos;s world isn&apos;t on the server any more.</p>
              )}
              {anyFar && (
                <p className="mc-note">
                  <b>Far</b> places are more than 48 blocks from {tag}. In game, counting them takes
                  a deep scan (one per check, for all far places). Here they&apos;re read from the
                  world files, which never does.
                </p>
              )}
            </>
          )}
          <div className="mc-goal-asof">
            <p className="mc-note">
              {state?.error && !c ? (
                <>Can&apos;t count right now — {sentence(state.error)} </>
              ) : c && c.as_of !== null ? (
                <>
                  Chests {asOfText(c, nowMs)} (the world&apos;s saved files)
                  {c.carried !== null ? "; carried counted then" : ""}.{" "}
                </>
              ) : null}
              Counting here is free and never spends {tag}&apos;s deep scans.
            </p>
            <button
              type="button"
              className="mc-btn"
              disabled={state?.busy ?? false}
              onClick={onCountAgain}
            >
              <RefreshIcon size={16} />
              {state?.busy ? "Counting…" : "Count again"}
            </button>
          </div>
        </>
      ) : (
        <p className="mc-note">
          No count — this goal doesn&apos;t count an item. Made {shortDate(goal.created_at)}.
        </p>
      )}
      {goal.notes && <p className="mc-note">{goal.notes}</p>}
      <h4 className="mc-goal-jh">Journal</h4>
      <Journal
        entries={journalOf(pg, goal.id)}
        tag={tag}
        nowMs={nowMs}
        empty="Nothing in the journal for this goal yet."
      />
    </div>
  );
}

// ---- the journal ----

const SOURCE_ICON = {
  player: <GamepadIcon size={14} />,
  dave: <BotIcon size={14} />,
  owner: <UserIcon size={14} />,
  auto: <InfoIcon size={14} />,
};

function whenText(at: number, nowMs: number): string {
  const day = dayOf(at, nowMs);
  return `${day.charAt(0).toUpperCase()}${day.slice(1)}, ${clockOf(at)}`;
}

export function Journal({
  entries,
  tag,
  nowMs,
  empty,
}: {
  entries: MinecraftJournalEntry[];
  tag: string;
  nowMs: number;
  empty: string;
}) {
  if (entries.length === 0) return <p className="mc-note mc-log-empty">{empty}</p>;
  return (
    <ul className="mc-log">
      {entries.map((e) => (
        <li className="mc-le" key={e.id}>
          <span className="mc-le-src" aria-hidden="true">
            {SOURCE_ICON[e.source]}
          </span>
          <span className="mc-le-t">{e.text}</span>
          <small>
            {whenText(e.at, nowMs)} · {sourceText(e, tag)}
          </small>
        </li>
      ))}
    </ul>
  );
}

// ---- every goal of one player ----

/** A player's goals: open ones each opening in place (one at a time), finished ones folded
 *  under "1 done · 1 abandoned", and the look-only footnote. */
export function GoalList({
  pg,
  state,
  nowMs,
  onCount,
  idPrefix,
}: {
  pg: MinecraftPlayerGoals;
  state: GoalCountState | undefined;
  nowMs: number;
  onCount: (flush: boolean) => void;
  idPrefix: string;
}) {
  const [opened, setOpened] = useState<string | null>(null);
  const [finOpen, setFinOpen] = useState(false);
  const open = openGoals(pg);
  const fin = finishedGoals(pg);
  return (
    <div className="mc-goals">
      {open.map((g) => {
        const c = countOf(state, g);
        const isOpen = opened === g.id;
        const panel = `${idPrefix}-${g.id}`;
        return (
          <div className={`mc-goal-item${isOpen ? " open" : ""}`} key={g.id}>
            <button
              type="button"
              className="mc-goal"
              aria-expanded={isOpen}
              aria-controls={isOpen ? panel : undefined}
              onClick={() => setOpened(isOpen ? null : g.id)}
            >
              <span className={`mc-gic${isCounted(g) ? " count" : ""}`} aria-hidden="true">
                {isCounted(g) ? <CubeIcon size={16} /> : <FlagIcon size={16} />}
              </span>
              <span className="mc-gbody">
                <span className="mc-gtop">
                  <span className="mc-gt">{g.title}</span>
                  {isCounted(g) && <CountFigure goal={g} c={c} busy={state?.busy ?? false} />}
                </span>
                {isCounted(g) && !isOpen && <GoalBar goal={g} c={c} />}
                {!isOpen && (
                  <WhyLine goal={g} c={c} state={state} tag={pg.gamertag} nowMs={nowMs} />
                )}
              </span>
              <span className="mc-gchev" aria-hidden="true">
                {isOpen ? <ChevronUpIcon size={16} /> : <ChevronDownIcon size={16} />}
              </span>
            </button>
            {isOpen && (
              <div id={panel}>
                <GoalDetail
                  pg={pg}
                  goal={g}
                  c={c}
                  state={state}
                  nowMs={nowMs}
                  onCountAgain={() => onCount(true)}
                />
              </div>
            )}
          </div>
        );
      })}
      {fin.length > 0 && (
        <>
          <button
            type="button"
            className="mc-fin"
            aria-expanded={finOpen}
            onClick={() => setFinOpen(!finOpen)}
          >
            {finishedText(pg)}
            {finOpen ? <ChevronUpIcon size={16} /> : <ChevronDownIcon size={16} />}
          </button>
          {finOpen &&
            fin.map((g) => (
              <div className="mc-goal flat" key={g.id}>
                <span className={`mc-gic${g.status === "done" ? " done" : ""}`} aria-hidden="true">
                  {g.status === "done" ? <CheckIcon size={16} /> : <BanIcon size={16} />}
                </span>
                <span className="mc-gbody">
                  <span className="mc-gt muted">{g.title}</span>
                  <span className="mc-gs">
                    <span className={`mc-pill ${g.status === "done" ? "done" : "aband"}`}>
                      {g.status}
                    </span>{" "}
                    {g.finished_at !== null ? shortDate(g.finished_at).replace(" ", NB) : ""}
                    {isCounted(g) ? ` · counted ${g.item}${NB}×${g.item_count}` : ""}
                  </span>
                </span>
              </div>
            ))}
        </>
      )}
      <p className="mc-note mc-goals-foot">
        Goals and the journal are kept by Dave from what players tell him in game. To change one,{" "}
        {pg.gamertag} asks Dave.
      </p>
    </div>
  );
}

// ---- the Players table's glance line ----

/** The line under a player's row: their first counted goal (or first goal) and how many more.
 *  A button of its own, below the row's overlay: it opens every goal in place. */
export function GoalGlance({
  pg,
  state,
  expanded,
  onToggle,
  panelId,
}: {
  pg: MinecraftPlayerGoals;
  state: GoalCountState | undefined;
  expanded: boolean;
  onToggle: () => void;
  panelId: string;
}) {
  const pick = glanceGoal(pg);
  const goal = pick?.goal ?? null;
  const c = goal ? countOf(state, goal) : null;
  const more = pick ? moreText(pick.more) : "";
  const label = goal
    ? `${pg.gamertag}'s goals: ${goal.title}${
        isCounted(goal) ? `, ${c?.have ?? "not counted"} of ${goal.item_count}` : ""
      }${pick?.more ? `, and ${plural(pick.more, "more goal")}` : ""}`
    : `${pg.gamertag}'s goals: ${finishedText(pg).replaceAll(NB, " ")}`;
  return (
    <button
      type="button"
      className="mc-glance"
      aria-expanded={expanded}
      aria-controls={expanded ? panelId : undefined}
      aria-label={label}
      onClick={onToggle}
    >
      {goal && isCounted(goal) ? (
        <span className="mc-glance-bar">
          <GoalBar goal={goal} c={c} />
        </span>
      ) : (
        <FlagIcon size={14} />
      )}
      <span className="mc-glance-txt">
        {goal ? (
          <>
            {isCounted(goal) && (
              <span className="mc-glance-n">
                {c && c.have !== null ? c.have : state?.busy && !c ? "…" : "—"}
                {NB}/{NB}
                {goal.item_count}
              </span>
            )}
            <span className="mc-glance-t">
              {isCounted(goal) ? itemWords(goal.item ?? "", goal.item_count ?? 0) : goal.title}
            </span>
            {isReached(c) && <span className="mc-glance-x">· reached</span>}
            {more && <span className="mc-glance-x">· {more}</span>}
          </>
        ) : (
          <span className="mc-glance-t">No open goals · {finishedText(pg)}</span>
        )}
      </span>
      <span className="mc-gchev">
        {expanded ? <ChevronUpIcon size={14} /> : <ChevronDownIcon size={14} />}
      </span>
    </button>
  );
}

/** The Sheet's Goals section and the player's latest journal (B's sheet). */
export function SheetGoals({
  pg,
  state,
  error,
  nowMs,
  onCount,
  gamertag,
}: {
  pg: MinecraftPlayerGoals | null;
  state: GoalCountState | undefined;
  error: string | null;
  nowMs: number;
  onCount: (flush: boolean) => void;
  gamertag: string;
}) {
  const open = pg ? openGoals(pg).length : 0;
  return (
    <>
      <h3 className="mc-sect">
        Goals<span className="mc-sect-r">{plural(open, "open", "open")}</span>
      </h3>
      <div className="mc-card">
        {pg && pg.goals.length > 0 ? (
          <GoalList pg={pg} state={state} nowMs={nowMs} onCount={onCount} idPrefix="sheet-goal" />
        ) : error ? (
          <p className="mc-empty">Couldn&apos;t read the goals — {error}</p>
        ) : (
          <div className="mc-goals-empty">
            <span className="mc-goals-eic" aria-hidden="true">
              <FlagIcon size={20} />
            </span>
            <b>No goals yet</b>
            <p className="mc-note">
              Players make them by asking Dave in game —{" "}
              <i>&ldquo;Dave, remind me I need 23 iron blocks&rdquo;</i> — and he keeps count from
              what they carry and the chests near their saved places. {gamertag} changes them the
              same way.
            </p>
          </div>
        )}
      </div>
      <h3 className="mc-sect">
        Journal<span className="mc-sect-r">latest</span>
      </h3>
      <div className="mc-journal">
        <Journal
          entries={pg ? latestJournal(pg) : []}
          tag={gamertag}
          nowMs={nowMs}
          empty="Nothing in the journal yet."
        />
      </div>
    </>
  );
}
