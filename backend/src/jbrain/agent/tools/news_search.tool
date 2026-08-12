---
name: news_search
version: 1
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: The news topic to search for — the story in plain terms (topic, people, place, and optionally an outlet). Do NOT put a calendar date in the query; use `since` for recency.
    since:
      type: string
      description: How recent the results must be — one of day, week, month, or year (default day). Use day for "today's news", week for a recent story, month/year for background. An unrecognized value falls back to day.
    limit:
      type: integer
      description: Maximum number of results (default 6, max 10).
  required: [query]
---
Search recent NEWS and return dated article leads — each with its title, publish date,
outlet, URL, and a short snippet, filtered to how recent you ask for with `since`. Use
this instead of web_search whenever you want current news: it queries news engines (not
the general web), so it surfaces actual articles with dates rather than calendar pages,
almanac entries, or an old "facts about August" page. Search by TOPIC, never by stuffing
a date into the query — the `since` window handles recency.

CRITICAL — a result is a LEAD, not a fact. The headline and snippet are an UNVERIFIED
preview: routinely wrong, stale, or an aggregator's guess, and must NEVER be reported,
summarized, or cited on their own. Before you treat anything from a result as true — a
number, a name, that an event happened — you MUST web_fetch its URL, read the real page,
and check the page's own date against today (a "live"/"latest" page can be days old).

Prefer freely-fetchable outlets — the wire services and public broadcasters (AP / apnews.com,
NPR, PBS, BBC, The Guardian) and the primary source itself (a government agency, company,
or operator's own page). A few majors (Reuters, the Wall Street Journal, Bloomberg) block
this system's fetches, so a URL from one is usually a dead end the reader can't open — if
that's your only lead for a story, find the same story on a free outlet before settling.
Never build a cache/proxy URL (webcache, r.jina.ai) to get around a block; only open real
article URLs. These are public web pages, not the owner's notes — cite the page you fetch.
