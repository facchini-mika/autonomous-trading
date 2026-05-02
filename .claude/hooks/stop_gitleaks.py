#!/usr/bin/env python3
"""Stop — run gitleaks on staged diff before turn end.

Re-uses the gitleaks binary already installed via pre-commit (Phase 0). On a
secret hit the hook exits 2 with stderr passthrough so the turn fails. If
gitleaks is not on PATH the hook degrades to a non-blocking warning rather
than failing every Stop event in environments without it (CI handles secrets
on its own).
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def main() -> int:
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        sys.stderr.write("stop_gitleaks: gitleaks not on PATH; skipping\n")
        return 0

    result = subprocess.run(
        [gitleaks, "protect", "--staged", "--no-banner", "--redact"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.stderr.write("\nstop_gitleaks: secret detected in staged diff\n")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
