"""`python -m network_sync [--dry-run]` makes every copy of 4th year the same."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Run as a plain script, so the repo root is not on the path yet.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from network_sync.sync import sync_everything  # noqa: E402

sync_everything(dry_run="--dry-run" in sys.argv[1:])
