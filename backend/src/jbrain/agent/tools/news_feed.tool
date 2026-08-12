---
name: news_feed
version: 1
permission: web
params:
  type: object
  properties:
    category:
      type: string
      description: Which curated feed set to pull — a topic word such as space, ai_tech, national, economy, world, or local. Match it to your angle (a space-industry angle -> space; a markets angle -> economy). An unknown category returns the list of known ones.
    since:
      type: string
      description: How recent — one of day, week, month, or year (default day). Filters items to that window; undated items are kept.
    limit:
      type: integer
      description: Maximum items (default 8, max 10).
  required: [category]
---
Pull recent articles from a curated set of trusted RSS/Atom news feeds for a topic
CATEGORY, newest first. Prefer this over news_search for the daily brief's core topics:
the feeds are hand-picked primary outlets, so you skip the search-engine throttling and
get clean, dated article leads straight away.

Some feeds carry the FULL article text. Those items are returned WITH the article body
inline and marked "✓ full text": treat such an item as a page you have already OPENED —
write your findings straight from that body and do NOT web_fetch it again (re-fetching
wastes time and can hit a bot-wall the feed already got you past). Items marked
"lead only" carry just a headline and short summary — web_fetch the URL to read the real
article before citing it, exactly like a news_search lead.

Always check each item's own date against today; a feed can carry an older story with no
new development. These are public web pages, not the owner's notes — cite the article you
used.
