# Career Agent — Claude Code Instructions

Build a local-first AI Career Agent for a 2–5 person team.

Before modifying a subsystem, read its relevant skills/*.md file.

Core rules:
1. Candidate facts must be evidence-grounded.
2. Never fabricate resume/application information.
3. Human approval is mandatory before final submission.
4. Never bypass CAPTCHA, authentication, access controls, rate limits or site restrictions.
5. Prefer official/public APIs and permitted integrations.
6. Original resume/LaTeX remains immutable.
7. LLM outputs are proposals; deterministic code validates/applies them.
8. Important actions are auditable.
9. Optimize correctness/recoverability before scale.
10. Build mock integrations before real browser automation.

When uncertain, stop safely and ask the user rather than guessing or submitting.

---

## Actual layout

Matches the original target layout, with `packages/core` added for shared
infrastructure that belongs to no single domain (see docs/architecture.md §2).

```
apps/api/            FastAPI routes only — thin: parse, delegate, serialise
apps/web/            frontend (M6, Vite + React + TypeScript)
packages/schemas/    Pydantic domain contracts (API + services + LLM I/O)
packages/core/       settings, logging, db, ids, errors, audit, llm/
packages/prompts/    versioned prompt templates
services/candidate/  services/jobs/  services/matching/
services/resume/     services/application/  services/browser/
migrations/          Alembic revisions
tests/unit/          fast, no I/O        tests/integration/  API, migrations, mock site
data/                gitignored candidate data
docs/                PRD + architecture decision record
scripts/             bootstrap.sh (vendored toolchain), seed.py
```

## Environment

* Python is **3.11**, not the system 3.6: `PYTHON=/usr/local/bin/python3.11`.
* `uv` manages dependencies. Run things as `uv run <cmd>` or via `make`.
* LaTeX is vendored `tectonic` in `.tooling/bin` (musl build — glibc here is 2.27).
* Node is vendored and pinned to **16**: this host's glibc cannot run Node 18+, which
  is also why the frontend pins Vite 4. Raise both together on a newer machine.
* Playwright's bundled driver needs glibc >= 2.28 and cannot start here. The runner is
  driver-agnostic; use `HttpFormDriver` locally or the official Playwright Docker
  image. Do not add a workaround that patches around the driver.
* `make install`, `make dev`, `make test`, `make lint`, `make format`, `make seed`,
  `make migrate`, `make revision m="..."`, `make mock-site`, `make check`.

## Conventions

* Imports are repo-root relative: `from packages.schemas.job import Job`. Nothing
  is pip-installed; this is an application, not a library.
* **Business logic never imports `fastapi`.** Services take a `Session` and raise
  `DomainError` subclasses; `apps/api/main.py` maps them to HTTP in one place.
* Services receive a `Session`, they do not open one — so a whole workflow shares
  one transaction. `session_scope()` is for outermost callers only.
* Only `packages/core/settings.py` reads the environment.
* Every LLM call goes through `LLMClient.run()` with a Pydantic `output_model` and
  a named task. No ad-hoc model calls.
* Untrusted text (job descriptions, scraped pages) must be passed as
  `UntrustedContent`, never concatenated into a prompt string.
* Pass `allowed_evidence_ids` on any task that may cite evidence.
* `mypy --strict` and `ruff` must both be clean. Full type annotations.
* New dependencies need a justification comment in `pyproject.toml`.
* Never log or persist secrets, cookies, tokens or salary answers. Use
  `safe_extra()` when passing dynamic keys to `logging`'s `extra=`.
* Migrations are mandatory for model changes; `tests/integration/test_migrations.py`
  fails on drift. Custom column types render as plain `sa.*` types via `render_item`,
  so migrations never import application code.
* Text that originated from an untrusted source stays untrusted even after extraction.
  If a requirement pulled from a posting is echoed into a later prompt, it goes through
  the same fencing — never concatenated in raw.
* Sensitive form fields (salary, authorization, visa, clearance, notice period,
  demographics) always route to the user, however confident a fact looks.
* The SPA is mounted at `/`, so any route registered after it is unreachable. Add API
  routers before the mount.

## Definition of done

Implementation + tests (including a regression test for every bug found) + error
handling + logs + docs + a local reproduction command. Run `make check` before
declaring a milestone complete.
