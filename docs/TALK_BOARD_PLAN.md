# Talk board — build plan (Phase 6, mock B)

Chosen GUI direction **B — threaded topics** (`docs/mocks/wiki-talk-b-topics.html`; A/C retained
as the record per `docs/mocks/wiki-talk-README.md`). The Talk page is a persistent, article-
anchored editorial board with **two voices**: the batch **Builder** (per-build decision summaries,
in an auto *Build log* topic) and the interactive **Editor** (the Phase-4 agent, conversing +
enacting via the sanctioned wiki tools). The wiki stays **machine-written**; Talk is the
conversational front-end over the sanctioned levers (correction note, source exclusion, rebuild).

## Delivery split (owner-approved)

- **Wave T1 (this plan):** the persistent threaded board + the **Builder** voice + owner
  topics/replies + read/write APIs + the full B-topics frontend (5 fixture states) + RLS isolation
  tests. **No live agent** — owner replies are stored plainly; the Editor arrives in T2.
- **Wave T2 (next):** the **Editor** voice — an owner reply drives `AgentLoop.run_stream` with the
  wiki tools (read_wiki + file_correction/request_rebuild/add_source_exclusion), streamed into the
  topic and persisted as `editor` posts with outcome chips. Reuses the chat infra wholesale.

---

## T1 design

> **Red-team resolutions (v2).** C1 seq race → posts are **append-only, ordered by `created_at, id`**
> (no `seq` column). C2 build-log failure isolation → the Build-log post runs in its **own
> transaction AFTER** the per-entity build commits, **best-effort** (try/except, log-and-continue) —
> a cosmetic post never aborts a real build. H1 → find-or-create the Build-log topic with
> `INSERT … ON CONFLICT DO NOTHING` on the partial unique index. H2 → **absolute** time is formatted
> **client-side** off `created_at`; "rev N" is **derived** from the Build-log post's 1-based position
> (no schema column). H3 → App.tsx layering pinned below. M1 → Build-log summaries are
> **domain-neutral** (counts, never domain names) + a narrowed-owner read assertion in the isolation
> test. The `run_id` column is forward-looking and **unused in T1** (no query joins `app.runs`).

### Schema — migration 0053 (head is 0052), two owner-only tables

Talk is **owner-only** (mirrors `wiki_articles`: `app.is_owner()` USING+CHECK, FORCE RLS). It is NOT
domain-scoped: a topic/Build-log entry is editorial metadata *about the (cross-domain) article
shell*, not the underlying domain facts. Because `app.is_owner()` is true for a *narrowed* owner too
(`owner_scoped=True` keeps `principal_kind='owner'`), a future P7 narrowed/capability session would
read all Talk rows — so **Build-log summaries are written domain-neutral** (counts + the subject
title, never a domain name like "Finances"); domain-scoping Build-log posts is a noted **P7** item.
No `principals` FK on author (SYSTEM_CTX.principal_id is the string `"worker"`, not a uuid); author is
a closed enum matching the mock's three voices (`editor` is **reserved for T2** — no T1 writer).

```
app.wiki_talk_topics
  id           uuid pk default gen_random_uuid()
  article_id   uuid NOT NULL REFERENCES app.wiki_articles(id) ON DELETE CASCADE
  kind         text NOT NULL DEFAULT 'discussion' CHECK (kind IN ('discussion','build_log'))
  title        text NOT NULL
  status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved'))
  last_post_at timestamptz NOT NULL DEFAULT now()
  created_at   timestamptz NOT NULL DEFAULT now()
  UNIQUE INDEX wiki_talk_one_build_log ON (article_id) WHERE kind='build_log'  -- ≤1 build_log/article
  INDEX (article_id, last_post_at DESC)

app.wiki_talk_posts
  id          uuid pk default gen_random_uuid()
  topic_id    uuid NOT NULL REFERENCES app.wiki_talk_topics(id) ON DELETE CASCADE
  author      text NOT NULL CHECK (author IN ('owner','editor','builder'))
  body        text NOT NULL
  source_json jsonb                                -- optional source card {note_id,meta,snippet,domain}
  outcome     text                                 -- optional outcome chip ("correction filed → rebuild queued")
  run_id      uuid REFERENCES app.runs(id) ON DELETE SET NULL   -- forward-looking; UNUSED in T1 (no join)
  created_at  timestamptz NOT NULL DEFAULT now()
  INDEX (topic_id, created_at, id)                 -- append-only order; no seq, no reorder, race-free
```

Both: ENABLE + FORCE RLS; `*_owner` policy `USING (app.is_owner()) WITH CHECK (app.is_owner())`;
GRANT SELECT/INSERT/UPDATE/DELETE to `jbrain_app`. Downgrade drops both (posts first — FK order).

ORM models in `models/wiki.py` (`WikiTalkTopic`, `WikiTalkPost`) mirroring the migration (queries are
raw SQL, but the repo keeps models in sync with schema).

### Builder Build-log seam (`wiki/builder.py`) — failure-isolated, post-commit

`_build_entity` returns an optional `BuildLogNote(article_id, summary)` describing what it did; the
`refresh`/`rebuild` loops, **after the per-entity `session.commit()` succeeds**, post it in a
**separate** `scoped_session` wrapped in `try/except` (log-and-continue) — the build transaction stays
pure, and a Build-log failure can never roll back sections or strand the entity.

- `_ensure_article` returns `(article_id, created: bool)` (today returns just the id).
- `_enact_redirect` returns `(survivor_article_id: uuid|None, gone_name: str)` (today returns None and
  has only uuids): it must `SELECT canonical_name` for the gone entity and surface the locally-resolved
  `survivor_article` so the caller can build the merge note. `survivor_article` may be NULL (the
  early-return when the gone entity had no article, and the un-merge case) → **no Build-log post**.
- `_post_build_log(maker, article_id, summary)` (best-effort, own txn): find-or-create the `build_log`
  topic with the **exact** partial-index inference form, then insert the `author='builder'` post and
  bump `last_post_at` **in the same txn** (so post+bump are atomic):
  ```sql
  INSERT INTO app.wiki_talk_topics (article_id, kind, title)
  VALUES (:a, 'build_log', 'Build log')
  ON CONFLICT (article_id) WHERE kind = 'build_log' DO NOTHING;   -- predicate REQUIRED for a partial index
  ```
  (migration index: `CREATE UNIQUE INDEX wiki_talk_one_build_log ON app.wiki_talk_topics (article_id) WHERE kind = 'build_log'`).
  then `SELECT id … WHERE article_id=:a AND kind='build_log'`, insert the post, `UPDATE … last_post_at`.
- `_build_entity` returns an optional `BuildLogNote(article_id, summary)`; the loop posts it post-commit.
  **Per-exit map** (the five exits of `_build_entity`): missing-entity (`sourced is None`),
  not-notable, and no-sections → **return None** (nothing built, no post). Redirect → `BuildLogNote`
  with `f"Merged in {gone_name}."` targeting the survivor article **iff `survivor_article_id` is not
  None** (else None). Built → `BuildLogNote(article_id, f"{'Created' if created else 'Rebuilt'} article
  ({sourced.kind} guide); {len(claims)} facts across {n_domains} domains.")` (n_domains = distinct
  section domains; summary is **domain-neutral** — counts + subject kind, never a domain name).
- Posts only on an *actual* build (`refresh` touches only dirty entities; `rebuild('all')` is a rare
  manual op and one post/article/run is the honest record). Runs under SYSTEM_CTX (= owner ⇒
  `is_owner()` true). No `run_id` threaded to the builder today (column stays nullable).

### Read/write APIs (`api/wiki.py`, assembly in new `wiki/talkstore.py`)

`WikiTalkStore.get_board(ctx, article_id)` → `{title, topics:[{id,kind,title,status,meta,posts:[{id,
author,body,source,outcome,created_at}]}]}`. **Serves active articles only** (lockstep with the reader,
which 404s merged/archived). Order: discussion topics by `last_post_at` desc, **Build log last**; posts
by `created_at, id` asc. `meta` for build_log = `"auto · N entries"`; each build_log post carries a
derived **`rev`** = its 1-based position (the mock's "rev N"). `created_at` is returned ISO; the client
formats the absolute label.

- `GET  /api/wiki/{id}/talk` — owner board (PrincipalDep; owner-only pre-P7); 404 if no active article.
- `POST /api/wiki/{id}/talk/topics` `{title, body}` (both `Field(min_length=1)`) → OwnerDep; creates a
  `discussion` topic + first `owner` post atomically; returns the topic. 404 if no active article.
- `POST /api/wiki/{id}/talk/topics/{topic_id}/posts` `{body}` (min_length=1) → OwnerDep; appends an
  `owner` post; **409 if the topic is the build_log**; 404 if topic/article not found.
- `PATCH /api/wiki/{id}/talk/topics/{topic_id}` `{status}` → OwnerDep; resolve/reopen a discussion
  topic; **409 on a build_log topic**.

All write-path scope/kind/existence checks run **inside the same RLS-scoped session as the write** (no
TOCTOU between an unscoped check and a scoped write — the `file_correction` precedent).

Route ordering: `/wiki/{id}/talk*` paths have a segment after `{id}`, so they don't shadow `/wiki/{id}`
or `/wiki/{id}/image`. In `mock.ts` the `…/talk/topics` POST regex must be declared **before**
`…/talk/topics/{tid}/posts`, and the `…/talk` GET before the generic `/wiki/{id}` matcher.

### Frontend (mock B) — `frontend/src/screens/TalkScreen.tsx`

Faithful to `wiki-talk-b-topics.html`: topbar (back, "Talk / {title}", open-article button — theme is
the app's global toggle), "Discussion" bar + **New topic**, a scroll of collapsible topics (chevron,
title, `open`/`resolved` badge, or `auto · N entries` meta for Build log), per-topic posts (signature
`You`/`Editor`/`Builder` + an **absolute** time label, body, optional source card, optional outcome
chip; build_log posts show `· rev N`), a reply composer per open topic, and a New-topic form (title +
body). Tokens-only; `.talk-*` classes. New client formatter `talkTime(iso)` → `today 9:14` /
`Mar 12` / `Mar 17 02:14` (off `created_at`, viewer-local).

**App.tsx layering (pinned):** a top-level layer like the reader. Add `talkArticle: string | null`
state; `closeTalk()`; include `(talkArticle !== null ? 1 : 0)` in `overlayDepth`; in `closeTopLayer`
add `if (talkArticle !== null) return closeTalk();` **before** the `wikiArticle` check (Talk stacks
above the reader). Render `<TalkScreen articleId={talkArticle} syncStatus=… onClose={closeTalk}
onOpenArticle={(id)=>{setWikiArticle(id); setTalkArticle(null);}} />`. `WikiScreen` gains
`onOpenTalk?: (id)=>void` and a **Discussion** affordance (a topbar/icon button) that calls it; the
existing DiscussSheet correction form (#260) is **unchanged** (no regression).

API client: `getTalk`, `createTalkTopic`, `postTalkReply`, `setTalkTopicStatus`. Types: `WikiTalkOut`,
`WikiTalkTopic`, `WikiTalkPost`. Mock-mode routes in `mock.ts` + a `TALK` fixture.

**DoD fixtures (mock states):** empty (Build-log topic with 1 entry, zero discussion topics — the real
state of a freshly-built article, since the builder always posts), long-thread, pending-action (an
owner post with no reply yet — T1's terminal state), error (board fails to load), offline (sync pill).
Built against fixture data, graph-independent.

### Tests

- **Backend integration (real PG):** `wiki_talk_topics`/`wiki_talk_posts` **RLS isolation** — non-owner
  capability sees none + cannot write (the per-new-table requirement) **and a narrowed-owner read
  assertion** (a narrowed owner still reads Talk, documenting the owner-only/P7 posture); the builder
  posts a domain-neutral Build-log entry on build + "Merged in X" to the survivor on redirect; the
  store assembles the board (active-only, Build-log last, derived `rev`); create-topic / post-reply /
  resolve round-trip; build_log find-or-create is idempotent under a repeated build.
- **Backend unit:** the Talk API with a stubbed store (auth required, owner-gating on writes, **409 on
  build_log post + 409 on build_log resolve**, 404 on missing article, min_length validation) —
  TestClient pattern, no Docker.
- **Frontend:** `TalkScreen.test.tsx` (render topics + badges + Build log + derived rev; expand/collapse;
  new topic posts; reply posts; resolve toggles; empty + error states; `talkTime` formatter unit) +
  `mock.test.ts` route coverage.
- ruff / ruff format / pyright / biome / tsc all green; 80% backend / security-100%.

### Docs

- `docs/DESIGN.md`: record the Talk surface — chosen **B**, rationale (durable threaded record + auto
  Build-log; A/C retained), reference mock — per the GUI gate (the choice lands in DESIGN.md when
  built).
- `docs/PHASE6_WIKI_PLAN.md`: flip the Talk board follow-on from "remaining" to "T1 shipped; T2
  (Editor) pending".

### Non-negotiables check

LLM via adapter (n/a in T1 — no LLM); file I/O n/a; **RLS owner-only enforced in Postgres + isolation
test per new table**; tests-with-code; Conventional Commits + per-wave PR + CI green; no new deps
(dev-setup.sh unchanged); the wiki stays machine-written (Talk posts are discussion/Build-log
metadata, never article prose; the article's sections/revisions are untouched).

## T2 sketch (next wave, not built here)

Owner reply → `AgentLoop.run_stream(session=read_context(...), scopes=…, conversation=topic history)`
with the wiki registry; stream `editor` posts + tool effects into the topic; outcome chips from
`JobEnqueuedEvent`/tool results; reuse `Bubble`/`Worked`/`StepRow`/`parseChatStream`. Retire
DiscussSheet in favor of the in-topic correction once this lands.
