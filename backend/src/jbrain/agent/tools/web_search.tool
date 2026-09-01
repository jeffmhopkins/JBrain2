---
name: web_search
version: 4
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: What to search the web for.
    since:
      type: string
      description: 'Optional recency window — one of day, week, month, or year. Filters on when a PAGE WAS PUBLISHED, NOT on what the page is about, and it also drops the search engines that cannot filter by date. Set it ONLY when you want recently-published pages (a story that broke this week, a just-released version). Do NOT set it for "what is on today", showtimes, hours, prices, addresses, or anything else that lives on a standing page — the page is years old and the window hides it. Omit for no time limit; an unrecognized value is ignored. For a news brief, prefer news_search.'
    limit:
      type: integer
      description: Maximum number of results (default 6, max 10).
  required: [query]
---
Search the open web and return the most relevant results — each with a title, its
URL, and a short snippet. Use this to find current events, recent or specific facts,
or anything outside your own knowledge: search before guessing, with a precise query
built from the key terms of what you need.

`since` (day/week/month/year) bounds the PUBLISH DATE of the pages returned — it is not a
way to ask about today. A cinema's showtimes, a shop's hours, or a phone number live on a
page written long ago, so `since=day` hides exactly the page you want. Leave it off unless
you specifically need recently-published pages. If a search comes back empty, changing the
wording is rarely the fix — check your arguments first.

When the web has a direct answer to a factual query — a definition, a unit or currency
conversion, a population, a birth date — the reply may lead with a **knowledge panel** or
an **instant answer** above the results. That IS a direct answer (assembled from Wikidata /
Wikipedia and similar), so you can use it without opening a page — but it comes from
third-party data, so still verify anything load-bearing against a fetched source.

CRITICAL — a search result is a LEAD, not a fact. The title and snippet are an
UNVERIFIED preview: they are routinely wrong, stale, or an aggregator's guess, and
must NEVER be reported, summarized, or cited as real information on their own. Before
you treat ANYTHING from a result as true — a number, a date, a name, or that an event
actually happened — you MUST `web_fetch` its URL and read the real page. If you did not
fetch it, you do not know it: report it as unconfirmed rather than repeating a snippet.
So don't stop at the results list — OPEN the most promising ones (several of them) with
`web_fetch` and build your answer only from what those fetched pages actually say.

Results are public web pages, not the owner's notes — cite the page you fetched.
