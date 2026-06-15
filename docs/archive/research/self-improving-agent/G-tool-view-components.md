# Dossier G: Agent Tool-View Component Registry

**Investigation role:** Researcher G — the closed, first-party React component
registry that tools render into the Full Brain chat via a schema-validated,
data-only `view` payload (ASSISTANT.md "Tool result views"; DESIGN.md "Agent tool
views"; ASSISTANT_PLAN.md P4.2 / P4.5). Designs the starter set we build.
**Mandate constraint:** LEAN. A small, composable, first-party set on shadcn/ui +
Tailwind + one chart lib; mobile-first; adding a component is a deliberate
versioned change. Reject sprawl. Every component obeys the binding contract:
data-only payload → registered component → typed slots; **never** model-authored
HTML/markdown/URLs (I-9), **never** a direct write (I-1, Proposal primitive),
citation refs as pointers-not-copies, RLS-scoped data at the source.
**Date:** 2026-06-12
**Evidence labels:** `[web]` = sourced post-cutoff via search (URLs in §6);
`[training]` = model prior knowledge to Jan 2026.

---

## 1. Executive recommendation

Ship a **flat, named, schema-validated registry of ~7 components**, built on three
composable primitives, rendered from a fixed map (`frontend/src/agent/views/`).
The registry is the strictest point on the generative-UI spectrum (§2): the model
fills typed slots of **one** named component per view — it does **not** emit a
component tree, HTML, or code. That single design choice discharges I-1 and I-9 at
the architecture level rather than by runtime sanitization.

**The registry (MVP highlighted in bold):**

| Component | Purpose | Surface | Domain(s) | Mode | Tier |
|---|---|---|---|---|---|
| **`data_table`** | Generic columnar rows with typed cells + per-row citation | inline / sheet | all | read-only | **MVP** |
| **`stat_block`** | 1–N glanceable stat tiles (value · unit · delta · cited) | inline | all | read-only | **MVP** |
| **`citation_card`** | The hover/expand citation surface (fact/entity/note pointer) | inline / sheet | all | read-only | **MVP** |
| **`lab_plot`** | Lab series over time with reference-range band + latest value | inline / sheet | health | read-only | **MVP** |
| **`record_list`** | List + items, with staged add/remove/check actions | inline / sheet | all (esp. general) | interactive → `list_*` tool | **MVP** |
| **`appointment_card`** | One appointment: when/where/who + ICS subscribe + reschedule | inline / sheet | general / health | interactive → `manage_appointment` Proposal | **MVP** |
| **`confirm_panel`** | Render a staged **Proposal preview** for approve / reject | sheet / dialog | all | interactive → approve/reject Proposal node | **MVP** |
| `entity_card` | Entity summary (kind, current facts as edges, links into entity page) | inline / sheet | all | read-only | Standard |
| `timeline` | Event/measurement sequence (med changes, appts, supersessions) | inline / sheet | all | read-only | Standard |
| `wiki_preview` | Read-only excerpt of a wiki article + "open article →" | inline / sheet | all | read-only | Standard |
| `med_card` | Medication: name · dose · schedule · status, cited | inline / sheet | health | read-only | Standard |
| `txn_table` | Transactions/receipt lines (specialization of `data_table`) | inline / sheet | finance | read-only | Standard |
| `place_card` | A place by name/address/relative bearing — **no map tile** (§5) | inline | location | read-only | Later/Maybe |

**Why this MVP set (7):** it covers the four read shapes that recur across every
generative-UI system — **table, stat/metric, chart, detail card** (§2) — plus the
two interaction shapes JBrain2 actually needs in Phase 4: **a list you can edit**
(`record_list` → the `manage_list` tool that already exists) and **the universal
approve surface** (`confirm_panel` → the Proposal primitive). `lab_plot` is the one
genuinely domain-specific MVP component, justified because labs are the marquee
health read and a band-plus-point plot is not expressible as a table. `citation_card`
is MVP because *every other component depends on it* — it is the shared rendering of
the pointers-not-copies refs all views carry.

This maps cleanly onto P4.2's seed (`lab_plot`, `table`, `confirm`): `table` becomes
the more useful `data_table`, `confirm` becomes `confirm_panel`, `lab_plot` stays.
We add `stat_block`, `citation_card`, `record_list`, `appointment_card` to reach a
daily-usable Phase-4 surface.

---

## 2. The recurring taxonomy from the generative-UI field

Every shipping generative-UI system answers one question — *how does model output
become rendered UI without becoming an injection or exfiltration channel?* — and
they sit on a spectrum from "model emits code" (most powerful, least safe) to "model
fills slots of one named component" (least powerful, safest). JBrain2's contract is
the safe pole, by design.

**The spectrum, named:**

- **Code/markup emission (reject).** Claude **Artifacts** lets the model emit HTML/JS
  and runs it in a cross-origin sandboxed iframe under a strict CSP, with full-site
  process isolation, to contain it `[web]`. This is the *right* answer if you must
  run arbitrary model code — but it is exactly the "secure playground for LLM-generated
  code" JBrain2 refuses: it is an exfiltration surface managed by CSP rather than
  forbidden by construction, and ASSISTANT.md I-9 ("agent output cannot trigger
  external resource loads") plus "no code execution in the agent" rule it out. We do
  not render model markup at all; there is nothing to sandbox.

- **Component-tree emission (reject the generality, keep the allowlist).**
  **assistant-ui** is the closest production analogue to our contract: the agent emits
  a **JSON spec** — a tree of `{component, props, children}` nodes — and each name is
  resolved against a **consumer-provided allowlist**; an unknown name throws
  `GenerativeUIRenderError` with **no implicit fallback**; there is **no eval, no
  dynamic import** `[web]`. Its own security guidance is the load-bearing lesson:
  *"spec props are spread directly onto your allowlisted components, so treat every
  allowlisted component as receiving untrusted input — never forward props into
  `dangerouslySetInnerHTML`, validate/reject `href`/`src`, block `javascript:` URLs,
  and the safest components accept only primitive, display-oriented props"* `[web]`.
  We adopt the **allowlist + unknown-name-renders-nothing + primitives-only**
  discipline verbatim (it is already in DESIGN.md), but **reject the arbitrary tree**:
  a JBrain2 view names **one** component and fills **its** typed slots. A model that
  cannot nest components cannot smuggle a layout that leaks.

- **Server-validated structured outputs (adopt).** **OpenAI Apps SDK / widgets** has
  the server return `structuredContent` validated against a declared `outputSchema`,
  with UI metadata pointing at a template the client renders; the component reads
  `window.openai.toolOutput` and re-calls tools via `tools/call` `[web]`. Two ideas
  port: **(a)** the payload is validated against a schema *server-side before delivery*
  (P4.2: "a `view` failing its component schema is rejected, not rendered"), and **(b)**
  interactive widgets **dispatch tool calls, never mutate locally** — which is exactly
  our "interactive views never mutate directly; a button dispatches a tool call or
  stages a Proposal." We reject the Apps SDK's **iframe + HTML template + postMessage
  bridge** delivery (same I-9 problem as Artifacts, and it hosts third-party app code);
  our components are first-party React in the PWA bundle, fed JSON.

- **Tool-result → component binding (the baseline pattern).** **Vercel AI SDK**
  generative UI / `streamUI` binds a tool's typed result to a React component, with
  generator functions yielding a loading state then the final component `[web]`. This
  is the *mechanism* (P4.5's `tool_view` SSE event is our version), and the loading→final
  transition maps onto our streaming (`tool_call` → `tool_view`). **CopilotKit** frames
  the same as "render a component for an in-progress or completed action" and
  **Thesys/C1** returns *JSON describing a component tree* from an OpenAI-compatible
  endpoint, shipping a built-in set of **tables, charts, and forms** `[web]`. **Hashbrown**
  and **LangChain generative UI** are the same shape with an allowlist. Across all of
  them the recurring built-in vocabulary is small and identical.

**The recurring component vocabulary (what every system ships or examples).** Stripped
of framework branding, the set converges `[web]`/`[training]`:

1. **Table / data grid** — columnar records. (Thesys, C1, every example.)
2. **Stat / metric / KPI tile** — a number with label, unit, delta. (Dashboards everywhere.)
3. **Chart** — line/area/bar over a time or category axis. (Thesys "charts"; Artifacts' Chart.js.)
4. **Detail / entity card** — one record's fields in a titled card. (assistant-ui's `Card`.)
5. **List** — ordered items, often with row actions. (To-do/list demos are the canonical genUI example.)
6. **Form / input** — collect typed input. *(JBrain2 mostly refuses this — §4.)*
7. **Confirm / action panel** — a button or two that dispatch an action. (assistant-ui's `Button`; human-in-the-loop.)
8. **Map / location** — a place. *(Universally an external-tile component — the one JBrain2 cannot have, §5.)*

**How they stay safe, distilled:** (i) a **closed allowlist** keyed by name, unknown →
nothing; (ii) **no eval / no dynamic import**; (iii) **props are data, validated against
a schema**, and components accept **display primitives only** — never raw HTML, never
unsanitized `href`/`src`; (iv) the safest systems **validate server-side before the
payload reaches the client**. JBrain2 satisfies all four and adds two the field does
**not** uniformly have: **the data already passed an RLS firewall at the tool source**
(so a view physically cannot carry cross-domain data), and **no interactive control can
write** — it can only re-enter the tool loop or stage a Proposal.

---

## 3. Per-component specs

Schema sketches are TypeScript-ish; `FactRef`/`EntityRef`/`NoteRef` are the
pointer-not-copy citation types (a bare ID + a denormalized label for render; the
hover-card fetches live). Every component receives a top-level
`refs?: CitationRef[]` it may surface as a footer "sources" affordance, plus the
field-level refs called out below. All payloads validate against the component's
registered schema server-side (P4.2) or render nothing.

```ts
type FactRef    = { kind: "fact";   fact_id: string;   label: string };
type EntityRef  = { kind: "entity"; entity_id: string; label: string; domain: Domain };
type NoteRef    = { kind: "note";   note_id: string;   label: string };
type CitationRef = FactRef | EntityRef | NoteRef;
type Domain = "general" | "health" | "finance" | "location";
type Surface = "inline" | "sheet" | "dialog";
```

### MVP

**`data_table`** — generic columnar rows; the workhorse that absorbs most "show me
a list of things" requests and the base for `txn_table`.
```ts
{
  view: "data_table"; surface: Surface;
  caption?: string;
  columns: { key: string; label: string;
             align?: "start"|"end"; kind: "text"|"number"|"date"|"badge" }[];
  rows: { cells: (string|number)[]; ref?: CitationRef }[];   // per-row citation
  domain?: Domain;                                            // tints the surface
}
```
Read-only. Mobile rule: ≤4 columns rendered as a table; beyond that the component
falls back to stacked key/value cards (no horizontal scroll — DESIGN.md phone-first).
A row's `ref` drives a tap-to-cite hover-card.

**`stat_block`** — 1–N glanceable tiles for "what's my latest X." The
generative-UI "metric/KPI" archetype.
```ts
{
  view: "stat_block"; surface: "inline";
  stats: { label: string; value: string|number; unit?: string;
           delta?: { value: string; dir: "up"|"down"|"flat";
                     tone: "good"|"bad"|"neutral" };   // tone, not raw color
           ref?: FactRef }[];                           // each stat cites its fact
  domain?: Domain;
}
```
Read-only. `tone` (not a color) keeps the component the sole authority on the
token mapping; a measurement carries `ref` to the originating fact.

**`citation_card`** — the shared rendering of a pointer-not-copy reference; the
hover-card/expand target the other components defer to. Building it once is the
anti-bloat move (§4).
```ts
{
  view: "citation_card"; surface: Surface;
  ref: CitationRef;
  snippet?: string;        // the cited words, server-trimmed — never model-authored prose
  meta?: { domain: Domain; when?: string };
}
```
Read-only. Tapping "open" descends to the existing note sheet / entity page — it is
a *pointer surface*, not a copy of the wiki/graph. Reuses the entity-page/note-sheet
citation paradigm (DESIGN.md "Analysis tab + entity pages": "tapping a fact cites
back to the highlighted source words").

**`lab_plot`** — the marquee health read: a lab series over time with the reference
range as a shaded band and the latest value called out. Justified as its own component
because a band+point plot is not a table; this is the field's "chart with reference
range = shaded area" consensus `[web]`.
```ts
{
  view: "lab_plot"; surface: Surface; domain: "health";
  test: string; unit: string;
  points: { t: string; value: number; ref: FactRef }[];   // every point cites its lab fact
  ref_range?: { low?: number; high?: number; label?: string };  // shaded band
  latest_flag?: "in_range"|"low"|"high";                  // computed at source, tone via token
}
```
Read-only. Single series only (multi-test = small-multiples of `lab_plot`, not a
multi-line tangle — `[web]` favors small multiples for cross-series on mobile). Out
of range tints `--rose`; never the only encoding (pair with the flag text per
DESIGN.md meters rule).

**`record_list`** — a JBrain2 `list` + its items, with **staged** row actions. The
canonical interactive genUI demo (the to-do list), made safe.
```ts
{
  view: "record_list"; surface: Surface; domain?: Domain;
  list_id: string; title: string;
  items: { item_id: string; text: string; checked?: boolean; ref?: NoteRef }[];
  actions?: { add?: boolean; remove?: boolean; check?: boolean };  // which controls show
}
```
**Interactive → dispatches a tool call, never a write.** Add/remove/check buttons call
the existing `list_add` / `list_remove` / `list_check` tools (ASSISTANT.md tool set,
namespaced) through the agent loop under the session action policy; `manage_list` is
`mutate`-class, so under the default owner policy these **stage** rather than execute
directly (ASSISTANT.md "Writes … always staged"). The control's optimistic state shows
`pending` (amber) until the staged op is enacted — it never mutates local truth.

**`appointment_card`** — one appointment, the read counterpart to the ICS feed.
```ts
{
  view: "appointment_card"; surface: Surface; domain: "general"|"health";
  appt_id: string; title: string;
  when: string; ends?: string; where?: string;
  with_entity?: EntityRef;                       // the provider/person, cited
  status: "proposed"|"confirmed"|"cancelled";
  ics_url?: string;                              // the read-only subscribe feed (ARCHITECTURE.md)
  source: NoteRef;
  actions?: { reschedule?: boolean; cancel?: boolean };
}
```
**Mixed.** Read fields are inert. `ics_url` is the **one sanctioned external link**
and it is an *outbound subscribe* to JBrain2's own read-only ICS endpoint, not a
fetched resource — it does not violate I-9 (no resource is *loaded into* the view; the
phone's native calendar subscribes). It must be an allowlisted same-origin path, never
a model-authored URL. Reschedule/cancel dispatch `manage_appointment` (`mutate`) →
**staged Proposal** with a preview, never a direct edit (appointments already route
through the review inbox — ARCHITECTURE.md).

**`confirm_panel`** — the universal "approve the thing I staged" surface; renders a
**Proposal node preview** and offers approve/reject. The human-in-the-loop archetype,
bound to JBrain2's Proposal primitive.
```ts
{
  view: "confirm_panel"; surface: "sheet"|"dialog";
  proposal_id: string; node_id: string;
  kind: "correction"|"knowledge"|"appointment"|"list"|"wiki-restructure";
  preview: { summary: string; before?: string; after?: string };  // the rendered effect, from the executor
  provenance?: CitationRef[];                                     // what prompted it
  permission_class: "mutate"|"sensitive";
}
```
**Interactive → approves/rejects a Proposal node, never a write itself.** The buttons
hit the Proposals API to flip a node's `status` (`staged → approved | rejected`); the
**machine executor** enacts under owner authority (ASSISTANT.md "Staging & approval").
The owner approves *a shown effect, not an intent string* (the anti-fatigue rule) — so
`preview` is mandatory and is produced by the executor, not the model. `dialog` surface
+ destructive variant for `sensitive`; otherwise `sheet`. This is **the** in-chat entry
to the same trees the full Proposals page shows (§5 boundary) — a lightweight approve
without leaving the conversation, not a second approval system.

### Standard

**`entity_card`** — an entity summary: kind, aliases, current facts as outbound edges,
"open entity →". Read-only; mirrors the entity-page hub (DESIGN.md graph-forward) at
card scale. Slots: `{ entity: EntityRef; facts: {predicate:string; value:string; ref:FactRef}[]; aliases?:string[] }`. Standard because for Phase 4 a `citation_card`
+ "open entity" covers the need; the richer in-chat card is a soon-after nicety.

**`timeline`** — vertical event/measurement sequence (med changes, appointment
history, fact supersessions). Read-only. Slots: `{ events: {t:string; label:string; detail?:string; ref?:CitationRef; tone?:"neutral"|"good"|"bad"}[]; domain?:Domain }`.
Reuses the entity-page "revision histories as vertical timeline rails" idiom.

**`wiki_preview`** — read-only excerpt of a wiki article + "open article →". Slots:
`{ article_id:string; title:string; excerpt:string; updated_at:string }`. The excerpt
is server-trimmed article prose (machine-written, I-7), **not** model-authored. Standard
and gated on Phase 6 (the wiki). Must not duplicate the wiki screen — it is a peek with
a link (§5).

**`med_card`** — medication detail (name · dose · schedule · status), cited. Slots:
`{ name:string; dose?:string; schedule?:string; status:"active"|"stopped"|"prn"; ref:FactRef; entity?:EntityRef }`. Standard, not MVP: meds are real but lower-volume
than labs in Phase 4, and a `stat_block` or `data_table` row covers the interim.

**`txn_table`** — a thin finance specialization of `data_table` (money formatting,
running balance, debit/credit tone) for transactions/receipt lines/statement rows.
Standard and gated on the finance domain coming online. If `data_table` proves
sufficient with a `kind:"money"` cell, **`txn_table` is refused** and folded back in
(§4) — decide at build time, not now.

### Later / Maybe

**`place_card`** — show a place by **name + address + relative description** with **no
map tile** (§5). Slots: `{ name:string; address?:string; bearing?:string; ref?:NoteRef }`.
Deferred until the location domain (Phase 7) and only if a non-map rendering proves
worth a dedicated component over a `stat_block`/`data_table` row.

---

## 4. Composable primitives & what we refuse (anti-bloat)

**The three primitives that hold the count down.** The registry is deliberately a
*small base + specializations*, not N bespoke cards:

1. **`data_table`** is the substrate for any columnar read — `txn_table` is a
   configured `data_table`, and "show me my last 5 X" never needs a new component.
2. **`stat_block`** is the substrate for any "latest value(s)" read across all four
   domains — it absorbs what would otherwise be a dozen one-number cards.
3. **`citation_card`** is the substrate for *every* pointer-not-copy ref the other
   components carry — built once, every component's "sources" affordance reuses it.
   This is the single highest-leverage anti-bloat decision: citations are universal,
   so they are a primitive, not a per-component reimplementation.

`tone`/`flag`/`kind` enums (never raw colors or HTML) let the model express
*semantics* while the components remain the **sole authority over tokens** (DESIGN.md
"components reference tokens only") — so the model can never author a color, a hex, or
a style, only a meaning the component maps.

**What we refuse, and why:**

- **A generic `form` component — refused.** Forms are where genUI systems sprawl, and a
  model-driven form that collects input *is* the confused-deputy/injection risk the
  whole assistant design avoids. JBrain2 collects structured input through the existing
  **composer**, **bottom sheets**, and the **review inbox** — not through in-chat
  model-authored forms. Interaction in views is restricted to **dispatch a known tool**
  or **approve a staged Proposal**, both of which have fixed, typed action surfaces.
- **`markdown` / `rich_text` / `html` component — refused (hard).** This is the
  exfiltration channel (I-9) and the instruction-as-data violation (I-1). Model prose
  streams as the chat answer text (already escaped by the transcript renderer); it never
  becomes a *rendered* view. No component accepts an HTML/markdown/URL slot.
- **`image` / `map` / `iframe` / any external-resource component — refused.** I-9: views
  render no external resources. Images that exist (note attachments) are shown by the
  **existing note-sheet attachment manager**, reached via a `citation_card` link — not
  re-rendered inline by a model-authored view. (Map: §5.)
- **`chart` as a generic kitchen-sink — refused; `lab_plot` only (MVP).** A general
  "render any chart from model-specified config" invites the model to drive axes,
  series, and encodings — bloat and a subtle trust surface. We ship *purpose-built*
  plots (`lab_plot` now; a finance `balance_sparkline` only if it earns its place).
  One chart lib, mobile-first; sparklines/small-multiples over multi-series tangles
  `[web]`.
- **`button` / `link` as free primitives — refused.** assistant-ui ships a bare
  `Button` `[web]`; we do **not**, because a free-floating model-placed button is an
  un-typed action surface. Actions exist only *inside* a component whose action set is
  declared and bound to a specific tool/Proposal (`record_list`, `appointment_card`,
  `confirm_panel`).
- **Dashboard / multi-pane / grid-layout components — refused.** No nested layout, no
  component-tree (the assistant-ui generality we dropped in §2). One view = one
  component; the chat transcript is the layout. Multiple views in a turn render as
  multiple sequential inline cards, not a composed dashboard.
- **`txn_table` is provisional.** If `data_table` + a `money` cell kind suffices, it
  collapses back into the primitive. Adding it is a decision deferred to finance
  build-time, not a slot reserved now.

**The litmus test for any future component** (mirrors ASSISTANT.md's lean test): does
it express a read shape the three primitives genuinely cannot (a band-plot, a map)?
Does it carry citation refs and no external resource? Are its interactions limited to
tool-dispatch or Proposal-approval? Is it worth a versioned DESIGN.md entry? If it's a
re-skin of `data_table`/`stat_block`, refuse it.

---

## 5. The maps/location decision and the in-chat vs top-level boundary

### Maps/location: render the *fact*, never a tile — defer the component

The tension is real and structural: the agent has **no external-fetch tool** and view
output **cannot trigger external resource loads** (I-9). Every map component in the
field is an external-tile component (it loads tiles from Mapbox/Google/OSM) — which is
*precisely* the forbidden resource load. So the universal genUI "map" archetype (§2,
item 8) is the **one JBrain2 cannot have in its standard form.** The call:

- **No external map tile, ever — not behind a setting, not "just for me."** A tile
  request is an outbound load that leaks the queried coordinates to a third party; for a
  location-domain query that is the exact exfiltration I-9 exists to stop, and location
  is owner-eyes-only (DESIGN.md "Capture location": scoped tokens never receive location
  fields). Reject.
- **Three buildable options, and the chosen tier for each:**
  1. **Textual `place_card`** (name + address + relative bearing/"~3km NE of home,"
     computed from PostGIS at the tool source). **This is the answer for MVP/Standard:
     it needs no component beyond text and is covered today by `stat_block`/`data_table`
     /`citation_card`.** Ship location reads as text first.
  2. **PostGIS-derived self-contained SVG** — the tool computes a tiny vector sketch
     (a point, a geofence polygon, a track polyline in a local projection) and the view
     carries **SVG path data as typed numeric slots** (not a model-authored `<svg>`
     string, not an image URL) that a `mini_map` component draws with **no basemap**.
     This is I-9-clean (no external load; the geometry came from RLS-scoped PostGIS) and
     is the *only* way to show spatial shape without a tile server. **Tier: Later/Maybe**
     — build it only if "where was I" reads prove the textual card insufficient, and only
     as a `mini_map` whose slots are numeric path arrays the component renders.
  3. **Defer entirely** until Phase 7 (location domain) lands. **Default stance.**
- **Decision:** **defer the dedicated component; serve location as text now** via the
  primitives; if a spatial view is later justified, build a **basemap-free PostGIS-SVG
  `mini_map`** with numeric-path slots — never a tile, never a model-authored SVG string,
  never an image. The "no external load" invariant is satisfied by construction, the same
  way the whole view contract is.

### In-chat ephemeral surfaces vs top-level screens

Tool views are **in-chat, ephemeral surfaces** — they live in the Full Brain transcript
or pop into the **shared `<Sheet>`/`<Dialog>`** (DESIGN.md: "the component is the
*content*; the modal-system rules still bind"). They are **not** new top-level screens
and must not duplicate the destinations that already own these jobs:

- **Omnibox / transcript stream** — owns capture and the conversation itself. Views
  render *into* it as cards; they do not replace or re-implement the stream. A `tool_view`
  SSE event (P4.5) appends a card to the current turn.
- **Review inbox / Proposals page** — owns durable approval of staged Proposal **trees**
  (whole/subtree/leaf, dependency holds — DESIGN.md "Full Brain lateral shortcuts").
  `confirm_panel` is the **in-chat lightweight approve** of a single node/leaf for an
  effect the owner is looking at right now; the **Proposals page remains the canonical,
  always-tappable home** (left-swipe from the composer) for the full tree, partial
  approval, and anything the owner didn't approve in-line. `confirm_panel` is a shortcut
  into the same `proposals`/`proposal_nodes` records, **not** a parallel approval store.
- **Note sheet / entity page / wiki view** — own the durable, navigable detail. `citation_card`,
  `entity_card`, `wiki_preview` are **peeks with a link** that descend into those existing
  surfaces; they never become a second copy of the entity page or the article. Pointers,
  not copies — at the UI layer too.
- **Calendar** — owns scheduling. `appointment_card` is an in-chat read + ICS-subscribe +
  staged-reschedule; it does not re-implement the calendar grid.

The rule: **a view is a glanceable, ephemeral, citation-bearing card that either reads
(and links to the real screen) or dispatches one known action; the moment it wants to
*be* a screen, it should have linked to one.**

---

## 6. Sources

| # | Source | URL | Label | Used for |
|---|---|---|---|---|
| 1 | assistant-ui — Generative UI (JSON spec) | https://www.assistant-ui.com/docs/tools/generative-ui | [web] | JSON-spec node shape; consumer allowlist; unknown-name→error no fallback; no eval/no dynamic import; props-are-untrusted; reject `dangerouslySetInnerHTML`; validate `href`/`src`; primitives-only; `Card`/`Button` examples |
| 2 | assistant-ui — Defining Tools / Generative UI API reference | https://www.assistant-ui.com/docs/tools/defining-tools | [web] | toolkit name→render map; tool schema; render-per-tool |
| 3 | Vercel — Introducing AI SDK 3.0 with Generative UI | https://vercel.com/blog/ai-sdk-3-generative-ui | [web] | tool-result→React-component binding; the baseline genUI mechanism |
| 4 | Vercel AI SDK — Generative User Interfaces (docs) | https://ai-sdk.dev/docs/ai-sdk-ui/generative-user-interfaces | [web] | `streamUI`; generator loading→final; `message.parts` typed tool parts; multiple widgets per turn |
| 5 | OpenAI Apps SDK — Build your MCP server / Reference | https://developers.openai.com/apps-sdk/build/mcp-server | [web] | `structuredContent` + `outputSchema` server-side validation; widget reads `toolOutput`; UI dispatches `tools/call`; iframe+postMessage delivery (rejected) |
| 6 | OpenAI Apps SDK — Design components | https://developers.openai.com/apps-sdk/plan/components | [web] | the shipped widget/component taxonomy (cards, lists, tables) |
| 7 | Thesys / C1 + comparison (CopilotKit vs Vercel vs Thesys) | https://www.generativeui.ru/en/learn/copilotkit-vs-vercel-ai-sdk-vs-thesys | [web] | model emits JSON component tree; built-in set = tables/charts/forms; framework-agnostic renderer |
| 8 | CopilotKit — Developer's Guide to Generative UI in 2026 | https://www.copilotkit.ai/blog/the-developer-s-guide-to-generative-ui-in-2026 | [web] | render-component-for-action; actions + readable-state primitives; field landscape |
| 9 | Pragmatic Engineer — How Anthropic built Artifacts | https://newsletter.pragmaticengineer.com/p/how-anthropic-built-artifacts | [web] | cross-origin sandboxed iframe + process isolation + strict CSP for LLM-generated code; "secure playground" framing (the model JBrain2 rejects) |
| 10 | Microsoft Fabric community — best viz for annual blood lab data | https://community.fabric.microsoft.com/t5/Desktop/What-is-the-best-visualization-for-annual-blood-lab-data/td-p/3249544 | [web] | line+point with reference range as shaded band; current vs prior values; the `lab_plot` shape |
| 11 | jjstatsplot — Line charts for clinical time series & trend | https://www.serdarbalci.com/jjstatsplot/articles/30-linechart-comprehensive.html | [web] | clinical trend line chart conventions; reference-range banding |
| 12 | Domo / Displayr — Sparkline charts: what & when | https://www.domo.com/learn/charts/sparkline-chart | [web] | sparkline = axis-less micro-trend in tables; small-multiples for cross-series on mobile; finance growth-over-time |
| — | Generative-UI taxonomy, allowlist-render, shadcn/Tailwind, mobile chart priors | (model knowledge) | [training] | recurring component vocabulary; primitives-vs-specialization; tone-enum-not-color; SVG-from-geometry vs tile |
