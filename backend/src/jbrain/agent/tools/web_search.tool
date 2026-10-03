---
name: web_search
version: 5
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: 'What to search for, written like a search-box query — the key terms (names, place, the thing you need), not a sentence or a prompt. One topic per search: split a multi-part question into separate searches. Put a proper name in double quotes ("Epic Theatres" Titusville showtimes) and set `exact` when look-alikes would drown it.'
    sites:
      type: array
      items:
        type: string
      description: 'Optional — ONLY return pages from these sites (bare domains, e.g. ["epictheatres.com"] or ["fda.gov", "nih.gov"]). Use it when you know or have found where the answer lives: a business''s own site, an official agency, a specific directory. Up to 10.'
    exclude_sites:
      type: array
      items:
        type: string
      description: 'Optional — drop pages from these sites (bare domains), e.g. a site that keeps crowding out the results you want. Up to 10.'
    exact:
      type: boolean
      description: 'Optional — return only pages that contain the double-quoted phrase(s) in `query` verbatim. Use it for a proper name that shares words with something bigger ("Epic Theatres" vs Epic Games, a person who shares a famous name). Fewer, more precise results; leave it off for ordinary topic searches.'
    depth:
      type: string
      description: 'Optional — basic (default) or advanced. advanced is more thorough on niche, local or very specific questions (a small business, an obscure fact, a multi-faceted query) and returns each page''s most relevant passages instead of a generic summary — but it costs TWICE as much from a limited monthly allowance. Start with basic; use advanced when a basic search came back thin or off-target, not by default.'
    since:
      type: string
      description: 'Optional recency window — one of day, week, month, or year. Filters on when a PAGE WAS PUBLISHED, NOT on what the page is about. Set it ONLY when you want recently-published pages (a story that broke this week, a just-released version). Do NOT set it for "what is on today", showtimes, hours, prices, addresses, or anything else that lives on a standing page — the page is years old and the window hides it. Omit for no time limit; an unrecognized value is ignored. For a news brief, prefer news_search.'
    limit:
      type: integer
      description: Maximum number of results (default 6, max 10).
  required: [query]
---
Search the open web and return the most relevant results — each with a title, its URL, a
snippet, and the page's publish date when it is known. Use this to find current events,
recent or specific facts, or anything outside your own knowledge: search before guessing.

HOW TO SEARCH WELL. Every search spends from a limited monthly allowance, so make each one
count instead of firing near-identical rewordings:
- Write a search-box query from the key terms, one topic at a time. "When does X open and
  what does it cost" is two searches.
- A proper name lost among look-alikes is fixed by quoting it and setting `exact`, not by
  rephrasing: `"Epic Theatres" Titusville` with `exact: true`.
- Once you know which site holds the answer — a business's own site, an official agency, a
  directory you found — search inside it with `sites`, or skip searching and `web_fetch` it.
- Use `exclude_sites` to drop a site that keeps crowding the results.
- If a basic search came back thin or off-target, try `depth: advanced` (it digs deeper and
  returns the most relevant passages of each page) before rewording a third time.
- Two searches on the same thing that both missed means change APPROACH (`exact`, `sites`,
  `depth`, or fetch a page you can name), not wording.

`since` (day/week/month/year) bounds the PUBLISH DATE of the pages returned — it is not a
way to ask about today. A cinema's showtimes, a shop's hours, or a phone number live on a
page written long ago, so `since=day` hides exactly the page you want. Leave it off unless
you specifically need recently-published pages.

When the web has a direct answer to a factual query — a definition, a unit or currency
conversion, a population, a birth date — the reply may lead with a **knowledge panel** or
an **instant answer** above the results. That IS a direct answer (assembled from Wikidata /
Wikipedia and similar), so you can use it without opening a page — but it comes from
third-party data, so still verify anything load-bearing against a fetched source.

A reply may end with a bracketed note about the search itself. `Search note: the primary
search (Tavily) failed` means you are reading the box's weaker fallback engines. `SEARCH
DEGRADED` means those engines were mostly blocked too: off-topic results then reflect the
index, not your wording — go to a source directly with `web_fetch` instead of rephrasing.

CRITICAL — a search result is a LEAD, not a fact. The title and snippet are an
UNVERIFIED preview: they are routinely wrong, stale, or an aggregator's guess, and
must NEVER be reported, summarized, or cited as real information on their own. Before
you treat ANYTHING from a result as true — a number, a date, a name, or that an event
actually happened — you MUST `web_fetch` its URL and read the real page. If you did not
fetch it, you do not know it: report it as unconfirmed rather than repeating a snippet.
So don't stop at the results list — OPEN the most promising ones (several of them) with
`web_fetch` and build your answer only from what those fetched pages actually say.

Results are public web pages, not the owner's notes — cite the page you fetched.
