"""Resume AST: addressable, editable regions of a LaTeX document.

Built on the extractor from M1, so the offsets are the same ones already tested to
slice their own source. Two properties make editing safe:

* **Stable target ids.** ``experience.1.bullet.2`` addresses a bullet by its place
  in the document structure, not by a line number that shifts the moment anything
  above it changes. An edit proposal made against one version can be checked
  against another.
* **Unique addressing.** Applying an edit requires the target to resolve to exactly
  one region *and* the recorded ``old_text`` to still match. Ambiguity is a refusal,
  never a guess.
"""

from __future__ import annotations

from packages.core.errors import ValidationFailed
from packages.schemas.ingestion import ExtractedBlock, ExtractedDocument
from packages.schemas.resume import ResumeAst, ResumeSection

#: Block kinds that may be edited. Section headings and the preamble are structural:
#: renaming "Experience" or touching template macros is not tailoring.
EDITABLE_KINDS = frozenset({"bullet", "line", "entry"})


def _slug(section: str | None) -> str:
    return section or "unlabelled"


def build_ast(document: ExtractedDocument, resume_id: str) -> ResumeAst:
    """Derive an AST from an extracted LaTeX document.

    Target ids are assigned per section and per kind, in document order, so they are
    reproducible for identical input and legible in a diff.
    """
    if document.preamble_sha256 is None:
        raise ValidationFailed(
            "an editable resume must be LaTeX with a detectable preamble",
            details={"source_type": document.source_type.value},
        )

    sections: list[ResumeSection] = []
    counters: dict[tuple[str, str], int] = {}
    entry_index: dict[str, int] = {}

    for block in document.blocks:
        if block.start_offset is None or block.end_offset is None:
            continue

        section = _slug(block.section)

        if block.kind == "entry":
            # Entries number within their section and reset the bullet counter, so a
            # bullet id reads as "the second bullet of the first role".
            entry_index[section] = entry_index.get(section, 0) + 1
            counters[(section, "bullet")] = 0
            target_id = f"{section}.entry.{entry_index[section]}"
        elif block.kind == "bullet":
            key = (section, "bullet")
            counters[key] = counters.get(key, 0) + 1
            position = entry_index.get(section, 0)
            target_id = (
                f"{section}.{position}.bullet.{counters[key]}"
                if position
                else f"{section}.bullet.{counters[key]}"
            )
        elif block.kind == "heading":
            target_id = f"{section}.heading"
        else:
            key = (section, "line")
            counters[key] = counters.get(key, 0) + 1
            target_id = f"{section}.line.{counters[key]}"

        sections.append(
            ResumeSection(
                target_id=target_id,
                kind=block.kind,
                title=block.text if block.kind == "heading" else None,
                text=block.text,
                start_offset=block.start_offset,
                end_offset=block.end_offset,
            )
        )

    return ResumeAst(
        resume_id=resume_id,
        preamble_sha256=document.preamble_sha256,
        sections=sections,
    )


def editable_targets(ast: ResumeAst) -> list[ResumeSection]:
    """Regions a tailoring pass is allowed to touch."""
    return [section for section in ast.sections if section.kind in EDITABLE_KINDS]


def resolve(ast: ResumeAst, target_id: str) -> ResumeSection:
    """Resolve a target id to exactly one region.

    Raises rather than picking one when the id is ambiguous: silently editing the
    wrong bullet is the worst available outcome.
    """
    matches = [section for section in ast.sections if section.target_id == target_id]
    if not matches:
        raise ValidationFailed(
            f"unknown edit target {target_id!r}",
            details={"available": [section.target_id for section in ast.sections][:40]},
        )
    if len(matches) > 1:
        raise ValidationFailed(
            f"edit target {target_id!r} is ambiguous ({len(matches)} matches)",
            details={"target_id": target_id},
        )
    section = matches[0]
    if section.kind not in EDITABLE_KINDS:
        raise ValidationFailed(
            f"target {target_id!r} is structural ({section.kind}) and must not be edited",
            details={"kind": section.kind},
        )
    return section


def blocks_by_target(ast: ResumeAst) -> dict[str, ExtractedBlock]:
    """Convenience view for prompts: target id -> the text at that target."""
    return {
        section.target_id: ExtractedBlock(
            locator=f"offset={section.start_offset}",
            text=section.text,
            kind=section.kind,
        )
        for section in ast.sections
    }
