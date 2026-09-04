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
  GENERAL,
  SERVICES,
  type SdrRadio,
  type SdrRadios,
  asRadios,
  generalOutcome,
  isKnownRole,
  labelFor,
  outcomeFor,
  roleLabel,
} from "../sdrRadios";

/** One radio's editable fields, held as typed so a half-written name is legal. */
interface Draft {
  name: string;
  description: string;
  role: string;
}

function draftOf(radio: SdrRadio): Draft {
  return { name: radio.name, description: radio.description, role: radio.role };
}

export function SdrRadiosCard() {
  const [state, setState] = useState<SdrRadios | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const adopt = useCallback((payload: unknown) => {
    const next = asRadios(payload);
    if (next === null) return;
    setState(next);
    setDrafts(Object.fromEntries(next.radios.map((r) => [r.serial, draftOf(r)])));
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
      adopt(await api.describeSdrRadio(serial, draft));
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
      adopt(await api.forgetSdrRadio(serial));
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
      draft.role !== radio.role
    );
  };

  return (
    <section className="settings-card">
      <h2 className="settings-label">Radios</h2>
      <p className="settings-meta">
        name each radio, say what it is plugged into, and set what it is for. All three are
        remembered against the radio's serial, so they survive unplugging it or moving it to another
        USB port. A radio dedicated to a service is not one the tuner may borrow.
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
