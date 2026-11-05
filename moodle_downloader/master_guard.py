"""Keep the master folder free of duplicate files.

Only the master folder: the rest of 4th year may hold as many copies as you like.
Two files are duplicates when their bytes are identical, wherever they sit in the
master folder.  Every way in is covered:

- Moodle downloads check each saved file with `twin_of` (downloader.py), so the
  download record points at the copy that is kept and never fetches it again;
- the sync will not copy a file back from the server or OneDrive when its bytes
  are already in the master folder (sync.build_plan);
- `sweep` catches anything that arrived some other way - dragged in by hand,
  printed slides, OneDrive - and runs before every dashboard scan, sync and download.

Of each set of identical files one is kept and the rest are moved to
_data/duplicate-bin, so nothing is lost.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from moodle_downloader.config import REPO_ROOT, master_folder
from moodle_downloader.file_index import FileIndex
from moodle_downloader.naming import DATA_FOLDER

BIN_FOLDER = "duplicate-bin"
README = "readme.md"  # written per course by the downloader; never a duplicate of anything real
PROBE_PREFIX = ".write-test-"  # the sync's write check leaves one of these for a moment

Log = Callable[[str], None]

_digests: dict[tuple[str, int, int], str] = {}


def bin_folder(root: Path = REPO_ROOT) -> Path:
    return root / DATA_FOLDER / BIN_FOLDER


def digest(path: Path) -> str:
    """sha256 of a file, cached on size and modified time so rescans don't re-read it."""

    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _digests:
        hasher = hashlib.sha256()
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(1_048_576), b""):
                hasher.update(chunk)
        _digests[key] = hasher.hexdigest()
    return _digests[key]


def master_files(master: Path) -> list[Path]:
    if not master.is_dir():
        return []
    return sorted(
        path
        for path in master.rglob("*")
        if path.is_file()
        and path.name.lower() != README
        and not path.name.endswith(".part")
        and not path.name.startswith(PROBE_PREFIX)
    )


def is_inside(path: Path, master: Path) -> bool:
    try:
        path.resolve().relative_to(master.resolve())
        return True
    except ValueError:
        return False


def twin_of(saved: Path, master: Path | None = None) -> Path | None:
    """Another file in the master folder with exactly the bytes of `saved`, if there is one.

    Only files of the same size are read.
    """

    master = master or master_folder()
    try:
        size = saved.stat().st_size
        mine = None
        for other in master_files(master):
            if other == saved or other.stat().st_size != size:
                continue
            mine = mine or digest(saved)
            if digest(other) == mine:
                return other
    except OSError:
        return None
    return None


def duplicate_sets(master: Path) -> list[list[Path]]:
    """Every group of two or more identical files in the master folder."""

    by_size: dict[int, list[Path]] = {}
    for path in master_files(master):
        try:
            by_size.setdefault(path.stat().st_size, []).append(path)
        except OSError:
            continue
    sets: list[list[Path]] = []
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        by_digest: dict[str, list[Path]] = {}
        for path in paths:
            try:
                by_digest.setdefault(digest(path), []).append(path)
            except OSError:
                continue
        sets += [group for group in by_digest.values() if len(group) > 1]
    return sets


def _keeper(group: list[Path], owned: set[Path]) -> Path:
    """The copy Moodle's download record points at, else the one that arrived first."""

    def rank(path: Path):
        try:
            created = path.stat().st_ctime  # creation time on Windows
        except OSError:
            created = float("inf")
        return (path.resolve() not in owned, created, len(path.as_posix()), path.as_posix())

    return min(group, key=rank)


def sweep(master: Path | None = None, root: Path = REPO_ROOT, log: Log = print) -> list[tuple[str, str]]:
    """Move every duplicate out of the master folder; returns (removed, kept) pairs."""

    master = master or master_folder()
    sets = duplicate_sets(master)
    if not sets:
        return []

    owned = FileIndex(root).owned_paths(master)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    removed: list[tuple[str, str]] = []
    for group in sets:
        keep = _keeper(group, owned)
        kept = keep.relative_to(master).as_posix()
        for path in group:
            if path == keep:
                continue
            relative = path.relative_to(master).as_posix()
            target = bin_folder(root) / stamp / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
            except OSError as error:
                log(f"Couldn't remove duplicate {relative}: {error}")
                continue
            log(f"Removed duplicate {relative} - it is the same as {kept}. Moved to {target}")
            removed.append((relative, kept))
    return removed
