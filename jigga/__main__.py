"""Enable `python -m jigga` (used by the autostart service unit's ExecStart, so
it doesn't depend on the `jigga` console script being on the service's PATH)."""

from __future__ import annotations

from jigga.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
