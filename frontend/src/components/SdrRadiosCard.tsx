// Settings → Radios. Name each dongle, say what it is plugged into, set what it is for.
// Binding spec: docs/mocks/sdr-dongles/a-named-roles.html.
//
// The problem it solves, measured 2026-09-03: two NESDR SMArt v5s attached, and both
// `rtl_fm` and `rtl_power` invoked with no `-d`, so they opened whichever librtlsdr
// enumerated first. One radio on a desk whip and one on a long wire, and APRS could
// change antenna on a re-plug with no symptom but worse reception.

import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import {
  GAIN_AUTO,
  GAIN_RUNGS,
  GENERAL,
  HAM_IT_UP_MHZ,
  SERVICES,
  type SdrRadio,
  type SdrRadios,
  asRadios,
  converterNote,
  gainNote,
  generalOutcome,
  isKnownRole,
  labelFor,
  outcomeFor,
  roleLabel,
} from "../sdrRadios";

/** One radio's editable fields, held as typed so a half-written name is legal.
 *
 *  `upconverterMhz` is TEXT, not a number, because it is being typed: "125." and ""
 *  are legal half-written states that `parseFloat` collapses, and a field that
 *  re-rendered itself as 125 the moment the owner cleared it could not be edited. */
interface Draft {
  name: string;
  description: string;
  role: string;
  gain: string;
  converter: boolean;
  upconverterMhz: string;
}

function draftOf(radio: SdrRadio): Draft {
  return {
    name: radio.name,
    description: radio.description,
    role: radio.role,
    gain: radio.gain,
    converter: radio.upconverter_hz > 0,
    upconverterMhz: radio.upconverter_hz > 0 ? String(radio.upconverter_hz / 1_000_000) : "",
  };
}

/** The offset a draft would save, in Hz. Off, blank or unreadable is 0 — no shift.
 *
 *  Zero rather than a refusal for a half-typed number: the Save button is the only
 *  thing that commits, and the card says what will happen beside it. */
function offsetHzOf(draft: Draft): number {
  if (!draft.converter) return 0;
  const mhz = Number.parseFloat(draft.upconverterMhz);
  return Number.isFinite(mhz) && mhz > 0 ? Math.round(mhz * 1_000_000) : 0;
}

export function SdrRadiosCard() {
  const [state, setState] = useState<SdrRadios | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  /**
   * Take a server payload, keeping edits in progress on the OTHER radios.
   *
   * `keep` is the serial just saved, whose draft the server has now superseded.
   * Rebuilding the whole map on every save threw away a description half-typed on
   * another card — silently, and with its Save button then greyed out because the
   * draft matched the server again. Per-card save has to mean per-card adopt.
   */
  const adopt = useCallback((payload: unknown, keep?: string) => {
    const next = asRadios(payload);
    if (next === null) return;
    setState(next);
    setDrafts((current) =>
      Object.fromEntries(
        next.radios.map((r) => {
          const edited = current[r.serial];
          const stale = keep === undefined || r.serial === keep || edited === undefined;
          return [r.serial, stale ? draftOf(r) : edited];
        }),
      ),
    );
  }, []);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const next = await api.getSdrRadios();
        if (live) adopt(next);
      } catch {
        // A box with no radio answers this the same way as one whose api is briefly
        // unreachable, and neither is worth an error on a settings screen the owner
        // opened for something else. The card simply does not appear.
      } finally {
        if (live) setLoaded(true);
      }
    })();
    return () => {
      live = false;
    };
  }, [adopt]);

  async function save(serial: string): Promise<void> {
    const draft = drafts[serial];
    if (!draft) return;
    setSaving(serial);
    setError(null);
    try {
      adopt(
        await api.describeSdrRadio(serial, {
          name: draft.name,
          description: draft.description,
          role: draft.role,
          gain: draft.gain,
          upconverter_hz: offsetHzOf(draft),
        }),
        serial,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that radio.");
    } finally {
      setSaving(null);
    }
  }

  async function forget(serial: string): Promise<void> {
    setSaving(serial);
    setError(null);
    try {
      adopt(await api.forgetSdrRadio(serial), serial);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not forget that radio.");
    } finally {
      setSaving(null);
    }
  }

  // Nothing to configure on a box with no radio, and nothing to say while we are still
  // asking. Rendering an empty "Radios" heading on every other box would be noise.
  if (!loaded || state === null || state.radios.length === 0) return null;

  const dirty = (radio: SdrRadio): boolean => {
    const draft = drafts[radio.serial];
    if (!draft) return false;
    return (
      draft.name !== radio.name ||
      draft.description !== radio.description ||
      draft.role !== radio.role ||
      draft.gain !== radio.gain ||
      offsetHzOf(draft) !== radio.upconverter_hz
    );
  };

  return (
    <section className="settings-card">
      <h2 className="settings-label">Radios</h2>
      <p className="settings-meta">
        name each radio, say what it is plugged into, set what it is for, and how it is wired —
        tuner gain and any upconverter in front of it. All of it is remembered against the radio's
        serial, so it survives unplugging it or moving it to another USB port. A radio dedicated to
        a service is not one the tuner may borrow.
      </p>

      {!state.scan_ok && (
        <output className="settings-meta">
          the USB scan could not be reached, so whether each radio is attached is unknown rather
          than no.
        </output>
      )}

      {state.radios.map((radio) => {
        const draft = drafts[radio.serial] ?? draftOf(radio);
        const conflicting = (state.conflicts[radio.role] ?? []).includes(radio.serial);
        return (
          <div key={radio.serial} className="settings-subcard">
            <h3 className="settings-label">
              {labelFor(radio)}{" "}
              <span className="settings-meta">
                {radio.serial} · {radio.attached ? "attached" : "not attached"}
              </span>
            </h3>

            <label className="settings-field">
              Name
              <input
                value={draft.name}
                placeholder="e.g. Long wire"
                spellCheck={false}
                autoComplete="off"
                onChange={(e) =>
                  setDrafts((d) => ({ ...d, [radio.serial]: { ...draft, name: e.target.value } }))
                }
              />
            </label>

            <label className="settings-field">
              Description
              <input
                value={draft.description}
                placeholder="what is it plugged into?"
                spellCheck={false}
                autoComplete="off"
                onChange={(e) =>
                  setDrafts((d) => ({
                    ...d,
                    [radio.serial]: { ...draft, description: e.target.value },
                  }))
                }
              />
            </label>

            <label className="settings-field">
              Used for
              <select
                value={draft.role}
                onChange={(e) =>
                  setDrafts((d) => ({ ...d, [radio.serial]: { ...draft, role: e.target.value } }))
                }
              >
                <option value={GENERAL}>General use — anything may take it</option>
                {SERVICES.map((service) => (
                  <option key={service.id} value={service.id}>
                    Dedicated — {service.label}
                  </option>
                ))}
                {/* A role this build does not know stays selectable, so opening Settings
                    cannot silently free a radio reserved by a newer one. */}
                {!isKnownRole(draft.role) && (
                  <option value={draft.role}>{roleLabel(draft.role)}</option>
                )}
              </select>
            </label>

            {/* TWO MORE FIELDS, not a new surface. The owner discarded three richer
                rivals — an inline disclosure, a page per radio with the gain ladder
                charted, a five-bead signal chain — against "needs to be simpler", and
                they were right: this card already models the radio and what it is for,
                keyed by serial with one Save. Gain and a converter are two more answers
                to that same question (docs/mocks/radio-settings/README.md). */}
            <fieldset className="settings-field">
              <legend>Gain</legend>
              <div className="seg-row">
                {[...GAIN_RUNGS, GAIN_AUTO].map((rung) => (
                  <button
                    key={rung}
                    type="button"
                    className={draft.gain === rung ? "seg seg-on" : "seg"}
                    aria-pressed={draft.gain === rung}
                    onClick={() =>
                      setDrafts((d) => ({
                        ...d,
                        // Tapping the chosen rung again CLEARS it, because unset is a
                        // real state with its own behaviour (AGC listening, 30 dB
                        // measuring) and a control that could reach every value except
                        // the one it started at would be a trap.
                        [radio.serial]: { ...draft, gain: draft.gain === rung ? "" : rung },
                      }))
                    }
                  >
                    {rung === GAIN_AUTO ? "Auto" : rung === "0" ? "0 dB" : rung}
                  </button>
                ))}
              </div>
              {/* The note carries its own tone: `warn` for a choice the measurements
                  argue against, `off` for a setting that is not doing anything. */}
              <p className={`settings-note ${gainNote(draft.gain, draft.converter).tone}`}>
                {gainNote(draft.gain, draft.converter).text}
              </p>
            </fieldset>

            <fieldset className="settings-field">
              <legend>Upconverter</legend>
              <div className="seg-row">
                <button
                  type="button"
                  className={draft.converter ? "seg" : "seg seg-on"}
                  aria-pressed={!draft.converter}
                  onClick={() =>
                    setDrafts((d) => ({ ...d, [radio.serial]: { ...draft, converter: false } }))
                  }
                >
                  Off
                </button>
                <button
                  type="button"
                  className={draft.converter ? "seg seg-on" : "seg"}
                  aria-pressed={draft.converter}
                  onClick={() =>
                    setDrafts((d) => ({
                      ...d,
                      [radio.serial]: {
                        ...draft,
                        converter: true,
                        // The Ham It Up's nominal crystal as a STARTING POINT only —
                        // 125 is this unit's number, not every unit's, which is why the
                        // field is editable at all.
                        upconverterMhz: draft.upconverterMhz || String(HAM_IT_UP_MHZ),
                      },
                    }))
                  }
                >
                  Inline
                </button>
              </div>
              {draft.converter && (
                <label className="settings-offset">
                  <input
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step={0.001}
                    value={draft.upconverterMhz}
                    aria-label="Upconverter offset in megahertz"
                    onChange={(e) =>
                      setDrafts((d) => ({
                        ...d,
                        [radio.serial]: { ...draft, upconverterMhz: e.target.value },
                      }))
                    }
                  />
                  MHz offset
                </label>
              )}
              <p className={`settings-note ${converterNote(offsetHzOf(draft)).tone}`}>
                {converterNote(offsetHzOf(draft)).text}
              </p>
            </fieldset>

            {conflicting && (
              <p className="settings-meta" role="alert" style={{ color: "var(--danger)" }}>
                more than one radio is dedicated to {roleLabel(radio.role)}. Every frame would be
                logged twice — set one back to general use.
              </p>
            )}

            <div className="settings-actions">
              <button
                type="button"
                className="seg"
                aria-label={`Save ${labelFor(radio)}`}
                disabled={saving !== null || !dirty(radio)}
                onClick={() => void save(radio.serial)}
              >
                {saving === radio.serial ? "Saving…" : "Save"}
              </button>
              {/* Only offered for a radio that is gone: forgetting one still on the desk
                  would just make it reappear, unnamed, on the next scan. */}
              {!radio.attached && (
                <button
                  type="button"
                  className="seg"
                  aria-label={`Forget ${labelFor(radio)}`}
                  disabled={saving !== null}
                  onClick={() => void forget(radio.serial)}
                >
                  Forget
                </button>
              )}
            </div>
          </div>
        );
      })}

      <h3 className="settings-label">What will actually happen</h3>
      <ul className="settings-meta">
        {SERVICES.map((service) => {
          const outcome = outcomeFor(state, service.id);
          return (
            <li key={service.id}>
              <strong>{service.label}:</strong> {outcome.text}
            </li>
          );
        })}
        <li>
          <strong>Tuner &amp; band sweeps:</strong> {generalOutcome(state).text}
        </li>
      </ul>

      {error !== null && (
        <p className="settings-meta" role="alert" style={{ color: "var(--danger)" }}>
          {error}
        </p>
      )}
    </section>
  );
}
