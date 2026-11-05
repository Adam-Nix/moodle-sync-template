"""Remember which Moodle file each saved file came from, so nothing is saved twice.

Files are numbered by their position on the course page (see course_page.py), so
when a lecturer adds something near the top every later file gets a new number
and used to come down again beside the old copy.  This index ties each Moodle
file to the name it was first saved under and keeps it there.  A download whose
bytes are already in the master folder points at that file instead (see
master_guard.py), which catches a lecturer uploading the same file twice.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from moodle_downloader.course_page import RemoteFile
from moodle_downloader.naming import DATA_FOLDER

INDEX_FILE = "file-index.json"
README = "readme.md"
CLOCK_SLACK = 2  # seconds, as in downloader.py
_NUMBER = re.compile(r"^\d+-")


class FileIndex:
    """_data/file-index.json: course folder name -> Moodle file key -> where it is saved.

    Each entry is {"path": relative to the course folder (it may start ../ when
    the kept copy is in another course), "size" and "modified":
    the version Moodle had when it was saved, "own": false when the path is
    another Moodle file's, because the two have the same contents}.
    """

    def __init__(self, root: Path) -> None:
        self.path = root / DATA_FOLDER / INDEX_FILE
        try:
            self.courses: dict[str, dict[str, dict]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.courses = {}

    def course(self, folder: Path) -> dict[str, dict]:
        return self.courses.setdefault(folder.name, {})

    def owned_paths(self, master: Path) -> set[Path]:
        """Every file in the master folder that a Moodle file is saved as (resolved paths)."""

        return {
            (master / course / entry["path"]).resolve()
            for course, entries in self.courses.items()
            for entry in entries.values()
            if entry.get("own", True)
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".part")
        partial.write_text(json.dumps(self.courses, indent=4, ensure_ascii=False), encoding="utf-8")
        os.replace(partial, self.path)


def entry_for(path: str, remote: RemoteFile, own: bool = True) -> dict:
    return {"path": path, "size": remote.size, "modified": remote.modified, "own": own}


def unchanged(entry: dict | None, remote: RemoteFile) -> bool:
    """True when Moodle still has the version that was saved."""

    return bool(entry) and entry["size"] == remote.size and entry["modified"] == remote.modified


def _unnumbered(relative: str) -> str:
    """'10-mitm.pdf' -> 'mitm.pdf'; '05-week-1/lab.pdf' -> 'week-1/lab.pdf'."""

    return "/".join(_NUMBER.sub("", part) for part in relative.split("/"))


def _saved_files(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file() and path.name != README and not path.name.endswith(".part")
    )


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _looks_saved_from(path: Path, remote: RemoteFile) -> bool:
    """Same size; or, for pages, which Moodle lists as 0 bytes, the same modified time."""

    if remote.size:
        return _size(path) == remote.size
    try:
        return bool(remote.modified) and abs(path.stat().st_mtime - remote.modified) <= CLOCK_SLACK
    except OSError:
        return False


def _free_name(relative: str, taken: set[str], folder: Path) -> str:
    """`relative`, or 'x-2.pdf', 'x-3.pdf', ... if another Moodle file already has it."""

    if relative not in taken:
        return relative
    head, dot, extension = relative.rpartition(".")
    if not head or "/" in extension:
        head, dot, extension = relative, "", ""
    count = 2
    while True:
        candidate = f"{head}-{count}{dot}{extension}"
        if candidate not in taken and not (folder / candidate).exists():
            return candidate
        count += 1


def place_files(folder: Path, files: list[RemoteFile], known: dict[str, dict]) -> dict[str, str]:
    """Where each file on the course page is saved: numbered name -> name on disk.

    In order: where the index says; its numbered name, if a file is already there
    that no other Moodle file owns; an unowned file with the same name apart from
    the number that looks like this version (a file saved before the number changed, or
    before this index existed); otherwise its numbered name, made unique if
    another Moodle file owns it.
    """

    places: dict[str, str] = {}
    owned = {
        entry["path"]
        for entry in known.values()
        if entry.get("own", True) and (folder / entry["path"]).is_file()
    }
    waiting: list[RemoteFile] = []

    for remote in files:
        entry = known.get(remote.key)
        # A shared path only holds while the file is unchanged; a new version needs a place of its own.
        if entry and (folder / entry["path"]).is_file() and (entry.get("own", True) or unchanged(entry, remote)):
            places[remote.relative_path] = entry["path"]
        elif remote.relative_path not in owned and (folder / remote.relative_path).is_file():
            places[remote.relative_path] = remote.relative_path
            owned.add(remote.relative_path)
        else:
            waiting.append(remote)

    by_name: dict[str, list[str]] = {}
    for relative in _saved_files(folder):
        by_name.setdefault(_unnumbered(relative), []).append(relative)

    for remote in waiting:
        match = next(
            (
                relative
                for relative in by_name.get(_unnumbered(remote.relative_path), [])
                if relative not in owned and _looks_saved_from(folder / relative, remote)
            ),
            None,
        )
        place = match or _free_name(remote.relative_path, owned, folder)
        places[remote.relative_path] = place
        owned.add(place)
    return places
