// The desk panel's alarms, on its card in jpanel → Panels (docs/mocks/jpanel-c6/PwaAlarms.dc.html).
//
// THE PWA IS WHERE ALARMS ARE MADE (owner decision, mocks README): time, repeat days, label and
// sound are set here; the panel only switches them on and off. The panel keeps its own copy and
// rings from its own clock, so what this screen owes the owner is an honest answer to "has the
// panel got it yet?" — "On the panel" only once the panel asks the box with this version, which
// it does only after storing it.

import { useCallback, useEffect, useState } from "react";

import {
  ALARMS_MAX,
  ALARM_LABEL_CHARS,
  ALARM_LABEL_MAX,
  ALARM_SOUNDS,
  type AlarmSound,
  ApiError,
  type PanelAlarm,
  type PanelAlarmIn,
  type PanelAlarms as PanelAlarmsOut,
  api,
} from "../api/client";

/** While a change has not reached the panel, ask again this often. The panel fetches on its
 *  one-minute manifest pass, so a change lands within about a minute. */
const SYNC_POLL_MS = 10_000;
/** The second tap on Delete must come within this; walking away is a cancel (the Revoke rule). */
const ARMED_MS = 3_000;

const SHORT = ["S", "M", "T", "W", "T", "F", "S"];
const FULL = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const SOUND_NAMES: Record<AlarmSound, string> = { chime: "Chime", beeps: "Beeps", rise: "Rise" };

/** The box's mask is the panel's: bit 0 Monday … bit 6 Sunday. The chips run Sunday first, as
 *  the mock does, so chip `k` (0 = Sunday) is bit (k + 6) % 7. */
export function dayBit(k: number): number {
  return 1 << ((k + 6) % 7);
}

/** "Once", "Every day", "Weekdays", "Weekends", or the days Sunday-first — the mock's words. */
export function daysText(days: number): string {
  const d = days & 0x7f;
  if (d === 0) return "Once";
  if (d === 0x7f) return "Every day";
  if (d === 0x1f) return "Weekdays";
  if (d === 0x60) return "Weekends";
  return NAMES.filter((_, k) => d & dayBit(k)).join(", ");
}

/** 24-hour, on the panel and here (owner decision). */
export function hm(at: number): string {
  return `${String(Math.floor(at / 60)).padStart(2, "0")}:${String(at % 60).padStart(2, "0")}`;
}

function minutes(value: string): number | null {
  const m = /^(\d{1,2}):(\d{2})/.exec(value);
  if (!m) return null;
  const h = Number(m[1]);
  const mm = Number(m[2]);
  return h < 24 && mm < 60 ? h * 60 + mm : null;
}

type Editing = { id: number | "new"; form: PanelAlarmIn };

/** What the box takes: the alarm without its id, which is the URL's. */
function fields(a: PanelAlarm): PanelAlarmIn {
  return { at: a.at, days: a.days, on: a.on, label: a.label, sound: a.sound };
}

const NEW_ALARM: PanelAlarmIn = { at: 7 * 60, days: 0x1f, on: true, label: "", sound: "chime" };

export function PanelAlarms({ deviceId }: { deviceId: string }) {
  const [data, setData] = useState<PanelAlarmsOut | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    try {
      setData(await api.panelAlarms(deviceId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not read the alarms.");
    }
  }, [deviceId]);

  useEffect(() => {
    void load();
  }, [load]);

  const synced = data !== null && data.panel_etag === data.etag;
  // Only while something is on its way: a settled list costs nothing to leave open.
  useEffect(() => {
    if (data === null || synced) return;
    const t = setInterval(() => void load(), SYNC_POLL_MS);
    return () => clearInterval(t);
  }, [data, synced, load]);

  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), ARMED_MS);
    return () => clearTimeout(t);
  }, [armed]);

  async function act(run: () => Promise<unknown>, failed: string): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      await run();
      await load();
      return true;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : failed);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function toggle(a: PanelAlarm): Promise<void> {
    await act(
      () => api.setPanelAlarm(deviceId, a.id, { ...fields(a), on: !a.on }),
      "Could not switch it.",
    );
  }

  async function save(): Promise<void> {
    if (!editing) return;
    const form = { ...editing.form, label: editing.form.label.trim() };
    const ok = await act(
      () =>
        editing.id === "new"
          ? api.addPanelAlarm(deviceId, form)
          : api.setPanelAlarm(deviceId, editing.id, form),
      "Could not save it.",
    );
    if (ok) setEditing(null);
  }

  async function remove(): Promise<void> {
    if (!editing || editing.id === "new") return;
    if (!armed) {
      setArmed(true);
      return;
    }
    const id = editing.id;
    const ok = await act(() => api.deletePanelAlarm(deviceId, id), "Could not delete it.");
    setArmed(false);
    if (ok) setEditing(null);
  }

  function edit(next: Editing | null): void {
    setArmed(false);
    setError(null);
    setEditing(next);
  }

  const alarms = data?.alarms ?? [];
  const full = alarms.length >= (data?.max ?? ALARMS_MAX);
  const form = editing?.form;
  const labelOk =
    form === undefined ||
    (form.label.trim().length <= ALARM_LABEL_MAX && ALARM_LABEL_CHARS.test(form.label));
  const syncNote = data === null ? "" : synced ? "On the panel" : "Sending to panel…";

  return (
    <section className="jp-alarms" aria-label="Alarms">
      <div className="jp-alarms-head">
        <span className="jp-alarms-title">Alarms</span>
        <span className={`jp-alarms-sync${synced ? " ok" : ""}`} aria-live="polite">
          {syncNote}
        </span>
      </div>

      {data !== null && alarms.length === 0 && (
        <p className="jp-unit-hint">
          No alarms yet. Add one and the panel rings it from its own clock.
        </p>
      )}

      {alarms.length > 0 && (
        <ul className="jp-alarm-list">
          {alarms.map((a) => (
            <li key={a.id} className="jp-alarm-row">
              <button
                type="button"
                className={`jp-alarm-edit${a.on ? "" : " off"}`}
                aria-label={`Edit alarm ${hm(a.at)}`}
                onClick={() => edit({ id: a.id, form: fields(a) })}
              >
                <span className="jp-alarm-time">{hm(a.at)}</span>
                <span className="jp-alarm-sub">
                  {daysText(a.days)}
                  {a.label ? ` · ${a.label}` : ""} · {SOUND_NAMES[a.sound]}
                </span>
              </button>
              <button
                type="button"
                role="switch"
                aria-checked={a.on}
                aria-label={`Alarm ${hm(a.at)}, ${a.on ? "on" : "off"}`}
                className={`jp-switch${a.on ? " on" : ""}`}
                disabled={busy}
                onClick={() => void toggle(a)}
              >
                <span className="jp-knob" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {editing === null ? (
        <>
          <button
            type="button"
            className="jp-alarm-add"
            disabled={data === null || full || busy}
            onClick={() => edit({ id: "new", form: { ...NEW_ALARM } })}
          >
            Add alarm
          </button>
          {full && <p className="jp-unit-hint">The panel holds {ALARMS_MAX} alarms.</p>}
        </>
      ) : (
        form && (
          <div className="jp-alarm-form">
            <span className="jp-alarm-form-title">
              {editing.id === "new" ? "New alarm" : "Edit alarm"}
            </span>
            <label>
              Time
              <input
                type="time"
                value={hm(form.at)}
                onChange={(e) => {
                  const at = minutes(e.target.value);
                  if (at !== null) setEditing({ ...editing, form: { ...form, at } });
                }}
              />
            </label>
            <div className="jp-alarm-days">
              <span>Repeat</span>
              <div className="jp-day-chips">
                {SHORT.map((s, k) => {
                  const on = (form.days & dayBit(k)) !== 0;
                  return (
                    <button
                      // biome-ignore lint/suspicious/noArrayIndexKey: seven fixed days, never reordered
                      key={k}
                      type="button"
                      aria-pressed={on}
                      aria-label={FULL[k]}
                      className={on ? "on" : ""}
                      onClick={() =>
                        setEditing({ ...editing, form: { ...form, days: form.days ^ dayBit(k) } })
                      }
                    >
                      {s}
                    </button>
                  );
                })}
              </div>
              <span className="jp-unit-hint">
                {form.days
                  ? daysText(form.days)
                  : "No days picked: rings once, then switches itself off."}
              </span>
            </div>
            <label>
              Label (optional)
              <input
                type="text"
                value={form.label}
                maxLength={ALARM_LABEL_MAX}
                placeholder="Work"
                onChange={(e) =>
                  setEditing({ ...editing, form: { ...form, label: e.target.value } })
                }
              />
            </label>
            {!labelOk && (
              <p className="jp-unit-error">
                Letters, digits and plain punctuation, but no " or \ — the panel cannot take the
                rest.
              </p>
            )}
            <fieldset className="jp-alarm-sounds">
              <legend>Sound</legend>
              <div className="jp-sound-seg">
                {ALARM_SOUNDS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    aria-pressed={form.sound === s}
                    className={form.sound === s ? "on" : ""}
                    onClick={() => setEditing({ ...editing, form: { ...form, sound: s } })}
                  >
                    {SOUND_NAMES[s]}
                  </button>
                ))}
              </div>
            </fieldset>
            <div className="jp-unit-actions">
              {editing.id !== "new" && (
                <button
                  type="button"
                  className={armed ? "danger armed" : "danger"}
                  disabled={busy}
                  onClick={() => void remove()}
                >
                  {armed ? "Tap again to delete" : "Delete"}
                </button>
              )}
              <span className="jp-alarm-spacer" />
              <button type="button" onClick={() => edit(null)}>
                Cancel
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy || !labelOk}
                onClick={() => void save()}
              >
                {busy ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        )
      )}

      {error !== null && <p className="jp-unit-error">{error}</p>}
      <p className="jp-unit-hint">
        The panel keeps its alarms and rings from its own clock, so an alarm still goes off if Wi-Fi
        drops overnight. On the panel they can only be switched on and off.
      </p>
    </section>
  );
}
