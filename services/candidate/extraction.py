"""Turn a resume file into an ``ExtractedDocument``.

This is the deterministic floor of the whole product: whatever a model later
claims, it can only cite blocks produced here, and every quote is checkable
against this text. No LLM is involved.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from packages.core.errors import ValidationFailed
from packages.schemas.enums import SourceType
from packages.schemas.ingestion import ExtractedBlock, ExtractedDocument
from services.candidate import latex

#: Canonical section names, and the heading keywords that map to them. Order
#: matters: the first match wins, so specific phrases precede generic ones.
SECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("summary", ("summary", "objective", "profile", "about me")),
    ("experience", ("experience", "employment", "work history", "professional background")),
    ("education", ("education", "academic")),
    ("skills", ("technical skills", "skills", "technologies", "competencies", "toolkit")),
    ("projects", ("projects", "portfolio")),
    ("publications", ("publications", "papers", "research", "patents")),
    ("certifications", ("certifications", "certificates", "licenses", "licences")),
    ("awards", ("awards", "honors", "honours", "achievements")),
    ("languages", ("languages",)),
    ("interests", ("interests", "hobbies")),
)

_BULLET_PREFIX = re.compile(r"^\s*[\u2022\u25e6\u2023\u2043\-*\u00b7]\s+")
_MAX_HEADING_WORDS = 6


def classify_section(heading: str) -> str | None:
    """Map a heading to a canonical section name, or None if unrecognised."""
    normalized = re.sub(r"[^a-z ]", " ", heading.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return None
    for canonical, keywords in SECTION_KEYWORDS:
        for keyword in keywords:
            if keyword in normalized:
                return canonical
    return None


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_source_type(path: Path) -> SourceType:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return SourceType.PDF
    if suffix in (".tex", ".latex"):
        return SourceType.LATEX
    if suffix in (".txt", ".md"):
        return SourceType.TEXT
    raise ValidationFailed(
        f"unsupported resume format {suffix or '(none)'}",
        details={"supported": [".pdf", ".tex", ".txt", ".md"]},
    )


# --------------------------------------------------------------------- LaTeX


def _logical_lines(body: str, offset_base: int) -> list[tuple[int, int, str]]:
    """Group physical lines into complete LaTeX constructs.

    Two things force grouping, and templates in the wild do both:

    1. An unbalanced brace count — arguments opened on one line, closed on a later
       one.
    2. A *balanced* line whose arguments continue below, as in::

           \\resumeSubheading
             {Senior Machine Learning Engineer}{March 2022 -- Present}
             {Infilect Technologies}{Bengaluru, India}

       Each line here is individually balanced, so brace counting alone would emit
       three fragments — splitting an employer away from the role it belongs to and
       making both unquotable as evidence. A following line that begins with ``{``
       is a continuation of the command above it.
    """
    masked = latex.mask_comments(body)
    physical_lines = masked.split("\n")

    # Precompute (start, end) for every physical line.
    spans: list[tuple[int, int]] = []
    position = 0
    for physical in physical_lines:
        spans.append((position, position + len(physical)))
        position += len(physical) + 1  # account for the newline

    lines: list[tuple[int, int, str]] = []
    index = 0
    total = len(physical_lines)

    while index < total:
        if not physical_lines[index].strip():
            index += 1
            continue

        start = spans[index][0]
        end = spans[index][1]
        depth = latex.brace_balance(physical_lines[index])
        index += 1

        while index < total:
            nxt = physical_lines[index]
            if depth > 0:
                pass  # still inside an open group: must keep consuming
            elif nxt.strip().startswith("{"):
                pass  # balanced, but the next line continues this command's args
            else:
                break
            depth += latex.brace_balance(nxt)
            end = spans[index][1]
            index += 1

        lines.append((start + offset_base, end + offset_base, body[start:end]))

    return lines


def _latex_block_kind(masked_line: str) -> str:
    """Classify a logical line.

    ``heading`` and ``entry`` are kept distinct because only a section command
    changes which section we are in. A role heading such as "Engineer, Education
    Technology Inc" would otherwise flip the current section to *education* and
    mislabel every bullet under it.
    """
    stripped = masked_line.lstrip()
    for command in latex.SECTION_COMMANDS:
        if re.match(rf"\\{command}\b|\\{command}\*", stripped):
            return "heading"
    for command in latex.HEADING_COMMANDS:
        if re.match(rf"\\{command}\b", stripped):
            return "entry"
    for command in latex.ITEM_COMMANDS:
        if re.match(rf"\\{command}\b", stripped):
            return "bullet"
    return "line"


def _latex_heading_text(raw_line: str) -> str:
    """Extract the display text of a heading line."""
    masked = latex.mask_comments(raw_line)
    for command in latex.SECTION_COMMANDS:
        found = latex.command_argument(masked, command, masked.find(f"\\{command}"))
        if found is not None:
            return latex.to_plain_text(found[0])
    return latex.to_plain_text(raw_line)


def extract_latex_source(source: str, *, source_path: str, sha256: str) -> ExtractedDocument:
    """Extract blocks from LaTeX source text."""
    preamble, body = latex.find_preamble(source)
    body_offset = len(preamble)

    blocks: list[ExtractedBlock] = []
    sections: list[str] = []
    current_section: str | None = None
    warnings: list[str] = []

    for start, end, raw_line in _logical_lines(body, body_offset):
        masked_line = latex.mask_comments(raw_line).strip()
        if not masked_line:
            continue

        kind = _latex_block_kind(masked_line)
        if kind == "heading":
            heading_text = _latex_heading_text(raw_line)
            canonical = classify_section(heading_text)
            if canonical is not None:
                current_section = canonical
                if canonical not in sections:
                    sections.append(canonical)
            display = heading_text
        else:
            # Entries, bullets and loose lines keep their full text: for an entry
            # that means role, dates, employer and location stay in one quotable
            # unit, which is what makes "employer must appear in the evidence" a
            # meaningful check during fact validation.
            display = latex.to_plain_text(raw_line)

        if not display:
            continue

        # Line numbers are 1-based and counted in the original source, so a
        # locator can be opened in an editor directly.
        line_number = source.count("\n", 0, start) + 1
        blocks.append(
            ExtractedBlock(
                locator=f"line={line_number}",
                text=display,
                kind=kind,
                section=current_section,
                start_offset=start,
                end_offset=end,
            )
        )

    if not blocks:
        warnings.append("no readable content found in LaTeX source")

    return ExtractedDocument(
        source_type=SourceType.LATEX,
        source_path=source_path,
        sha256=sha256,
        raw_text=source,
        blocks=blocks,
        sections=sections,
        preamble_sha256=sha256_bytes(preamble.encode("utf-8")),
        warnings=warnings,
    )


# ----------------------------------------------------------------------- PDF


def _looks_like_heading(line: str) -> bool:
    words = line.split()
    if not words or len(words) > _MAX_HEADING_WORDS:
        return False
    if line.endswith((".", ",", ";")):
        return False
    letters = [char for char in line if char.isalpha()]
    if letters and all(char.isupper() for char in letters):
        return True
    return classify_section(line) is not None


def extract_pdf(path: Path, *, sha256: str) -> ExtractedDocument:
    """Extract per-line blocks from a PDF, one locator per visual line."""
    import pymupdf  # imported lazily: only PDF ingestion needs the native library

    blocks: list[ExtractedBlock] = []
    sections: list[str] = []
    current_section: str | None = None
    warnings: list[str] = []
    raw_pages: list[str] = []

    # pymupdf ships no type stubs; open() is untyped in a strict context.
    with pymupdf.open(path) as document:  # type: ignore[no-untyped-call]
        page_count = document.page_count
        for page_number, page in enumerate(document, start=1):
            page_text: str = page.get_text("text")
            raw_pages.append(page_text)

            for line_number, raw_line in enumerate(page_text.splitlines(), start=1):
                text = raw_line.strip()
                if not text:
                    continue

                kind = "line"
                if _BULLET_PREFIX.match(raw_line):
                    kind = "bullet"
                    text = _BULLET_PREFIX.sub("", raw_line).strip()
                elif _looks_like_heading(text):
                    canonical = classify_section(text)
                    if canonical is not None:
                        kind = "heading"
                        current_section = canonical
                        if canonical not in sections:
                            sections.append(canonical)

                if not text:
                    continue

                blocks.append(
                    ExtractedBlock(
                        locator=f"page={page_number};line={line_number}",
                        text=text,
                        kind=kind,
                        section=current_section,
                    )
                )

    if not blocks:
        # A scanned resume produces zero text. Say so plainly: silently returning
        # an empty profile would look like a parsing bug to the user.
        warnings.append(
            "no extractable text found; the PDF may be a scan, which this pipeline does not OCR"
        )

    return ExtractedDocument(
        source_type=SourceType.PDF,
        source_path=str(path),
        sha256=sha256,
        raw_text="\n".join(raw_pages),
        blocks=blocks,
        page_count=page_count,
        sections=sections,
        warnings=warnings,
    )


# ---------------------------------------------------------------------- text


def extract_plain_text(text: str, *, source_path: str, sha256: str) -> ExtractedDocument:
    blocks: list[ExtractedBlock] = []
    sections: list[str] = []
    current_section: str | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue

        kind = "line"
        content = stripped
        if _BULLET_PREFIX.match(raw_line):
            kind = "bullet"
            content = _BULLET_PREFIX.sub("", raw_line).strip()
        elif _looks_like_heading(stripped):
            canonical = classify_section(stripped)
            if canonical is not None:
                kind = "heading"
                current_section = canonical
                if canonical not in sections:
                    sections.append(canonical)

        blocks.append(
            ExtractedBlock(
                locator=f"line={line_number}",
                text=content,
                kind=kind,
                section=current_section,
            )
        )

    return ExtractedDocument(
        source_type=SourceType.TEXT,
        source_path=source_path,
        sha256=sha256,
        raw_text=text,
        blocks=blocks,
        sections=sections,
    )


def extract(path: Path, source_type: SourceType | None = None) -> ExtractedDocument:
    """Extract ``path`` according to its type."""
    if not path.is_file():
        raise ValidationFailed(f"resume file not found: {path}")

    resolved = source_type or detect_source_type(path)
    digest = sha256_file(path)

    if resolved is SourceType.PDF:
        return extract_pdf(path, sha256=digest)
    if resolved is SourceType.LATEX:
        return extract_latex_source(
            path.read_text(encoding="utf-8", errors="replace"),
            source_path=str(path),
            sha256=digest,
        )
    if resolved is SourceType.TEXT:
        return extract_plain_text(
            path.read_text(encoding="utf-8", errors="replace"),
            source_path=str(path),
            sha256=digest,
        )
    raise ValidationFailed(f"no extractor for source type {resolved.value}")
