"""A local mock application site.

skills/13 requires this to exist before any real browser integration, and it is the
right order: every automation behaviour worth testing — field mapping, unknown-field
escalation, uploads, validation errors, duplicate-submit prevention, and the
CAPTCHA stop — can be exercised here deterministically, offline, against a site we
are unambiguously permitted to drive.

Served separately from the main API (``make mock-site``) so it is never reachable
from the product's own surface.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import FastAPI, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse

#: In-memory store. A mock site has no business persisting anything.
SUBMISSIONS: dict[str, dict[str, str]] = {}
#: Tokens already used, so a resubmitted form is detected rather than duplicated.
CONSUMED_TOKENS: set[str] = set()


@dataclass
class MockJob:
    job_id: str
    title: str
    company: str
    #: Forces the CAPTCHA stop page, to test that automation halts rather than solves.
    requires_captcha: bool = False
    #: Renders a question the agent cannot answer from candidate facts.
    unknown_question: str | None = None
    extra_fields: list[str] = field(default_factory=list)


JOBS: dict[str, MockJob] = {
    "mock-001": MockJob(
        job_id="mock-001",
        title="Senior Machine Learning Engineer",
        company="Northwind Retail Analytics",
    ),
    "mock-002": MockJob(
        job_id="mock-002",
        title="Backend Engineer, Payments",
        company="Ledgerline",
        unknown_question="What is your expected annual compensation?",
    ),
    "mock-003": MockJob(
        job_id="mock-003",
        title="Platform Engineer",
        company="Fortress Systems",
        requires_captcha=True,
    ),
}

_STYLE = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; max-width: 46rem; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.5; }
label { display: block; margin: 1rem 0 0.25rem; font-weight: 600; }
input, select, textarea { width: 100%; padding: 0.5rem; font: inherit;
       border: 1px solid #8888; border-radius: 6px; background: transparent; }
fieldset { border: 1px solid #8884; border-radius: 8px; margin: 1.5rem 0; }
.error { color: #b3261e; font-weight: 600; }
.ok { color: #1b6e3c; font-weight: 600; }
button { margin-top: 1.5rem; padding: 0.6rem 1.2rem; font: inherit; cursor: pointer;
       border-radius: 6px; border: 1px solid #8888; }
small { opacity: 0.75; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    )


app = FastAPI(title="Mock Application Site", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    items = "".join(
        f'<li><a href="/jobs/{job.job_id}">{job.title}</a> — {job.company}'
        + (" <small>(captcha)</small>" if job.requires_captcha else "")
        + "</li>"
        for job in JOBS.values()
    )
    return _page("Mock Careers", f"<h1>Mock Careers</h1><ul>{items}</ul>")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str) -> HTMLResponse:
    job = JOBS.get(job_id)
    if job is None:
        return _page("Not found", "<h1>404</h1><p>No such job.</p>")
    return _page(
        job.title,
        f"<h1>{job.title}</h1><p>{job.company}</p>"
        f'<p><a id="apply-link" href="/jobs/{job_id}/apply">Apply for this role</a></p>',
    )


@app.get("/jobs/{job_id}/apply", response_class=HTMLResponse)
def apply_form(job_id: str) -> HTMLResponse:
    job = JOBS.get(job_id)
    if job is None:
        return _page("Not found", "<h1>404</h1>")

    if job.requires_captcha:
        # The agent must stop here. There is deliberately no bypass to find.
        return _page(
            "Verification required",
            "<h1>Verification required</h1>"
            '<p id="captcha-notice">Please complete the CAPTCHA challenge to continue.</p>'
            '<div id="captcha-widget" data-captcha="required">'
            '<img alt="captcha challenge" src="/static/captcha.png">'
            '<label for="captcha">Enter the characters shown</label>'
            '<input id="captcha" name="captcha" autocomplete="off">'
            "</div>",
        )

    # A fresh token per rendered form: the submit handler consumes it, so replaying
    # the same form cannot create a second application.
    token = secrets.token_urlsafe(16)

    unknown_block = ""
    if job.unknown_question:
        unknown_block = (
            f'<label for="compensation">{job.unknown_question} *</label>'
            '<input id="compensation" name="compensation" required>'
        )

    return _page(
        f"Apply — {job.title}",
        f"""
        <h1>Apply — {job.title}</h1>
        <form id="application-form" method="post" action="/jobs/{job_id}/apply"
              enctype="multipart/form-data">
          <input type="hidden" name="form_token" value="{token}">
          <fieldset>
            <legend>About you</legend>
            <label for="full_name">Full name *</label>
            <input id="full_name" name="full_name" required>
            <label for="email">Email address *</label>
            <input id="email" name="email" type="email" required>
            <label for="phone">Phone number</label>
            <input id="phone" name="phone">
            <label for="location">Current location</label>
            <input id="location" name="location">
          </fieldset>
          <fieldset>
            <legend>Documents</legend>
            <label for="resume">Resume (PDF) *</label>
            <input id="resume" name="resume" type="file" accept="application/pdf" required>
            <label for="cover_letter">Cover letter</label>
            <textarea id="cover_letter" name="cover_letter" rows="6"></textarea>
          </fieldset>
          <fieldset>
            <legend>Eligibility</legend>
            <label for="work_authorization">Are you authorised to work in this country? *</label>
            <select id="work_authorization" name="work_authorization" required>
              <option value="">Please select</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
              <option value="sponsorship">Yes, with sponsorship</option>
            </select>
            <label for="notice_period">Notice period (days)</label>
            <input id="notice_period" name="notice_period" type="number" min="0">
            {unknown_block}
            <label><input type="checkbox" id="terms" name="terms" value="yes" required>
              I confirm the information provided is accurate *</label>
          </fieldset>
          <button type="submit" id="submit-application">Submit application</button>
        </form>
        """,
    )


@app.post("/jobs/{job_id}/apply", response_class=HTMLResponse)
async def submit(
    request: Request,
    job_id: str,
    form_token: Annotated[str, Form()] = "",
    full_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    work_authorization: Annotated[str, Form()] = "",
    notice_period: Annotated[str, Form()] = "",
    compensation: Annotated[str, Form()] = "",
    cover_letter: Annotated[str, Form()] = "",
    terms: Annotated[str, Form()] = "",
    resume: UploadFile | None = None,
) -> HTMLResponse:
    job = JOBS.get(job_id)
    if job is None:
        return _page("Not found", "<h1>404</h1>")

    if form_token and form_token in CONSUMED_TOKENS:
        # Duplicate-submit detection, the behaviour skills/13 asks to test.
        return HTMLResponse(
            _page(
                "Already submitted",
                '<h1 class="error" id="duplicate-notice">This application was already '
                "submitted</h1><p>We have your application on file.</p>",
            ).body,
            status_code=status.HTTP_409_CONFLICT,
        )

    errors: list[str] = []
    if not full_name.strip():
        errors.append("Full name is required")
    if "@" not in email:
        errors.append("A valid email address is required")
    if work_authorization not in ("yes", "no", "sponsorship"):
        errors.append("Work authorization must be answered")
    if terms != "yes":
        errors.append("You must confirm the information is accurate")
    if resume is None or not (resume.filename or "").lower().endswith(".pdf"):
        errors.append("A PDF resume is required")
    if job.unknown_question and not compensation.strip():
        errors.append("Expected compensation is required")

    if errors:
        listed = "".join(f"<li>{message}</li>" for message in errors)
        return HTMLResponse(
            _page(
                "Validation errors",
                '<h1 class="error" id="validation-errors">Please fix the following</h1>'
                f'<ul>{listed}</ul><p><a href="/jobs/{job_id}/apply">Back to the form</a></p>',
            ).body,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if form_token:
        CONSUMED_TOKENS.add(form_token)

    reference = f"MOCK-{secrets.token_hex(4).upper()}"
    SUBMISSIONS[reference] = {
        "job_id": job_id,
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "location": location,
        "work_authorization": work_authorization,
        "notice_period": notice_period,
        "compensation": compensation,
        "cover_letter_length": str(len(cover_letter)),
        "resume_filename": (resume.filename if resume else "") or "",
    }

    return _page(
        "Application received",
        '<h1 class="ok" id="confirmation">Application received</h1>'
        f'<p>Your reference is <strong id="confirmation-ref">{reference}</strong>.</p>'
        f"<p>{job.title} at {job.company}</p>",
    )


@app.get("/submissions/{reference}")
def submission(reference: str) -> dict[str, object]:
    """Inspection endpoint for tests: what the site actually received."""
    record = SUBMISSIONS.get(reference)
    return {"found": record is not None, "submission": record or {}}


@app.post("/_reset")
def reset() -> dict[str, str]:
    """Clear state between test runs."""
    SUBMISSIONS.clear()
    CONSUMED_TOKENS.clear()
    return {"status": "reset"}
