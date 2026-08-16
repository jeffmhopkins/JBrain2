// The vitals detail surface: what the box is doing, and which turn is doing it.
//
// Binding mock: docs/mocks/vitals-detail/i-drilldown.html (option I, chosen at the GUI
// gate over G "instrument panel" and H "dossier"). Two levels, each doing one job.
// Level 1 is the graph at 1/5/15 minutes plus a roster of what is running; tapping a
// turn pushes level 2, which is only that turn — its call, its run, its step trail, its
// children, and its raw output, which gets the whole viewport instead of an accordion.
//
// The graph needs no backend: the 1 Hz vitals stream the top bar already holds feeds a
// shared 900-sample ring (hostVitals.ts), so 15 minutes is just a wider window onto
// samples the app was receiving anyway. That history is client-side, so it starts empty
// after a reload — surviving that would need a server-side ring.

import { useEffect, useRef, useState } from "react";

import {
  type CapturedPrompt,
  type LiveTurn,
  type LiveTurns,
  type TurnDetail as TurnDetailPayload,
  api,
} from "../api/client";
import { type PlotSeries, TimeSeriesPlot } from "../components/TimeSeriesPlot";
import { ChevronLeftIcon, ChevronRightIcon } from "../components/icons";
import { type VitalsSample, seedVitalsHistory, vitalsHistory } from "../hostVitals";
import { useForeground } from "../visibility";

/** The windows the mock offers, in seconds. */
const RANGES = [
  { key: "1m", seconds: 60 },
  { key: "5m", seconds: 300 },
  { key: "15m", seconds: 900 },
] as const;

type RangeKey = (typeof RANGES)[number]["key"];

/** How often the roster refetches while the surface is open and in the foreground. A
 *  turn's elapsed time ticks locally between fetches, so this only has to keep up with
 *  turns starting and settling, not with the clock. */
const ROSTER_POLL_MS = 3000;

/** How often level 2 refetches a RUNNING turn, so a streaming answer grows on screen.
 *  A settled turn is fetched once — its output cannot change. */
const DETAIL_POLL_MS = 2000;

/** Columns across the plot. Every range renders the same number of columns, so the
 *  shape stays comparable as the window widens — only the seconds each column covers
 *  changes. */
const COLUMNS = 60;

/** GPU load that reads as pinned. Matches the top bar's band. */
const HOT_PERCENT = 85;

const KIND_LABEL: Record<string, string> = {
  agent: "agent",
  subagent: "sub-agent",
  pipeline: "workflow",
  integration: "integration",
  job: "job",
};

interface VitalsScreenProps {
  /** The turn whose detail level is open, held by App so the back gesture and the
   *  platform back button climb one level at a time like every other stacked layer. */
  selectedTurnId: string | null;
  onSelectTurn: (id: string | null) => void;
}

export function VitalsScreen({ selectedTurnId, onSelectTurn }: VitalsScreenProps) {
  const [range, setRange] = useState<RangeKey>("1m");
  const seconds = RANGES.find((r) => r.key === range)?.seconds ?? 60;
  useSeededHistory();
  // The range drives the ROSTER as well as the plot. It used to drive only the graph,
  // which left the two halves of the screen describing different spans of time: fifteen
  // minutes of GPU history above a list that emptied the moment a turn finished.
  const roster = useLiveTurns(seconds);
  const selected = roster?.turns.find((t) => t.id === selectedTurnId) ?? null;

  return (
    <>
      <main className="screen-body vitals-detail">
        <VitalsPlot
          range={range}
          onRange={setRange}
          gpuNow={roster?.gpu_busy_percent ?? null}
          turnCount={roster?.turns.length ?? 0}
        />
        <Roster roster={roster} range={range} onSelect={onSelectTurn} />
      </main>
      {selected !== null && (
        <TurnDetail
          turn={selected}
          kids={roster?.turns.filter((t) => t.parent_run_id === selected.id) ?? []}
          onSelect={onSelectTurn}
          onClose={() => onSelectTurn(null)}
        />
      )}
    </>
  );
}

// ---- level 1: the plot ----------------------------------------------------

function VitalsPlot({
  range,
  onRange,
  gpuNow,
  turnCount,
}: {
  range: RangeKey;
  onRange: (r: RangeKey) => void;
  gpuNow: number | null;
  turnCount: number;
}) {
  const seconds = RANGES.find((r) => r.key === range)?.seconds ?? 60;
  const samples = useTickingHistory(seconds);
  // Mean AND peak per bucket, drawn as the Ops screen draws every other metric: a solid
  // average with a fainter peak line above it. The columns this replaced could only
  // carry one of the two, so the peak was chosen and the ordinary load was never shown.
  const gpuMean = bucket(samples, seconds, COLUMNS, (s) => s.gpu, "mean");
  const gpuPeak = bucket(samples, seconds, COLUMNS, (s) => s.gpu, "peak");
  const tpsMean = bucket(samples, seconds, COLUMNS, (s) => s.tps, "mean");
  const tpsPeak = bucket(samples, seconds, COLUMNS, (s) => s.tps, "peak");
  const hasGpu = gpuMean.some((v) => v !== null);

  const series: PlotSeries[] = [
    {
      label: "GPU busy",
      // Pinned 0–100: a percentage fitted to its own range draws an idle box hovering
      // at 1–3% as the same dramatic range as one that was pinned all minute.
      scale: { min: 0, max: 100 },
      lines: [{ color: "var(--amber)", values: gpuMean, band: gpuPeak }],
      fmt: (v) => `${Math.round(v)}%`,
    },
    {
      label: "tokens/sec",
      // Unpinned: a token rate has no ceiling to draw against, so it fits its own window
      // like every other Ops series.
      lines: [{ color: "var(--steel)", values: tpsMean, band: tpsPeak }],
      fmt: (v) => `${Math.round(v)}/s`,
    },
  ];

  return (
    <section className="card vitals-plot-card">
      {hasGpu ? (
        <TimeSeriesPlot series={series} />
      ) : (
        <p className="vitals-quiet">no readings for this window yet</p>
      )}
      <div className="vitals-axis">
        <span>−{range}</span>
        <span>now</span>
      </div>
      {/* biome-ignore lint/a11y/useSemanticElements: a segmented range picker, not a form group — a fieldset/legend would announce it as one. */}
      <div className="vitals-ranges" role="group" aria-label="Range">
        {RANGES.map((r) => (
          <button
            key={r.key}
            type="button"
            className={r.key === range ? "on" : ""}
            aria-pressed={r.key === range}
            onClick={() => onRange(r.key)}
          >
            {r.key}
          </button>
        ))}
      </div>
      {gpuNow !== null && gpuNow >= HOT_PERCENT && turnCount > 0 && (
        <p className="vitals-honesty">
          GPU busy counts <b>everything the box does</b> — an image render or a model load pins it
          just as an agent turn does. A high reading is not proof these turns are what is using it.
        </p>
      )}
      <p className="vitals-note">
        {range === "1m"
          ? "One-second samples."
          : `Each point averages ${Math.round(seconds / COLUMNS)}s; the fainter line above is that bucket's PEAK — an average alone would hide the spike you opened this for.`}
      </p>
    </section>
  );
}

// ---- level 1: the roster --------------------------------------------------

function Roster({
  roster,
  range,
  onSelect,
}: {
  roster: LiveTurns | null;
  range: RangeKey;
  onSelect: (id: string) => void;
}) {
  if (roster === null) {
    return <p className="vitals-quiet">reading the run log…</p>;
  }
  const gpu = roster.gpu_busy_percent;
  // Two lists, because "running" and "ran" are different claims. Merging them behind one
  // "turns" heading would put a turn that finished eleven minutes ago next to one still
  // streaming, with only a dot colour between them.
  // Loose null, deliberately: a payload that omits the field entirely must read as
  // "still running", not as a finished turn with no end time. Strict `=== null` let an
  // absent field fall through to the settled group and badged every live turn as done.
  const running = roster.turns.filter((t) => t.ended_at == null);
  const settled = roster.turns.filter((t) => t.ended_at != null);

  if (roster.turns.length === 0) {
    return (
      <section className="card vitals-empty">
        <p className="hl">No agent turns in the last {range}.</p>
        <p>
          {gpu !== null && gpu >= 20
            ? `The GPU is at ${Math.round(gpu)}% because the box is doing something else —
               generating an image, or loading a model. That is real work, but it is not an
               agent turn, so it has no row here.`
            : "The box has been idle."}{" "}
          GPU busy counts everything the box does; this list counts turns.
        </p>
      </section>
    );
  }

  return (
    <>
      {running.length > 0 && (
        <RosterGroup heading="Running now" turns={running} all={roster.turns} onSelect={onSelect} />
      )}
      {settled.length > 0 && (
        <RosterGroup
          heading={`Finished, last ${range}`}
          turns={settled}
          all={roster.turns}
          onSelect={onSelect}
        />
      )}
    </>
  );
}

/** One heading plus its parent rows, each with its children nested underneath. Children
 *  are drawn under their parent wherever that parent is, so a settled parent carries its
 *  settled fan rather than scattering it across both groups. */
function RosterGroup({
  heading,
  turns,
  all,
  onSelect,
}: {
  heading: string;
  turns: LiveTurn[];
  all: LiveTurn[];
  onSelect: (id: string) => void;
}) {
  const ids = new Set(turns.map((t) => t.id));
  // A child whose parent is in this group is drawn nested under it. One whose parent
  // fell outside the window is promoted to a top-level row instead of vanishing.
  const parents = turns.filter((t) => t.parent_run_id === null || !ids.has(t.parent_run_id));

  return (
    <>
      <div className="vitals-sec-head">
        {heading} <span className="count">{turns.length}</span>
      </div>
      <section className="card vitals-roster">
        {parents.map((parent) => {
          const kids = all.filter((t) => t.parent_run_id === parent.id && ids.has(t.id));
          return (
            <div key={parent.id}>
              <TurnRow turn={parent} childCount={kids.length} onSelect={onSelect} />
              {kids.map((kid) => (
                <TurnRow key={kid.id} turn={kid} childCount={0} isChild onSelect={onSelect} />
              ))}
            </div>
          );
        })}
      </section>
    </>
  );
}

function TurnRow({
  turn,
  childCount,
  isChild = false,
  onSelect,
}: {
  turn: LiveTurn;
  childCount: number;
  isChild?: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <button
      type="button"
      className={`vitals-row${isChild ? " child" : ""}`}
      onClick={() => onSelect(turn.id)}
    >
      <span className="vr-name">
        <i className={`vitals-dot ${dotClass(turn)}`} />
        <span className="n">{turn.name}</span>
        <span className="kind">{KIND_LABEL[turn.kind] ?? turn.kind}</span>
      </span>
      <span className="vr-meta">
        {turn.progress_note ?? `${turn.step_count} steps`}
        {childCount > 0 && (
          <span className="sep">
            · {childCount} {childCount === 1 ? "child" : "children"}
          </span>
        )}
      </span>
      <span className="vr-right">
        <Elapsed sinceMs={turn.elapsed_ms} ticking={turn.ended_at == null} />
        <span className="vr-tok">{formatTokens(turn.cost_tokens)}</span>
      </span>
    </button>
  );
}

// ---- level 2: one turn ----------------------------------------------------

function TurnDetail({
  turn,
  kids,
  onSelect,
  onClose,
}: {
  turn: LiveTurn;
  kids: LiveTurn[];
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const call = turn.call;
  return (
    <div className="subscreen vitals-level2">
      <header className="top-bar">
        <button type="button" className="back-btn" onClick={onClose} aria-label="Back">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">{turn.name}</span>
        </button>
      </header>
      <main className="screen-body vitals-detail">
        <section className="card vitals-hero">
          <div className="vh-top">
            <i className={`vitals-dot ${dotClass(turn)}`} />
            <span className="vh-status">{turn.status === "error" ? "failed" : turn.status}</span>
            <span className="kind">{KIND_LABEL[turn.kind] ?? turn.kind}</span>
          </div>
          <div className="vh-grid">
            <div>
              <div className="k">{turn.ended_at == null ? "elapsed" : "took"}</div>
              <div className="v">
                <Elapsed sinceMs={turn.elapsed_ms} ticking={turn.ended_at == null} />
              </div>
            </div>
            <div>
              <div className="k">steps</div>
              <div className="v">{turn.step_count}</div>
            </div>
            <div>
              <div className="k">cost</div>
              <div className="v">{formatTokens(turn.cost_tokens)}</div>
            </div>
          </div>
          {turn.progress_note !== null && <p className="vh-note">{turn.progress_note}</p>}
        </section>

        {kids.length > 0 && (
          <>
            <div className="vitals-sec-head">
              Children <span className="count">{kids.length}</span>
            </div>
            <section className="card">
              {kids.map((kid) => (
                <button
                  key={kid.id}
                  type="button"
                  className="vitals-child-row"
                  onClick={() => onSelect(kid.id)}
                >
                  <i className={`vitals-dot ${kid.status === "error" ? "err" : "run"}`} />
                  <span className="cn">{kid.name}</span>
                  <ChevronRightIcon size={16} />
                </button>
              ))}
            </section>
          </>
        )}

        <div className="vitals-sec-head">The call</div>
        <section className="card vitals-kv">
          {call?.model != null && (
            <Field k="model" v={`${call.model}${call.provider ? ` · ${call.provider}` : ""}`} />
          )}
          {call?.reasoning_effort != null && <Field k="reasoning" v={call.reasoning_effort} />}
          {call?.context_window != null && (
            <Field k="context" v={`${call.context_window.toLocaleString("en-US")} tokens`} />
          )}
          {call?.vision != null && <Field k="vision" v={call.vision ? "yes" : "text only"} />}
          {call?.persona != null && (
            <Field
              k="agent"
              v={`${call.persona}${turn.prompt_version ? ` · ${turn.prompt_version}` : ""}`}
            />
          )}
          {call !== null && (
            <Field
              k="tools"
              v={
                call.tools === null || call.tools === undefined
                  ? "every in-scope tool"
                  : call.tools.length === 0
                    ? "none"
                    : call.tools.join(", ")
              }
            />
          )}
          {call === null && (
            <p className="vitals-quiet">
              This run was started before its call was recorded, so there is nothing to show here.
            </p>
          )}
        </section>

        <div className="vitals-sec-head">The run</div>
        <section className="card vitals-kv">
          <Field k="run id" v={turn.id} mono />
          {turn.parent_run_id !== null && <Field k="parent" v={turn.parent_run_id} mono />}
          <Field k="started" v={new Date(turn.started_at).toLocaleTimeString()} />
          <Field k="trigger" v={turn.trigger_pipeline ?? "you, from this session"} />
          {turn.session_id !== null && <Field k="session" v={turn.session_id} mono />}
          <Field k="domain" v={`${turn.domain_code ?? "general"} · ran as ${turn.ran_as}`} />
        </section>

        {call?.user_message != null && (
          <>
            <div className="vitals-sec-head">Triggering message</div>
            <section className="card">
              <p className="vitals-quote">
                {call.user_message}
                {call.user_message_truncated === true && <span className="vitals-quiet"> …</span>}
              </p>
            </section>
          </>
        )}

        {/* Trail then raw output, last so the output can run as long as it likes. */}
        <TurnTrailAndOutput runId={turn.id} running={turn.status === "running"} />
      </main>
    </div>
  );
}

/** A turn's step trail and its raw output. Polled while the turn runs so a streaming
 *  answer grows on screen; fetched once for a settled one. Separate from the roster
 *  fetch because it is per-turn and only wanted while level 2 is open. */
function TurnTrailAndOutput({ runId, running }: { runId: string; running: boolean }) {
  const detail = useTurnDetail(runId, running);
  if (detail === null) return <p className="vitals-quiet">reading the turn…</p>;

  const output = detail.output;
  const empty = output === null || (output.answer === "" && output.reasoning === "");
  return (
    <>
      {detail.steps.length > 0 && (
        <>
          <div className="vitals-sec-head">
            Steps <span className="count">{detail.steps.length}</span>
          </div>
          <section className="card vitals-trail">
            {detail.steps.map((step) => (
              <div
                key={`${step.idx}-${step.name}`}
                className={`vitals-step${step.ok === null ? " now" : ""}${
                  step.ok === false ? " bad" : ""
                }`}
              >
                <span className="i">{step.idx + 1}</span>
                <span className="t">
                  <b>{step.kind}</b> {step.name}
                </span>
                <span className="d">
                  {step.ok === null
                    ? "live"
                    : step.error !== null
                      ? "failed"
                      : formatTokens(step.cost_tokens)}
                </span>
              </div>
            ))}
          </section>
        </>
      )}

      {detail.prompt != null ? (
        <PromptCard prompt={detail.prompt} />
      ) : (
        <>
          <div className="vitals-sec-head">Prompt sent</div>
          <section className="card">
            <p className="vitals-quiet">
              Not recorded for this turn. Prompts are kept in the server's memory for the life of
              the process, so a run from before a restart — or an older one since evicted — has
              none.
            </p>
          </section>
        </>
      )}

      <div className="vitals-sec-head">
        Raw output
        {output?.live === true && <span className="count live">streaming</span>}
      </div>
      <section className="card vitals-raw">
        {empty ? (
          <p className="vitals-raw-empty">
            {running
              ? "not visible yet — this turn has no live handle, so its output appears once it settles"
              : "this turn produced no output"}
          </p>
        ) : (
          <pre>
            {output !== null && output.reasoning !== "" && (
              <>
                <span className="marker">» reasoning</span>
                {`\n${output.reasoning}\n\n`}
              </>
            )}
            {output !== null && output.answer !== "" && (
              <>
                <span className="marker">» answer</span>
                {`\n${output.answer}`}
              </>
            )}
          </pre>
        )}
        {output !== null && !output.live && !empty && (
          <p className="vitals-note">
            Read from the stored transcript — this turn has no live handle (a sub-agent, or one that
            has already settled).
          </p>
        )}
      </section>
    </>
  );
}

/** The assembled prompt the model actually received. Collapsed by default: it is the
 *  largest thing on the screen and is wanted only when a turn is behaving oddly. */
function PromptCard({ prompt }: { prompt: CapturedPrompt }) {
  const [open, setOpen] = useState(false);
  const total = prompt.system_chars + prompt.message_chars;

  return (
    <>
      <div className="vitals-sec-head">
        Prompt sent <span className="count">round {prompt.round_index}</span>
      </div>
      <section className="card vitals-prompt">
        <button
          type="button"
          className="vitals-prompt-toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span>
            {open ? "Hide" : "Show"} the {formatChars(total)} actually sent
          </span>
          <span className="vitals-prompt-meta">
            {prompt.messages.length} messages · {prompt.tools.length} tools
          </span>
        </button>
        {open && (
          <>
            {prompt.truncated && (
              <p className="vitals-note">
                Clipped for display — the turn sent {formatChars(total)}, of which the head is
                shown.
              </p>
            )}
            <p className="vitals-note">
              Verbatim, as the model received it. Held in memory only, so it does not survive a
              restart.
            </p>
            <pre>
              <span className="marker">» system</span>
              {`\n${prompt.system}\n\n`}
              {prompt.messages.map((m, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: a prompt's messages are an ordered transcript, not a keyed set.
                <span key={`m-${i}`}>
                  <span className="marker">{`» ${m.role}`}</span>
                  {`\n${m.content}\n\n`}
                </span>
              ))}
            </pre>
          </>
        )}
      </section>
    </>
  );
}

export function formatChars(n: number): string {
  if (n < 1000) return `${n} chars`;
  return `${Math.round(n / 1000).toLocaleString("en-US")}k chars`;
}

/** The per-turn detail, refetched while the turn is still running. */
function useTurnDetail(runId: string, running: boolean): TurnDetailPayload | null {
  const foreground = useForeground();
  const [detail, setDetail] = useState<TurnDetailPayload | null>(null);

  useEffect(() => {
    setDetail(null); // a different turn: never show the previous one's output
    if (!foreground) return;
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const next = await api.opsTurnDetail(runId);
        if (!cancelled) setDetail(next);
      } catch {
        // A failed poll leaves what is on screen; the next tick retries.
      }
    };
    void load();
    // A settled turn's output cannot change, so it is fetched once — but the cleanup
    // still has to run. Returning early left `cancelled` unset, so tapping a child row
    // (which changes runId WITHOUT unmounting) could let the previous turn's in-flight
    // response resolve last and render under the new turn's header.
    const timer = running ? setInterval(() => void load(), DETAIL_POLL_MS) : null;
    return () => {
      cancelled = true;
      if (timer !== null) clearInterval(timer);
    };
  }, [runId, running, foreground]);

  return detail;
}

function Field({ k, v, mono = false }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="vitals-field">
      <span className="k">{k}</span>
      <span className={`v${mono ? " mono" : ""}`}>{v}</span>
    </div>
  );
}

// ---- data + helpers -------------------------------------------------------

/** Pull the server's recorded GPU history into the shared ring, once, when the surface
 *  opens — so the graph has a past on a device that has only just loaded the app. */
function useSeededHistory(): void {
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const samples = await api.opsVitalsHistory(900);
        if (!cancelled) seedVitalsHistory(samples);
      } catch {
        // No history is survivable — the graph fills from the live stream as before.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
}

/** The roster for the selected window, refetched while the surface is open and the app
 *  is in the foreground. Changing the range refetches at once rather than waiting out a
 *  poll, so the list and the graph never disagree about which window is on screen. */
function useLiveTurns(seconds: number): LiveTurns | null {
  const foreground = useForeground();
  const [roster, setRoster] = useState<LiveTurns | null>(null);

  useEffect(() => {
    if (!foreground) return;
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const next = await api.opsTurns(seconds);
        if (!cancelled) setRoster(next);
      } catch {
        // A failed poll leaves the last roster on screen; the next tick retries.
      }
    };
    void load();
    const timer = setInterval(() => void load(), ROSTER_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [foreground, seconds]);

  return roster;
}

/** The shared history, re-read once a second so the plot advances while open. */
function useTickingHistory(seconds: number): VitalsSample[] {
  const [samples, setSamples] = useState<VitalsSample[]>(() => vitalsHistory(seconds));

  useEffect(() => {
    const read = (): void => setSamples(vitalsHistory(seconds));
    read(); // re-window at once when the range changes, not a second later
    const timer = setInterval(read, 1000);
    return () => clearInterval(timer);
  }, [seconds]);

  return samples;
}

/** Fold samples into `count` columns, as either the mean or the PEAK of each bucket.
 *
 *  Both are drawn, and deliberately: this gauge is read to find the moment the box was
 *  pinned, and averaging a 15-minute window flattens a 10-second spike into nothing —
 *  but a peak-only plot never shows what the box was doing the rest of the time. The
 *  mean is the line, the peak is the band above it, and the gap between them is the
 *  burstiness. */
export function bucket(
  samples: VitalsSample[],
  seconds: number,
  count: number,
  channel: (s: VitalsSample) => number | null = (s) => s.gpu,
  agg: "peak" | "mean" = "peak",
): (number | null)[] {
  if (samples.length === 0) return new Array<number | null>(count).fill(null);
  const end = Date.now();
  const start = end - seconds * 1000;
  const width = (seconds * 1000) / count;
  const sums = new Array<number>(count).fill(0);
  const counts = new Array<number>(count).fill(0);
  const peaks = new Array<number | null>(count).fill(null);
  for (const sample of samples) {
    const value = channel(sample);
    if (value === null || sample.at < start) continue;
    const slot = Math.min(count - 1, Math.floor((sample.at - start) / width));
    const held = peaks[slot];
    peaks[slot] = held === null || held === undefined ? value : Math.max(held, value);
    sums[slot] = (sums[slot] ?? 0) + value;
    counts[slot] = (counts[slot] ?? 0) + 1;
  }
  if (agg === "peak") return peaks;
  // A bucket with no samples stays null — a gap, never a zero, which would read as an
  // idle GPU or as a turn generating nothing.
  return counts.map((n, i) => (n === 0 ? null : (sums[i] ?? 0) / n));
}

/** Elapsed, ticking locally so a RUNNING turn's age advances between roster polls.
 *
 *  A settled turn is shown as a fixed duration: its elapsed time stopped when it did, and
 *  advancing it would have a finished turn appear to still be aging — the wider windows
 *  are full of those, so the distinction has to hold. */
function Elapsed({ sinceMs, ticking = true }: { sinceMs: number; ticking?: boolean }) {
  const mountedAt = useRef(Date.now());
  const base = useRef(sinceMs);
  base.current = sinceMs;
  mountedAt.current = Date.now();
  const [, force] = useState(0);

  useEffect(() => {
    if (!ticking) return;
    const timer = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [ticking]);

  const total = ticking ? base.current + (Date.now() - mountedAt.current) : base.current;
  return <span className="vr-elapsed">{formatElapsed(total)}</span>;
}

/** The row's state dot. A settled turn is neither "running" nor "failed" — without a
 *  third class every finished turn in a 15-minute window rendered as still running. */
function dotClass(turn: LiveTurn): string {
  if (turn.status === "error") return "err";
  return turn.ended_at == null ? "run" : "done";
}

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

export function formatTokens(n: number): string {
  if (n < 1000) return `${n} tok`;
  return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k tok`;
}
