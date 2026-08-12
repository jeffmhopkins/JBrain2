---
name: science_search
version: 1
permission: web
params:
  type: object
  properties:
    query:
      type: string
      description: The scholarly topic to search for — the concept, method, finding, drug, gene, or paper title. Use the field's real terms; author names and a year help narrow it.
    limit:
      type: integer
      description: Maximum number of results (default 6, max 10).
  required: [query]
---
Search the SCHOLARLY literature — arXiv, PubMed, Semantic and Google Scholar, and
Crossref — and return paper leads, each with its title, authors, publish date, URL, and
an abstract snippet. Use this instead of web_search whenever the question deserves a
primary source: a study's actual findings, a method, a drug or gene, a theorem — anything
where a peer-reviewed paper or preprint beats a blog's summary of it.

CRITICAL — a result is a LEAD, not the finding. The abstract snippet is an UNVERIFIED
preview and must NEVER be reported or cited as the paper's result on its own. Before you
treat anything as established, web_fetch the paper and read it: arXiv and PubMed/PMC pages
are open and usually carry a free PDF or full text. Note the publish date and venue — and
when both a preprint and a peer-reviewed version exist, prefer the peer-reviewed one; flag
a preprint as not-yet-reviewed.

Report a claim with only the specificity and certainty its paper carries — a single study
is not settled science; say "one study found", not "it is known". Prefer a review or
meta-analysis for the state of a field, a primary study for a specific result. These are
public papers, not the owner's notes — cite the paper you fetch.
