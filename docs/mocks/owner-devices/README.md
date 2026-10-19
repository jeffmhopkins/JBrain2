# GUI gate — owner phones in Settings (owner tunnel wave A5)

> **Status:** Plan · **Last verified:** 2026-10-19
>
> **Decided 2026-10-19: B — a Devices page** is the binding spec (`b-devices-page.html`);
> A and C are kept as the rivals. Chosen because a phone waiting for the owner is a security
> step, and a banner at the top of its own page is the hardest place to miss it; the link
> address and the home network get room of their own. Behaviour source:
> `docs/plans/OWNER_TUNNEL_PLAN.md` §3.5 and wave A5.

Three interactive mocks of how the owner adds a phone and confirms it. Open each `.html` in
a browser; **Phone scans the box's QR** above the phone plays the enrollment, and `#waiting`
in the URL opens straight on a phone waiting for the owner.

| Mock | Shape |
|---|---|
| `a-access-card.html` | One card in Settings → Access; the waiting phone's words inline; the link address and home network under "More". |
| `b-devices-page.html` | A Devices tile and page: a banner for a waiting phone, an Add a phone card with a countdown, your phones, then a card each for the link address and the home network. |
| `c-guided-sheet.html` | A plain list; Add a phone opens a three-step sheet that ends with comparing the words. |

Shared behaviour: arming shows the code on the box's screen for two minutes; a new phone is
pending until the owner taps Confirm (Not mine revokes it); a phone works only once confirmed.
