"""How many files each copy held last time, to spot one that has gone missing.

Shared by the dashboard and `python -m network_sync`, so both refuse to sync
with a copy that looks broken (e.g. OneDrive still starting up, or a share
that is mounted but empty).
"""

from __future__ import annotations

import json

from moodle_downloader.config import REPO_ROOT

STATE_FILE = REPO_ROOT / "_data" / "sync-state.json"
GUARD_MIN_FILES = 5  # below this a copy is too small to judge
GUARD_DROP = 0.4  # a copy losing this share of its files is treated as broken, not empty


def load_counts() -> dict:
    """How many files each destination held last time, to spot a copy going missing."""

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("counts", {})
    except (OSError, ValueError):
        return {}


def save_counts(counts: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"counts": counts}, indent=2), encoding="utf-8")
    except OSError:
        pass  # a missing baseline only costs us the warning, so never fail a scan over it


def guard(name: str, present: int, baseline: dict) -> dict:
    """Decide whether a destination has lost so many files that it looks broken."""

    known = baseline.get(name, {}).get("files")
    lost = known - present if isinstance(known, int) else 0
    suspect = (
        isinstance(known, int)
        and known >= GUARD_MIN_FILES
        and present < known * (1 - GUARD_DROP)
    )
    return {
        "baseline": known,
        "present": present,
        "lost": lost if suspect else 0,
        "suspect": suspect,
        "message": (
            f"{name} had {known} files and now shows {present}. "
            "It may not be fully connected or ready, so it is being left alone."
            if suspect
            else ""
        ),
    }
