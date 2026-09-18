# Room endpoint — GUI rounds

Plan: `../../proposed/ROOM_ENDPOINT_PLAN.md` · Design system: `../../reference/DESIGN.md`

| Round | Mock | Question | Status |
|---|---|---|---|
| 1 | `device.html` | What is the panel *for*? Four shapes. | **Answered by research, not by review** — see below |
| 2 | `pet-face.html` | How does a robot pet read to a **four-year-old** on 29 mm? | **Open** — one question left, three face variants |

## The measurement that governs both rounds

> **1.8″ diagonal at 368×448 = 29.0 × 35.3 mm, 322 ppi.**

A smartwatch panel, about a postage stamp. Tick **“True physical size”** in either mock before
judging anything; at 1:1 desktop pixels every design flatters itself.

For scale: **Vector's entire face — the best-executed robot emotion display ever shipped — is
184 × 96 px on 23 × 12 mm.** We have ~9× the pixels in ~3.6× the area. Pixel count is not the
constraint. Timing, variety and motion are.

## Round 1 closed on evidence

Three of the four shapes are eliminated by measurement rather than taste:

| Shape | Verdict |
|---|---|
| **D Room** | **Dead.** A 20 mm child touch target (NN/g) is 69% × 57% of this panel. Two do not fit in either axis. Four tappable props is ergonomically impossible. |
| **C Face** | **Dead as drawn.** It leans on text; he is pre-literate, and icons don't rescue it — abstract symbols are learned from device experience he doesn't have. |
| **A Matrix** | **Dead for this child.** 16×16 has too few pixels for an expression, and its only other input was swipe — direction discrimination on a screen *narrower than his finger* is untested by anyone. Kept in `device.html` as the aesthetic record. |
| **B Porthole** | **Survives.** `pet-face.html` is its renovation. |

## Round 2 — three findings rebuilt the thing

**1. Emotion does not currently exist.** The shipped pet has all six emotions, but the Wall
renders each as a chest-badge colour plus the width of one horizontal mouth bar — `curious` and
`sleepy` are pixel-identical apart from hue, `happy` and `excited` differ by 0.06 of a bar, and
in **any animal form emotion is invisible entirely** (the creature renderer draws no badge and
no mouth). `PetOut.emotion` is never read at all. So "clear emotions" is not a simplification of
what exists — it is the net-new work, and this panel is the first surface to carry it.

**2. The whole screen is the only touch target, and long-press does not exist at four.**
Children aged 4-5 produce **ordinary taps lasting up to 4.2 seconds** (adults: 53 ms). Round 1
used a 550 ms long-press to mute the mic — for him, every tap would have muted the robot.
Multi-touch completes 54% of the time and is out.

**3. The wake word will miss him often.** Whisper on child speech is ~32% WER against ~3% for
adults, worse on small models; ~1% of output is hallucinated, triggered by silence and
half-speech — and **a hallucinated command the robot acts on is worse than a miss**, because it
reads as the toy being broken. No wake-word false-reject rate has ever been published for
3-5 year olds; that is the biggest open risk in the whole plan and is cheap to measure.

### The conflict, and the design that resolves it

Findings 2 and 3 collide: if tap = poke and hold = talk, a child whose ordinary tap runs to
4.2 s triggers "talk" by accident every time. No threshold fixes it — **his tap and his hold are
the same gesture.** So there is no gesture discrimination at all:

- **Touch** = *"I'm paying attention to you."* The pet flinches toward the finger in under
  100 ms and the mic opens for as long as he holds.
- **Release** = if he said something, do it. If he didn't, be silly.

One target, one gesture, no thresholds — and press-to-talk arrives free, because the touch
budget was already spent. The sub-0.5 s micro-reaction, *not* the latency of the real response,
is what drives perceived aliveness (η² = .407).

## How the face works (all borrowed, none invented)

- **~17 tweened floats**, after Cozmo/Vector's 43-float `ProceduralFace`: whole-face
  `{x, y, scaleX, scaleY, angle}` + per-eye `{scaleX, scaleY, upperLidY, upperLidAngle,
  lowerLidY, lowerLidBend}`. Ekman's set is reachable from lid **Y** and **angle** alone.
- **Asymmetry is the entire signal** for curious and silly. Free.
- **Tween by halving** — `cur = (cur+target)/2` per frame. ~90% in 66 ms, ease-out for nothing.
- **Never fully at rest** — a breathing sine always runs underneath. "When robots stop moving
  they look dead."
- **Blink 167 ms per half, and the eye widens as it closes.** Saccades 200 ms, 0-2 s apart.
- **Gags hold the bewildered face.** 4-5 year olds read a pratfall as funny when the character
  looks *bewildered*, and as not-funny when it looks pained or smug. The long hold after the
  fart **is** the joke; the fart is the setup. Tick "Slow motion" to see the structure.
- **Colour is not an emotion channel.** Vector deliberately made eye colour a user preference;
  Nabaztag carried state in LED *pattern*, not hue. Here colour is identity and play — which is
  exactly what the child uses it for.

## Anti-boredom is an engine, not a content pile

Loona ships 700-1000 expressions and reviewers still describe it being shelved. Three of
Vector's five layers are cheap and are **implemented for real** in the mock, with a live
read-out so you can watch them work:

- weighted-random selection within a per-action variant pool,
- per-variant cooldown (recency suppression),
- a **repetition penalty** — poke it ten times fast and the tenth lands at ×0.35.

Note the tension with age: preschoolers *love* repetition. So suppress recency **within** a gag,
never the gag itself — let him get the burp a hundred times, and make the burp different each time.

## Settled: small body (owner, 2026-09-13)

Chosen over *eyes-only* and *eyes + mouth*, both retained in the variant switcher as the record.
Recorded in `../../reference/DESIGN.md`.

The emotions never needed a body — lid geometry carries them. **The gags did.** So the limbs are
a real rig (two arms, two legs, per-action poses) inside the same transform as the head, and
`hide` finally means something: **the hands come up over the eyes.** That fixes a shipped dud —
today `hide` renders identically to `sit`, walking to a corner and squatting with nothing
occluding it — and peekaboo is squarely on-target for a four-year-old.

Three defects were found and fixed by measuring rather than looking, and each is a trap worth
remembering:

| Symptom | Cause | Test that caught it |
|---|---|---|
| “hide” played the **wave** animation | the intent mirror matched substrings, so `hide` matched the keyword `hi` — `intents.py:407` uses **word-boundary regex** for exactly this reason | a 14-case routing table |
| the figure clipped off the top | jump lift was applied *outside* the figure scale, and cat ears are the tallest silhouette | a pixel scan of the drawn extent across all 7 forms, idle and mid-jump |
| peekaboo read as “arms up beside the head” | the hand knob is 29 px and the widest eye is 43 — the arm reached the eye, the hand just couldn’t cover it | counting eye-white pixels with the hands up (must be zero) |

## Carried into the plan, not the mock

- **65 dB(A)** close-to-ear cap (ASTM F963 / EN 71-1) — he *will* hold a 29 mm toy to his ear.
- **One hour of bright light before bed suppressed melatonin ~88% in 4-year-olds, persisting
  50 minutes.** Quiet hours must cut luminance, not just volume, and end well before bedtime.
- **ICO Children's Code requires a recording indicator** — so "muted is a promise" is a
  compliance requirement, not only a design principle.
- **COPPA:** command audio deleted promptly is a narrow carve-out; keeping his voice to
  fine-tune the model needs verifiable parental consent — and fine-tuning roughly halves child
  ASR error, so this is a real fork.
