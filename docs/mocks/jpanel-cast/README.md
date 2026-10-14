# jpanel Cast — notices on the house's Chromecast TVs

> **Status:** Proposed, awaiting the owner's review · **Last verified:** 2026-10-14

Open `index.html` in a browser. It is one standalone page: the PWA side (launcher tile, TVs,
one TV's settings, Send, the result sheet, Sent) and the 16:9 frames a TV shows.

The owner, 2026-10-14: *"I don't want an app. I want the ability to just cast. We'd check to
make sure the TV wasn't in use … I'd be okay to also cast to a TV that is off if it turns it
on. But it would always have to be idle … I'd also want this to show up as a launcher icon."*

## What the mock commits to

- **No app on the TVs.** The box renders the notice to one 1920×1080 frame (a short clip when
  it carries sound) and casts it to Google's Default Media Receiver. That avoids a custom
  receiver, its Cast developer registration and an HTTPS origin the TVs must reach.
- **The idle gate, checked at the instant of casting.** Cast only over the ambient screen,
  screensaver or an idle home screen. A TV in standby is woken, shown the notice, and put back.
  Anything open, whether playing or paused, is skipped and never covered. Quiet hours hold a
  notice rather than drop it.
- **The composer is the panel composer** (title, text, picture, sound, Unsend, history) being
  built for the panel notices (`JPANEL_C6_PLAN.md` §2 "The owner's composer"). Reply choices are
  left out, because a cast cannot be answered. One send fans out to several TVs and reports
  per TV.
- **Its own launcher tile**, "Cast", beside jpanel. Its badge counts the TVs that would take a
  notice now.

## Open before a plan

- **Does an app opened with the remote report to Cast?** On Android TVs with Chromecast
  built in, receiver status reliably covers cast sessions. Whether Netflix opened from the home
  screen shows up is unverified. The first wave is a probe that lists what each TV reports in
  each state. A set that hides local apps is marked *can't tell when busy* and only takes
  notices while off.
- Discovery is mDNS, so the cast sender runs on the host network (like the Minecraft sidecar),
  not in the api container.
