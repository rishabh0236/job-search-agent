"""Database package.

Importing this package registers every ORM model on ``Base.metadata``. That import
is deliberate, not incidental: ``Base.metadata.create_all()`` only creates tables it
knows about, so without it whether a table exists depends on whether some earlier
module happened to import ``models`` first. That produced a test suite where the
same test passed in a full run and failed when run alone.
"""

from packages.core.db import models
from packages.core.db.base import Base
from packages.core.db.types import StrEnumType, UtcDateTime, utcnow

__all__ = ["Base", "StrEnumType", "UtcDateTime", "models", "utcnow"]
