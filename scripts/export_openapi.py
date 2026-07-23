"""Export the OpenAPI 3.1 document to ``openapi.json``.

The spec is generated from the code rather than hand-maintained, so it cannot drift from
the implementation. ``make openapi`` regenerates it, and CI fails if the committed file is
out of date - which makes a stale spec a build error instead of a support ticket.

Usage:
    python -m scripts.export_openapi [output_path]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Importable without a configured .env: the spec depends only on route definitions.
from app.config import Settings
from app.main import create_app


def main() -> int:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    settings = Settings(portal_username="unused", portal_password="unused")
    schema = create_app(settings).openapi()
    destination.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    paths = len(schema.get("paths", {}))
    print(f"wrote {destination} ({paths} paths, OpenAPI {schema.get('openapi')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
