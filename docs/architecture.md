# Architecture & Decision Record

Decisions taken while implementing the PRD, with the reasoning. Where a decision
deviates from the PRD, the deviation is stated explicitly.

## 1. Shape: modular monolith

One FastAPI process, one SQLite file, one local data directory. Services are
plain Python modules with no framework coupling, so they are callable from the
API, a script, a test or a future background worker without change.

Boundaries are drawn so the PRD's scaling path (§16) is a substitution rather than
a rewrite:

| Seam | Today | Later |
|---|---|---|
| `LLMProvider` | `StubProvider`, `AnthropicProvider` | routed/multi-model, cost controls |
| `JobSource` | small set of permitted adapters | more adapters, per-source rate limits |
| `LatexCompiler` | vendored `tectonic` | Docker TeX Live, compile worker |
| `EmbeddingIndex` | numpy cosine over stored vectors | FAISS, pgvector |
| Database | SQLite | PostgreSQL (schema is already compatible) |
| Storage | `./data` | object storage |

## 2. Deviations from the PRD

### Frontend: Vite + React instead of Next.js

The PRD specifies Next.js. For a local, single-user tool there is no SSR, no SEO
and no server-component benefit to collect, while the cost is a second runtime and
a second dev server. Vite builds a static bundle that FastAPI can serve directly —
one process, one port. Component work (React, TypeScript, Tailwind) is unchanged.

*Approved by the user before implementation.*

### LaTeX: tectonic instead of a system pdflatex/xelatex

No TeX distribution was installed on this host, and the disk had ~26 GB free
against a ~5 GB full TeX Live. `tectonic` is a single static binary that fetches
only the packages a document needs. It sits behind a `LatexCompiler` interface so
a Docker TeX Live image can replace it without touching the tailoring pipeline.

Note: the `x86_64-unknown-linux-gnu` build requires GLIBC 2.29; this host has
2.27, so `scripts/bootstrap.sh` installs the statically linked **musl** build.

*Approved by the user before implementation.*

### Vector search: numpy instead of FAISS

The PRD suggests FAISS. One candidate's evidence set is on the order of a hundred
short chunks; exact cosine similarity over a numpy array is faster than a FAISS
index at that size, adds no dependency, and cannot drift out of sync with the
database. `EmbeddingIndex` keeps FAISS and pgvector as drop-in replacements.

### Added: `packages/core`

`CLAUDE.md` lists `packages/schemas` and `packages/prompts`. Settings, logging,
database access, the audit writer and the LLM seam are shared infrastructure that
belongs to no single domain service, so they live in `packages/core` rather than
being duplicated or forced into a domain package.

### Added: application uniqueness and idempotency

The PRD requires duplicate-submit prevention (§14) but the data model (§9) has no
support for it. Added a unique constraint on `(candidate_id, job_id)` and a unique
`idempotency_key` set immediately before a submit attempt, so a retry after an
uncertain network response cannot create a second submission.

## 3. Evidence model

The PRD stores `evidence_ref` as a field on `CandidateFact`. Evidence is instead a
first-class table with a many-to-many link to facts, because one sentence often
supports several facts and because deleting a source must cascade to everything it
supported (explicit deletion is a stated privacy requirement).

Provenance and verification are separate fields, which is what makes the four UI
trust labels expressible without ambiguity:

| `provenance` | `verified` | UI label |
|---|---|---|
| `resume` | true | Verified candidate fact |
| `resume` | false | Extracted (unverified) |
| `user` | – | User-provided information |
| `ai_suggestion` | – | AI suggestion |
| `unknown` | – | Unknown / requires confirmation |

A missing fact is `unknown`. It never becomes a negative in matching, and it never
becomes an answer on a form.

## 4. LLM orchestration

Every call is a narrow, named task with a Pydantic `output_model`. There is no
general-purpose "ask the model" entry point.

```
LLMRequest(task, system, blocks, output_model, allowed_evidence_ids)
    -> LLMProvider.complete()          # transport, native structured output
    -> LLMClient                       # schema validation + bounded repair loop
    -> guards.assert_evidence_allowed  # fabricated citation => reject
    -> LLMResult(output, usage, attempts)
```

Two retry budgets, kept separate so they cannot multiply:

* **Transport** (429, 5xx, timeouts) — handled by the Anthropic SDK, which honours
  `retry-after`.
* **Schema repair** — handled by `LLMClient`, which re-asks with the validation
  error attached, up to `CA_LLM_MAX_RETRIES`.

A fabricated evidence id is deliberately *not* retried. The model had a closed
list of references in context and produced something else; that is a correctness
failure to surface, not a formatting slip to coax it out of.

### Prompt injection

Job descriptions and scraped pages are attacker-controlled. Defences are
structural:

1. Untrusted text is wrapped in `<untrusted_content source="...">` fences.
2. Forged delimiters inside that text are neutralised, so a posting containing
   `</untrusted_content>` cannot close the fence early and have the rest read as
   trusted instructions.
3. Source labels are sanitised so they cannot break out of their attribute.
4. The system preamble states that fenced content is data, and that evidence ids
   may only be used verbatim.
5. Output is validated against the allowlist regardless of what the prompt said.

Layers 1–4 reduce the chance of a successful injection; layer 5 bounds the damage
if one succeeds.

## 5. Determinism boundary

The LLM proposes. Deterministic code decides.

| Owned by code | Owned by the model |
|---|---|
| persistence, transactions | extraction proposals |
| LaTeX parsing and patching | edit proposals with rationale |
| compilation and validation | match explanations |
| score arithmetic and weights | requirement classification |
| state transitions | draft cover letters and answers |
| evidence resolution | — |

`JobMatch.recompute_score()` re-derives a score from its components, so a stored
number is always reconstructable and the UI can never display a figure no
component supports.

## 6. Persistence

SQLAlchemy 2.0 typed models, Alembic migrations from the first commit.

* Prefixed string ids (`job_…`, `cand_…`) — opaque to the database, readable in
  logs and audit trails.
* `UtcDateTime` coerces on write and re-attaches UTC on read. SQLite otherwise
  returns naive datetimes that compare unequal to aware ones.
* `JSON().with_variant(JSONB, "postgresql")` — indexable JSON on PostgreSQL, no
  migration needed at the switch.
* SQLite pragmas set per connection: `foreign_keys=ON` (off by default, which
  would silently ignore every `ondelete` clause), plus WAL for concurrent reads.
* Batch mode enabled for SQLite migrations, so a future column change does not
  fail locally while passing on PostgreSQL.

## 7. Auditing and logging

`audit.record()` adds a row to the caller's transaction and does not commit, so an
audit entry lands atomically with the change it describes — there is never an
audit trail for a rolled-back action.

Redaction lives in the log formatter rather than at call sites. A careless
`logger.info(..., extra={"headers": headers})` still cannot leak a credential.
Identifiers stay loggable so events remain traceable.

`safe_extra()` prefixes keys that collide with `LogRecord` attributes. This exists
because a real bug: the safety-stop handler logged `extra={"message": ...}`, which
makes stdlib logging raise `KeyError` — turning a 409 with an explanation into a
500, in the one code path whose entire job is to hand control back to the user.
Covered by a regression test.

## 8. Testing strategy

| Layer | Content |
|---|---|
| Unit | schema invariants, state machine, redaction, guards, scoring |
| Safety | fabricated evidence, invented metrics, injection escape attempts, duplicate submits, foreign-key enforcement |
| Integration | API behaviour, migration/model parity, mock ATS forms (M3) |
| Golden | fixed candidate + JD produce expected evidence and bounded scores (M2) |

The suite runs with the stub provider — no API key, no network, no flakiness.

## 9. Candidate intelligence (M1)

```
file -> extract -> deterministic parse -> evidence rows -> model proposals
     -> validate -> persist (unverified) -> human review
```

**Extraction is the deterministic floor.** Whatever a model later claims, it can
only cite blocks produced by the extractor, and every quote is checkable against
that text. LaTeX keeps exact character offsets (comments are masked with spaces
rather than removed, preserving length), which is what the M3 patcher will address
spans with. PDF blocks deliberately carry no offsets — they are not editable, and
leaving them None prevents misuse.

**Deterministic before probabilistic.** Contacts, links and skill lists are
extracted by code at confidence 1.0. Only the interpretive work — what a role
involved, what an achievement was — goes to a model. A candidate therefore gets a
usable profile with no API key at all, and the ingestion report says so explicitly
rather than appearing to have succeeded fully.

**Evidence is a table, ids are content-addressed.** `ev_<sha256(source|locator|quote)>`
means re-ingesting a document produces identical ids, so golden tests are stable and
re-import does not orphan references. Fact ids are content-addressed the same way,
which makes ingestion idempotent: a second pass merges evidence into existing facts
instead of duplicating the profile, and a fact the user already verified stays
verified.

### The five validation gates

Every fact — regex-extracted or model-proposed — passes `services/candidate/facts.py`:

| Gate | Outcome |
|---|---|
| Cites an evidence id that was never supplied | **rejected** — the model had a closed list; anything else is invention |
| States a figure absent from the cited evidence | **rejected** — the most damaging resume hallucination |
| States a year absent from the cited evidence | **rejected** |
| Names an employer/institution absent from the cited evidence | **rejected** — blocks a plausible-sounding invented workplace |
| Cites nothing at all | stored as **UNKNOWN**, never as fact; queued for confirmation |

Nothing is ever stored verified. High-risk categories (experience, education,
certification, authorization, compensation) additionally have their confidence
capped below 1.0, so the review screen can never present a model's certainty as a
human's.

The simulated extractors in `tests/support/extractors.py` read the evidence listing
out of the request the pipeline actually built and respond as a model would —
faithfully, or with each of the four fabrication modes. That exercises real prompt
construction and real evidence ids rather than a frozen fixture payload.

### Bugs this milestone surfaced

Each is now a regression test:

* `Mapped[SomeEnum]` over a plain `String` column returned `str` on read while the
  type checker still believed it was an enum — so `fact.category.value` type-checked
  and crashed at runtime. Fixed with a `StrEnumType` decorator; latent across every
  enum column.
* `Base.metadata.create_all()` only creates tables it knows about, and models were
  registered by whichever module happened to import them first — the same test
  passed in a full run and failed alone. Fixed by registering models in
  `packages/core/db/__init__.py`.
* A multi-line `\resumeSubheading` was split into three blocks by brace counting,
  separating an employer from its role — which would quietly defeat the
  "employer must appear in the evidence" gate.
* `2015 - 2019` has eight digits and a separator, so the phone pattern matched a
  date range. A wrong phone number on an application is a real failure.
* `\\` flattened to a space merged "Languages: Python, Go, SQL" with
  "Frameworks: FastAPI", letting a group label absorb the previous group's last
  skill — dropping a real skill and inventing "SQL Frameworks".

## 10. Job discovery and matching (M2)

Adapters implement one four-method protocol (`search`, `fetch`, `health_check`, plus
`normalize`). Two ship: a **local fixture source** (offline, deterministic, always
registered so the whole pipeline is developable without a third party) and
**Greenhouse's public board API** — the same JSON a company's own careers page
consumes, no credentials, requests serialised with a courtesy delay. Deliberately
absent: anything that scrapes, logs in, or works around a control.

**Dedupe** uses four signals, cheapest and most reliable first: source identity
(a database constraint), canonical URL with tracking parameters stripped, requisition
id *within one company*, and only then description shingle overlap at a high
threshold. The bar is high because a false merge hides a job the candidate never
sees — worse than showing one twice.

**Scoring** is entirely deterministic across seven weighted components (PRD §10).
Two invariants:

* A missing fact is never a negative. Unknowns leave the denominator and are reported
  separately, so an incomplete profile lowers *confidence*, not the score.
* `recompute_score()` reproduces the total from its components exactly, so the UI can
  break down any figure it shows.

The model writes only the prose explanation, and only from the strengths list the
scorer already produced. If it fails, the match still stands without prose.

Retrieval is lexical (hashed token cosine), which is honest about what it is: it
orders evidence for a scorer and a human, and never decides truth. `EmbeddingIndex`
is the seam for a real embedding backend.

## 11. Tailoring and the compile loop (M3)

```
.tex -> AST (stable target ids) -> proposed edits -> factuality gate
     -> patch (back-to-front, offset-safe) -> compile -> compare PDFs -> new version
```

The patcher refuses more than it accepts, by design. Each edit must resolve to
exactly one editable region, match the `old_text` it was proposed against, contain
balanced braces, introduce no command outside a small allowlist (`\input`,
`\write18`, `\def` and friends are rejected outright), and not overlap another edit
in the batch. Edits apply back to front so unapplied spans keep their offsets, and the
preamble is re-hashed afterwards — a changed template aborts the whole patch.

Section headings and the preamble are structural and cannot be targeted at all:
tailoring rewrites prose inside `\resumeItem{...}`, preserving the wrapper, so a
tailored resume looks identical to the original.

Nothing becomes a version unless it compiles *and* survives comparison against the
original PDF (page-count change, lost proper nouns/years/figures, or a large text
shrink). A blocked run returns its findings so the review screen can show precisely
what was refused.

## 12. Applications (M4)

Answer mapping runs in three tiers of decreasing trust: deterministic contact/name
mapping from verified facts, then **sensitive fields that always go to the human**
(salary, authorization, visa, clearance, notice period, demographics, criminal
history — regardless of how well a fact seems to answer them), then the model, which
must either cite evidence or say it cannot. Model output is validated against the
field's own constraints, and placeholder answers ("N/A", "TBD") are rejected: a
required field filled with a placeholder looks answered, which is worse than empty.

Three independent mechanisms guard submission, so no single mistake defeats it:

1. The transition table makes `SUBMITTING` reachable only from `USER_APPROVED`.
2. `claim_submission` re-checks the full blocker list and the kill switch.
3. A unique idempotency key is claimed *before* anything is typed, so a retry after an
   uncertain response cannot produce a second application.

## 13. Browser automation (M5)

The runner is written against a `BrowserDriver` protocol with two implementations:
`HttpFormDriver` (no browser — parses and posts forms, which is what the tests drive)
and `PlaywrightDriver` (isolated context, semantic selectors, snapshot plus screenshot
on failure). Because the seam exists, the runner's entire state machine including every
safety stop is verified without a browser, and the Playwright host requirement is a
deployment detail.

Safety assessment is a pure function over page HTML — testable, reproducible, and
impossible for a model to talk out of. It is deliberately eager: a false stop costs a
click, a missed CAPTCHA means software working around an anti-bot control.

`prepare_run` has no code path to a form POST. Submission is a separate method that
begins by claiming the idempotency key. An uncertain outcome (5xx, no confirmation
found) parks the application in `VERIFICATION_REQUIRED` rather than retrying, because
a blind retry risks a duplicate application.

## 14. Frontend (M6)

Vite 4 + React 18 + TypeScript, no router or data library: hash routing keeps deep
links working with no server rewrites, and a ~40-line `useAsync` hook covers the
screens without a cache whose staleness could show an approved resume that no longer
exists. `tsc --strict` is clean; the bundle is ~58 KB gzipped and FastAPI serves it, so
the deployed system is one process on one port.

The UI's job is to make provenance impossible to miss. `TrustBadge` is the single
mapping from `(provenance, verified)` to the five labels, used on every screen. Two
consequences worth noting:

* **Submit is absent, not disabled,** until the checklist is clear — there is nothing
  to click past.
* **A blocked tailoring run is presented as the guardrail working,** with the rejected
  edits shown, rather than as a bare failure.

Node is pinned to 16 and Vite to 4 because this host's glibc (2.27) cannot run Node
18+. Raise both together on a newer machine.

## 15. Environment constraints found on this host

| Constraint | Consequence |
|---|---|
| glibc 2.27 | Node 16 only; Playwright's driver and Node 18+ need 2.28 |
| No TeX distribution | tectonic musl build vendored into `.tooling/` |
| System Python 3.6 | Everything runs on `/usr/local/bin/python3.11` via uv |
| No npm initially | Node + npm vendored by `scripts/bootstrap.sh` |
| Disk 99% full | Full TeX Live and Docker Playwright images avoided locally |

## 16. Bugs surfaced across M2-M6

Each is now a regression test.

* **Untrusted text laundered into a trusted prompt block.** A hostile posting's
  injected line contained "requires", so requirement extraction classified it as a
  requirement, and it was echoed back to the explainer as a "gap" — inside a *trusted*
  block, where the fence escaping did not apply. Fixed on both sides: delimiters are
  neutralised in every block, and instruction-like text is excluded from requirements
  and surfaced to the user.
* **`extra={"created": ...}` crashed job discovery.** `LogRecord` owns that attribute,
  so stdlib logging raised `KeyError` — but only once the level was enabled, which is
  why it passed alone and failed in a full run. Fixed with a `SafeLogger` class that
  sanitises inside `makeRecord`, immunising every call site including future ones.
* **A stale ORM relationship silently emptied an application.** Answers were inserted
  with `session.add()` after the already-loaded collection had been cleared, so
  `prepare()` returned an application with no answers. Then deleting rows individually
  left them in the collection, doubling it on re-prepare. Fixed by using the
  relationship and its delete-orphan cascade.
* **`IGNORECASE` over an uppercase character class** made confirmation-reference
  extraction match ordinary prose: "Application received" yielded the reference
  "received". The flag is now scoped to the keyword only.
* **Section-heading detection swallowed a sentence.** "You must have strong PostgreSQL
  experience." matched the "must have" heading and was consumed as a section break
  instead of read as the requirement it is.
* **A two-word bullet was dropped entirely** ("Strong Python") by a three-word minimum.
* **A static mount at `/` shadows later routes.** Making `serve_frontend` explicit
  documents the constraint instead of leaving it to be rediscovered.
