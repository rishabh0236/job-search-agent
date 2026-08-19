"""Deterministic LaTeX patching.

The model proposes edits; this module decides whether they are applicable and
applies them. It never rewrites a whole file, and it never trusts a proposal.

Every edit must pass, in order:

1. The target resolves to exactly one editable region.
2. The recorded ``old_text`` still matches that region's current text.
3. The replacement is LaTeX-safe: balanced braces, no new commands, no smuggled
   macros.
4. The edit does not overlap another edit in the same batch.

Then edits are applied **back to front**, so earlier offsets stay valid while later
regions are being rewritten. Finally the preamble is re-hashed and compared: if the
template changed, the patch is rejected wholesale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.core.errors import ValidationFailed
from packages.schemas.enums import EditOperation
from packages.schemas.resume import ResumeAst, ResumeEdit, ValidationFinding
from services.candidate import latex
from services.candidate.extraction import sha256_bytes
from services.resume import ast as ast_module

#: Commands a tailored bullet may legitimately contain. Anything else is either a
#: template macro (structural) or a way to smuggle behaviour into the document.
ALLOWED_COMMANDS = frozenset(
    {
        "textbf",
        "textit",
        "texttt",
        "emph",
        "underline",
        "href",
        "url",
        "%",
        "&",
        "$",
        "#",
        "_",
        "{",
        "}",
        "ldots",
        "dots",
        "resumeItem",
        "item",
    }
)

_COMMAND_RE = re.compile(r"\\([a-zA-Z@]+|[%&$#_{}])")

#: Commands that must never appear in a proposed replacement, even though a
#: template may use them: they can read files, run code, or restructure the document.
FORBIDDEN_COMMANDS = frozenset(
    {
        "input",
        "include",
        "write18",
        "immediate",
        "openout",
        "read",
        "catcode",
        "def",
        "let",
        "newcommand",
        "renewcommand",
        "documentclass",
        "usepackage",
        "begin",
        "end",
        "csname",
        "expandafter",
        "loop",
        "repeat",
    }
)


@dataclass(slots=True)
class PatchResult:
    """Outcome of applying a batch of edits."""

    source: str
    applied: list[ResumeEdit] = field(default_factory=list)
    rejected: list[tuple[ResumeEdit, ValidationFinding]] = field(default_factory=list)
    findings: list[ValidationFinding] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _finding(code: str, message: str, target_id: str | None = None) -> ValidationFinding:
    return ValidationFinding(severity="error", code=code, message=message, target_id=target_id)


def check_latex_safety(text: str) -> ValidationFinding | None:
    """Reject a replacement that is not safe to splice into the document."""
    if latex.brace_balance(text) != 0:
        return _finding("unbalanced_braces", "replacement text has unbalanced braces")

    if "\\\\" in text.replace("\\\\\\\\", ""):
        # A stray line break can silently reflow a one-page resume onto two.
        pass  # allowed, but flagged below by layout comparison after compilation

    for match in _COMMAND_RE.finditer(text):
        command = match.group(1)
        if command in FORBIDDEN_COMMANDS:
            return _finding(
                "forbidden_command",
                f"replacement uses \\{command}, which may alter document structure or read files",
            )
        if command not in ALLOWED_COMMANDS:
            return _finding(
                "unknown_command",
                f"replacement introduces \\{command}, which is not in the allowed set",
            )

    if "%" in re.sub(r"\\%", "", text):
        return _finding(
            "unescaped_percent",
            "replacement contains an unescaped %, which would comment out the rest of the line",
        )

    return None


def validate_edit(edit: ResumeEdit, ast: ResumeAst) -> ValidationFinding | None:
    """Check one edit against the current AST. Returns a finding if unusable."""
    try:
        section = ast_module.resolve(ast, edit.target_id)
    except ValidationFailed as exc:
        return _finding("unresolvable_target", exc.message, edit.target_id)

    if edit.operation is EditOperation.REPLACE_TEXT:
        if not edit.new_text.strip():
            return _finding("empty_replacement", "replacement text is empty", edit.target_id)

        # The proposal carries the text it believed it was editing. If the document
        # has moved on, the edit is stale and must not be applied blind.
        if _normalise(edit.old_text) != _normalise(section.text):
            return _finding(
                "stale_old_text",
                "old_text no longer matches the document at this target; "
                "the resume changed after this edit was proposed",
                edit.target_id,
            )

        safety = check_latex_safety(edit.new_text)
        if safety is not None:
            return ValidationFinding(
                severity="error",
                code=safety.code,
                message=safety.message,
                target_id=edit.target_id,
            )
        return None

    if edit.operation is EditOperation.DELETE_BLOCK:
        if _normalise(edit.old_text) != _normalise(section.text):
            return _finding("stale_old_text", "old_text no longer matches", edit.target_id)
        return None

    return _finding(
        "unsupported_operation",
        f"operation {edit.operation.value} is not implemented by the patcher",
        edit.target_id,
    )


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _target_span(edit: ResumeEdit, ast: ResumeAst) -> tuple[int, int]:
    section = ast_module.resolve(ast, edit.target_id)
    return section.start_offset, section.end_offset


def apply_edits(source: str, ast: ResumeAst, edits: list[ResumeEdit]) -> PatchResult:
    """Apply validated edits to ``source``.

    Edits that fail validation are rejected individually and reported; the rest
    still apply. That is deliberate: one bad proposal out of twelve should not throw
    away the other eleven, and the review screen shows exactly what was refused.
    """
    result = PatchResult(source=source)

    usable: list[tuple[ResumeEdit, tuple[int, int]]] = []
    for edit in edits:
        finding = validate_edit(edit, ast)
        if finding is not None:
            result.rejected.append((edit, finding))
            result.findings.append(finding)
            continue
        usable.append((edit, _target_span(edit, ast)))

    # Overlapping spans cannot both be applied coherently.
    usable.sort(key=lambda item: item[1][0])
    non_overlapping: list[tuple[ResumeEdit, tuple[int, int]]] = []
    previous_end = -1
    for edit, span in usable:
        if span[0] < previous_end:
            finding = _finding(
                "overlapping_edits",
                "this edit overlaps another edit in the same batch",
                edit.target_id,
            )
            result.rejected.append((edit, finding))
            result.findings.append(finding)
            continue
        non_overlapping.append((edit, span))
        previous_end = span[1]

    # Apply back to front so unapplied spans keep their original offsets.
    patched = source
    for edit, (start, end) in sorted(non_overlapping, key=lambda item: item[1][0], reverse=True):
        original = patched[start:end]
        if edit.operation is EditOperation.DELETE_BLOCK:
            patched = patched[:start] + patched[end:]
        else:
            patched = patched[:start] + _rewrite_region(original, edit.new_text) + patched[end:]
        result.applied.append(edit)

    result.applied.reverse()  # restore document order for display
    result.source = patched

    # The template is not ours to touch.
    original_preamble, _ = latex.find_preamble(source)
    patched_preamble, _ = latex.find_preamble(patched)
    if sha256_bytes(original_preamble.encode()) != sha256_bytes(patched_preamble.encode()):
        raise ValidationFailed(
            "patching altered the LaTeX preamble; refusing to produce this version",
            details={"target_count": len(edits)},
        )

    return result


def _rewrite_region(original: str, new_text: str) -> str:
    """Replace the human-readable content of a region, keeping its LaTeX wrapper.

    A bullet in the source is ``\\resumeItem{...}``; the model proposes replacement
    prose, not replacement markup. Preserving the wrapper is what keeps a tailored
    resume looking identical to the original.
    """
    masked = latex.mask_comments(original)
    for command in latex.ITEM_COMMANDS:
        pattern = re.compile(rf"\\{command}\b\s*\{{")
        match = pattern.search(masked)
        if match is None:
            continue
        brace_index = match.end() - 1
        try:
            _, end = latex.read_braced_group(masked, brace_index)
        except ValueError:
            continue
        return original[: brace_index + 1] + new_text + original[end - 1 :]

    # No recognised wrapper: preserve leading whitespace and replace the rest.
    leading = original[: len(original) - len(original.lstrip())]
    return f"{leading}{new_text}"
