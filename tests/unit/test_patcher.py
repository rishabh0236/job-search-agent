"""LaTeX AST addressing and patching.

The patcher is the most dangerous component in the product: a bug here corrupts the
document a candidate sends to an employer. Every test asserts a refusal or an exact
outcome — nothing here tolerates "close enough".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.core.errors import ValidationFailed
from packages.schemas.enums import EditOperation, SourceType
from packages.schemas.resume import ResumeAst, ResumeEdit, ResumeSection
from services.candidate import extraction
from services.resume import ast as ast_module
from services.resume import patcher

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "resume_sample.tex"


@pytest.fixture(scope="module")
def source() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ast(source: str) -> ResumeAst:
    document = extraction.extract_latex_source(source, source_path=str(FIXTURE), sha256="0" * 64)
    return ast_module.build_ast(document, "res_1")


def _edit(target_id: str, old_text: str, new_text: str) -> ResumeEdit:
    return ResumeEdit(
        id="edit_1",
        resume_id="res_1",
        operation=EditOperation.REPLACE_TEXT,
        target_id=target_id,
        old_text=old_text,
        new_text=new_text,
    )


def _bullet(ast: ResumeAst, phrase: str) -> ResumeSection:
    return next(s for s in ast.sections if phrase in s.text and s.kind == "bullet")


class TestAst:
    def test_target_ids_are_structural_and_readable(self, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "p99 latency")
        assert bullet.target_id.startswith("experience.1.bullet.")

    def test_bullets_are_numbered_within_their_entry(self, ast: ResumeAst) -> None:
        """The second role's bullets restart at 1, so ids survive edits above them."""
        second_role = [s for s in ast.sections if s.target_id.startswith("experience.2.bullet.")]
        assert len(second_role) == 2

    def test_ids_are_unique(self, ast: ResumeAst) -> None:
        ids = [section.target_id for section in ast.sections]
        assert len(ids) == len(set(ids))

    def test_ids_are_reproducible(self, source: str) -> None:
        def build() -> list[str]:
            document = extraction.extract_latex_source(source, source_path="x", sha256="0" * 64)
            return [s.target_id for s in ast_module.build_ast(document, "res_1").sections]

        assert build() == build()

    def test_headings_are_not_editable(self, ast: ResumeAst) -> None:
        heading = next(s for s in ast.sections if s.kind == "heading")
        with pytest.raises(ValidationFailed, match="structural"):
            ast_module.resolve(ast, heading.target_id)

    def test_unknown_target_is_refused(self, ast: ResumeAst) -> None:
        with pytest.raises(ValidationFailed, match="unknown edit target"):
            ast_module.resolve(ast, "experience.99.bullet.99")

    def test_preamble_hash_is_recorded(self, ast: ResumeAst) -> None:
        assert len(ast.preamble_sha256) == 64

    def test_pdf_documents_cannot_produce_an_ast(self) -> None:
        document = extraction.extract_plain_text("text", source_path="x", sha256="0" * 64)
        object.__setattr__(document, "source_type", SourceType.PDF)
        with pytest.raises(ValidationFailed, match="detectable preamble"):
            ast_module.build_ast(document, "res_1")


class TestLatexSafety:
    def test_balanced_prose_is_accepted(self) -> None:
        assert patcher.check_latex_safety("Cut \\textbf{p99 latency} by 35\\%") is None

    def test_unbalanced_braces_are_rejected(self) -> None:
        finding = patcher.check_latex_safety("Cut latency by {35")
        assert finding is not None and finding.code == "unbalanced_braces"

    @pytest.mark.parametrize(
        "command",
        ["\\input{/etc/passwd}", "\\write18{rm -rf /}", "\\def\\x{y}", "\\usepackage{tikz}"],
    )
    def test_dangerous_commands_are_rejected(self, command: str) -> None:
        finding = patcher.check_latex_safety(f"Text {command} more")
        assert finding is not None
        assert finding.code in ("forbidden_command", "unknown_command")

    def test_unescaped_percent_is_rejected(self) -> None:
        """An unescaped % comments out the rest of the line and silently drops text."""
        finding = patcher.check_latex_safety("Improved latency by 35% overall")
        assert finding is not None and finding.code == "unescaped_percent"

    def test_escaped_percent_is_allowed(self) -> None:
        assert patcher.check_latex_safety("Improved latency by 35\\%") is None


class TestValidation:
    def test_stale_old_text_is_refused(self, ast: ResumeAst) -> None:
        """The document moved on since the proposal; applying blind would corrupt it."""
        bullet = _bullet(ast, "p99 latency")
        finding = patcher.validate_edit(
            _edit(bullet.target_id, "text that is not in the document", "New text"), ast
        )
        assert finding is not None and finding.code == "stale_old_text"

    def test_matching_old_text_validates(self, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "p99 latency")
        assert patcher.validate_edit(_edit(bullet.target_id, bullet.text, "Rewritten"), ast) is None

    def test_whitespace_differences_are_tolerated(self, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "p99 latency")
        spaced = f"  {bullet.text}   "
        assert patcher.validate_edit(_edit(bullet.target_id, spaced, "Rewritten"), ast) is None

    def test_empty_replacement_is_refused(self, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "p99 latency")
        finding = patcher.validate_edit(_edit(bullet.target_id, bullet.text, "   "), ast)
        assert finding is not None and finding.code == "empty_replacement"


class TestApply:
    def test_single_edit_replaces_only_that_bullet(self, source: str, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "Mentored three engineers")
        result = patcher.apply_edits(
            source, ast, [_edit(bullet.target_id, bullet.text, "Mentored three new engineers")]
        )

        assert len(result.applied) == 1
        assert "Mentored three new engineers" in result.source
        assert "Mentored three engineers joining" not in result.source
        # Everything else is untouched.
        assert "p99 latency by 35\\%" in result.source
        assert "Zeta Systems" in result.source

    def test_latex_wrapper_is_preserved(self, source: str, ast: ResumeAst) -> None:
        """The tailored file must still be the same template."""
        bullet = _bullet(ast, "Mentored three engineers")
        result = patcher.apply_edits(
            source, ast, [_edit(bullet.target_id, bullet.text, "Mentored three engineers well")]
        )
        assert "\\resumeItem{Mentored three engineers well}" in result.source

    def test_multiple_edits_apply_without_corrupting_offsets(
        self, source: str, ast: ResumeAst
    ) -> None:
        first = _bullet(ast, "Mentored three engineers")
        second = _bullet(ast, "Migrated batch reporting")
        edits = [
            _edit(first.target_id, first.text, "Mentored three platform engineers"),
            _edit(second.target_id, second.text, "Moved batch reporting onto Apache Airflow"),
        ]
        result = patcher.apply_edits(source, ast, edits)

        assert len(result.applied) == 2
        assert "Mentored three platform engineers" in result.source
        assert "Moved batch reporting onto Apache Airflow" in result.source

    def test_preamble_is_never_modified(self, source: str, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "Mentored three engineers")
        result = patcher.apply_edits(
            source, ast, [_edit(bullet.target_id, bullet.text, "Rewritten")]
        )

        original_preamble = source.split("\\begin{document}")[0]
        patched_preamble = result.source.split("\\begin{document}")[0]
        assert original_preamble == patched_preamble

    def test_rejected_edits_do_not_block_valid_ones(self, source: str, ast: ResumeAst) -> None:
        """One bad proposal must not throw away the good ones."""
        good = _bullet(ast, "Mentored three engineers")
        edits = [
            _edit(good.target_id, good.text, "Mentored three senior engineers"),
            _edit("experience.99.bullet.1", "nonexistent", "should be rejected"),
        ]
        result = patcher.apply_edits(source, ast, edits)

        assert len(result.applied) == 1
        assert len(result.rejected) == 1
        assert "Mentored three senior engineers" in result.source

    def test_overlapping_edits_are_refused(self, source: str, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "Mentored three engineers")
        edits = [
            _edit(bullet.target_id, bullet.text, "First rewrite"),
            _edit(bullet.target_id, bullet.text, "Second rewrite"),
        ]
        result = patcher.apply_edits(source, ast, edits)

        assert len(result.applied) == 1
        assert any(f.code == "overlapping_edits" for f in result.findings)

    def test_no_edits_leaves_the_source_identical(self, source: str, ast: ResumeAst) -> None:
        result = patcher.apply_edits(source, ast, [])
        assert result.source == source
        assert result.changed is False

    def test_deletion_removes_the_region(self, source: str, ast: ResumeAst) -> None:
        bullet = _bullet(ast, "Mentored three engineers")
        edit = _edit(bullet.target_id, bullet.text, "")
        edit = edit.model_copy(update={"operation": EditOperation.DELETE_BLOCK})
        result = patcher.apply_edits(source, ast, [edit])
        assert "Mentored three engineers" not in result.source
