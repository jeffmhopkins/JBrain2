# Report share links — GUI gate

Binding mock for the public report share-link surfaces (owner mint/manage + the public
recipient view). Three directions were presented (A · Quiet, B · Share sheet, C ·
Manage-first); the owner chose **B**. `chosen-b-share-sheet.html` is the interactive mock —
open it and use the top switcher (Variant × Side × theme); B is the binding path.

**B — Share sheet (chosen):**
- **Owner.** A report's ⋯ action sheet gains **Share**, which opens a sheet with the link, a
  Copy button, a "public · read-only · no expiry" line, the link's view count + last-opened,
  and a **Revoke** action. A report written from private notes (`source_mode`
  `library`/`library_first`) shows an amber **warning** in the sheet (warn + confirm, not a
  hard block). Folders get their own **Share folder** button in the folder header.
- **Public.** A shell-less page: an amber "shared research · read-only" band for context, then
  the report rendered with the existing report renderer (`ReportDetailBody` + `Markdown`). A
  folder link renders a banner + report **cards** that open each report through the same link.

Tokens only, amber = research/read-only, steel = links (per `docs/reference/DESIGN.md`).
Backend: migration 0150 + `api/research_share.py` (public) + share routes on the research
library router. Build record: `docs/plans/RESEARCH_SHARE_LINKS_PLAN.md`.
