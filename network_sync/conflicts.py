"""Sort out the conflict copies year_sync.py keeps when a file was edited in two places.

`FYP/Plan (conflict 2026-09-24 1830 OneDrive).xlsx` sits beside `FYP/Plan.xlsx`
in every copy of 4th year.  Resolving the pair picks one version for all of them:

* keep "current"  - the conflict copy is dropped.
* keep "conflict" - the conflict copy takes the original's name, replacing it.

The version not kept is put in _data/conflict-bin on this PC, so it can still be
got back, and removed from the other copies.  A copy that is offline is tidied
by the next "make identical" run (see year_sync.record_discarded).
"""

from __future__ import annotations

import difflib
import html
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from moodle_downloader.config import MASTER_FOLDER, REPO_ROOT, year_root
from network_sync import year_sync
from network_sync.sync import _digest, check_remote

BIN = REPO_ROOT / "_data" / "conflict-bin"
DIFF_LIMIT = 400  # diff lines sent to the page
TEXT_LIMIT = 2_000_000  # bytes; bigger files are not diffed
RUNNABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".js", ".msi", ".lnk", ".scr", ".py", ".pyw"}
KEEP = ("current", "conflict")
INVISIBLE = re.compile("[﻿​‌‍⁠­]")  # zero-width spaces and joiners, BOM, soft hyphen


class ResolveError(RuntimeError):
    """Nothing was changed, for the reason given."""


# --------------------------------------------------------------------------- #
# finding
# --------------------------------------------------------------------------- #
def _info(path: Path) -> dict | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return {"size": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()}


def find(root: Path | None = None) -> list[dict]:
    """Every conflict copy in this PC's 4th year folder, with the file it clashed with."""

    root = root or year_root()
    found = []
    for copy in year_sync.scan(root).values():
        parsed = year_sync.parse_conflict(copy.path)
        if not parsed:
            continue
        original, place = parsed
        found.append(
            {
                "path": copy.path,
                "original": original,
                "place": place,
                "conflict": _info(root / copy.path),
                "current": _info(root / original),
            }
        )
    return sorted(found, key=lambda item: item["path"].lower())


def local_file(relative: str, root: Path | None = None) -> Path:
    """A file inside this PC's 4th year folder (not the master folder); ValueError otherwise."""

    root = (root or year_root()).resolve()
    parts = PurePosixPath(relative).parts
    if not parts or parts[0].lower() == MASTER_FOLDER:
        raise ValueError("That file isn't in the 4th year folder.")
    target = (root / relative).resolve()
    if root not in target.parents:
        raise ValueError("That file isn't in the 4th year folder.")
    return target


def _pair(relative: str, root: Path) -> tuple[str, str, str]:
    """(conflict path, original path, place whose edit it is), checked."""

    parsed = year_sync.parse_conflict(relative)
    if not parsed:
        raise ValueError("That isn't a conflict copy.")
    local_file(relative, root)
    return relative, parsed[0], parsed[1]


# --------------------------------------------------------------------------- #
# comparing
# --------------------------------------------------------------------------- #
def _docx_text(path: Path) -> list[str] | None:
    """The paragraphs of a Word document, or None if it can't be read."""

    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
        return None
    xml = xml.replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    paragraphs = (html.unescape(re.sub(r"<[^>]+>", "", part)) for part in xml.split("</w:p>"))
    # Word scatters invisible characters about; they would show as "changed" lines that look the same.
    visible = (INVISIBLE.sub("", paragraph).strip() for paragraph in paragraphs)
    return [paragraph for paragraph in visible if paragraph]


def _text(path: Path) -> list[str] | None:
    """The lines of a text file (or paragraphs of a .docx), or None for anything else."""

    if path.suffix.lower() == ".docx":
        return _docx_text(path)
    try:
        if path.stat().st_size > TEXT_LIMIT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return None


def compare(relative: str, root: Path | None = None) -> dict:
    """Whether the two versions differ, and how, where that can be shown as text."""

    root = root or year_root()
    conflict, original, place = _pair(relative, root)
    current_path, conflict_path = root / original, root / conflict
    result = {"same": None, "diff": None, "truncated": False, "kind": None}
    current, theirs = _info(current_path), _info(conflict_path)
    if not current or not theirs:
        return result

    result["same"] = current["size"] == theirs["size"] and _digest(current_path) == _digest(conflict_path)
    if result["same"]:
        return result

    old, new = _text(current_path), _text(conflict_path)
    if old is None or new is None:
        return result
    lines = list(difflib.unified_diff(old, new, "current", place, lineterm="", n=2))[2:]  # skip ---/+++
    result.update(
        diff=lines[:DIFF_LIMIT],
        truncated=len(lines) > DIFF_LIMIT,
        kind="docx" if current_path.suffix.lower() == ".docx" else "text",
    )
    return result


# --------------------------------------------------------------------------- #
# resolving
# --------------------------------------------------------------------------- #
def _fingerprint(path: Path) -> tuple[int, float] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime


def _same(a: tuple[int, float] | None, b: tuple[int, float] | None) -> bool:
    if a is None or b is None:
        return a is b
    return a[0] == b[0] and abs(a[1] - b[1]) <= year_sync.CLOCK_SLACK


def _into_bin(source: Path, bin_dir: Path, relative: str, move: bool) -> Path:
    target = bin_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    (shutil.move if move else shutil.copy2)(str(source), str(target))
    return target


def _check_copies_match(here: dict[str, tuple[int, float] | None], reachable: dict[str, Path]) -> None:
    """Refuse unless every other reachable place holds exactly what this PC does."""

    for where, root in reachable.items():
        if where == year_sync.LOCAL:
            continue
        for path, expected in here.items():
            try:
                found = _fingerprint(root / path)
            except OSError as error:
                raise ResolveError(f"Couldn't read {where}: {error}") from error
            if not _same(found, expected):
                raise ResolveError(
                    f"{PurePosixPath(path).name} is different in {where}. "
                    "Run Make all identical first so every copy matches, then try again."
                )


def resolve(relative: str, keep: str, roots: dict[str, Path]) -> str:
    """Keep one version of a conflicting file in every reachable copy of 4th year.

    Refuses (ResolveError, nothing changed) unless every reachable copy holds the
    same pair of files as this PC, so no edit made elsewhere is thrown away unseen.
    Returns what was done.
    """

    if keep not in KEEP:
        raise ResolveError("Pick which version to keep.")
    local = roots[year_sync.LOCAL]
    try:
        conflict, original, place = _pair(relative, local)
    except ValueError as error:
        raise ResolveError(str(error)) from error
    name = PurePosixPath(original).name

    reachable = {where: root for where, root in roots.items() if check_remote(str(root)).available}
    offline = [where for where in roots if where not in reachable]
    if year_sync.LOCAL not in reachable:
        raise ResolveError("This PC's 4th year folder can't be written to right now.")

    here = {conflict: _fingerprint(local / conflict), original: _fingerprint(local / original)}
    if here[conflict] is None:
        raise ResolveError("That conflict copy is gone - it may already have been sorted out.")
    _check_copies_match(here, reachable)

    size, mtime = here[conflict]
    bin_dir = BIN / datetime.now().strftime("%Y-%m-%d %H%M%S")
    others = [where for where in reachable if where != year_sync.LOCAL]
    failed: list[str] = []
    now = time.time()  # the kept version's new modified time: newer than any stale copy offline, so it wins there later
    try:
        if keep == "current":
            _into_bin(local / conflict, bin_dir, conflict, move=True)
            kept, binned = "the current version", f"{place}'s version"
        else:
            if here[original] is not None:
                _into_bin(local / original, bin_dir, original, move=False)
            os.replace(local / conflict, local / original)
            os.utime(local / original, (now, now))
            kept, binned = f"{place}'s version", "the old current version"
    except OSError as error:
        raise ResolveError(f"Couldn't change this PC's copy: {error}") from error

    for where in others:
        root = reachable[where]
        try:
            if keep == "current":
                (root / conflict).unlink(missing_ok=True)
            else:
                os.replace(root / conflict, root / original)
                os.utime(root / original, (now, now))
        except OSError:
            failed.append(where)

    year_sync.record_discarded(conflict, size, mtime, offline + failed)

    message = f"Kept {kept} of {name} everywhere reachable; {binned} is in {bin_dir.relative_to(REPO_ROOT)}."
    if offline:
        verb = "is" if len(offline) == 1 else "are"
        message += f" {' and '.join(offline)} {verb} offline - tidied on the next Make all identical."
    if failed:
        message += f" Couldn't change {' and '.join(failed)} - the next Make all identical will."
    return message
