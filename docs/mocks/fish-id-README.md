# Fish-identification chat view — mock gate (Fish-ID Wave F3, GUI gate #1)

Three interactive directions for the **in-chat `fish_identification` tool-view** —
the card jerv shows after an `identify_fish` tool call (`docs/PROCESS.md` GUI gate;
`docs/FISH_ID_PLAN.md` "Wave F3"). **Pick one**; the chosen mock becomes the binding
spec for the `FishIdentification` component and the other two are retained here as the
record (mirrors the `genimage-README` / `wiki-talk-README` convention).

All three honor the tool-view contract (`docs/DESIGN.md` "Agent tool views"): the
model authors **no markup and no URLs** — it only fills data-only slots
(`{thumb_id, thumb_kind ('attachment'|'image'), candidates:[{species, common,
score}], model, species_count}`). The component builds the photo `<img src>` from
the id (`/api/attachments/${thumb_id}` or the generated-image route) and maps the
confidence to a token **enum** (good/mid/low), never a raw color. Mocks are
**tokens-only** (the app token set in `frontend/src/styles/tokens.css`),
phone-framed, dark-first with the global light/dark toggle, outline icons, and the
card frame matches the live `.tool-view` (`--surface-2` / `--border` / 12px radius).
They render offline: each photo is an inline SVG `data:` URI standing in for the
by-id production source (noted in each file's header comment).

| File | Direction | Shape | Best when |
|---|---|---|---|
| `fish-id-a-hero-verdict.html` | **Hero verdict.** The photo fills the card; the top species reads over a bottom scrim with a confidence pill; runners-up are one muted line. | The picture *is* the message. | You want the calmest, most photo-feed-like card and the top guess is usually enough. |
| `fish-id-b-ranked-list.html` | **Ranked list.** A thumbnail beside the model's full top-k, each with a confidence bar; the top match highlighted. | Uncertainty is first-class. | Close calls are common (overlapping species) and you always want to see the alternatives. |
| `fish-id-c-verdict-expand.html` | **Verdict + expand.** A compact verdict row by default (thumb + species + confidence pill + chevron); tap to reveal the full ranked top-k. | Calm default, honesty one tap away. | You want A's low chrome but B's ranking available without scrolling back. |

## Trade-offs

- **A** is the calmest and most native to a chat scroll, and the least code to ship
  — but it buries the alternatives in one line, so a genuinely close call (two
  species within a few points) reads as more certain than it is.
- **B** is the most honest about model uncertainty — a 61%/28% split is impossible
  to miss — at the cost of always spending vertical space on the full list, even when
  the top guess is a confident 96%.
- **C** keeps A's compact default while putting B's full ranking one tap away (the
  genimage-b disclosure pattern, applied to candidates); the cost is a second
  interaction surface and a touch more component state, and the default row hides
  the alternatives until tapped.

## Decision

**Chosen: A — hero verdict** (`fish-id-a-hero-verdict.html`). It is the **binding
spec** for the `FishIdentification` component and its `.tv-fish-*` classes in
`frontend/src/agent/views/registry.tsx`: the owner's photo fills a sized hero frame
(no layout shift), the top species reads over a bottom scrim with a confidence
**pill** whose tone is an enum (good ≥ ~70% / mid otherwise, never a raw color), and
the runners-up are one muted "also considered" line below. A won as the calmest,
most photo-feed-like card — the picture is the message, and the top guess is usually
what's wanted; the alternatives stay visible (one line) so a close call isn't hidden
entirely. **B** (ranked list) and **C** (verdict + expand) are retained in this
directory as the record. The selection + rationale also land in `docs/DESIGN.md` (the
new `fish_identification` tool-view entry, per the GUI gate).
