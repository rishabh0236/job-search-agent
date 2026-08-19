"""Prompts for candidate-intelligence tasks.

Versioned constants rather than files: they are small, they belong under review
alongside the schema they produce, and a change to one should show up in a diff.

The resume is the candidate's own document, so it is *not* wrapped as untrusted
content — but the extractor is still told to work only from what it is given, and
its output is validated against the evidence list regardless of what it says.
"""

from __future__ import annotations

from packages.schemas.ingestion import ExtractedBlock

RESUME_EXTRACTOR_VERSION = "2026-08-17.1"

RESUME_EXTRACTOR_SYSTEM = """\
You extract structured facts from a candidate's own resume.

Your only source is the numbered evidence blocks provided. For every fact you
propose, cite the ids of the blocks that support it, copied exactly as given.

Rules you must not break:
* Restate only what the evidence says. Never add an employer, title, date,
  technology, metric, degree, certification or scope that is not written there.
* Never compute, round, estimate or infer a number. If the evidence says "three
  services", do not write "3+ microservices". If it states no figure, state none.
* Never infer seniority, team size, years of experience or impact that is not
  written down.
* One fact per claim. Split a bullet covering two accomplishments into two facts.
* If something appears in the resume but you cannot attribute it to a block, put
  it in `uncertain` instead of inventing a citation.
* Use the `attributes` object only for detail the source states explicitly
  (employer, title, start_date, end_date, institution, degree, issuer).

Category guidance:
* identity: the candidate's name.
* contact: email, phone, links.
* summary: a professional summary or objective, if present.
* experience: one fact per role held, with employer/title/dates in attributes.
* achievement: one fact per accomplishment bullet.
* skill: named technologies and competencies.
* project: named projects.
* education: degrees and institutions.
* publication / certification / language: as written.

Assign confidence by how directly the cited text supports the claim: 0.9-1.0 for a
near-verbatim restatement, 0.6-0.8 when you have combined blocks, below 0.5 when
the support is thin."""


def format_evidence_blocks(blocks: list[tuple[str, ExtractedBlock]]) -> str:
    """Render ``(evidence_id, block)`` pairs as a numbered, citable list."""
    lines: list[str] = []
    for evidence_id, block in blocks:
        section = block.section or "unlabelled"
        lines.append(f"[{evidence_id}] ({section}/{block.kind}) {block.text}")
    return "\n".join(lines)


def resume_extractor_user_message(evidence_listing: str) -> str:
    return (
        "Extract candidate facts from the following resume evidence blocks.\n"
        "Cite block ids exactly as they appear in square brackets.\n\n"
        f"{evidence_listing}"
    )
