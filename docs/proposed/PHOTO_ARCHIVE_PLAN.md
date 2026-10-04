# Photos and People — Design Spec

> **Status:** Proposed · **Last verified:** 2026-10-04

> **Status: proposed, not scheduled.** Nothing here is built. Reworked 2026-10-04 around the
> owner's direction: **family-first face recognition with InsightFace**, two launcher apps
> (**People** to enroll the family, **Photos** to browse), and Flash-Next as the box's
> vision model. The 2026-07 draft assumed a text-only `gpt-oss-120b` behind a separate small
> VLM; Flash-Next sees images itself, so that bridge layer is gone. When picked up it must
> meet the root `CLAUDE.md` non-negotiables: model calls through the LLM adapter, files
> through the storage abstraction, and an RLS isolation test for every new table. Face
> data is **biometric** — at least as sensitive as health, finance or location.

Two things, built in order:

1. **Know the family.** About a dozen named people. Their faces are enrolled once in a
   People launcher; from then on, whenever a photo reaches jerv, a dedicated face model
   says who is in it and the vision model reasons with those names.
2. **The archive.** A decade of phone dumps ingested, de-duplicated, dated, captioned and
   searchable — "photos of Emma at the lake, 2019" — through a Photos launcher.

Part 1 stands alone and is useful on day one; part 2 reuses all of it.

---

## 1. How identity works, and why not "just ask the vision model"

Apple Photos, Google Photos, Immich, PhotoPrism and digiKam all use one pipeline:
**detect** each face → **align** it → **embed** it as a vector, where the same person lands
close together across lighting, angle and years → **name** a group once → **match** every
new face by cosine similarity against the named people → **feed corrections back**.

Handing a vision model labelled reference photos and asking "who is this?" is the
tempting shortcut, and it is the wrong one. VLMs are not trained for identity: they name
confidently and wrongly, and often refuse. It is also expensive — a reference image costs
~2,048 tokens on Flash-Next, so three references for twelve people is ~72k tokens on every
photo.

**The split here:** InsightFace decides *who*; Flash-Next gets the answer as text —
`face 1 [box] = Emma (0.71, strong); face 2 [box] = unknown` — and does the describing.
The model is told never to put a name on an `unknown`.

---

## 2. Technology

| Layer | Choice | Notes |
|---|---|---|
| Face detect + embed | **InsightFace `buffalo_l`** (SCRFD detector + ArcFace R50, 512-d) | CPU through ONNX Runtime; tens of ms per face. Chosen over OpenCV's SFace for children, profiles and faces that age. Model weights are licensed for non-commercial use — fine for a personal box. |
| Where it runs | The existing **`rapidocr` sidecar**, extended | Already CPU-only ONNX Runtime + OpenCV, already does face *detection* (YuNet, `deploy/rapidocr/server.py`). New routes: `/faces/embed`. Weights (~280 MB) come from the weights volume like every other model, never baked into the image. Lazy-loaded and idle-freed like the OCR engine, so it costs nothing while unused and never touches the GPU Flash-Next holds. |
| Vision + reasoning | **Flash-Next** (text + image + video) | Captions, OCR and the archive's residual dating all run here; no separate small VLM. |
| Image search | CLIP-class image/text embedding | Part 2 only; on the same sidecar. |
| Database | PostgreSQL + `pgvector`, the same store as the RAG index | One query path. |
| Metadata | `exiftool` | Part 2: EXIF dates, GPS, filename-date backfill. |

---

## 3. Part 1 — Know the family

### Data model

```sql
-- One row per enrolled person. Linked to the knowledge graph so "Emma" in a photo,
-- in a note and in the wiki is one person.
people (
  id          uuid primary key,
  entity_id   uuid references app.entities(id),  -- the graph's person node
  display     text not null,
  created_at  timestamptz default now()
)

-- The reference faces the owner enrolled: 3-10 per person, spread across ages and angles.
face_refs (
  id           uuid primary key,
  person_id    uuid references people(id) on delete cascade,
  embedding    vector(512) not null,       -- ArcFace, L2-normalised
  crop_sha256  text not null,              -- aligned 112x112 crop, via the storage abstraction
  source_sha   text,                       -- the photo it came from
  taken_at     timestamptz,                -- EXIF date: a child's face changes with age
  det_score    real,
  created_at   timestamptz default now()
)
```

Both tables are owner-only under RLS with a jmolt restrictive deny, each with an isolation
test. Deleting a person cascades away every embedding and crop. **Face data never leaves
the box:** crops and embeddings are never sent to a cloud model, and identification runs
only on the local sidecar.

### Matching

1. Detect and embed every face in the photo.
2. Score each person by their **best single reference** (max cosine), not a mean prototype
   — a toddler-era reference should still match a toddler photo.
3. Three bands, thresholds measured on the family's own photos at build time:
   **strong** → the name; **near** → "possibly Emma"; **below** → `unknown`.
4. Only names, boxes and bands reach the model. Faces too small or blurred to embed well
   (low detector score, under ~40 px) are reported as `unrecognisable`, not guessed.

### Where it shows up

- **Chat.** When a photo reaches jerv, identification runs automatically and its result
  rides in as text beside the image. `canvas` can then label each box with the name.
- **A tool.** `identify_people(source_attachment_id)` for jerv to call explicitly, and for
  video frames later.
- **Enrolment from chat.** "Number the faces" → canvas draws 1..n → "2 is Emma" → a
  `face_refs` row. The owner confirms before anything is stored.

### Identical twins

The family includes identical twin girls, and a generic face model cannot tell them apart.
ArcFace is trained to separate *different people* by facial structure, and identical twins
share almost all of it. Their twin-to-twin similarity lands in the same range as two photos
of one girl, so no threshold splits them without also failing to recognise each as
herself. Published twin evaluations of commercial matchers found the same: identical twins
routinely score as one person. Children make it harder still — less developed faces that
change quickly. What does tell these two apart is small and real: **one has a facial mole
the other does not, and they smile slightly differently.** The design stacks four layers so
the answer is right when it can be and honest when it can't.

1. **A pair, not two people.** The twins are enrolled as two people and marked as a
   *twin pair*. A face that matches either one is first resolved only to the pair:
   "one of the twins". Names are never assigned by ArcFace's margin between the two.
2. **Both in one photo.** When two faces both match the pair, the photo has both twins —
   stated with confidence even when which-is-which is not, and the two are always assigned
   different names (one assignment over the pair, never the same name twice).
3. **The tiebreaker — a classifier for these two only.** ArcFace deliberately throws away
   skin texture and expression, which is exactly where the difference lives, so the
   tiebreaker reads the face again:
   - **Input:** the aligned face crop at a higher resolution than ArcFace's 112 px
     (224–448 px, so a mole is several pixels across), taken from the same detection.
   - **Model:** a general image embedding that keeps fine texture (DINOv2-class, CPU,
     same sidecar) with a small two-class head trained on the family's own labelled photos
     of the twins. Expression is kept, not normalised away, so the smile difference is
     learnable; smiling and neutral photos are both needed in training.
   - **Data:** labels come from enrolment and from every correction in chat ("that's
     Twin B"). It does not run until each twin has a minimum labelled set (~30 photos,
     both expressions), and retrains as labels arrive, weighting recent photos — their
     faces will keep changing.
   - **Trusted only when measured:** held-out accuracy is shown in the People launcher,
     and the tiebreaker names a twin only above a confidence bar set from that held-out
     set. Below it, the answer stays "one of the twins".
4. **The mole check — a known mark at a known place.** In the People launcher the owner
   marks the mole on a reference face. It is stored relative to facial landmarks, so it
   can be found again on any aligned face. When a face is sharp and large enough
   (inter-eye distance over a measured minimum), the sidecar inspects that patch for a
   dark spot against the surrounding skin. A clear mark names that twin; a clean patch on
   a sharp face names the other. The check abstains on blur, a low angle, shadow or makeup.
   - **Mirrored selfies.** Front cameras often save a mirror image, which moves the mole
     to the other side. The check looks on both sides and only trusts the side that
     matches the photo's orientation when EXIF or the camera says which it is; when it
     can't tell, it reports presence but not which side.

**Combining them:** name a twin only when the evidence agrees — the mole check and the
tiebreaker pointing the same way, or one of them confident while the other abstains. If
they disagree, the answer is "one of the twins". Within one burst or event, a twin named
with confidence (or by the owner) carries to the same child in the rest of the set by
clothing and hair. Standing hints the owner gives ("Twin A's hair is shorter right now")
go to Flash-Next as text, with an expiry, since hair and clothes change.

### The People launcher

A launcher app, like Images: the family, and nothing else.

- **People grid** — one card per person: name, a representative face, how many references.
- **Person page** — their reference faces; **add** by uploading photos (the sidecar
  detects faces; the owner picks which one is this person when there are several);
  **remove** a bad reference; **rename**; **link** to the graph's person node; **delete**
  the person and all their data.
- **Twin pair** — mark two people as identical twins; **mark a distinguishing feature**
  (the mole) on a reference face; see the tiebreaker's held-out accuracy and how many
  labelled photos each twin still needs.
- **Quality hints** — warn when a person has fewer than three references, or none from
  the last few years (children).
- **Test a photo** — drop a photo, see who it would recognise, at what band. This is how
  thresholds are checked without a terminal.

GUI gate: three mocks, owner picks, before any of it is built (`docs/reference/DESIGN.md`).

---

## 4. Part 2 — The archive and the Photos launcher

The archive is a **staged, idempotent map over files**, not an agent loop over images.
Cheap deterministic work runs on every file, model work runs on every file, and the costly
reasoning runs **only on the residual** — what still lacks a date after the cheap passes.

### Data model

```sql
-- One logical asset per unique file content: dedup falls out of the key.
assets (
  id                      uuid primary key,
  sha256                  text unique not null,
  mime                    text,
  width                   int,
  height                  int,
  captured_at             timestamptz,
  captured_at_source      text,        -- exif | filename | inferred | unknown
  captured_at_confidence  real,
  captured_at_rationale   text,        -- why, when inferred
  category                text,        -- photo | screenshot | meme | document | receipt
  caption                 text,        -- Flash-Next
  ocr_text                text,
  image_emb               vector(768), -- CLIP
  status                  jsonb,       -- which stages are done
  created_at              timestamptz default now()
)

asset_paths ( asset_id uuid references assets(id), path text, present boolean default true )

-- Every detected face in the archive. person_id is set by matching against face_refs,
-- or left null (see open decision 1 on unknown faces).
faces (
  id         uuid primary key,
  asset_id   uuid references assets(id) on delete cascade,
  bbox       int[],
  embedding  vector(512),
  person_id  uuid references people(id),
  score      real,          -- best match cosine
  det_score  real
)
```

**Why hash-keyed:** an exact duplicate only adds an `asset_paths` row and is never
reprocessed. Near-duplicates (re-compressed messenger copies) are caught later by CLIP
similarity as a review queue.

### Pipeline

```
read-only inbox
   │ ingest_inbox()        hash, dedup, write assets + paths
   ▼
[CHEAP · all files]        no model
   extract_exif()          date, GPS, camera, dimensions
   date_from_filename()    IMG_2015…, Screenshot_…, WhatsApp patterns
   ▼
[MODEL · all files]        sidecar + Flash-Next, overnight batch
   classify()              photo / screenshot / meme / document / receipt
   caption() / ocr()       text for search and reasoning
   embed_image()           CLIP vector
   detect_faces()          box + ArcFace embedding per face, matched to face_refs
   ▼
[COSTLY · residual only]   Flash-Next + RAG over the owner's notes
   infer_date()            caption + OCR + GPS + people → notes → a date range
   ▼
assets ──► search tools ──► Photos launcher
```

The unique unlock is the last stage: dating a photo by cross-referencing the owner's own
notes ("we moved to the blue house in 2019") and who is in it. A pixel-only pipeline can't.

### Tools (granular, returning IDs and counts — never blobs)

- `ingest_inbox(path)`, `pipeline_status()`
- `extract_exif(ids)`, `date_from_filename(ids)`
- `classify(ids)`, `caption(ids)`, `ocr(ids)`, `embed_image(ids)`, `detect_faces(ids)`
- `infer_date(id)` — writes source=`inferred`, never overwrites a real EXIF date
- `search_text(query)`, `search_person(name)`, `search_similar(id)`, `get_assets(filters, page)`

### The Photos launcher

- **Timeline** — a chronological grid; **inferred dates drawn distinctly** so a guess
  never reads as fact.
- **People filter** — tap a family member → every photo of them (from `faces.person_id`).
- **Search** — free text through CLIP ("beach sunset 2016"), combinable with people/date.
- **Similar** — "more like this", which doubles as near-duplicate review.
- **Asset detail** — the photo, its faces labelled, caption, OCR text, metadata, and the
  inferred-date rationale when there is one.
- **Queues** — duplicates to resolve, inferred dates below the confidence bar.

Browsing reads the database directly and never waits on a model. Its own GUI gate.

---

## 5. Principles

1. **Identity comes from embeddings, never from the VLM's guess.** Unknown stays unknown.
2. **Face data stays on the box** and is deletable per person, completely.
3. **Staged and idempotent.** Every archive stage acts on the undone set and is re-runnable.
4. **Inferred ≠ known.** A guessed date carries its source, confidence and rationale.
5. **Immutable inbox.** Originals are read-only; derived data goes to the DB and the
   storage abstraction.
6. **No terminal.** Enrolment, thresholds, the archive's progress and its failures are all
   visible and operable from the PWA (`CLAUDE.md` #10).

---

## 6. Open decisions

1. **Unknown faces.** Store unnamed faces so they can be grouped and named later (the full
   Photos/Immich model), or drop them so nobody outside the enrolled family is ever kept?
   Leaning: chat drops them; the archive keeps them but only on owner opt-in.
2. **Thresholds.** Measured on the family's own photos at build time through the People
   launcher's "test a photo"; the bands are per-box settings, not constants.
3. **Archive scale and library layout.** How big is the backlog, where do originals live,
   and are they copied or referenced in place.
4. **CLIP model choice** for part 2, and whether it shares the sidecar or gets its own.

---

## 7. Build order

1. **P1 — sidecar.** InsightFace on the `rapidocr` sidecar: detect + embed, weights through
   the weights volume, lazy load and idle free. Debug route to run it on an attachment.
2. **P2 — store and match.** `people` + `face_refs` with RLS tests; matching with the three
   bands; `identify_people`; the chat hook so every photo jerv sees carries its names.
3. **P3 — People launcher** (GUI gate first): enrol by upload, remove, rename, link, delete,
   test a photo. Enrolment from chat.
4. **P4 — the twins.** Pair rule and both-in-one-photo assignment (cheap, ship with P2's
   matching if possible); then the mole check and the two-class tiebreaker once enough
   labelled photos exist, each shown with its measured accuracy before it is trusted.
5. **A1 — archive spine.** `assets` / `asset_paths`, ingest + dedup, EXIF + filename dates.
6. **A2 — model passes.** classify, caption, OCR, CLIP, faces across the archive.
7. **A3 — Photos launcher** (GUI gate first): timeline, people filter, search, similar,
   detail, queues.
8. **A4 — residual dating** over the owner's notes.
</content>
</invoke>
<invoke name="Bash">
<parameter name="command">cd /home/user/JBrain2 && python3 - <<'EOF'
import pathlib,re
p=pathlib.Path("docs/proposed/README.md"); s=p.read_text()
old=s[s.index("- `PHOTO_ARCHIVE_PLAN.md`"):s.index("- `MUSIC_GEN_PLAN.md`")]
new="""- `PHOTO_ARCHIVE_PLAN.md` — photos and people: family-first face recognition (InsightFace
  `buffalo_l` on the `rapidocr` sidecar, ~a dozen enrolled people, identity from embeddings
  and never from the VLM, face data never off the box) surfaced to jerv as names beside each
  photo, a **People** launcher to enrol the family, then the archive — hash-keyed dedup,
  deterministic dating, Flash-Next captions, CLIP search, notes-backed date inference — and a
  **Photos** launcher to browse it.
"""
s=s.replace(old,new); p.write_text(s)
EOF
grep -n "Last verified" docs/proposed/README.md | head -1; bash scripts/docs-freshness.sh 2>&1 | tail -2