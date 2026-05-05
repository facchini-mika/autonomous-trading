"""Entry point for ``python -m shared.observability``."""

from __future__ import annotations

import sys

from shared.observability.exporter import main

if __name__ == "__main__":
    sys.exit(main())
