"""Shared fixtures.

Every test runs against a throwaway SQLite file in ``tmp_path`` with the stub LLM
provider, so the suite needs no API key, no network and no shared state.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Importing the db package registers every model on Base.metadata before
# create_all() runs. Without it, table creation depends on import order.
from packages.core.db import Base
from packages.core.db.session import get_engine, get_session_factory, reset_engine
from packages.core.llm.client import LLMClient
from packages.core.llm.stub import StubProvider
from packages.core.settings import Settings, get_settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Isolated settings pointing at a temporary database and data directory."""
    monkeypatch.setenv("CA_APP_ENV", "test")
    monkeypatch.setenv("CA_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("CA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CA_LLM_PROVIDER", "stub")
    monkeypatch.setenv("CA_LLM_MAX_RETRIES", "2")

    get_settings.cache_clear()
    reset_engine()

    resolved = get_settings()
    resolved.ensure_dirs()
    yield resolved

    get_settings.cache_clear()
    reset_engine()


@pytest.fixture
def db(settings: Settings) -> Iterator[Session]:
    """A session against a freshly created schema.

    Uses ``create_all`` for speed; ``test_migrations.py`` separately asserts the
    migrations produce this same schema, so the shortcut cannot hide drift.
    """
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    """API client sharing the test database."""
    from apps.api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# ------------------------------------------------------------------ resumes

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def sample_tex_path() -> Path:
    """The fixture LaTeX resume."""
    return FIXTURES_DIR / "resume_sample.tex"


@pytest.fixture(scope="session")
def sample_pdf_path(sample_tex_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture resume compiled to a real PDF.

    Compiled once per session with the vendored tectonic, rather than committing a
    binary or hand-rolling a synthetic PDF: PDF extraction has to work against the
    kind of file a real LaTeX resume actually produces — ligatures, kerned lines and
    column layout included.

    Skips (does not fail) when the toolchain is absent, so the suite still runs on a
    machine where ``scripts/bootstrap.sh`` has not been run.
    """
    tectonic = REPO_ROOT / ".tooling" / "bin" / "tectonic"
    if not tectonic.exists():
        resolved = shutil.which("tectonic")
        if resolved is None:
            pytest.skip("tectonic not installed; run scripts/bootstrap.sh")
        tectonic = Path(resolved)

    outdir = tmp_path_factory.mktemp("compiled-resume")
    result = subprocess.run(  # noqa: S603 - fixed binary, no shell, fixture input
        [str(tectonic), "-X", "compile", str(sample_tex_path), "--outdir", str(outdir)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    pdf_path = outdir / f"{sample_tex_path.stem}.pdf"
    if result.returncode != 0 or not pdf_path.exists():
        pytest.skip(f"tectonic could not compile the fixture: {result.stderr[-400:]}")
    return pdf_path


@pytest.fixture
def candidate_id(db: Session) -> str:
    """A persisted candidate with no facts yet."""
    from services.candidate.service import CandidateService

    candidate = CandidateService(db).create_candidate(display_name="Fixture Candidate")
    db.commit()
    return candidate.id


@pytest.fixture
def stub_provider() -> StubProvider:
    return StubProvider()


@pytest.fixture
def llm(stub_provider: StubProvider, settings: Settings) -> LLMClient:
    return LLMClient(stub_provider, settings)
