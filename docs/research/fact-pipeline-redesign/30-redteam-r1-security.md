# Red-team round 1 — Security, RLS & domain-firewall leakage

**Lens:** SECURITY / RLS / DOMAIN-FIREWALL LEAKAGE (adversarial).
**Target:** `20-spec-v0.md` (synthesis v0), reconciled against `00-framing.md` §4
invariants. Consulted: `10-research-F-security.md`, `10-research-C-corrections.md`.
**Method:** assume the note author is the primary adversary (intake links, forwarded
email bodies, OCR'd images, web clips all become LLM input), the extractor LLM is
attacker-steerable, the owner is fallible/fatigued, and a `general`-scoped surface
(shared view, leaked wiki render, stolen general token) is the exfil sink. Goal:
move a health/finance/location *value* into a general-readable position, or write a
row with the wrong `domain_id`, or corrupt the audit/undo state.

**Severity:** SEV-1 = a firewall breach or an unauthorized write that crosses a
band · SEV-2 = a leak/integrity defect that is exploitable but bounded or needs a
precondition · SEV-3 = nit / hardening gap.

Headline: the propose/commit split and per-domain projections are the right spine,
but **v0 leaves the *cross-domain identity resolver* (F open-Q 1/2) completely
unspecified, and it is the single asset that re-introduces every leak the projection
model claims to kill.** Three of the four SEV-1 findings route through it or through
the audit/undo plane. Each firewall has a concrete chain below.

---

## SEV-1 findings (one chain per firewall, plus the resolver)

### S1 — [HEALTH] The cross-domain identity resolver is a read-oracle (confused-deputy)

**Severity: SEV-1 (firewall read across health→general).**

**Where v0 is silent:** §3.3 and §7(a) adopt F's per-domain projection model and
*explicitly defer* the privileged cross-domain resolver ("is this the same Dad?") to
F open-Q 1/2 — "a new high-value asset" with no design. §2.4's `candidate_ids` and
the integrator's link step both require deciding, across domains, whether a general
"Dad" projection and a health "Dad" projection share a `canonical_id`. *Something*
must hold both domains' data to answer that. v0 names the asset and walks away.

**Attack chain (general session as the adversary surface):**
1. Owner has a health projection of "Dad" carrying a sensitive health display name
   variant or alias (e.g. a clinic-assigned identifier used as an alias, or "Dad
   (oncology patient #…)"). Adversary controls a general-domain intake note.
2. Adversary's note seeds many general "Dad"-like mentions with crafted surface
   strings and spans, forcing the integrator to invoke the cross-domain resolver
   repeatedly to decide candidate identity.
3. The resolver, to decide "same Dad," computes a similarity/embedding/blocking-key
   comparison between the general mention and *every* candidate canonical, including
   the health projection's name/aliases. Its **observable behavior is the oracle**:
   - *Error/timing channel:* a match against the health alias resolves the
     `entity_identity` row and short-circuits; a non-match runs the full candidate
     scan. The general session observes latency or a "linked to existing" vs "minted
     new" outcome — a binary oracle over the secret health alias (attack F-2's
     chosen-plaintext oracle, now *across* the firewall via the resolver instead of
     via relink).
   - *Confidence/candidate-rank leak:* if the resolver writes any
     candidate-confidence or "matched-on" signal into the general-readable
     `candidate_ids`/audit, the general side reads a function of the health name.
4. Iterating the crafted surface (binary search over alias substrings) reconstructs
   the protected health alias **without ever reading a health row** — RLS never
   trips because the resolver, by construction, is the one component scoped to both.

**Why projections don't save you:** F's model kills the *FK* and the *relink* oracle
but the resolver is the *replacement* for both and v0 never gave it a security
contract. The projection model's entire SEV advantage (§7(a) "what would flip it")
is conditional on this resolver not being an oracle — and v0 ships it unbuilt.

**Mitigation (must-fix before sign-off):**
- The resolver is a **separately-privileged, audited, rate-limited service** that
  (a) never returns a cross-domain *attribute*, only an opaque `canonical_id`
  match/no-match boolean; (b) is **constant-time / constant-work** w.r.t. the
  protected side (always run the full candidate set, decoy-padded, no early-exit on
  health match) to kill timing; (c) emits **no** candidate-rank, confidence, or
  "matched-on" signal into any general-scoped row — `candidate_ids` for a general
  fact must list only *general* projections; (d) its own invocations are
  rate-limited per session and audited in *both* domains (so a binary-search attack
  is forensically visible and throttled); (e) the *decision* to merge identities is
  an owner-gated `identity_merge` op (already §7(g)-adjacent), never an automatic
  integrator side-effect from untrusted-note volume. Demand an isolation test that
  the resolver leaks no attribute and no timing signal (S-test 5 below).

---

### S2 — [FINANCE] `move_domain` undo→retime laundering survives copy-forward (op-log as laundering channel)

**Severity: SEV-1 (unauthorized cross-band write / value laundered to general).**

**Where v0 is silent/soft:** §7(f) provisionally adopts F's copy-forward downgrade
and *itself flags* that a `move→undo→retime` sequence might launder a value
(F open-Q 3) — but v0 does **not** specify undo's interaction with `retime` and
`supersede`, and §4.1 makes `retract`/`supersede`/`retime` all *first-class
extractor- or human-emittable ops on the live floor*. The laundering chain exploits
the gap between "copy-forward is reversible" and "the inverse is a stored op replayed
later."

**Attack chain (finance→general):**
1. Owner downgrades **one** finance fact (a transaction amount) to general via the
   gated copy-forward: a new general fact G is minted citing finance fact P as
   provenance; P is marked `superseded`; audit rows in both domains; inverse op I
   stored (I = "retract G, restore P").
2. Owner (or an injected review card, see S4) **undoes**: I runs, G is retracted, P
   restored. So far so good — but G's `assertion_id` row still exists as
   `state=retracted` and its general-domain audit row persists.
3. Adversary now issues, on the *general* fact G (which is general-domain and thus
   within an *extractor/human-general* op's allowlist), a `retime` or `unretract` /
   `supersede` that re-derives a **new** general live row from G's retained
   value_json — *without re-invoking `move_domain` at all*. The finance value is now
   live in general again, but its lineage points at G (a general row), not at finance
   P. The both-domain audit that made the *original* move visible **does not fire**,
   because this second write is a pure intra-general op on a general row.
4. Net: the finance value is general-live; the finance side shows P merely
   `superseded` then `restored`; the move is no longer forensically attributable.
   The copy-forward's "reversible + audited in both domains" guarantee is defeated
   because the *retained general row* is a re-attachable handle.

**Root cause:** copy-forward leaves a general-domain row carrying the protected
value (G, even retracted) that intra-general ops can resurrect, and v0 never says
"a retracted move-forward row's value_json is purged / the row is a tombstone with
no resurrectable value." §7(f)'s own "what would flip it" condition (a surviving
move→undo→retime) **is met** — by spec, downgrade should become **(iii) one-way**.

**Mitigation:**
- Adopt §7(f) option **(iii): downgrade is one-way.** No stored inverse that
  restores a *general* value-bearing row. Undo of a downgrade must **purge G's
  value_json** (tombstone, not retract) so nothing intra-general can resurrect it;
  re-protecting requires authoring a *new* fact, not unretracting G.
- `unretract`/`retime`/`supersede` must be **forbidden on any row whose
  `created_by_op` is a `move_domain`** (lineage flag), so a moved row is never a
  re-attachment handle.
- Demand isolation test S-test 4 (move→undo→retime corpus) as a CI gate.

---

### S3 — [LOCATION] Location object-link + `add_fact` masquerade builds a movement oracle in general

**Severity: SEV-1 (location pattern reconstructed in general band).**

**Where v0 is silent:** F open-Q 5 flags "can a location fact be a link object for a
general fact ('Me —visited→ place')? That edge could be a movement-pattern oracle" —
v0 **never answers it**. §2.2's `Ref`/`canonical_id` model lets a general fact's
object projection share a `canonical_id` with a location projection (that's the whole
point of cross-domain identity). And §4.1's `add_fact` lets a human/agent mint a
general fact the extractor never produced, with `human_assertion` provenance.

**Attack chain (location→general):**
1. Location domain holds the owner's place visits (device-key sensor feed: home,
   clinic, a specific address at specific times).
2. Adversary (or a compromised agent actor, since §4.1 lists `agent` as an op source
   and ASSISTANT.md self-improving agents emit ops) issues `add_fact` general
   relationship facts `Me —visited→ <place>` whose object projection is *minted in
   general* but **associated to the same `canonical_id` as a location place
   projection** (the §2.4-rule-3 "associate to existing canonical_id" path).
3. Now a *general* edge exists whose object canonical-thread points at a location
   place. v0's R3 says rendering dereferences only the *general* projection's name —
   but the **existence and timing** of the general `visited` edges, correlated across
   many `add_fact`s, reconstructs the movement pattern *in the general band*. The
   adversary doesn't need the location *attribute*; the general-side graph of
   visited-canonicals over time **is** the movement oracle F warned about.
4. `add_fact`'s `human_assertion` provenance launders the origin: the general edges
   look human-asserted, not derived from location, so no firewall check ever
   correlates them with the location feed.

**Why this slips through:** every individual `add_fact` is intra-general and passes
`WITH CHECK` (general domain, in-scope general projection). The leak is **emergent
across rows via the shared `canonical_id`**, which v0's per-row materialization rule
(R3) does not police. The cross-domain thread is supposed to be opaque, but
*creating* the association at general-fact-mint time (§2.4 rule 3) is where location
identity bleeds into the general band's structure.

**Mitigation:**
- **No general fact may associate its object to a `canonical_id` that has a
  location/health/finance projection** without going through the owner-gated
  `identity_merge` op (close the §2.4-rule-3 side-door for protected canonicals).
  The committer must check the candidate `canonical_id`'s cross-domain footprint —
  which means the resolver (S1) must answer "does this canonical have a protected
  projection?" as a *gated, audited* query, never an integrator convenience.
- `add_fact` from an `agent` source is **allowlisted away from cross-domain
  canonical association** entirely; only owner principal may create such a link, with
  the §3 downgrade-grade confirm.
- Treat "general edge whose object canonical has a location projection" as a
  first-class firewall-test fixture (S-test 7).

---

### S4 — [INTEGRITY] Confused-deputy committer: model/human-claimed domain on `add_fact` + provenance-domain mismatch

**Severity: SEV-1 (row written to wrong domain band).**

**Where v0 is soft:** §6 says the committer "ignores any `domain_id` the model
claims and re-derives domain from the operands (subject projection's domain,
provenance note's domain)." But §4.1's `add_fact` and §2.5's `source_kind=
"human_assertion"` create a fact whose **provenance is the correction op itself, not
a note** — so "re-derive from provenance note's domain" has *no note to derive from*.
And `add_fact`'s subject may be a freshly minted projection whose domain is whatever
the op *claims*. The committer's two re-derivation anchors (subject-domain,
provenance-note-domain) **both collapse** for `add_fact` with human/agent provenance.

**Attack chain:**
1. A compromised `agent` actor (or an injection that reaches an agent's op-emission
   path) issues `add_fact` with `source_kind="human_assertion"`, a *minted* subject
   projection, and a value copied from an observed health fact.
2. There is no provenance *note* (provenance cites the op), and the subject is newly
   minted, so the committer cannot re-derive a protected domain from operands — it
   falls back to the **op-claimed** `domain: general`. RLS `WITH CHECK` passes
   because the claim *is* general and the session *is* general-scoped. The health
   value is now a general fact, written legitimately.
3. Because §7(e) provisional (i) keeps `add_fact` with only a *flag*, and §7(e)'s own
   "what would flip it" is "attribution is droppable downstream so a human-asserted
   fact can masquerade as machine-extracted" — the laundered value now reads as an
   ordinary general fact.

**Root cause:** the committer's domain re-derivation is **only sound when an operand
carries an authoritative domain** (a real subject projection or a real provenance
note). `add_fact`/`human_assertion` is precisely the op where both anchors are
attacker-supplied, so the "ignore claimed domain" defense has nothing to fall back
to and silently trusts the claim.

**Mitigation:**
- `add_fact` **must cite a real, in-scope provenance note/span** (Track C's *other*
  option in §2.3: "a human-authored fact with no source is rejected by the arbiter").
  Reject the "cite the op as source" escape for any fact entering a state where
  domain must be re-derived. If there is genuinely no note, the subject must be an
  **existing** projection whose domain is authoritative — minting subject *and*
  claiming domain in one op is forbidden.
- The committer must **fail closed when it cannot independently re-derive domain
  from a non-op-controlled operand** — never fall back to the claimed value.
- `add_fact` attribution (`human_assertion`) must be a **non-droppable, indexed
  column** carried on the row and every downstream projection, with an isolation
  test that a human-asserted fact is distinguishable from machine-extracted at every
  read surface (S-test 8). Per §7(e) "what would flip it," this finding **forces
  option (ii)** unless attribution is provably non-droppable.

---

## SEV-2 findings

### S5 — Prompt-injection: extractor emits in-scope `relink_object` that the committer faithfully applies (SEV-2)

The op-allowlist (§6, F §4.2) lets the extractor emit `relink(in-scope only)`. An
injected note ("…and Sam now works for the place Dad sees for treatment…") steers the
extractor to *relink an existing general edge's object to a different in-scope general
projection* that the adversary controls or can read. This is **not** a cross-firewall
write — but it is an attacker *choosing graph shape* via untrusted prose, and if the
chosen general projection shares a `canonical_id` with a protected projection it
becomes the S1/S3 setup. **Mitigation:** relink ops sourced from `extractor` should be
*proposals routed to review*, never auto-committed, when the new object's canonical
has any cross-domain footprint; and the integrator-emitted `relink` must re-verify the
candidate set was not adversarially seeded (candidate-rank consistency, D §3 D-class,
extended to canonical footprint).

### S6 — Audit table read is itself a cross-change oracle on batch/timing (SEV-2)

§3.2 scopes `fact_audit` by `domain_id = ANY(current_domains())`, so a general
session can't read a health audit *row*. But §4.3's `batch_id` groups a review
session's ops as "jointly undoable," and §7(f) writes move audit in *both* domains. A
general session that can see the **general half** of a both-domain move audit learns
*that a protected fact was downgraded and when* — and via `batch_id` correlation,
*how many* protected facts moved together. That is metadata leakage about protected
activity. **Mitigation:** the general-domain audit row for a `move_domain` must be
**redacted to the moved value only** (which is now legitimately general) and must
**not** carry `batch_id`, source-domain, or sibling-count; cross-domain audit
linkage lives only in an owner-scoped (all-domains) view.

### S7 — `set_field{domain}` still latent in the op vocabulary (SEV-2)

§7(g) provisionally removes `domain` from `set_field` in favor of `move_domain` —
but §4.1 group A and C §2.3/§2.4 **still list `domain` as a `set_field` field**, and
the storage `op_kind` enum in §3.2 lists both `set_field` and `move_domain`. If *any*
code path or schema validates `set_field{field:"domain"}`, it is an in-place
`domain_id` flip that bypasses copy-forward, both-domain audit, and the owner gate —
the exact confused-deputy F §3.4 rule 4 forbids ("does not flip `domain_id` in
place"). **Mitigation:** `domain` must be a **schema-level illegal value** for
`set_field`'s field discriminator (closed enum excludes it), enforced by the op
schema and a unit test, not just by convention. This is a hard pre-sign-off gate, not
a nit.

### S8 — Pinned-row + reprocess interaction can launder a human-asserted value past migration (SEV-2)

§3.2 says a `pinned` row can't be superseded by `reprocess`, only human/agent. Combine
with S4: an `agent`-asserted general fact (laundered protected value) that is then
`pin`ned becomes **immune to the re-analysis migration** (D §4) that might otherwise
diff it against source notes and flag it as unsourced. Pinning is the *persistence*
mechanism for a laundered fact. **Mitigation:** `pin` on a `source_kind=
"human_assertion"`/`agent` fact requires owner principal (not agent), and pinned
human-asserted facts are *included*, not exempt, in the unsourced-fact watch-metric
(§7(e)) and migration diff.

---

## Positions on the open conflicts (§7)

**(a) Global entity+redirect (B) vs per-domain projections (F).**
**Position: per-domain projections (F's (ii)), NOT the hybrid (iii) as written, and
ONLY if the cross-domain resolver gets a security contract (S1).** The hybrid's
"attribute-free global canonical thread" sounds safe but §7(a)'s own flip-condition
(the resolver is an unavoidable oracle) **is realized** — S1 and S3 both weaponize the
`canonical_id` thread itself, attribute-free or not. The opaque thread is not benign:
*associating* to it (§2.4 rule 3) and *resolving* over it are the leaks. So adopt
projections, but treat `canonical_id` as a **gated capability**, not a free join key:
no general fact may bind to a protected canonical without an owner `identity_merge`,
and the resolver is constant-work, attribute-blind, and audited. Reject B's global
table outright (FK-bypass is a documented Postgres leak, invariant-fatal).

**(e) `add_fact` op vs forced correction-note.**
**Position: force the correction-note round-trip (option (ii)) for any `add_fact`
that cannot cite a real in-scope provenance note, OR whose subject is newly minted.**
S4 shows the direct-`add_fact` "cite the op as source" escape is precisely the case
where the committer's domain re-derivation collapses, and §7(e)'s own flip-condition
(masquerade as machine-extracted) is achievable. Direct `add_fact` is acceptable
**only** when it cites an existing note/span and an existing subject projection (so
domain is operand-derivable); otherwise round-trip the extractor. Attribution must be
non-droppable and indexed.

**(f) Domain-move reversibility / laundering.**
**Position: one-way downgrade (option (iii)).** S2 demonstrates a surviving
move→undo→retime laundering chain through the retained value-bearing general row,
which is exactly §7(f)'s stated flip-condition to (iii). Copy-forward stays for the
*mechanism*, but undo must **purge the general value (tombstone)**, and `retime/
unretract/supersede` are forbidden on `move_domain`-lineage rows. Re-protecting after
a mistaken downgrade is authoring a new fact, not unwinding.

**(g) `set_field` super-op hiding firewall risk.**
**Position: split `domain` out (option (ii)) — and go further: make `domain` a
schema-illegal `set_field` field, not merely "not offered."** §7(g)'s provisional pick
is right but underspecified; S7 shows the latent `set_field{domain}` path still exists
in §4.1/§3.2. An op-type allowlist is only as strong as the guarantee that the
high-risk action *cannot be expressed* as a discriminator value inside a permitted op.
The closed op schema must make `field ∈ {predicate,qualifier,value,modality,kind,
confidence,...}` with `domain` *absent*, enforced by validation + test.

---

## Required isolation tests (must ship with the code; CI-gated, real Postgres via testcontainers)

Beyond F §5's seventeen, the chains above demand these **new** adversarial tests:

- **S-test 1 (S4 — confused-deputy fail-closed):** an `add_fact` with a minted
  subject and op-as-provenance and a *claimed* `domain` that mismatches any
  operand-derivable domain → **rejected**; assert no row written. Variant: agent-
  sourced `add_fact` claiming `general` over a value copied from a health row → rejected.
- **S-test 2 (S7 — `set_field{domain}` unrepresentable):** the op schema **rejects**
  `set_field` with `field="domain"` at validation; assert no committer path accepts it.
- **S-test 3 (S6 — audit metadata redaction):** `S_general` reading the general-side
  audit of a `move_domain` sees the moved value only — **no** `batch_id`,
  source-domain, sibling-count, or timing that distinguishes a 1-fact from an
  N-fact move.
- **S-test 4 (S2 — move→undo→retime corpus):** for each of {downgrade→undo→retime,
  downgrade→undo→unretract, downgrade→undo→supersede}, assert the protected value is
  **not** general-live afterward and the moved row's value_json is purged
  (tombstone), and any attempt is rejected on a `move_domain`-lineage row.
- **S-test 5 (S1 — resolver is attribute- and timing-blind):** the cross-domain
  resolver, given a crafted general mention, returns only match/no-match; assert (a)
  no protected attribute in any return or general-scoped row; (b) constant work /
  no early-exit timing signal between health-match and no-match (statistical timing
  test); (c) `candidate_ids` on a general fact contains only general projections;
  (d) resolver invocations are rate-limited and audited in both domains.
- **S-test 6 (S5 — extractor relink to cross-canonical object):** an injected note
  steering an in-scope `relink_object` whose new object's canonical has a protected
  projection → routed to review, **not** auto-committed.
- **S-test 7 (S3 — general edge to protected canonical):** `add_fact`/relink that
  would bind a general object to a `canonical_id` owning a location/health/finance
  projection → rejected unless via owner `identity_merge`; assert emergent
  movement-oracle fixture (N general `visited` edges sharing location canonicals)
  cannot be constructed from a general or agent principal.
- **S-test 8 (S4/S8 — non-droppable attribution + pin):** a `human_assertion`/agent
  fact is distinguishable from machine-extracted at **every** read surface (fact row,
  review payload, wiki render, search); `pin` on such a fact requires owner principal;
  pinned human-asserted facts appear in the unsourced-fact watch-metric and are
  **not** exempt from migration diff.
- **S-test 9 (S8 — agent op source allowlist):** assert `agent`-sourced ops cannot
  emit `move_domain`, cross-domain `identity_merge`, `pin`, or cross-canonical
  `add_fact`/relink — same hard allowlist as `extractor`, since ASSISTANT.md agents
  consume untrusted graph content.

---

*The propose/commit split and projections are sound; the unbuilt cross-domain
resolver and the value-bearing retained rows (move-forward, add_fact, pin) are where
v1 must close the firewall.*
