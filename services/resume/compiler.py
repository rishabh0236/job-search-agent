"""LaTeX compilation and post-compile validation.

The pipeline the skills file mandates: patch -> compile -> validate -> preview ->
approval. This module owns the middle two.

Compilation runs in a scratch directory with a timeout, no shell, and a fixed
binary. A tailored resume that does not compile is never offered for use, and one
that compiles but changed page count or lost content is flagged loudly.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from packages.core.errors import CompilationFailed
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings
from packages.schemas.resume import CompileResult, ValidationFinding

logger = get_logger(__name__)

#: Keep log excerpts short but useful: the first error is what matters.
_LOG_EXCERPT_CHARS = 1500
_LATEX_ERROR_RE = re.compile(r"^!.*$", re.MULTILINE)


class LatexCompiler(Protocol):
    """The seam. tectonic today; Docker TeX Live or a compile worker later."""

    engine: str

    def available(self) -> bool: ...

    def compile(self, source: str, *, workdir: Path | None = None) -> CompileResult: ...


@dataclass(slots=True)
class TectonicCompiler:
    """Compiles via the vendored ``tectonic`` binary."""

    binary: Path
    timeout_seconds: int = 120
    engine: str = "tectonic"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> TectonicCompiler:
        resolved = settings or get_settings()
        binary = resolved.latex_bin
        if not binary.exists():
            found = shutil.which(resolved.latex_engine)
            if found:
                binary = Path(found)
        return cls(
            binary=binary,
            timeout_seconds=resolved.latex_timeout_seconds,
            engine=resolved.latex_engine,
        )

    def available(self) -> bool:
        return self.binary.exists()

    def compile(self, source: str, *, workdir: Path | None = None) -> CompileResult:
        if not self.available():
            raise CompilationFailed(
                f"LaTeX engine not found at {self.binary}; run scripts/bootstrap.sh",
                details={"engine": self.engine},
            )

        owns_workdir = workdir is None
        directory = workdir or Path(tempfile.mkdtemp(prefix="career-agent-latex-"))
        directory.mkdir(parents=True, exist_ok=True)
        tex_path = directory / "resume.tex"
        tex_path.write_text(source, encoding="utf-8")

        started = time.monotonic()
        try:
            process = subprocess.run(  # noqa: S603 - fixed binary, no shell, temp cwd
                [
                    str(self.binary),
                    "-X",
                    "compile",
                    str(tex_path),
                    "--outdir",
                    str(directory),
                    "--keep-logs",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=directory,
            )
            output = f"{process.stdout}\n{process.stderr}"
            success = process.returncode == 0
        except subprocess.TimeoutExpired:
            # A runaway compile is a failure, not something to wait out.
            return CompileResult(
                success=False,
                log_excerpt=f"compilation exceeded {self.timeout_seconds}s and was terminated",
                duration_ms=int((time.monotonic() - started) * 1000),
                engine=self.engine,
            )
        finally:
            if owns_workdir:
                pass  # caller inspects the directory; cleanup is theirs

        duration_ms = int((time.monotonic() - started) * 1000)
        pdf_path = directory / "resume.pdf"

        if not success or not pdf_path.exists():
            errors = _LATEX_ERROR_RE.findall(output)
            excerpt = "\n".join(errors[:5]) if errors else output[-_LOG_EXCERPT_CHARS:]
            logger.warning(
                "resume.compile_failed",
                extra={"engine": self.engine, "duration_ms": duration_ms},
            )
            return CompileResult(
                success=False,
                log_excerpt=excerpt.strip()[:_LOG_EXCERPT_CHARS],
                duration_ms=duration_ms,
                engine=self.engine,
            )

        return CompileResult(
            success=True,
            pdf_path=str(pdf_path),
            log_excerpt="",
            page_count=_page_count(pdf_path),
            duration_ms=duration_ms,
            engine=self.engine,
        )


def _page_count(pdf_path: Path) -> int | None:
    try:
        import pymupdf

        with pymupdf.open(pdf_path) as document:  # type: ignore[no-untyped-call]
            return int(document.page_count)
    except Exception:
        return None


def pdf_text(pdf_path: Path) -> str:
    """Extract text from a compiled PDF, for factuality comparison."""
    try:
        import pymupdf

        with pymupdf.open(pdf_path) as document:  # type: ignore[no-untyped-call]
            return "\n".join(page.get_text("text") for page in document)
    except Exception:
        return ""


def compare_output(
    original: CompileResult,
    tailored: CompileResult,
    *,
    original_text: str,
    tailored_text: str,
    expected_removals: list[str] | None = None,
) -> list[ValidationFinding]:
    """Compare a tailored PDF against the original (FR-36).

    Checks that layout did not blow up and that no content vanished unintentionally.
    Both are things a reviewer would catch by eye, and both are easy to miss when
    the diff looks reasonable.
    """
    findings: list[ValidationFinding] = []

    if original.page_count and tailored.page_count:
        if tailored.page_count > original.page_count:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    code="page_count_increased",
                    message=(
                        f"tailored resume is {tailored.page_count} pages, original was "
                        f"{original.page_count}; a reflow may have pushed content over"
                    ),
                )
            )
        elif tailored.page_count < original.page_count:
            findings.append(
                ValidationFinding(
                    severity="warning",
                    code="page_count_decreased",
                    message=(
                        f"tailored resume is {tailored.page_count} pages, original was "
                        f"{original.page_count}; check nothing was dropped"
                    ),
                )
            )

    removals = {item.lower() for item in (expected_removals or [])}
    lost = _lost_markers(original_text, tailored_text, removals)
    if lost:
        findings.append(
            ValidationFinding(
                severity="error",
                code="content_lost",
                message=(
                    "content present in the original is missing from the tailored PDF: "
                    + "; ".join(lost[:5])
                ),
            )
        )

    if tailored_text and len(tailored_text) < len(original_text) * 0.6:
        findings.append(
            ValidationFinding(
                severity="error",
                code="content_shrank",
                message=(
                    "tailored PDF has substantially less text than the original; "
                    "this is more likely a patching fault than an intended edit"
                ),
            )
        )

    return findings


#: Tokens worth checking for survival: proper nouns, years, and figures. Ordinary
#: prose legitimately changes during tailoring; these should not vanish.
_MARKER_RE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9&.+-]{2,}|(?:19|20)\d{2}|\d+(?:\.\d+)?%)\b")


def _lost_markers(original_text: str, tailored_text: str, allowed: set[str]) -> list[str]:
    if not original_text or not tailored_text:
        return []
    tailored_lower = tailored_text.lower()
    lost: list[str] = []
    for marker in dict.fromkeys(_MARKER_RE.findall(original_text)):
        lowered = marker.lower()
        if lowered in allowed:
            continue
        if lowered not in tailored_lower:
            lost.append(marker)
    return lost


def build_compiler(settings: Settings | None = None) -> LatexCompiler:
    return TectonicCompiler.from_settings(settings)
