"""Evidence retrieval for matching.

skills/03 asks for semantic retrieval of candidate evidence relevant to a job
requirement. This implements it behind an ``EmbeddingIndex`` interface with a
local, dependency-free lexical vectoriser.

Being explicit about the limitation: hashed token overlap is **lexical**, not
semantic. It will not connect "containerisation" to "Docker" on its own. That is an
acceptable first implementation because retrieval here only *orders* candidate
evidence for a scorer and a human reviewer — it never decides truth, and a missed
association shows up as a gap the user can see rather than a false claim. Swapping in
a real embedding provider means implementing one interface.

Critically, retrieval similarity is **not** evidence (skills/01). A high score marks
text as worth showing; only an explicit evidence citation supports a claim.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")

#: Words too common in resumes and postings to carry signal.
STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those of in on at to for with by
    from as is are was were be been being have has had do does did will would can could
    should may might must our your their its it we you they i he she them us
    experience work working years year role position job company team strong good great
    excellent ability able using use used help helping across via etc
    """.split()
)

DEFAULT_DIMENSIONS = 512


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in STOPWORDS
    ]


class Vector:
    """A sparse term-frequency vector with cosine similarity.

    Sparse dicts rather than dense numpy arrays: a resume has a few hundred distinct
    terms against 512+ dimensions, so the dense form would be mostly zeros, and this
    keeps the module dependency-free.
    """

    __slots__ = ("_norm", "weights")

    def __init__(self, weights: dict[int, float]) -> None:
        self.weights = weights
        self._norm = math.sqrt(sum(value * value for value in weights.values()))

    @property
    def norm(self) -> float:
        return self._norm

    def cosine(self, other: Vector) -> float:
        if not self._norm or not other._norm:
            return 0.0
        # Iterate the smaller side; intersection dominates the cost.
        left, right = (
            (self.weights, other.weights)
            if len(self.weights) <= len(other.weights)
            else (other.weights, self.weights)
        )
        dot = sum(value * right.get(key, 0.0) for key, value in left.items())
        return max(0.0, min(1.0, dot / (self._norm * other._norm)))


class Vectoriser(Protocol):
    def encode(self, text: str) -> Vector: ...


class HashingVectoriser:
    """Hashes tokens into a fixed number of buckets with sublinear term weighting."""

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        self._dimensions = dimensions

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self._dimensions

    def encode(self, text: str) -> Vector:
        counts: dict[int, float] = {}
        for token in tokenize(text):
            bucket = self._bucket(token)
            counts[bucket] = counts.get(bucket, 0.0) + 1.0
        # 1 + log(tf) damps repetition: a word used ten times is not ten times as
        # relevant, and postings repeat their own job title constantly.
        return Vector({key: 1.0 + math.log(value) for key, value in counts.items()})


@dataclass(slots=True)
class RetrievedItem:
    item_id: str
    text: str
    score: float


class EmbeddingIndex(Protocol):
    """The seam a real embedding backend (FAISS, pgvector) would implement."""

    def add(self, item_id: str, text: str) -> None: ...

    def query(self, text: str, top_k: int = 5) -> list[RetrievedItem]: ...


class InMemoryIndex:
    """Exact nearest-neighbour search over a small set of items.

    Exact rather than approximate: one candidate's evidence is on the order of a
    hundred short strings, where a brute-force scan is faster than building an index
    and cannot drift out of sync with the database.
    """

    def __init__(self, vectoriser: Vectoriser | None = None) -> None:
        self._vectoriser = vectoriser or HashingVectoriser()
        self._items: dict[str, tuple[str, Vector]] = {}

    def add(self, item_id: str, text: str) -> None:
        self._items[item_id] = (text, self._vectoriser.encode(text))

    def add_many(self, items: list[tuple[str, str]]) -> None:
        for item_id, text in items:
            self.add(item_id, text)

    def __len__(self) -> int:
        return len(self._items)

    def query(self, text: str, top_k: int = 5) -> list[RetrievedItem]:
        query_vector = self._vectoriser.encode(text)
        if not query_vector.norm:
            return []

        scored = [
            RetrievedItem(item_id=item_id, text=item_text, score=query_vector.cosine(vector))
            for item_id, (item_text, vector) in self._items.items()
        ]
        scored.sort(key=lambda item: (-item.score, item.item_id))
        return [item for item in scored[:top_k] if item.score > 0.0]


def text_similarity(left: str, right: str, vectoriser: Vectoriser | None = None) -> float:
    """Convenience cosine between two strings."""
    engine = vectoriser or HashingVectoriser()
    return engine.encode(left).cosine(engine.encode(right))
