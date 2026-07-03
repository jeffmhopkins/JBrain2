# Phone management mockups (Location → Phones)

Interactive review-round mockups for the Location screen's device tab, reframed
around **phones only** (the JBrain360 app), per the owner request. Open
`index.html`, or each file directly, in a phone-width window.

## Problem (from the current screen)

The shipped Devices tab (`frontend/src/screens/LocationScreen.tsx`) has two gaps
the owner called out, plus dead weight:

1. **"Only phones now."** The `+ Add device (OwnTracks)` path provisions a raw
   key for a manual OwnTracks setup. With the JBrain360 app it's dead weight —
   dropped in all three variants.
2. **No way to roll a token once a phone is paired.** A pairing code is one-time
   and consumed on redemption; afterwards there's no affordance to mint a fresh
   one for that same phone (new phone, reinstall, lost-device suspicion).
3. **"Rotate key" is broken for a paired phone.** The current `Rotate key`
   button reveals a raw key string — but a JBrain360 phone never pastes a key;
   it can only receive credentials by **redeeming a pairing code**. So rotating
   the key out-of-band leaves the phone unable to learn the new key.

### The unifying fix

For a paired phone, "roll the token" and "rotate the key" are the **same
action: re-pair** — mint a fresh one-time code (QR), the phone redeems it, and
the device key rotates server-side as part of redemption (the existing
`redeem_pairing_code` already mints a key on redeem). The current key keeps
working until the phone redeems, so there's no lockout window. Re-pair also
**restores a revoked phone** (history stays attached). This maps onto the
existing endpoints (`POST /api/pairing/codes` + redeem); a true "re-pair an
existing device" needs the mint to target an existing subject rather than always
creating a new one — a small backend addition flagged for the build phase.

Additional management the variants add: **rename** (label-only edit) and
**delete** (remove the device + its history, distinct from revoke which only
suspends the key). Phones are organised by **family member**.

## The three variants

| | Pattern | Best at |
|---|---|---|
| **A — family roster** (`a-family-roster.html`) | Phones grouped under each person; tap a phone → management **bottom sheet** (status + key state, re-pair, rename, danger zone). Revoked fold under a per-person disclosure. | "whose phone is whose" — family-first; full hub per phone |
| **B — swipe rail** (`b-swipe-rail.html`) | Active / Revoked filter; **swipe-left rail** (re-pair · rename · revoke · delete) reusing the settled home-note / chats paradigm. Inline rename, tap-again confirms, undo toast. | density + muscle-memory reuse |
| **C — inline accordion** (`c-inline-accordion.html`) | Each phone **unfolds in place**; panel led by a **credential-lifecycle strip** (active / aging / waiting / disabled), action grid, and the roll-code QR opens inside the card. | making the key's state legible; one-tap-deep |

All three: tokens-only, location teal accent, dark/light toggle (sun icon),
phone-first, bottom-sheet for the new-pair flow, and a fake QR that regenerates
on each "new code". They share one device fixture set (live / stale / pending /
revoked / never-reported) so states compare across variants.

## Decision

**Chosen: B — swipe rail.** It gives the most aggressive vertical density and
reuses the settled home-note / chats swipe paradigm. The pattern + reasoning are
recorded in `docs/reference/DESIGN.md` ("Phones tab — paired-phone management"), and B is
implemented in `LocationScreen.tsx` (re-pair / rename / revoke / delete + the
Active/Revoked filter) over the re-pair-aware pairing backend (migration 0077).
A and C are retained above as the record. Family-member grouping is deferred (it
needs the device→Person graph link surfaced in the device list).
