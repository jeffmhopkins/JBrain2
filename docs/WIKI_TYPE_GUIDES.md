# Wiki type guides (editorial config — Phase 6)

Per-entity-type editorial guides the **builder** loads when (re)writing an article: the
ordered **sections** (each with a default domain + an include-if rule), the **style**,
and the hard **requirements**. These are **data, not code** — they seed the editorial-config
table and the owner can tune them without a deploy. This is a starter set for the
article-worthy type families (the notability gate, `PHASE6_WIKI_PLAN.md §6`); the
remaining catalog types fall back to the generic guide until given their own.

Section `domain` is the firewall unit (single-domain per section). A section is **omitted**
when it has no cited facts. `health`/`finance`/`location` sections are hidden from
out-of-scope viewers (existence included).

## Shared requirements (apply to every guide)

```yaml
requirements:
  - every claim cites a CHUNK ([n] → References, rendered as the source note: date·domain·
    snippet); the cited chunk must be same-domain as the section; drop any uncitable claim
  - omit empty sections; never invent a section not in the guide
  - no speculation or inference beyond the cited facts
  - dates render in the note's local timezone
  - one domain per section; route a sensitive fact to its domain section
    (a salary fact → Finances, not Career)
  - resolve a mentioned entity to its wiki article if one exists, else a red-link
    to its entity page
  - neutral, third-person, encyclopedic voice; past tense for history, present for ongoing
  - the Lead is 1–2 sentences and, where relevant, states the subject's relation to the owner
style_default: "Neutral encyclopedic third person. Lead identifies the subject and (if a
  person/org/place in the owner's life) their relation to the owner. Prose, not lists,
  unless the facts are inherently enumerable."
```

## Writing style (binding — every article)

Encyclopedic, like a Wikipedia article. The builder follows these; the hard rules
(citation, single-domain, omit-empty) are enforced regardless. *(Validated against a
worked example — `docs/mocks/wiki-reader-example-priya.html`.)*

**Voice & reference**
- Neutral third person. **No first person** — "I/my" never appears.
- Past tense for history and events; present tense for current state.
- First mention of the subject = full name (bold in the lead); afterwards surname or she/he/they.
- The **owner is a named entity**: refer to them by name and wiki-link them like anyone
  else ("the younger sister of [[Jordan Hale]]") — never "the owner" or first person.
- **Minors are named by default** (private single-user KB); an editorial-config toggle can
  anonymize them ("a daughter") for owners who prefer it.
- First mention of any other entity is a wiki-link; red-link if it has no article yet.

**Facts, dates, numbers**
- Assert what's cited; no "reportedly"/hedging unless the fact's assertion status is
  reported/hypothetical. No speculation beyond cited content.
- Keep numbers/measurements **verbatim** (3:52, $4,000, 50 mcg) — never round or invent.
- Date only real-world-dated events; state an undated lifelong fact tenselessly (don't
  attach the note's capture date). A grounded interval may be a range ("2022–2024") when
  both endpoints are cited. Superseded facts are written as past, never as current.

**Citations**
- Every claim carries a Wikipedia-style `[n]` resolving to a **chunk** (rendered in the
  References list as its source note: date · domain · snippet). **Cite at the smallest
  distinct clause** (lent…[n]; repaid…[m]), not stacked at the sentence end — this is the
  same granularity the **grounding gate** verifies (each cited clause must be entailed by
  its chunk). Fact-backed and note-derived claims are cited identically (no visual tiering
  — the citation is the uniform trust signal).

**Prose vs. lists vs. tables**
- **Prose by default** — biography reads as paragraphs.
- **Bulleted list** only for ≥3 short, parallel, non-narrative items of one kind that don't
  flow as a sentence (talks, publications, awards, affiliations). No "Trivia" sections.
- **Table** for genuinely tabular data: a **time series** of measurements (one predicate
  over time → date/label | value) or structured records sharing fields (medications →
  name | dose | for; races → event | year | time). The builder picks format from the
  **shape of the underlying facts** (measurement-kind / repeated same-predicate `value_json`).
- 2 items or fewer → prose; structure only at ≥3 records or a time series (a genuinely
  tabular 2-row set is a borderline judgment call). Every list item / table row carries its
  own `[n]`; lists/tables stay **single-domain** (a Health medications table lives in
  Health). A type guide may declare a section's preferred format.

**Section hierarchy (subsections)**
- A section may nest into **subsections** (H2 → H3 → H4), like Wikipedia. A type guide can
  declare subsections under a section (e.g. Person → Career → {Training, Current practice,
  Talks and publications}), and the builder may **auto-subdivide** a long section into
  natural subsections. **Subsections inherit their top-level section's domain** — no
  cross-domain nesting; the top-level domain section is the firewall unit and its whole
  subtree hides together for out-of-scope viewers. Use subsections only when a section is
  genuinely long or has distinct sub-topics; short sections stay flat.

**Images**
- An entity's **owner-set profile image** (`entities.image_sha`) renders in the infobox (a
  person's photo, a business logo, a place's location shot). It is owner metadata, **not a
  cited claim** — no `[n]`. Uploaded in the entity view; the article picks it up
  automatically.

---

## Person

```yaml
type: Person
lead: "Who they are + relation to the owner (e.g. 'Celine Hopkins is the owner's spouse, a software engineer in Denver.')."
sections:
  - {name: Early life,    domain: general, include_if: birth / family-of-origin / hometown facts}
  - {name: Career,        domain: general, include_if: employers, roles, professional work}
  - {name: Personal life, domain: general, include_if: relationships, residence, interests, family}
  - {name: Health,        domain: health,  include_if: conditions, medications, allergies, providers}
  - {name: Finances,      domain: finance, include_if: accounts, income, obligations}
style: "Biographical. Past tense for history (former roles, past residences), present for current state."
```

## Organization

```yaml
type: Organization        # also: Company, Institution, Group
lead: "What it is + the owner's relation to it (employer, vendor, bank, club…)."
sections:
  - {name: Overview,     domain: general, include_if: what the org is / does}
  - {name: History,      domain: general, include_if: founding, milestones}
  - {name: People,       domain: general, include_if: leadership or the owner's contacts there}
  - {name: Products,     domain: general, include_if: products / services}
  - {name: Dealings,     domain: general, include_if: the owner's non-financial interactions}
  - {name: Finances,     domain: finance, include_if: accounts, payments, contracts with the owner}
style: "Factual, present tense for current structure, past for history."
```

## Place

```yaml
type: Place               # also: City, Venue, Building
lead: "What/where it is + significance to the owner."
sections:
  - {name: Overview,    domain: general,  include_if: what the place is, geography}
  - {name: History,     domain: general,  include_if: history / significance}
  - {name: Associations, domain: general, include_if: who/what the owner connects to it}
  # NOT seeded in v1: a Visits (domain: location) section. The `location` domain has no
  # RLS scope or color until Phase 7 — seeding a location-domain section now would create a
  # firewall unit whose isolation test can't be written. Add Visits when Phase 7 lands.
style: "Descriptive."
```

## Project

```yaml
type: Project             # also: Initiative, Effort
lead: "What it is + current status."
sections:
  - {name: Goals,      domain: general, include_if: scope / objectives}
  - {name: Timeline,   domain: general, include_if: milestones, dated events}
  - {name: People,     domain: general, include_if: who's involved}
  - {name: Status,     domain: general, include_if: current state / outcome}
  - {name: Finances,   domain: finance, include_if: budget, spend}
style: "Status-oriented; present tense for current state, with a dated timeline."
```

## Event

```yaml
type: Event               # also: Trip, Appointment-cluster, Occasion
lead: "What it was/is + when."
sections:
  - {name: Details, domain: general, include_if: when / where / who}
  - {name: Outcome, domain: general, include_if: what happened / notes}
style: "Temporal. Lead carries the date; order details chronologically."
```

## Concept

```yaml
type: Concept             # also: Topic, Term, Idea
lead: "A one-sentence definition."
sections:
  - {name: Definition, domain: general, include_if: what it means}
  - {name: Details,    domain: general, include_if: elaboration, examples}
  - {name: Related,    domain: general, include_if: links to related articles}
style: "Define first, then elaborate. Heavier on wiki-to-wiki links than prose."
```

## Medical entities (Drug / MedicalCondition / MedicalProcedure)

In a health-heavy personal KB these entities accrue cited facts and deserve real
articles, **not** the generic fallback. They are **article-worthy** (added to the
notability gate, §6 plan) and their primary sections are **health-domain**, so most of
each article is firewalled and hidden from out-of-scope viewers.

```yaml
type: Drug                 # a medication the owner or a subject takes
lead: "What it is + who takes it (link the person)."
sections:
  - {name: Overview,    domain: general, include_if: what the drug is / class}
  - {name: Use,         domain: health,  include_if: who takes it, dose, schedule, for what}
  - {name: Notes,       domain: health,  include_if: effects, changes, allergies/interactions}
style: "Clinical-neutral; current dose present tense, past regimens as history."
```
```yaml
type: MedicalCondition     # also: MedicalProcedure (Overview/Course/Treatment)
lead: "What it is + whose (link the person)."
sections:
  - {name: Overview,    domain: general, include_if: what the condition is}
  - {name: Course,      domain: health,  include_if: diagnosis date, progression, episodes}
  - {name: Treatment,   domain: health,  include_if: medications/procedures, providers}
style: "Clinical-neutral; chronological course; cite each clinical claim to its note."
```

## Product

```yaml
type: Product              # a device/possession/service the owner owns or uses
lead: "What it is + the owner's relation (owns / uses)."
sections:
  - {name: Overview,    domain: general, include_if: what it is}
  - {name: Ownership,   domain: general, include_if: acquired when, usage}
  - {name: Finances,    domain: finance, include_if: price, warranty, payments}
style: "Factual."
```

## Generic (fallback)

```yaml
type: _default            # any catalog type without its own guide yet
lead: "What it is + relation to the owner."
sections:
  - {name: Overview, domain: general, include_if: any general facts}
  - {name: Details,  domain: general, include_if: remaining general facts}
  - {name: Health,   domain: health,  include_if: health facts}
  - {name: Finances, domain: finance, include_if: finance facts}
style_default applies.
```

---

> **Tuning:** these are editorial config — adjust section order, add/remove sections, or
> tighten `requirements` per type without code. New article-worthy types get their own
> guide; until then `_default` covers them. The `include_if` rules are guidance the
> builder's rewrite prompt follows; the *hard* gates (citation, single-domain, omit-empty)
> are enforced by the builder + the Postgres firewall regardless of the guide.
