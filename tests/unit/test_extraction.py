"""Extraction against the fixture resume."""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.core.errors import ValidationFailed
from packages.schemas.enums import SourceType
from packages.schemas.ingestion import ExtractedDocument
from services.candidate import extraction


@pytest.fixture(scope="module")
def tex_document(request: pytest.FixtureRequest) -> ExtractedDocument:
    path = Path(request.config.rootpath) / "tests" / "fixtures" / "resume_sample.tex"
    return extraction.extract(path, SourceType.LATEX)


class TestSectionClassification:
    @pytest.mark.parametrize(
        ("heading", "expected"),
        [
            ("Experience", "experience"),
            ("WORK EXPERIENCE", "experience"),
            ("Technical Skills", "skills"),
            ("Education", "education"),
            ("Selected Projects", "projects"),
            ("Publications", "publications"),
            ("Summary", "summary"),
            ("Certifications", "certifications"),
        ],
    )
    def test_known_headings(self, heading: str, expected: str) -> None:
        assert extraction.classify_section(heading) == expected

    def test_unrecognised_heading_is_none(self) -> None:
        assert extraction.classify_section("Miscellaneous Ramblings") is None

    def test_specific_phrase_wins_over_generic(self) -> None:
        # "technical skills" and "skills" both match; the canonical result is the
        # same, but the ordering guarantee matters for future divergent keys.
        assert extraction.classify_section("Technical Skills") == "skills"


class TestLatexExtraction:
    def test_detects_all_document_sections(self, tex_document: ExtractedDocument) -> None:
        assert tex_document.sections == [
            "summary",
            "experience",
            "projects",
            "skills",
            "education",
        ]

    def test_offsets_address_the_original_source(self, tex_document: ExtractedDocument) -> None:
        """Every block's offsets must slice its own source region.

        This is the property the M3 patcher depends on.
        """
        source = tex_document.raw_text
        checked = 0
        for block in tex_document.blocks:
            assert block.start_offset is not None
            assert block.end_offset is not None
            slice_text = source[block.start_offset : block.end_offset]
            # The rendered text is derived from this slice, so a marker word from
            # the block must appear in it.
            marker = max(block.text.split(), key=len) if block.text.split() else ""
            if len(marker) > 4 and "\\" not in marker:
                assert marker in slice_text or marker in slice_text.replace("\\%", "%")
                checked += 1
        assert checked > 10, "offset check did not exercise enough blocks"

    def test_line_locators_point_at_the_right_source_line(
        self, tex_document: ExtractedDocument
    ) -> None:
        lines = tex_document.raw_text.splitlines()
        block = next(b for b in tex_document.blocks if "p99 latency" in b.text)
        line_number = int(block.locator.removeprefix("line="))
        assert "p99 latency" in lines[line_number - 1]

    def test_preamble_is_hashed_and_excluded_from_blocks(
        self, tex_document: ExtractedDocument
    ) -> None:
        assert tex_document.preamble_sha256 is not None
        assert len(tex_document.preamble_sha256) == 64
        # Template macros must never be offered as evidence.
        assert not any("newcommand" in block.text for block in tex_document.blocks)
        assert not any("documentclass" in block.text for block in tex_document.blocks)

    def test_bullets_are_identified(self, tex_document: ExtractedDocument) -> None:
        bullets = [b for b in tex_document.blocks if b.kind == "bullet"]
        texts = " | ".join(b.text for b in bullets)
        assert "Mentored three engineers" in texts
        assert len(bullets) >= 6

    def test_custom_heading_macro_spanning_lines_is_one_block(
        self, tex_document: ExtractedDocument
    ) -> None:
        """``\\resumeSubheading`` spreads its four arguments over three lines.

        Regression: brace counting alone emitted three fragments, separating the
        employer from the role. That breaks evidence quoting and would defeat the
        "employer must appear in the cited evidence" guard.
        """
        block = next(b for b in tex_document.blocks if "Senior Machine Learning Engineer" in b.text)
        assert block.kind == "entry"
        assert "March 2022" in block.text
        assert "Infilect Technologies" in block.text
        assert "Bengaluru" in block.text

    def test_role_heading_does_not_change_the_current_section(
        self, tex_document: ExtractedDocument
    ) -> None:
        entry = next(b for b in tex_document.blocks if "National Institute" in b.text)
        assert entry.section == "education"
        assert entry.kind == "entry"

    def test_escaped_percent_survives_extraction(self, tex_document: ExtractedDocument) -> None:
        block = next(b for b in tex_document.blocks if "p99 latency" in b.text)
        assert "35%" in block.text

    def test_comments_are_never_extracted(self, tex_document: ExtractedDocument) -> None:
        assert not any("HEADING" in block.text for block in tex_document.blocks)
        assert not any("Fixture resume" in block.text for block in tex_document.blocks)

    def test_sections_are_attributed_to_blocks(self, tex_document: ExtractedDocument) -> None:
        skills_blocks = tex_document.blocks_in("skills")
        assert skills_blocks
        assert any("Python" in block.text for block in skills_blocks)

        experience_bullets = [b for b in tex_document.blocks_in("experience") if b.kind == "bullet"]
        assert len(experience_bullets) == 5


class TestPdfExtraction:
    def test_extracts_text_from_a_compiled_resume(self, sample_pdf_path: Path) -> None:
        document = extraction.extract(sample_pdf_path)
        assert document.source_type is SourceType.PDF
        assert document.page_count == 1
        assert document.blocks

        all_text = " ".join(block.text for block in document.blocks)
        assert "Priya Raghavan" in all_text
        assert "Infilect Technologies" in all_text
        assert "35%" in all_text

    def test_locators_carry_page_and_line(self, sample_pdf_path: Path) -> None:
        document = extraction.extract(sample_pdf_path)
        assert all(block.locator.startswith("page=1;line=") for block in document.blocks)

    def test_sections_are_detected_in_pdf_text(self, sample_pdf_path: Path) -> None:
        document = extraction.extract(sample_pdf_path)
        assert "experience" in document.sections
        assert "education" in document.sections

    def test_pdf_blocks_have_no_edit_offsets(self, sample_pdf_path: Path) -> None:
        """Offsets are meaningless for PDFs; leaving them None prevents misuse."""
        document = extraction.extract(sample_pdf_path)
        assert all(block.start_offset is None for block in document.blocks)


class TestPlainText:
    def test_bullets_and_headings(self, tmp_path: Path) -> None:
        path = tmp_path / "resume.txt"
        path.write_text("EXPERIENCE\n- Built a thing\n- Built another thing\n")
        document = extraction.extract(path)

        assert document.sections == ["experience"]
        bullets = [b for b in document.blocks if b.kind == "bullet"]
        assert [b.text for b in bullets] == ["Built a thing", "Built another thing"]


class TestDispatch:
    def test_unknown_extension_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "resume.docx"
        path.write_bytes(b"not really a docx")
        with pytest.raises(ValidationFailed, match="unsupported resume format"):
            extraction.extract(path)

    def test_missing_file_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationFailed, match="not found"):
            extraction.extract(tmp_path / "absent.tex")

    def test_hash_is_stable_and_content_addressed(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a.txt", tmp_path / "b.txt"
        first.write_text("same content")
        second.write_text("same content")
        assert extraction.sha256_file(first) == extraction.sha256_file(second)
