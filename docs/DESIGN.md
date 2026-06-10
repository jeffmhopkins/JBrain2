# JBrain2 — GUI Design System

Binding reference for all UI work. Derived from the owner-supplied JBrain v1
reference screens (dark composer, knowledge hub, calendar, medical entry).
Components use **tokens only** — no raw hex values outside the token sheet.

## Principles

1. **Phone-first, one-thumb.** Primary actions live in the bottom half of the
   screen. Touch targets ≥ 44px. Bottom nav is the spine.
2. **Minimal / utilitarian.** Near-monochrome surfaces; color is *information*
   (state, domain), never decoration. No gradients, no glass, no shadows
   heavier than a hairline border.
3. **Comfortable density.** Generous padding and type sizes; fewer things per
   screen, each easily hittable. Data-dense surfaces (logs, lab tables,
   location history) may use the compact variants noted below.
4. **Color codes the domain.** Accents are muted and contextual: an active
   medical surface tints rose, research tints amber, general/info tints
   steel blue. The accent tells you *where you are and what kind of data
   you're touching*.
5. **Honest status, always visible.** Connectivity, sync, and server state are
   surfaced persistently (status dot + banner), never hidden behind a tap.
   The app must feel trustworthy about what it is and isn't doing.

## Theming

Dual theme, dark-first. Implementation:

- All colors are CSS custom properties on `:root`, overridden by
  `[data-theme="light"]`. Components reference tokens only.
- Default follows `prefers-color-scheme`; a Settings option overrides it
  (`system | dark | light`), persisted locally and (later) as a user setting.
- The PWA `theme-color` meta updates with the active theme.

## Color tokens

### Neutrals (dark / light)

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#0E0F11` | `#F7F7F5` | App background |
| `--surface` | `#17181B` | `#FFFFFF` | Cards, tiles, composer |
| `--surface-2` | `#1E2024` | `#F0F0EE` | Nested surfaces, inputs, inactive segments |
| `--border` | `#26282C` | `#E2E2DF` | Hairline borders (1px) |
| `--text` | `#E6E7E9` | `#1A1B1E` | Primary text |
| `--text-2` | `#9A9DA3` | `#5C5F66` | Secondary text, descriptions |
| `--text-3` | `#5C5F66` | `#9A9DA3` | Muted: placeholders, disabled, out-of-month days |

### Accents (identical across themes, tuned for both)

Muted, desaturated pastels — never saturated/neon. Each has a `-tint`
(translucent background for active segments, badges, banners).

| Token | Value | Tint | Meaning |
|---|---|---|---|
| `--accent` | `#7FA7C9` | 14% alpha | General/brand: wordmark dot, links, active nav, focus ring, today-marker |
| `--amber` | `#C9A36A` | 14% alpha | Research/search mode, in-progress states |
| `--rose` | `#CF8A8F` | 14% alpha | Health domain surfaces, warnings, destructive |
| `--green` | `#8FBC9A` | 14% alpha | Finance domain, success/healthy |
| `--violet` | `#A493C9` | 14% alpha | Location domain, automation (triggers/actions) |

Semantic aliases: `--ok: var(--green)`, `--warn: var(--amber)`,
`--danger: var(--rose)`, `--info: var(--accent)`.

**Domain coding rule**: surfaces operating on a firewalled domain take that
domain's accent for their active segment, status dot, and section markers —
health=rose, finance=green, location=violet, general=accent. This is a
usability feature of the security model: you can *see* which firewall you're
inside.

## Typography

- System font stack: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- Scale: 12 (micro/labels) · 14 (secondary) · 16 (body, inputs) · 18 (card
  titles) · 22 (screen titles) · 28 (wordmark/hero). Line-height 1.4.
- Weights: 400 body, 500 titles/buttons, 700 wordmark only.
- Section headers (e.g. KNOWLEDGE, AUTHORING): 12px, uppercase, letter-spacing
  0.08em, `--text-3`.
- The wordmark is `JBrain` + a `--accent` period: **JBrain.**

## Spacing & shape

- 4px base unit. Common steps: 4 / 8 / 12 / 16 / 24 / 32.
- Screen gutter 16px; card padding 16px; grid gap 12px.
- Radii: 16px cards/composer/tiles, 12px inputs/segments, 999px pills/dots.
- Borders: 1px `--border` on every raised surface; no drop shadows in dark,
  optional `0 1px 2px rgba(0,0,0,.06)` in light.
- Touch targets ≥ 44×44px; compact-variant rows may reduce to 36px height but
  never shrink tap areas below 44px including padding.

## Core components

**Top bar** — wordmark (or back chevron + screen title) left; right cluster:
status dot, notifications bell, mute, quick-action bolt. Height 56px.

**Status banner** — full-width strip under the top bar for connectivity/sync
problems: `--rose` text on rose-tint background, e.g. *"Browser online, but
JBrain server unreachable — retrying…"*. Auto-dismisses on recovery. Never
use modals for connectivity.

**Status dot** — 8px circle: green=healthy, amber=degraded/retrying,
rose=error, `--text-3`=unknown. Appears in top bar and composer footer.

**Segmented control** — pill row on `--surface-2`; inactive segments
transparent with `--text-2` label + icon; active segment gets the
*context-appropriate* accent tint background with accent icon and `--text`
label (Entry=accent, Research=amber, Medical=rose, Financial=green).

**Card / tile** — `--surface`, 16px radius, hairline border. Hub tiles:
3-column grid, outline icon top, 16px/500 title, 12px `--text-2` description.

**Composer** — the signature surface: card with mode segments on top, large
16px placeholder body, footer row with status dot + context microcopy left
(*"Files to notes/medical/ · PDFs staged."*) and paperclip + send icons right.

**Buttons** — primary: accent-tint background, accent text (no solid fills);
secondary: `--surface-2` + border; destructive: rose-tint + rose text;
ghost: text-only. All 12px radius, 44px min height. Destructive actions get
an inline confirm (button morphs to "Tap again to confirm") — `window.confirm`
is a Phase-0 placeholder to be replaced.

**Inputs** — `--surface-2` fill, hairline border, 12px radius, 16px text;
focus = 2px `--accent` ring (`:focus-visible` only). Selects match.

**Lists** — full-bleed rows inside cards, 1px `--border` separators, 44px+
rows; leading icon optional, trailing chevron for navigation.

**Badges** — 12px text on the relevant tint, pill radius (e.g. `running`,
`healthy` in the Ops screen).

**Calendar** — Day/Week/Month/List segments; month grid with hairline cell
borders, out-of-month days in `--text-3`, today = accent ring around the day
number; selected-day panel below with `+ Add` (accent link) and `Open day →`.

**Empty states** — one `--text-2` sentence with the action inline: *"Nothing
scheduled — tap to add."* No illustrations.

**Toasts** — bottom-anchored above the nav, `--surface-2`, auto-dismiss 4s,
single action max.

## Motion

Fast and physical: 120–180ms ease-out for state changes; segment/theme
changes crossfade ≤150ms; no springs, no parallax. Honor
`prefers-reduced-motion: reduce` by disabling all non-essential animation.

## Iconography

One outline set (Lucide), 1.5px stroke, 20px in controls / 24px in tiles and
nav. No filled icons except the status dot. No emoji in UI chrome.

## Voice & microcopy

Terse, factual, lowercase-calm with em-dashes; say what the system is doing
and what it won't do: *"Ask anything about your notes — I only read; I won't
change anything."* Errors state the situation + the recovery: *"Server
unreachable — retrying…"*. Never blame the user; never exclamation marks.

## Accessibility

- Text contrast ≥ 4.5:1 against its surface in both themes (the muted accents
  are for chrome/tints; body text is always `--text`/`--text-2`).
- Visible focus rings on `:focus-visible`; full keyboard operability on
  desktop layouts.
- Status conveyed by dot color is always paired with text.
- Respect safe-area insets (`env(safe-area-inset-*)`) in top bar and bottom nav.

## Implementation rules

1. Tokens live in one file (`frontend/src/styles/tokens.css`); components
   never hard-code colors, radii, or font sizes.
2. New components follow this document; if a needed pattern is missing, extend
   this document in the same PR that introduces the component.
3. Screenshot-test significant surfaces in both themes once Playwright lands.
