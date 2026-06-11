# Entity-type icons — design exploration

Open [`search-entity-icons.html`](./search-entity-icons.html) in a browser.
Two toggles at the top: **Theme** (dark/light) and **Iconography** (three sets).
The phone frame renders the Search screen exactly as `SearchScreen.tsx` does —
top bar, search bar, domain filter chips, result cards — now surfacing
**entity results with type icons** above the passage results. The right column
is a legend of every type icon in the active set.

Nothing here is wired into the app yet; this is the artifact for picking a
direction. Once a set is chosen we lift its glyphs into
`frontend/src/components/icons.tsx` and add an `EntityTypeIcon` that maps an
`Entity.kind` string to a glyph + the domain tint.

## Entity types covered

`Entity.kind` is deliberately **free text**, schema.org-guided (see
`backend/.../prompts/note_extract.prompt` and migration `0006`), not an enum.
The icons cover the high-frequency converged types; unknown kinds fall back to
**Thing**.

| Kind | Glyph | Notes |
|------|-------|-------|
| `Person` | head + shoulders | any individual |
| `Organization` | building | company, clinic, institution |
| `Place` | map pin | geographic location |
| `Event` | calendar | named event / occurrence |
| `Product` | box / bag / cube | physical product, vehicle, tech |
| `Animal` | paw | non-human creature (pets included) |
| `CreativeWork` | book | book, article, film |
| `MedicalCondition` | heart-pulse / ECG | diagnosis (health domain) |
| `MedicalProcedure` | stethoscope / clipboard | procedure (health domain) |
| `Drug` | capsule | medication (health domain) |
| `Thing` | tag / grid / hexagon | fallback for unresolved kinds |

Colour is **not** carried by the glyph — it stays the domain accent
(`general`=green, `health`=rose, `finance`=violet, `location`=steel) per
`DOMAIN_COLOR` in `notes/modes.ts`, so the icon set never competes with the
domain signal already established across the app.

## The three sets

1. **Tinted disc** — outline glyph (1.5px Lucide stroke) in a domain-tinted
   circle. Closest to today's chrome; softest.
2. **Duotone squircle** — closed-shape glyph with a filled body in a rounded
   square. Reads best at small sizes; slightly more "app".
3. **Hairline mono** — single-weight monochrome glyph in a 1px-border circle.
   Most restrained; the domain dot alone carries colour.

All three honour the binding `docs/DESIGN.md` iconography rules: 24×24 viewBox,
`currentColor`, outline-first, no emoji.
