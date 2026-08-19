"""Requirement extraction from a job description (FR-20/FR-21).

A deterministic pass classifies most lines correctly: section headings and modal
verbs carry the signal, and the result is reproducible and free. The model pass
(``JobAnalyzer``) then refines classification for descriptions written as prose.

The description is attacker-controlled text. It only ever reaches a model wrapped as
``UntrustedContent``, and the model's output is a fixed schema, so a posting that
says "ignore your instructions" changes nothing about what the system does.
"""

from __future__ import annotations

import re

from packages.schemas.enums import RequirementKind
from packages.schemas.job import JobRequirement

#: Headings that switch which bucket subsequent bullets fall into.
_REQUIRED_HEADINGS = (
    "requirements",
    "qualifications",
    "what you need",
    "what we need",
    "must have",
    "minimum qualifications",
    "basic qualifications",
    "you have",
    "who you are",
    "skills and experience",
)
_PREFERRED_HEADINGS = (
    "nice to have",
    "nice-to-have",
    "preferred",
    "bonus",
    "desirable",
    "great to have",
    "pluses",
)
#: Sections that describe the company or the offer, not the candidate. Bullets here
#: must never become requirements, or every posting "requires" dental insurance.
_IGNORED_HEADINGS = (
    "about us",
    "about the company",
    "about the team",
    "benefits",
    "perks",
    "compensation",
    "equal opportunity",
    "eeo",
    "our mission",
    "why join",
    "what we offer",
)

_REQUIRED_MARKERS = ("must ", "required", "requires", "you will need", "essential")
_PREFERRED_MARKERS = (
    "nice to have",
    "preferred",
    "bonus",
    "a plus",
    "ideally",
    "desirable",
    "familiarity with",
    "exposure to",
)

_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•◦·]|\d+[.)])\s+")
# En/em dashes are deliberate: typeset postings use them for ranges.
YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:to|-|–|—)?\s*(\d{1,2})?\s*years?",  # noqa: RUF001
    re.IGNORECASE,
)

#: Technologies worth a normalized key, so scoring can match them against skill
#: facts exactly. A modest list on purpose: an unlisted technology still scores
#: through the text path, it simply gets no exact-match key.
KNOWN_KEYS: tuple[str, ...] = (
    "python",
    "java",
    "javascript",
    "typescript",
    "golang",
    "go",
    "rust",
    "c++",
    "c#",
    "ruby",
    "php",
    "scala",
    "kotlin",
    "swift",
    "postgresql",
    "postgres",
    "mysql",
    "mongodb",
    "redis",
    "elasticsearch",
    "kafka",
    "spark",
    "airflow",
    "hadoop",
    "nosql",
    "sql",
    "aws",
    "gcp",
    "azure",
    "docker",
    "kubernetes",
    "terraform",
    "ansible",
    "jenkins",
    "django",
    "flask",
    "fastapi",
    "spring",
    "react",
    "angular",
    "vue",
    "node",
    "pytorch",
    "tensorflow",
    "scikit-learn",
    "keras",
    "opencv",
    "pandas",
    "numpy",
    "machine learning",
    "deep learning",
    "computer vision",
    "nlp",
    "mlops",
    "microservices",
    "graphql",
    "grpc",
    "rest",
    "ci/cd",
    "git",
    "linux",
)

#: A bullet may be as short as two words ("Strong Python"), or even one when it
#: names a recognised technology ("Kubernetes"). Requiring three dropped real
#: requirements on the floor.
#: Text in a posting that addresses an automated reader rather than a candidate.
#: A posting containing this is either testing screening tools or attacking them;
#: either way it is not a requirement, and the user deserves to be told it is there.
INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions", re.I),
    re.compile(r"\bsystem\s+(?:instruction|prompt|message)\b", re.I),
    re.compile(r"\byou\s+are\s+now\b", re.I),
    re.compile(r"\b(?:reveal|disregard|override|bypass)\s+(?:your|the|all)\b", re.I),
    re.compile(r"\b(?:unrestricted|developer|god)\s+mode\b", re.I),
    re.compile(r"\bnew\s+instructions\b", re.I),
    re.compile(r"</?untrusted_content", re.I),
    re.compile(r"\bstate\s+that\s+the\s+candidate\b", re.I),
)


def looks_like_injection(text: str) -> bool:
    """True when a line is addressed to an automated reader, not to a candidate."""
    return any(pattern.search(text) for pattern in INJECTION_PATTERNS)


def detect_suspicious_instructions(description: str) -> list[str]:
    """Lines in a posting that attempt to instruct an automated reader.

    Surfaced rather than silently dropped: a candidate should know a posting is
    trying to manipulate screening software.
    """
    found: list[str] = []
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if line and looks_like_injection(line):
            found.append(line[:300])
    return found


_MIN_REQUIREMENT_WORDS = 2
_MAX_REQUIREMENT_CHARS = 400
_MAX_HEADING_WORDS = 8


def classify_heading(line: str) -> str | None:
    """Return ``required``, ``preferred``, ``ignored``, or None if not a heading.

    A heading is a short label, not a sentence. Without that distinction,
    "You must have strong PostgreSQL experience." matched the "must have" heading
    and was consumed as a section break instead of being read as the requirement
    it plainly is.
    """
    stripped = line.strip()
    lowered = stripped.lower().rstrip(":").strip()
    if not lowered or len(lowered) > 60:
        return None
    if stripped.endswith((".", "!", "?")):
        return None
    if len(lowered.split()) > _MAX_HEADING_WORDS:
        return None
    for heading in _IGNORED_HEADINGS:
        if heading in lowered:
            return "ignored"
    for heading in _PREFERRED_HEADINGS:
        if heading in lowered:
            return "preferred"
    for heading in _REQUIRED_HEADINGS:
        if heading in lowered:
            return "required"
    return None


def normalized_key(text: str) -> str | None:
    """Derive a comparable key for a requirement, or None if it has no clean one.

    Years of experience become ``years_experience>=N`` so the scorer can compare a
    number rather than match a phrase.
    """
    lowered = text.lower()

    years = YEARS_RE.search(lowered)
    if years is not None and "year" in lowered:
        lower_bound = years.group(1)
        return f"years_experience>={lower_bound}"

    for key in KNOWN_KEYS:
        # Word-boundary match so "go" does not fire on "algorithms" and "react"
        # does not fire on "reaction".
        if re.search(rf"(?<![\w+#]){re.escape(key)}(?![\w+#])", lowered):
            return "go" if key == "golang" else key

    return None


def _kind_from_markers(text: str, default: RequirementKind) -> RequirementKind:
    lowered = text.lower()
    for marker in _PREFERRED_MARKERS:
        if marker in lowered:
            return RequirementKind.PREFERRED
    for marker in _REQUIRED_MARKERS:
        if marker in lowered:
            return RequirementKind.REQUIRED
    return default


def extract_requirements(description: str) -> list[JobRequirement]:
    """Extract classified requirements from a description, deterministically."""
    requirements: list[JobRequirement] = []
    seen: set[str] = set()
    current: RequirementKind | None = None
    ignoring = False

    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading = classify_heading(line)
        if heading is not None:
            ignoring = heading == "ignored"
            if heading == "required":
                current = RequirementKind.REQUIRED
            elif heading == "preferred":
                current = RequirementKind.PREFERRED
            continue

        if ignoring:
            continue

        # An injection attempt often trips a requirement marker ("...requires no
        # visa sponsorship"), which would echo attacker text into a later prompt and
        # into the UI as a gap. Refuse it here, at the boundary.
        if looks_like_injection(line):
            continue

        is_bullet = bool(_BULLET_PREFIX.match(raw_line))
        text = _BULLET_PREFIX.sub("", line).strip()
        key = normalized_key(text)
        if len(text) > _MAX_REQUIREMENT_CHARS:
            continue
        if len(text.split()) < _MIN_REQUIREMENT_WORDS and key is None:
            continue

        # Outside a requirements section, only take lines that mark themselves as a
        # requirement. Prose paragraphs about the company are not requirements.
        default = current if is_bullet and current is not None else RequirementKind.CONTEXTUAL
        kind = _kind_from_markers(text, default)
        if kind is RequirementKind.CONTEXTUAL and not is_bullet:
            explicit = _kind_from_markers(text, RequirementKind.CONTEXTUAL)
            if explicit is RequirementKind.CONTEXTUAL:
                continue
            kind = explicit

        fingerprint = text.lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        requirements.append(JobRequirement(text=text, kind=kind, key=key))

    return requirements


def required_years(requirements: list[JobRequirement]) -> int | None:
    """Highest explicitly required years-of-experience figure, if any."""
    values: list[int] = []
    for requirement in requirements:
        if requirement.kind is not RequirementKind.REQUIRED:
            continue
        if requirement.key and requirement.key.startswith("years_experience>="):
            values.append(int(requirement.key.split(">=")[1]))
    return max(values) if values else None
