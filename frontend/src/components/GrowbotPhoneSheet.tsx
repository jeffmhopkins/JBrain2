/** Set up another phone (the robot's): two codes, because that phone is signed in to nothing.
 *  The first, scanned with its camera app, opens the box's setup page (certificate download and
 *  a scanner); the second, scanned from inside that page, is a one-time ticket that opens the
 *  creature there on the robot picked here. Pairing stays JBrain's: the ticket names the robot. */
import { QRCodeSVG } from "qrcode.react";
import { useEffect, useState } from "react";
import { type GrowbotRobot, api } from "../api/client";
import { Sheet } from "./Sheet";

const NO_ROBOT = "";
// The api's ticket lives 120 s; a fresh one is minted a little before, so a scan never lands
// on a spent second.
const REFRESH_MS = 100_000;

interface Props {
  robots: GrowbotRobot[];
  onClose: () => void;
}

function setupUrl(ticketUrl: string): string {
  try {
    return `${new URL(ticketUrl).origin}/setup.html`;
  } catch {
    return "";
  }
}

export function GrowbotPhoneSheet({ robots, onClose }: Props) {
  const live = robots.filter((r) => !r.revoked_at);
  const [target, setTarget] = useState<string>(live[0]?.code ?? NO_ROBOT);
  const [ticket, setTicket] = useState("");
  const [setup, setSetup] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    // A reply for the robot picked before this one must not land after its own: code 2 would
    // then open the phone on a robot the sheet no longer names.
    let current = true;
    const mint = async () => {
      setError("");
      try {
        const url = (await api.growbotTicket(target)).url;
        if (!current) return;
        setTicket(url);
        // Code 1 names only the box, so it stays up when a later ticket fails to load.
        setSetup(setupUrl(url));
      } catch (e) {
        if (!current) return;
        setTicket("");
        setError(e instanceof Error ? e.message : String(e));
      }
    };
    setTicket("");
    void mint();
    const id = setInterval(() => void mint(), REFRESH_MS);
    return () => {
      current = false;
      clearInterval(id);
    };
  }, [target]);

  return (
    <Sheet title="Set up another phone" onClose={onClose}>
      <p className="gb-hint">
        For the robot's phone, which isn't signed in to JBrain. It needs both codes, in order.
      </p>
      <p className="gb-hint dim">
        On Android, first allow Chrome to reach the home network: Settings → Apps → Chrome →
        Permissions → Nearby devices (or Local network) → Allow. Without it Chrome says
        ERR_TOO_MANY_RETRIES.
      </p>
      <fieldset className="gb-choices">
        <legend>It opens as</legend>
        {live.map((r) => (
          <label key={r.code} className={`gb-choice${target === r.code ? " sel" : ""}`}>
            <input
              type="radio"
              name="gb-phone-target"
              checked={target === r.code}
              onChange={() => setTarget(r.code)}
            />
            <span className="gb-tx">
              {r.name || r.code}
              <em>The creature on this robot's body.</em>
            </span>
          </label>
        ))}
        <label className={`gb-choice${target === NO_ROBOT ? " sel" : ""}`}>
          <input
            type="radio"
            name="gb-phone-target"
            checked={target === NO_ROBOT}
            onChange={() => setTarget(NO_ROBOT)}
          />
          <span className="gb-tx">
            No robot
            <em>The creature on the phone alone.</em>
          </span>
        </label>
      </fieldset>
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
      {ticket && (
        <section className="gb-qr" aria-label="Code 2">
          <h3>2. Scan from the setup page's Scan the code</h3>
          <QRCodeSVG value={ticket} size={200} bgColor="#ffffff" fgColor="#000000" />
          <p className="gb-hint dim">
            Opens the creature on that phone. It works once, and a fresh one appears here every
            couple of minutes.
          </p>
        </section>
      )}
    </Sheet>
  );
}
