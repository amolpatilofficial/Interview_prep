#!/usr/bin/env python3
"""Create the DuckDB file and tables without starting the server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db  # noqa: E402
from app.settings import settings  # noqa: E402

print(f"Database ready at {settings.db_path}")
print("Tables:", ", ".join(sorted(r["name"] for r in db._rows("SHOW TABLES"))))
