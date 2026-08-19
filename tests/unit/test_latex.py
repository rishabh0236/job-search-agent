"""LaTeX source handling.

Offset preservation is the load-bearing property: the M3 patcher will address spans
by offset, so an off-by-N in comment masking would corrupt a resume.
"""

from __future__ import annotations

import pytest

from services.candidate import latex


class TestMaskComments:
    def test_length_and_offsets_are_preserved(self) -> None:
        source = "\\section{Skills} % a trailing comment\nPython\n"
        masked = latex.mask_comments(source)
        assert len(masked) == len(source)
        # Every newline stays at the same index, so line numbers still line up.
        assert [i for i, c in enumerate(masked) if c == "\n"] == [
            i for i, c in enumerate(source) if c == "\n"
        ]

    def test_comment_body_is_blanked(self) -> None:
        masked = latex.mask_comments("text % secret\nmore")
        assert "secret" not in masked
        assert masked.startswith("text ")
        assert masked.endswith("\nmore")

    def test_escaped_percent_is_not_a_comment(self) -> None:
        # "35\%" is a literal percent sign in a resume bullet, not a comment.
        source = r"Cut latency by 35\% across the fleet"
        assert latex.mask_comments(source) == source

    def test_full_line_comment_becomes_blank(self) -> None:
        masked = latex.mask_comments("%----------HEADING----------\n\\section{X}")
        assert masked.splitlines()[0].strip() == ""
        assert "\\section{X}" in masked


class TestBraceGroups:
    def test_reads_balanced_group(self) -> None:
        content, end = latex.read_braced_group("{outer {inner} tail} rest", 0)
        assert content == "outer {inner} tail"
        assert end == len("{outer {inner} tail}")

    def test_escaped_brace_is_not_structural(self) -> None:
        content, _ = latex.read_braced_group(r"{a \{ b}", 0)
        assert content == r"a \{ b"

    def test_unbalanced_group_raises(self) -> None:
        with pytest.raises(ValueError, match="unbalanced"):
            latex.read_braced_group("{never closed", 0)

    def test_balance_counts_net_depth(self) -> None:
        assert latex.brace_balance("\\resumeSubheading{a}{b") == 1
        assert latex.brace_balance("{a}{b}") == 0
        assert latex.brace_balance("}") == -1


class TestToPlainText:
    def test_unwraps_formatting_commands(self) -> None:
        assert latex.to_plain_text(r"\textbf{Senior Engineer}") == "Senior Engineer"

    def test_keeps_href_label_not_url(self) -> None:
        rendered = latex.to_plain_text(r"\href{mailto:a@b.com}{a@b.com}")
        assert rendered == "a@b.com"

    def test_unescapes_percent_and_ampersand(self) -> None:
        assert "35%" in latex.to_plain_text(r"Cut latency by 35\%")

    def test_drops_unknown_commands_without_inventing_words(self) -> None:
        rendered = latex.to_plain_text(r"\vspace{1pt} \small Python, Go")
        assert "Python, Go" in rendered
        assert "vspace" not in rendered
        assert "small" not in rendered

    def test_collapses_whitespace(self) -> None:
        assert latex.to_plain_text("a    \n   b") == "a b"


class TestPreamble:
    def test_splits_at_begin_document(self) -> None:
        source = "\\documentclass{article}\n\\begin{document}\nBody text\n\\end{document}"
        preamble, body = latex.find_preamble(source)
        assert preamble.endswith("\\begin{document}")
        assert "Body text" in body
        assert "documentclass" not in body

    def test_fragment_without_document_is_all_body(self) -> None:
        preamble, body = latex.find_preamble("just some text")
        assert preamble == ""
        assert body == "just some text"

    def test_commented_out_begin_document_is_ignored(self) -> None:
        source = "% \\begin{document}\n\\begin{document}\nreal body"
        _, body = latex.find_preamble(source)
        assert body.strip() == "real body"
