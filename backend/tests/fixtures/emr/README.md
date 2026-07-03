# Synthetic EMR fixture corpus

**Every byte here is synthetic.** No real patient name, MRN, accession, provider,
facility, or lab value appears in this directory. The real EMR exports are PHI and
are never committed (plan `docs/plans/EMR_IMPORT_PLAN.md` §2, §9). These fixtures
reproduce the *layout hazards* of each source system so the deterministic parsers
(Wave 2–3) and the integration/supersession path (Wave 1–2) can be tested without
patient data.

The synthetic patient is **Jordan A. Rivers**, MRN **55500123**, blood type **O
positive**, born **1971-03-14**. All facilities, providers, accession ids, and
analyte values below are invented.

## Files and the hazard each one encodes

| File | Source | Text layer | Hazards exercised (plan §) |
|---|---|---|---|
| `epic_report.txt` | Epic "EMR Report" | structured | banner bleed §2.1; inpatient-vs-outpatient by banner mode; MICU→A3 **facility transfer** (§3.4 `partOfEncounter`); transfusion orders → `transfusion` events; the bone-marrow **pathology narrative** kept as prose (§6.5) |
| `onecontent_account.txt` | OneContent | fixed-width columns | **ACCOUNT-keyed** grouping (not Adm/DC); per-row `collected_at` sets `valid_from`; abnormal-flag legend → `interpretation`; the fixed-width-column recovery hazard (§6.2) |
| `onecontent_words.json` | OneContent | word x-geometry | the `get_text("words")` word-box view of the SAME page, whose reading-order `text` offsets are deliberately reflowed — proves char-offset ruler slicing misaligns and **geometry slicing** recovers columns (§6.2 go/no-go) |
| `athena_panel.txt` | athena | structured | per-analyte `Specimen/Accession ID` blocks; explicit `Ordering Provider` → ambulatory encounter `attender[ordering]`; a **cancelled** `RESULT NOTE` that suppresses its value (§6.3) |
| `aria_ocr.txt` | ARIA (post-OCR) | none (canned OCR text) | line-oriented portal reprint, OCR noise; a **duplicate of the 2021 OneContent labs** for cross-source dedup (§6.4); one **readable-but-wrong** timestamp that must park in review |

## Page delimiters

Text fixtures use a line `--- page N ---` to delimit what each PDF page's
`get_text("text")` Segment would contain. Fixture loaders split on that marker to
reconstruct per-page `Segment`s (`ingest/extract.py` `Segment(kind, text, anchor="page N")`).

## What is deliberately absent

No discharge summaries, H&P, progress notes, **medication lists**, imaging, or
**vital signs** — the corpus lacks them and the parsers must not invent them
(plan §2). `aria_ocr.txt` intentionally contains no ordering provider (orphan
portal reprint) so the orphan-`encounter_id` path is covered.
