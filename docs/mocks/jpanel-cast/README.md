# jpanel Cast — notices on the house's Chromecast TVs

> **Status:** **Approved by the owner** (2026-10-14); the spike has run on the real TVs and the build is planned in `../../plans/JPANEL_CAST_PLAN.md` · **Last verified:** 2026-10-15

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

## Answered by the spike

The questions this mock left open were answered on the house's TVs on 2026-10-15
(`../../plans/JPANEL_CAST_PLAN.md` §K0). An app opened with the remote does report to Cast, and a
cast wakes a Philips from off once its power setting keeps Cast available in standby. A woken TV
stays on, though: Cast has no power-off and neither Philips went back to standby by itself, so
this mock's "back to sleep after" can't be built over Cast.
