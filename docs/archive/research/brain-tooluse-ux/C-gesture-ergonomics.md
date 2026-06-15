# C — Interaction & Gesture Ergonomics (Full Brain "Worked" tool-use reveal)

Lens: *Is swipe-to-flip the right interaction for revealing an assistant
answer's tool steps, given it competes with the lateral panel swipes and
vertical scroll, and given JBrain2's one-thumb / phone-first / accessibility
constraints?* This report ignores visual styling and information architecture
of the "Worked" content itself; it is strictly about **what you do with your
thumb and what happens when you do it.**

Files grounding this analysis:
`frontend/src/agent/FullBrainSurface.tsx`,
`frontend/src/styles.css` (`.fb-flip*`, `.fb-status`, `.panel`),
`docs/mocks/assistant-flip-tooluse.html`, `docs/DESIGN.md`.

---

## 1. Lens framing — what this interaction has to do

The job is small: an assistant answer that used tools should let the owner
**peek at the agent's work** (steps, sources, a staged proposal) and get back.
This is a textbook **progressive-disclosure** / row-detail problem — secondary
detail hidden behind the primary content, revealed on demand. NN/g's own
surface-paradigm advice and JBrain2's DESIGN.md both already have a name for
this exact job: *"Row-level detail that doesn't warrant navigation → inline
expansion within the list."* The current implementation instead reaches for a
**3D flip card driven by an invisible horizontal swipe** — a more elaborate
interaction than the job calls for, and one that collides with the surface's
two other horizontal meanings.

The binding constraints it must satisfy:

- **Phone-first, one-thumb** (DESIGN.md Principle 1): primary actions in the
  bottom half, touch targets ≥ 44px.
- **Honest, discoverable status** (Principle 5; "gestures proved unreliable on
  real devices and are an enhancement only" — the launcher decision).
- **Reduced-motion support** (Motion section: "Honor `prefers-reduced-motion:
  reduce` by disabling all non-essential animation").
- **Accessibility** section: visible focus rings, full keyboard operability,
  status never conveyed by motion/color alone.

The current flip violates or strains every one of these. Details below.

---

## 2. The gesture-conflict analysis of the current code

There are **three different meanings for "drag your thumb" on the Full Brain
surface, two of them horizontal**, and which one fires depends entirely on the
pixel your thumb happens to land on.

### 2.1 Three overlapping gesture domains

From `FullBrainSurface.tsx`:

1. **Surface-level horizontal swipe → lateral panels.** `fb-shell` has
   `onTouchStart/Move/End`. Axis locks horizontal after 10px
   (`Math.abs(t.clientX - d.x) > 10`); commit at `OPEN_PX = 56`. Swipe **right**
   opens Sessions, swipe **left** opens Proposals (lines 29, 69, 78-85).
2. **Card-level horizontal swipe → flip.** A flip card *opts out* of the
   surface handler via `target.closest("…, .fb-flip")` (line 57), then the
   `FlipBubble` runs its **own** pointer handlers. Axis claimed when
   `|dx| ≥ 8` **and** `|dx| > |dy|` (line 340); commit at `FLIP_PX = 44`
   (line 275). A left swipe shows "Worked", a right swipe returns to the answer.
3. **Vertical scroll** of the transcript (`fb-chat`), the dominant and most
   frequent gesture in a chat surface.

So a single horizontal thumb-drag means **"open Proposals"** if it starts on
empty bubble margin or a user bubble, but means **"flip this card to Worked"**
if it starts a few pixels over, on an AI bubble that happens to have tools.
This is precisely the anti-pattern NN/g calls **swipe ambiguity**: *"don't
employ the same swipe gesture to mean different things on different areas of
the same screen … it becomes harder for people to learn and remember them."*
Apple HIG is blunter: *"avoid creating a unique gesture to perform a standard
action"* and define custom gestures *"only when necessary."* A left-swipe here
is doing double duty as both a custom card action and a drawer-open.

### 2.2 The collision is invisible and unlabeled

There is no edge chrome (DESIGN.md forbids it on the main screen) and no
persistent label distinguishing the two horizontal behaviors. The only hints
are (a) the tiny `fb-cue` corner button — 10px text, `bottom: 8px; right: 11px`,
in `--text-3` (the *muted/disabled* token), well under the 44px target — and
(b) in the mock, a one-line "swipe to flip to the work" hintbar that does not
exist in the React component. A flip gesture is, in NN/g's words, *"invisible …
out of sight is out of mind."* The owner's "I'm really not digging how the
interface works" is the predictable result: the most important reveal on the
surface has no durable affordance, and the affordance it does have looks like
disabled text.

### 2.3 The two thresholds make mode-errors likely

`OPEN_PX` is 56; `FLIP_PX` is 44; the surface axis-locks at 10px of travel, the
card at 8px. These are close enough that **on a tools-bearing AI bubble the
thumb is in flip-land, and a few pixels of vertical offset onto the bubble's
margin puts it in panel-land** — with no visible boundary. A user who means to
open Proposals but starts the drag on the last answer bubble flips that bubble
instead; a user who means to flip but grazes the margin opens a full-screen
Proposals panel. Recovery from the wrong one (a full-screen drawer vs. a
rotated card) is jarring and asymmetric. The 0.55 drag-tracking multiplier
(line 351) also means the card under-tracks the finger, so the gesture doesn't
feel "stuck to the thumb" the way the lateral panels (which track 1:1) do —
two horizontal swipes that *feel different* adds to the mode confusion.

### 2.4 One-thumb reachability

The transcript is read bottom-up (newest answer just above the omnibox). To
flip a card the thumb must (a) land precisely on that specific bubble and (b)
execute a clean horizontal stroke that out-votes vertical scroll. Mid- and
upper-transcript answers sit in the screen's hard-to-reach zone — Hoober's
field data puts the comfortable one-thumb area at the bottom-center third, with
touch accuracy dropping to ~61% and +0.7-1.2s per interaction in the upper
third. A horizontal swipe that must *win an axis fight against scroll* in that
zone is the worst case: it's both far and fiddly.

### 2.5 Motion, 3D, and the reduced-motion gap (the sharpest defect)

`.fb-flip` uses `transform-style: preserve-3d` with `perspective: 1300px` and a
0.42s `rotateY(180deg)` (styles.css 3318-3346; `FullBrainSurface.tsx` 320-322).
A large surface rotating in 3D is exactly the class of motion the vestibular
literature flags: *"large elements flipping in 3D space can induce dizziness or
nausea … the vestibular system detects a dissonance between expectation and
reality."* Rotational animation is a named trigger for the ~70M people with
vestibular disorders.

**Critically, the flip has no `prefers-reduced-motion` override.** I checked
every `@media (prefers-reduced-motion: reduce)` block in `styles.css`: there
are rules for `.note-slide`, `.launcher`, `.subscreen`, `.sheet`,
`.review-card`, `.edit-layer`, **`.fb-status`**, and **`.panel`** — but **none
for `.fb-flip` / `.fb-flipwrap`**. So a reduced-motion user still gets the full
0.42s 3D rotateY. This directly violates DESIGN.md's Motion rule and is the one
concrete, fix-it-today bug regardless of which direction you go.

### 2.6 Screen-reader / keyboard story is fragile

The component leans on `inert` toggling between faces (`front.current.inert =
open` etc., lines 326-327) plus `backface-visibility: hidden`. This is a clever
"only one face in the a11y tree at a time" trick, but it's **state the screen
reader can't perceive as a relationship**: there is no `aria-expanded`, no
`aria-controls`, no announcement that flipping happened — the user just finds
that the content under their finger silently swapped. The keyboard path is the
`fb-cue` buttons (the code comment says "the keyboard path is the cue
buttons"), but those buttons are styled as 10px muted text, not as a control,
and there's no programmatic expanded/collapsed semantics tying front to back.
Compared to a native `<details>`/`aria-expanded` disclosure, this is a
hand-rolled substitute that AT users will experience as content teleporting.

**Verdict on the current model:** swipe-to-flip is the wrong primary
interaction. It is an invisible custom gesture, performing what should be a
standard disclosure, that shares an axis with two other meanings, sits in the
hard-to-reach zone, animates in a vestibular-risky way with no reduced-motion
escape, and exposes a brittle a11y story. The lateral-panel swipes are
defensible (they're a labeled-elsewhere *enhancement* with full-screen tappable
homes in the launcher, per DESIGN.md); the flip is not — it's the *primary* way
to see the agent's work, riding on the app's least reliable input.

---

## 3. Best practices & pitfalls (sources)

- **Swipe is low-discoverability; show a cue or it goes unused.** NN/g:
  *"Swiping is still less discoverable … visible cues should be included or
  [users] might never do so and miss offerings."* Don't reuse one swipe for
  different meanings on the same screen.
  [Contextual swipe](https://www.nngroup.com/articles/contextual-swipe/),
  [iPhone X: Rise of Gestures](https://www.nngroup.com/articles/iphone-x/).
- **Hidden gestures raise the learning curve.** Removing UI chrome in favor of
  gestures trades clutter for memorability cost.
  [In-App Gestures (Smashing)](https://www.smashingmagazine.com/2016/10/in-app-gestures-and-mobile-app-user-experience/).
- **Apple HIG:** use standard gestures; *avoid a unique gesture for a standard
  action*; define custom gestures only when necessary because they're hard to
  discover and remember.
  [HIG Gestures](https://developer.apple.com/design/human-interface-guidelines/gestures).
- **Thumb zone:** comfortable one-thumb area is the bottom-center third; the
  upper third is slow and error-prone (Hoober, 1,333 observations; ~75% of
  touches are thumb).
  [Thumb Zone (Smashing)](https://www.smashingmagazine.com/2016/09/the-thumb-zone-designing-for-mobile-users/),
  [Scott Hurff](https://www.scotthurff.com/posts/how-to-design-for-thumbs-in-the-era-of-huge-screens/).
- **Bottom sheets are the one-thumb-friendly disclosure surface;** progressive
  peek → half → full, swipe-down/scrim dismiss; don't use for content that
  deserves a full screen.
  [Material 3 bottom sheets](https://m3.material.io/components/bottom-sheets/guidelines),
  [Material 2](https://m2.material.io/components/sheets-bottom).
- **Accordion / inline expansion** is the canonical progressive-disclosure
  pattern for row detail; caveat: don't nest very long lists or users get lost.
  [NN/g Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/),
  [ui-patterns](https://ui-patterns.com/patterns/ProgressiveDisclosure).
- **3D rotation = vestibular trigger; always gate behind
  `prefers-reduced-motion`.** WCAG 2.3.3 (Animation from Interactions) requires
  interaction-triggered motion be disableable.
  [Vestibular disabilities (UX Planet)](https://uxplanet.org/web-accessibility-for-vestibular-disabilities-919a78d7b0b1),
  [WebKit: Responsive Design for Motion](https://webkit.org/blog/7551/responsive-design-for-motion/),
  [MDN prefers-reduced-motion](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/prefers-reduced-motion),
  [WCAG 2.3.3 plain English](https://aaardvarkaccessibility.com/wcag-plain-english/2-3-3-animation-from-interactions/).
- **Disclosure a11y:** a native `<details>` / `aria-expanded` + `aria-controls`
  relationship is the standard, AT-legible way to expose "this control reveals
  that content"; `inert`/`aria-hidden` only manage tree membership, they don't
  convey the relationship.
  [MDN backface-visibility](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/backface-visibility),
  [Orange a11y: accessible hiding](https://a11y-guidelines.orange.com/en/articles/accessible-hiding/).

---

## 4. Three concrete alternative interaction models

All three keep the lateral Sessions/Proposals swipes intact (they're a settled,
labeled-elsewhere enhancement) and **remove horizontal swipe as a per-bubble
meaning entirely**, killing the axis collision.

### Alternative A — In-place inline disclosure ("Worked" expander) — RECOMMENDED

- **Affordance:** the existing `fb-cue` row, promoted from 10px muted text to a
  real **44px-tall, full-bubble-width footer button** at the bottom of the
  answer: a gear glyph + `Worked · 2 steps · 4 sources` + a chevron that points
  **down** when collapsed, **up** when open. Visible on every tools-bearing
  answer; it *is* the disclosure control. No gesture required, nothing hidden.
- **Gesture:** **tap** (or keyboard Enter/Space) on that footer. No swipe. It
  sits at the bubble's bottom edge — and the newest answer's footer lands in
  the bottom-center thumb zone.
- **Animation:** the Worked block expands **below the answer in the same
  bubble** — answer stays put, steps/sources push down with a 120-180ms
  ease-out height/opacity reveal (matches DESIGN.md Motion's 120-180ms band).
  No 3D, no rotation, no width pinning, no max-height measurement hack.
- **A11y:** native `<button aria-expanded aria-controls="worked-N">` wrapping a
  region; or a `<details>/<summary>`. Screen reader announces
  "Worked, 2 steps, collapsed/expanded, button." Keyboard-operable for free;
  focus ring via `:focus-visible`. No `inert`/backface trickery.
- **Reduced motion:** `@media (prefers-reduced-motion: reduce)` → the region
  appears/disappears instantly (no height transition). Content identical;
  trivially correct because there's no transform to suppress.
- **Pros:** matches DESIGN.md's own "inline expansion within the list" rule for
  row detail; zero gesture conflict; most discoverable; cheapest animation;
  best a11y. **Cons:** a very long tool run pushes the transcript down and the
  expanded block can run tall mid-stream — mitigate by clamping the sources
  list with a "show all N" grow-in-place (the pattern DESIGN.md already uses for
  OCR text) and auto-scrolling the opened block into view.

### Alternative B — Bottom sheet "Worked" detail

- **Affordance:** same promoted 44px `Worked · N steps · M sources` footer
  button on the answer bubble. Tapping does **not** expand inline — it raises a
  bottom sheet.
- **Gesture:** **tap** to open; **swipe-down or scrim-tap** to dismiss (the
  shared `<Sheet>` already owns this per DESIGN.md's modal system). No
  per-bubble swipe.
- **Animation:** sheet slides up 150ms ease-out (reuses the existing `.sheet`
  animation and its existing reduced-motion rule — `.sheet` already has a
  `prefers-reduced-motion` override, so reduced motion is *already solved*). The
  answer stays fully visible behind the scrim.
- **A11y:** the shared `<Sheet>` already provides focus trap, body-scroll lock,
  Escape/back dismiss, safe-area padding. Trigger button gets
  `aria-haspopup="dialog"`. This is the most a11y-complete path because it rides
  proven infrastructure.
- **Reduced motion:** inherited from `.sheet` — already correct.
- **Pros:** maximally one-thumb friendly (content arrives at the bottom of the
  screen regardless of where the bubble was — solves §2.4 reachability);
  reuses settled modal infra; never fights scroll or lateral swipes; tall tool
  runs scroll *inside* the sheet instead of bloating the transcript.
  **Cons:** mild paradigm tension — DESIGN.md reserves sheets for "contextual
  quick forms & actions," and tool steps are read-only detail; it's a heavier
  surface than the job strictly needs, and it momentarily covers the
  conversation. Still well within sheet norms (contextual, dismissible,
  one primary surface).

### Alternative C — Keep the flip, but make it a *button-driven* 2D flip with a real affordance

- **Affordance:** replace the invisible swipe with the same promoted 44px
  footer button ("Worked" / "Answer" toggling its label). The swipe is
  **removed**; the button is the only trigger.
- **Gesture:** **tap / Enter / Space** only. (Optionally keep swipe as a
  *disabled-by-default, opt-in* power-user gesture, but never the sole path.)
- **Animation:** keep an in-place turn but **drop true 3D**: a fast 2D
  cross-fade + slight scale (or a 2D `rotateY` capped at a shallow angle) under
  ~180ms. This preserves the "two faces of one bubble" mental model the owner
  designed without the vestibular 3D rotation.
- **A11y:** the toggle button carries `aria-expanded`/`aria-pressed`; keep the
  `inert` face-swap but *add* the announced relationship so AT users hear the
  state change rather than discovering swapped content.
- **Reduced motion:** add the missing
  `@media (prefers-reduced-motion: reduce){ .fb-flip{ transition:none } }` and
  snap between faces. (This media-query fix should ship **regardless** of the
  chosen alternative — it's the §2.5 bug.)
- **Pros:** smallest change; honors the owner's existing visual concept and the
  built component; fixes the worst sins (invisible trigger, 3D motion, missing
  reduced-motion). **Cons:** still pins the bubble to a fixed width and animates
  height between faces (the measurement/`maxH` dance); still hides the answer
  behind the work while open (you can't see both); the "same box, two faces"
  model is inherently more confusing on a phone than expand-below; doesn't
  improve reachability for upper-transcript bubbles.

---

## 5. Recommendation

**Adopt Alternative A (in-place inline disclosure) as the primary model, and
fix the `prefers-reduced-motion` gap immediately regardless.**

Rationale:

1. It is the interaction DESIGN.md *already prescribes* for this exact job
   ("Row-level detail that doesn't warrant navigation → inline expansion within
   the list"). Choosing it isn't a new paradigm — it's applying a settled one.
2. It eliminates the §2.1-2.3 axis collision outright: with no per-bubble
   horizontal meaning, a horizontal swipe on the surface has exactly one
   meaning again (lateral panels), and vertical drags are unambiguously scroll.
3. It is the most discoverable (a persistent, labeled 44px control beats an
   invisible swipe — NN/g, HIG), the most one-thumb-honest (tap, not an
   axis-fighting stroke), the cheapest to animate (a height/opacity ease, no
   3D), and the most accessible (native `aria-expanded` disclosure, no
   `inert`/backface workaround).

**Promote Alternative B (bottom sheet) to the fallback** if, in a mockup round,
the expanded Worked block proves too tall/disruptive inline for heavy tool runs
— the sheet's bottom-anchored, internally-scrolling, infra-reused nature solves
exactly that, and its reduced-motion + a11y are already handled by the shared
`<Sheet>`. **Alternative C** is the *minimum* acceptable patch if the owner
wants to preserve the flip concept: kill the swipe trigger, drop true 3D, add
the reduced-motion rule and `aria-expanded`. But A is the right answer: the flip
is a beautiful solution to a problem this surface doesn't have.

One thing that ships no matter what: add
`@media (prefers-reduced-motion: reduce){ .fb-shell .fb-flip{ transition:none } }`
(and snap the transform) today — it's a clear DESIGN.md Motion violation and a
WCAG 2.3.3 gap in the current code.
