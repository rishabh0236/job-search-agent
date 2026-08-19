"""Deterministic fact extraction and normalization.

Anything a regular expression can settle should not cost a model call: contact
details, links, date ranges and skill lists are extracted here with confidence 1.0
and exact evidence. The LLM is then only asked about the genuinely interpretive
parts — what a role involved, what an achievement was.

This also means a candidate profile is never empty just because no API key is
configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.schemas.enums import FactCategory
from packages.schemas.ingestion import ExtractedBlock, ExtractedDocument

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s,;)]+", re.IGNORECASE)
LINKEDIN_RE = re.compile(r"\b(?:linkedin\.com/in/|linkedin:\s*)([\w-]+)", re.IGNORECASE)
GITHUB_RE = re.compile(r"\b(?:github\.com/|github:\s*)([\w-]+)", re.IGNORECASE)

#: Requires 8+ digits so years, ZIP codes and "2019-2021" are not read as phones.
PHONE_RE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])")

MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DATE_TOKEN = rf"(?:(?:{MONTHS})\.?\s*)?(?:19|20)\d{{2}}"
DATE_RANGE_RE = re.compile(
    # The en/em dashes are deliberate: typeset resumes use them far more often
    # than a plain hyphen, and a PDF extraction preserves whichever was used.
    rf"(?P<start>{_DATE_TOKEN})\s*(?:-|--|–|—|to|until|through)\s*"  # noqa: RUF001
    rf"(?P<end>{_DATE_TOKEN}|present|current|now|ongoing)",
    re.IGNORECASE,
)

_SKILL_SPLIT = re.compile(r"[,;|/•·]|\s{2,}|\s+and\s+")
# The fullwidth colon is intentional — it appears in resumes typeset with CJK fonts.
_LABEL_SPLIT = re.compile(r"^\s*([\w /+#.-]{2,40})\s*[:：]\s*(.+)$")  # noqa: RUF001

#: Canonical spellings for skills that appear many ways. Kept small and explicit —
#: an aggressive alias table starts silently rewriting things the candidate wrote.
SKILL_ALIASES: dict[str, str] = {
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "py": "Python",
    "python": "Python",
    "python3": "Python",
    "golang": "Go",
    "go": "Go",
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "psql": "PostgreSQL",
    "tf": "TensorFlow",
    "tensorflow": "TensorFlow",
    "pytorch": "PyTorch",
    "torch": "PyTorch",
    "sklearn": "scikit-learn",
    "scikit learn": "scikit-learn",
    "scikit-learn": "scikit-learn",
    "aws": "AWS",
    "gcp": "GCP",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    "rest": "REST",
    "graphql": "GraphQL",
    "sql": "SQL",
    "nosql": "NoSQL",
    "c++": "C++",
    "c#": "C#",
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "react": "React",
    "opencv": "OpenCV",
    "docker": "Docker",
    "terraform": "Terraform",
    "spark": "Apache Spark",
    "airflow": "Apache Airflow",
}

#: Words that mean a skills line is really a label, not a skill.
_SKILL_STOPWORDS = frozenset(
    {
        "languages",
        "language",
        "frameworks",
        "framework",
        "tools",
        "tooling",
        "technologies",
        "libraries",
        "skills",
        "developer tools",
        "databases",
        "platforms",
        "cloud",
        "other",
        "etc",
    }
)

_MAX_SKILL_WORDS = 5


@dataclass(slots=True)
class DeterministicFact:
    """A fact extracted by code, with the locators that prove it."""

    category: FactCategory
    claim: str
    locators: list[str]
    attributes: dict[str, str] = field(default_factory=dict)
    confidence: float = 1.0


def normalize_skill(raw: str) -> str | None:
    """Canonicalise a skill name, or None if it is not a plausible skill."""
    cleaned = raw.strip().strip(".,;:()[]{}").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) < 2 or len(cleaned.split()) > _MAX_SKILL_WORDS:
        return None
    if cleaned.lower() in _SKILL_STOPWORDS:
        return None
    if not any(char.isalnum() for char in cleaned):
        return None

    alias = SKILL_ALIASES.get(cleaned.lower())
    if alias is not None:
        return alias
    # Preserve the candidate's own capitalisation for anything not in the table:
    # rewriting "Kubeflow" to "kubeflow" would be an unrequested edit.
    return cleaned


def parse_date_range(text: str) -> dict[str, str] | None:
    """Extract ``start``/``end`` from a date range, marking current roles.

    Returns None when no range is present. Never guesses a missing endpoint — an
    unbounded range stays unbounded rather than becoming "present".
    """
    match = DATE_RANGE_RE.search(text)
    if match is None:
        return None

    end_raw = match.group("end").strip()
    is_current = end_raw.lower() in ("present", "current", "now", "ongoing")
    parsed = {
        "start_date": match.group("start").strip(),
        "end_date": "present" if is_current else end_raw,
    }
    if is_current:
        parsed["is_current"] = "true"
    return parsed


_YEAR_PAIR_RE = re.compile(r"^\s*(?:19|20)\d{2}\s*[^\d]{1,3}\s*(?:19|20)\d{2}\s*$")


def _looks_like_year_pair(text: str) -> bool:
    """True for "2015-2019" style spans, which are dates and never phone numbers."""
    return _YEAR_PAIR_RE.match(text) is not None


def _contact_facts(block: ExtractedBlock) -> list[DeterministicFact]:
    facts: list[DeterministicFact] = []
    text = block.text

    for email in dict.fromkeys(EMAIL_RE.findall(text)):
        facts.append(
            DeterministicFact(
                category=FactCategory.CONTACT,
                claim=email,
                locators=[block.locator],
                attributes={"kind": "email", "value": email},
            )
        )

    for handle in dict.fromkeys(LINKEDIN_RE.findall(text)):
        facts.append(
            DeterministicFact(
                category=FactCategory.CONTACT,
                claim=f"linkedin.com/in/{handle}",
                locators=[block.locator],
                attributes={"kind": "linkedin", "value": handle},
            )
        )

    for handle in dict.fromkeys(GITHUB_RE.findall(text)):
        facts.append(
            DeterministicFact(
                category=FactCategory.CONTACT,
                claim=f"github.com/{handle}",
                locators=[block.locator],
                attributes={"kind": "github", "value": handle},
            )
        )

    for candidate in dict.fromkeys(PHONE_RE.findall(text)):
        digits = re.sub(r"\D", "", candidate)
        if not 8 <= len(digits) <= 15:
            continue
        # A year range ("2015 - 2019") has eight digits and a separator, so digit
        # counting alone reads it as a phone number. Education lines are full of
        # them, and a wrong phone number on an application is a real failure.
        if DATE_RANGE_RE.search(candidate) or _looks_like_year_pair(candidate):
            continue
        facts.append(
            DeterministicFact(
                category=FactCategory.CONTACT,
                claim=candidate.strip(),
                locators=[block.locator],
                attributes={"kind": "phone", "value": candidate.strip()},
                # Slightly below 1.0: phone patterns are the most ambiguous of the
                # contact types, so the UI nudges the user to glance at it.
                confidence=0.9,
            )
        )

    for url in dict.fromkeys(URL_RE.findall(text)):
        if "linkedin.com" in url.lower() or "github.com" in url.lower():
            continue
        facts.append(
            DeterministicFact(
                category=FactCategory.CONTACT,
                claim=url.rstrip(".,"),
                locators=[block.locator],
                attributes={"kind": "link", "value": url.rstrip(".,")},
            )
        )

    return facts


def _skill_facts(block: ExtractedBlock) -> list[DeterministicFact]:
    """Split a skills block into individual skills.

    A skills block commonly holds several labelled groups, one per line::

        Languages: Python, Go, SQL
        Frameworks: FastAPI, PyTorch

    Each line is handled independently. Treating the block as one string would let
    a group label absorb the last skill of the preceding group ("SQL Frameworks"),
    dropping a real skill and inventing a nonexistent one.
    """
    facts: list[DeterministicFact] = []
    seen: set[str] = set()

    for line in block.text.splitlines():
        text = line
        group: str | None = None
        label_match = _LABEL_SPLIT.match(text)
        if label_match is not None:
            group = label_match.group(1).strip()
            text = label_match.group(2)

        for piece in _SKILL_SPLIT.split(text):
            skill = normalize_skill(piece)
            if skill is None or skill.lower() in seen:
                continue
            seen.add(skill.lower())
            attributes = {"skill": skill}
            if group:
                attributes["group"] = group
            facts.append(
                DeterministicFact(
                    category=FactCategory.SKILL,
                    claim=skill,
                    locators=[block.locator],
                    attributes=attributes,
                )
            )

    return facts


def extract_deterministic_facts(document: ExtractedDocument) -> list[DeterministicFact]:
    """Extract every fact that does not require interpretation.

    Contact details are searched across the whole document (they usually sit in an
    unlabelled header), while skills are only read from a recognised skills
    section — a comma-separated sentence in an experience bullet is prose, not a
    skills list.
    """
    facts: list[DeterministicFact] = []
    seen: set[tuple[str, str]] = set()

    for block in document.blocks:
        found = _contact_facts(block)
        if block.section == "skills" and block.kind != "heading":
            found.extend(_skill_facts(block))

        for fact in found:
            key = (fact.category.value, fact.claim.lower())
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)

    return facts
