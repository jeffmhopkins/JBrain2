/** On another phone: two codes, for a phone signed in to nothing (the robot's cradle phone). The
 *  first, scanned with its camera app, opens the box's setup page (certificate download and a
 *  scanner); the second, scanned from inside that page, is a one-time ticket naming the persona
 *  it opens as and, optionally, the robot it wears. "Copy code 2 as a link" is the same ticket
 *  as text: launching a persona from a link is one-time by design, never a standing link. */
import { QRCodeSVG } from "qrcode.react";
import { useEffect, useRef, useState } from "react";
import { ApiError, type GrowbotPersona, api } from "../api/client";
import { type Places, forgetLaunchHere, onPhone, riderOfPersonas } from "../growbot";
import { NEARBY, type Say } from "./GrowbotPersonaSheets";
import { Sheet } from "./Sheet";

// The api's ticket lives 120 s; a fresh one is minted a little before, so a scan never lands
// on a spent second.
const REFRESH_MS = 100_000;

interface Props {
  personas: GrowbotPersona[];
  pl: Places;
  initialPersona: string | null;
  say: Say;
  onClose: () => void;
}

function setupUrl(ticketUrl: string): string {
  try {
    return `${new URL(ticketUrl).origin}/setup.html`;
  } catch {
    return "";
  }
}

export function GrowbotPhoneSheet({ personas, pl, initialPersona, say, onClose }: Props) {
  const [pid, setPid] = useState<string>(initialPersona ?? personas[0]?.id ?? "");
  const [code, setCode] = useState<string | null>(null);
  const [ticket, setTicket] = useState("");
  const [setup, setSetup] = useState("");
  const [error, setError] = useState("");
  // The persona the codes were for was deleted (seen in the list, or the box's 404): minting
  // stops, since every new code would be refused too.
  const [gone, setGone] = useState("");
  const p = personas.find((x) => x.id === pid) ?? null;
  const listed = !!p;
  const name = useRef("");
  if (p) name.current = p.name;
  const live = pl.robots.filter((r) => !r.revoked_at);
  const cradle = live[0] ? `${live[0].name || live[0].code}'s cradle` : "a robot's cradle";

  useEffect(() => {
    if (!pid) return;
    if (!listed) {
      setTicket("");
      setGone(pid);
      return;
    }
    setGone("");
    // A reply for the choice made before this one must not land after its own: code 2 would
    // then open the phone as a persona, or on a robot, the sheet no longer names.
    let current = true;
    const mint = async () => {
      setError("");
      // That persona now opens on another phone, so a launch here for it can't be learned
      // from: the phone that redeems this code would be taken for this one.
      forgetLaunchHere(pid);
      try {
        const url = (await api.growbotTicket(code ? { persona: pid, code } : { persona: pid })).url;
        if (!current) return;
        setTicket(url);
        // Code 1 names only the box, so it stays up when a later ticket fails to load.
        setSetup(setupUrl(url));
      } catch (e) {
        if (!current) return;
        setTicket("");
        if (e instanceof ApiError && e.status === 404) {
          clearInterval(id);
          setGone(pid);
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
      }
    };
    setTicket("");
    const id = setInterval(() => void mint(), REFRESH_MS);
    void mint();
    return () => {
      current = false;
      clearInterval(id);
    };
  }, [pid, code, listed]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(ticket);
      say(
        "Copied a one-time link. It opens once, in the next 2 minutes — send it only to a phone you trust.",
      );
    } catch {
      say("The phone wouldn't copy it — scan code 2 instead.");
    }
  }

  return (
    <Sheet title="Open on another phone" onClose={onClose}>
      <p className="gb-hint">
        For a phone that isn't signed in to JBrain, like the one in {cradle}. It needs both codes,
        in order.
      </p>
      <p className="gb-hint dim">{NEARBY}</p>
      <fieldset className="gb-choices">
        <legend>It opens as</legend>
        <div className="gb-chips">
          {personas.map((x) => (
            <button
              key={x.id}
              type="button"
              className={`gb-btn${x.id === pid ? " primary" : ""}`}
              aria-pressed={x.id === pid}
              onClick={() => setPid(x.id)}
            >
              {x.name}
            </button>
          ))}
        </div>
      </fieldset>
      {pl.robots.length > 0 && (
        <fieldset className="gb-choices">
          <legend>With the body?</legend>
          <label className={`gb-choice${code === null ? " sel" : ""}`}>
            <input
              type="radio"
              name="gb-other-body"
              checked={code === null}
              onChange={() => setCode(null)}
            />
            <span className="gb-tx">
              Alone on that phone
              <em>
                No legs.
                {live
                  .map((r) => {
                    const rider = riderOfPersonas(personas, r.code);
                    return rider && rider.id !== pid
                      ? ` ${rider.name} keeps ${r.name || r.code}.`
                      : "";
                  })
                  .join("")}
              </em>
            </span>
          </label>
          {pl.robots.map((r) => {
            const b = r.name || r.code;
            const rider = riderOfPersonas(personas, r.code);
            return (
              <label
                key={r.code}
                className={`gb-choice wear${code === r.code ? " sel" : ""}${r.revoked_at ? " off" : ""}`}
              >
                <input
                  type="radio"
                  name="gb-other-body"
                  checked={code === r.code}
                  disabled={!!r.revoked_at}
                  onChange={() => setCode(r.code)}
                />
                <span className="gb-tx">
                  Wearing {b}
                  <em>
                    {r.revoked_at
                      ? `${b}'s key is revoked — reflash it first.`
                      : rider && rider.id !== pid
                        ? `${rider.name} lets go and carries on alone.`
                        : "It takes the legs once it opens."}
                  </em>
                </span>
              </label>
            );
          })}
        </fieldset>
      )}
      {p?.phone && (
        <p className="gb-msg warn">
          {p.name} is open {onPhone(p.phone, pl)}. Opening it on the new phone hands it over — it
          closes there and carries on here with everything it remembers.
        </p>
      )}
      {gone === pid && (
        <p className="gb-msg bad" role="alert">
          {name.current || "That persona"} was deleted, so there's no code for it.
          {personas.length ? " Pick another persona above." : " Make a persona first."}
        </p>
      )}
      {error && (
        <p className="gb-msg bad" role="alert">
          The code didn't load — {error}
        </p>
      )}
      {setup && (
        <section className="gb-qr" aria-label="Code 1">
          <h3>1. Scan with the phone's camera app</h3>
          <QRCodeSVG value={setup} size={200} bgColor="#ffffff" fgColor="#000000" />
          <p className="gb-hint dim">
            Opens the box's setup page. Download and install the certificate there, once per phone.
          </p>
        </section>
      )}
      {ticket && p && (
        <section className="gb-qr" aria-label="Code 2">
          <h3>2. Scan from the setup page's Scan the code</h3>
          <QRCodeSVG value={ticket} size={200} bgColor="#ffffff" fgColor="#000000" />
          <p className="gb-hint dim">
            Opens {p.name}
            {code ? ` wearing ${pl.robots.find((r) => r.code === code)?.name || code}` : ", alone"}.
            It works once, and a fresh one appears here every couple of minutes.
          </p>
          <button type="button" className="gb-linkbtn" onClick={() => void copy()}>
            Copy code 2 as a link instead
          </button>
        </section>
      )}
      <div className="gb-sheet-foot">
        <button type="button" className="gb-btn block" onClick={onClose}>
          Done
        </button>
      </div>
    </Sheet>
  );
}
