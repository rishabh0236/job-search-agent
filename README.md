# AI Career Agent

Local-first, evidence-grounded job discovery, resume tailoring and human-approved
application preparation. Built for a single machine and a 2–5 person team.

The product promise is narrow and load-bearing: **nothing reaches a resume or an
application form unless it traces back to something the candidate actually
provided.** The architecture exists to enforce that mechanically, not by asking a
model to behave.

## Status

All milestones **M0–M6** are implemented and tested.
See [docs/architecture.md](docs/architecture.md)
for the design decisions and [docs/BUILD_PACK_README.md](docs/BUILD_PACK_README.md)
for the original build order.

| Milestone | Scope | State |
|---|---|---|
| M0 | Repo, schemas, database, migrations, API skeleton, LLM seam, audit, tooling | ✅ done |
| M1 | Candidate intelligence: PDF/LaTeX ingestion, evidence linking, profile review | ✅ done |
| M2 | Job discovery, normalization, dedupe, hybrid matching | ✅ done |
| M3 | LaTeX AST, structured edits, compile/validate loop, mock application site | ✅ done |
| M4 | Cover letters, answer mapping, application state machine, tracker | ✅ done |
| M5 | Browser automation: driver seam, safety stops, runner | ✅ done (see note) |
| M6 | Web UI (Vite + React + TypeScript) | ✅ done |

**M5 note:** the runner, its state machine and every safety stop are implemented and
tested end-to-end against the local mock site through the HTTP driver. The Playwright
driver implements the same protocol but cannot start on this host — its bundled driver
needs GLIBC >= 2.28 and this machine has 2.27. Run it in the
`mcr.microsoft.com/playwright/python` image; no application code changes needed.

## Requirements

* Python 3.11+ (`/usr/local/bin/python3.11` on this host — the system `python3` is 3.6 and will not work)
* [uv](https://docs.astral.sh/uv/) for dependency management
* No LaTeX distribution or Node install needed — `scripts/bootstrap.sh` vendors both into `.tooling/`

## Quick start

```bash
make install              # create .venv, install deps, copy .env.example -> .env
scripts/bootstrap.sh      # vendor tectonic (LaTeX) and node/npm into .tooling/
make migrate              # create the SQLite schema
make seed                 # demo candidate + ingest the fixture resume
make web-install          # frontend dependencies
make web-build            # build the UI
make dev                  # API and UI together on http://localhost:8000
```

For frontend work run `make web-dev` alongside `make dev`: Vite serves :5173 and
proxies `/api` to the backend, the same shape as the built path.

Then open http://localhost:8000/docs for the OpenAPI UI, or:

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

`/health` reports per-capability status, so a missing LaTeX engine or an
unconfigured model shows up immediately rather than when a user clicks *Tailor*.

### Ingest a resume

```bash
CID=$(curl -s -X POST localhost:8000/candidates \
        -H 'Content-Type: application/json' \
        -d '{"display_name":"Your Name"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

curl -s -X POST localhost:8000/candidates/$CID/resumes \
     -F "file=@/path/to/resume.tex"          # or .pdf / .txt

curl -s localhost:8000/candidates/$CID       # canonical facts with evidence
```

The response is an ingestion report: blocks extracted, evidence records created,
facts created, facts awaiting review, facts *rejected* and why. Every fact on the
profile carries the verbatim quote and locator that supports it.

Facts arrive unverified. Confirm, correct, add or remove them:

```bash
curl -X POST  localhost:8000/candidates/$CID/facts/$FID/verify
curl -X PATCH localhost:8000/candidates/$CID/facts/$FID -d '{"claim":"..."}'
```

A correction is stored as *user-provided*, not as something the resume said.

## Commands

| Command | Purpose |
|---|---|
| `make install` | Sync dependencies into `.venv` |
| `make dev` | API with autoreload on :8000 |
| `make test` | Full test suite |
| `make lint` | `ruff check` + `ruff format --check` + `mypy --strict` |
| `make format` | Auto-format and apply safe fixes |
| `make check` | Everything CI would run |
| `make migrate` | Apply migrations |
| `make revision m="..."` | Autogenerate a migration |
| `make seed` | Load demo fixtures (idempotent) |
| `make mock-site` | Local mock application site on :8001 |
| `make web-install` | Frontend dependencies |
| `make web-dev` | Vite dev server on :5173 |
| `make web-build` | Build the UI for the API to serve |
| `make web-check` | Frontend type-check |

## Layout

```
apps/api/            FastAPI routes only — no business logic
apps/web/            frontend (M6)
packages/schemas/    Pydantic domain contracts, shared by API, services and LLM tasks
packages/core/       infrastructure: settings, logging, db, audit, LLM seam
packages/prompts/    versioned prompt templates (M1+)
services/*/          business logic per domain: candidate, jobs, matching, resume,
                     application, browser
migrations/          Alembic revisions
tests/unit/          fast, no I/O
tests/integration/   API, migrations, mock site
data/                local candidate data — gitignored, never committed
docs/                PRD and architecture notes
skills/              subsystem specs; read the relevant one before changing a subsystem
```

Imports are repo-root relative (`from packages.schemas.job import Job`). This is an
application, not a distributable library, so no package is built or installed.

## Configuration

All settings come from the environment with a `CA_` prefix, or `.env`. See
[.env.example](.env.example) for the full list. Nothing outside
`packages/core/settings.py` reads the environment directly.

The LLM provider defaults to `stub`: a deterministic fixture provider that needs
no API key and no network, which is what lets the test suite assert exact
behaviour. Set `CA_LLM_PROVIDER=anthropic` and `CA_ANTHROPIC_API_KEY` to use
Claude for real.

## Safety model

These are enforced in code, with tests, not left to prompt instructions:

* **Evidence allowlist** — any model output citing an evidence id that was not
  supplied is rejected as fabrication (`packages/core/llm/guards.py`).
* **Metric guard** — numbers in a tailored bullet must already appear in the
  candidate's own evidence.
* **Prompt-injection fencing** — job descriptions travel as `UntrustedContent`,
  wrapped in delimiters that forged closing tags cannot escape.
* **Immutable original** — a partial unique index allows exactly one original
  resume per candidate; every tailored version records its parent.
* **Human approval** — the application state machine cannot reach `SUBMITTING`
  without passing through `USER_APPROVED`, and a pre-submit checklist blocks on
  any unconfirmed sensitive answer.
* **Duplicate-submit prevention** — unique `(candidate_id, job_id)` plus a unique
  idempotency key.
* **Redaction** — passwords, tokens, cookies, PII and salary figures are redacted
  in the log formatter, so a careless call site still cannot leak them.
* **Safe stop** — CAPTCHA, unexpected authentication, suspicious pages, payment
  requests, access-denied and rate-limit pages all end a run with a reason, a
  redacted snapshot and an explanation. There is no retry loop and no bypass path.
* **Injection defence in depth** — postings travel fenced as `UntrustedContent`;
  forged delimiters are neutralised in *every* block, including data extracted from
  an untrusted source and echoed back later; instruction-like text is kept out of
  requirements and surfaced to the user instead.
* **Two independent submit gates** — your explicit per-application approval *and*
  `CA_ALLOW_BROWSER_SUBMIT`. Both must be true.
* **Snapshot redaction** — page snapshots strip input values, so a diagnostic
  artifact never captures the answers you typed.

## Testing

```bash
make test                       # everything
uv run pytest tests/unit -q     # fast unit tests only
uv run pytest -k injection -q   # a specific area
```

`tests/integration/test_migrations.py` asserts the migrations produce exactly the
schema the models define — the test fixtures use `create_all` for speed, and this
is what stops that shortcut from hiding a forgotten migration.

## Contributing rules

Read [CLAUDE.md](CLAUDE.md) first, then the relevant file in [skills/](skills/)
before changing a subsystem. Every discovered bug becomes a regression test.
