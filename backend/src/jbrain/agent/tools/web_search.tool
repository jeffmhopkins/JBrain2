---
name: web_search
version: 1
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
URL, and a short snippet. Use this to answer questions that depend on current
events, recent or specific facts, or anything outside your own knowledge: search
before guessing. Pass a precise query built from the key terms of what you need.
To read the full contents behind a promising result, follow up with web_fetch on
its URL. Results are public web pages, not the owner's notes — cite where the
information came from.
