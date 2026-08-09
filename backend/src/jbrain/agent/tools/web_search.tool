---
name: web_search
version: 2
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: What to search the web for.
    limit:
      type: integer
      description: Maximum number of results (default 6, max 10).
  required: [query]
---
Search the open web and return the most relevant results — each with a title, its
URL, and a short snippet. Use this to find current events, recent or specific facts,
or anything outside your own knowledge: search before guessing, with a precise query
built from the key terms of what you need.

CRITICAL — a search result is a LEAD, not a fact. The title and snippet are an
UNVERIFIED preview: they are routinely wrong, stale, or an aggregator's guess, and
must NEVER be reported, summarized, or cited as real information on their own. Before
you treat ANYTHING from a result as true — a number, a date, a name, or that an event
actually happened — you MUST `web_fetch` its URL and read the real page. If you did not
fetch it, you do not know it: report it as unconfirmed rather than repeating a snippet.
So don't stop at the results list — OPEN the most promising ones (several of them) with
`web_fetch` and build your answer only from what those fetched pages actually say.

Results are public web pages, not the owner's notes — cite the page you fetched.
