"""Keep the whole 4th year folder the same on this PC, OneDrive and the home server.

The master folder (the Moodle downloads) has its own sync in sync.py; this
covers everything else in 4th year - FYP, CV, lab reports and so on.

* Nothing is ever deleted.  A file missing in one place is copied back to it.
* A file edited in one place since the last sync is copied over the others.
* A file edited in two places since the last sync: the newest edit wins
  everywhere, and each other edit is kept beside it as
  "name (conflict 2026-09-24 1830 OneDrive).ext", so no work is lost.
* The one exception: a conflict copy discarded on the dashboard's conflicts
  screen (see conflicts.py) is removed from a place that was offline at the
  time, rather than copied back from it.  Its contents are already in
  _data/conflict-bin on this PC.

What every place held after the last sync is recorded in
_data/year-sync-state.json.  That record is what tells "edited here" apart from
"out of date here".  A place that is offline keeps its old record, so when it
comes back its copies are simply brought up to date rather than treated as edits.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable

from moodle_downloader.config import MASTER_FOLDER, REPO_ROOT, year_root
from network_sync import state
from network_sync.sync import PROBE_PREFIX, Destination, _copy, _digest, check_remote

LOCAL = "This PC"
MANIFEST_FILE = REPO_ROOT / "_data" / "year-sync-state.json"
RESOLVED_FILE = REPO_ROOT / "_data" / "year-sync-resolved.json"
CLOCK_SLACK = 2.0  # seconds; SMB and OneDrive can round modified times
SKIPPED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
SKIPPED_PREFIXES = ("~$", ".~lock.", PROBE_PREFIX)  # Office and LibreOffice lock files, write checks
COUNT_SUFFIX = " (4th year)"  # guard counts sit beside the master folder's in sync-state.json

ProgressCallback = Callable[[str], None]


# --------------------------------------------------------------------------- #
# where, and what is there
# --------------------------------------------------------------------------- #
@dataclass
class Copy:
    """One place's copy of a file."""

    path: str  # relative path, spelt the way that place spells it
    size: int
    mtime: float


def places(destinations: list[Destination]) -> dict[str, Path]:
    """Every copy of the 4th year folder, this PC first."""

    found = {LOCAL: year_root()}
    for destination in destinations:
        found[destination.name] = Path(destination.root)
    return found


def _skipped(name: str) -> bool:
    lower = name.lower()
    return lower in SKIPPED_NAMES or lower.startswith(SKIPPED_PREFIXES) or lower.endswith(".part")


def scan(root: Path) -> dict[str, Copy]:
    """Every file under root except the master folder, keyed by lower-case path.

    Windows, SMB and OneDrive all ignore case, so `FYP/Plan.xlsx` and
    `fyp/plan.xlsx` are one file.  Raises OSError if any folder can't be read,
    as files missing from a half-read listing would look like files to copy back.
    """

    errors: list[OSError] = []
    found: dict[str, Copy] = {}
    for folder, subfolders, files in os.walk(root, onerror=errors.append):
        here = Path(folder).relative_to(root)
        if here == Path("."):
            subfolders[:] = [name for name in subfolders if name.lower() != MASTER_FOLDER]
        for name in files:
            if _skipped(name):
                continue
            try:
                stat = (Path(folder) / name).stat()
            except OSError as error:
                errors.append(error)
                continue
            relative = (here / name).as_posix()
            found[relative.lower()] = Copy(relative, stat.st_size, stat.st_mtime)
    if errors:
        raise errors[0]
    return found


# --------------------------------------------------------------------------- #
# the last-sync record
# --------------------------------------------------------------------------- #
def _load_manifest() -> dict[str, dict[str, list[float]]]:
    """lower-case path -> place -> [size, modified time] after the last sync."""

    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError):
        return {}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    partial = MANIFEST_FILE.with_name(MANIFEST_FILE.name + ".part")
    partial.write_text(json.dumps({"files": manifest}, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(partial, MANIFEST_FILE)


def _matches_record(copy: Copy, record: list[float] | None) -> bool:
    return bool(record) and copy.size == record[0] and abs(copy.mtime - record[1]) <= CLOCK_SLACK


def load_resolved() -> dict[str, dict]:
    """Discarded conflict copies still to remove: lower-case path -> {path, size, mtime, places}."""

    try:
        return json.loads(RESOLVED_FILE.read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError):
        return {}


def save_resolved(resolved: dict) -> None:
    RESOLVED_FILE.parent.mkdir(parents=True, exist_ok=True)
    partial = RESOLVED_FILE.with_name(RESOLVED_FILE.name + ".part")
    partial.write_text(json.dumps({"files": resolved}, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(partial, RESOLVED_FILE)


def record_discarded(path: str, size: int, mtime: float, places: list[str]) -> None:
    """Remember a conflict copy the user threw away, so the places listed drop it too.

    Without this, a place that was offline would still hold the file, and the
    next sync would copy it back everywhere as "missing".
    """

    manifest = _load_manifest()
    if manifest.pop(path.lower(), None) is not None:
        _save_manifest(manifest)
    resolved = load_resolved()
    if places:
        resolved[path.lower()] = {"path": path, "size": size, "mtime": mtime, "places": sorted(places)}
    else:
        resolved.pop(path.lower(), None)
    save_resolved(resolved)


# --------------------------------------------------------------------------- #
# deciding
# --------------------------------------------------------------------------- #
@dataclass
class Action:
    """What to do with one file so every place ends up holding the same version."""

    key: str
    path: str  # spelt as the winning copy spells it
    source: str  # the place the winning copy is in
    size: int
    mtime: float
    targets: list[str] = field(default_factory=list)  # places it is missing from or out of date in
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # (place, conflict file) for overwritten edits
    retime: list[str] = field(default_factory=list)  # same contents, different modified time

    @property
    def pending(self) -> bool:
        return bool(self.targets or self.conflicts)


@dataclass
class YearPlan:
    """Every file in every reachable place, and what making them identical takes."""

    roots: dict[str, Path]  # places that can be used right now
    offline: dict[str, str]  # place -> why it is being left alone
    copies: dict[str, dict[str, Copy]]  # lower-case path -> place -> copy
    actions: list[Action]
    removals: list[tuple[str, str, str]] = field(default_factory=list)  # (key, place, path) of discarded conflict copies

    @property
    def pending(self) -> list[Action]:
        return [action for action in self.actions if action.pending]

    @property
    def copy_count(self) -> int:
        """Individual file copies needed, conflict files included."""

        return sum(len(a.targets) + len(a.conflicts) * len(self.roots) for a in self.pending)

    def to_json(self) -> dict:
        return {
            "ready": list(self.roots),
            "offline": [{"name": name, "why": why} for name, why in self.offline.items()],
            "copies": [
                {"path": a.path, "from": a.source, "to": a.targets} for a in self.pending if a.targets
            ],
            "conflicts": [
                {"path": a.path, "place": place, "as": conflict}
                for a in self.pending
                for place, conflict in a.conflicts
            ],
            "removals": [{"path": path, "place": place} for _key, place, path in self.removals],
            "total": self.copy_count,
        }


def _in_git(key: str) -> bool:
    return "/.git/" in f"/{key}"


def _conflict_name(path: str, place: str, mtime: float) -> str:
    """`FYP/Plan.xlsx` -> `FYP/Plan (conflict 2026-09-24 1830 OneDrive).xlsx`."""

    original = PurePosixPath(path)
    stamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H%M")
    return str(original.with_name(f"{original.stem} (conflict {stamp} {place}){original.suffix}"))


CONFLICT_PATTERN = re.compile(
    r"^(?P<stem>.+) \(conflict (?P<stamp>\d{4}-\d{2}-\d{2} \d{4}) (?P<place>[^()]+)\)(?P<suffix>\.[^.]*)?$"
)


def parse_conflict(path: str) -> tuple[str, str] | None:
    """`FYP/Plan (conflict 2026-09-24 1830 OneDrive).xlsx` -> (`FYP/Plan.xlsx`, `OneDrive`); None otherwise."""

    conflict = PurePosixPath(path)
    match = CONFLICT_PATTERN.match(conflict.name)
    if not match:
        return None
    return str(conflict.with_name(match["stem"] + (match["suffix"] or ""))), match["place"]


def _decide(key: str, copies: dict[str, Copy], roots: dict[str, Path], record: dict) -> Action:
    def same(a: str, b: str) -> bool:
        first, second = copies[a], copies[b]
        if first.size != second.size:
            return False
        if abs(first.mtime - second.mtime) <= CLOCK_SLACK:
            return True
        try:  # same size, different time: copied by hand, perhaps - check the contents
            return _digest(roots[a] / first.path) == _digest(roots[b] / second.path)
        except OSError:
            return False

    # Edited since the last sync (or never synced, so it can't be told apart from an edit).
    changed = [place for place in copies if not _matches_record(copies[place], record.get(place))]
    winner = max(changed or list(copies), key=lambda place: copies[place].mtime)
    best = copies[winner]
    matching = [place for place in copies if place == winner or same(place, winner)]

    action = Action(key, best.path, winner, best.size, best.mtime)
    action.targets = [place for place in roots if place not in matching]
    action.retime = [place for place in matching if abs(copies[place].mtime - best.mtime) > CLOCK_SLACK]

    if not _in_git(key):  # git's own files: newest wins, conflict copies would only confuse it
        kept: list[str] = []  # two places holding the same losing edit need only one conflict file
        for place in changed:
            if place in matching or any(same(place, other) for other in kept):
                continue
            kept.append(place)
            action.conflicts.append((place, _conflict_name(copies[place].path, place, copies[place].mtime)))
    return action


def plan(roots: dict[str, Path], on_progress: ProgressCallback | None = None) -> YearPlan:
    """Check and scan every place, then work out what each file needs.  Moves nothing."""

    report = on_progress or (lambda _message: None)
    baseline = state.load_counts()
    usable: dict[str, Path] = {}
    offline: dict[str, str] = {}
    scans: dict[str, dict[str, Copy]] = {}

    for name, root in roots.items():
        status = check_remote(str(root))
        if not status.available:
            offline[name] = status.message
            continue
        report(f"Scanning {name} ({root})...")
        try:
            found = scan(root)
        except OSError as error:
            offline[name] = f"could not read every folder - {error}"
            continue
        verdict = state.guard(name + COUNT_SUFFIX, len(found), baseline)
        if verdict["suspect"]:
            offline[name] = verdict["message"]
            continue
        baseline[name + COUNT_SUFFIX] = {"files": len(found), "seenAt": datetime.now().isoformat()}
        usable[name], scans[name] = root, found
    state.save_counts(baseline)

    manifest = _load_manifest()
    copies: dict[str, dict[str, Copy]] = {}
    for name, found in scans.items():
        for key, copy in found.items():
            copies.setdefault(key, {})[name] = copy

    # A discarded conflict copy still sitting somewhere is removed, not spread again -
    # unless it has been edited since, which makes it someone's work once more.
    removals: list[tuple[str, str, str]] = []
    for key, gone in load_resolved().items():
        held = copies.get(key, {})
        if held and all(_matches_record(copy, [gone["size"], gone["mtime"]]) for copy in held.values()):
            removals += [(key, place, copy.path) for place, copy in held.items()]
            del copies[key]

    actions = [_decide(key, copies[key], usable, manifest.get(key, {})) for key in sorted(copies)]
    return YearPlan(roots=usable, offline=offline, copies=copies, actions=actions, removals=removals)


# --------------------------------------------------------------------------- #
# doing
# --------------------------------------------------------------------------- #
@dataclass
class YearResult:
    """What a run actually did."""

    plan: YearPlan | None = None
    copied: list[tuple[str, str, int]] = field(default_factory=list)  # (path, place, bytes)
    conflicts: list[str] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)  # (path, place) of discarded conflict copies
    failed: list[tuple[str, str]] = field(default_factory=list)
    bytes_sent: int = 0

    def summary(self) -> str:
        where = " + ".join(self.plan.roots) if self.plan else "nowhere"
        removed = f"{len(self.removed)} discarded conflict copies removed, " if self.removed else ""
        return (
            f"{len(self.copied)} copied, {len(self.conflicts)} conflict copies kept, {removed}"
            f"{len(self.failed)} failed ({self.bytes_sent / 1_048_576:.1f} MB) across {where}"
        )


def _remove_discarded(work: YearPlan, dry_run: bool, report: ProgressCallback, result: YearResult) -> None:
    """Take discarded conflict copies out of places that were offline when they were discarded."""

    left: dict[str, set[str]] = {}  # key -> places a removal failed in
    for key, place, path in work.removals:
        report(f"{'Would remove' if dry_run else 'Removing'} {path} from {place} (discarded on the conflicts screen)")
        if dry_run:
            continue
        try:
            (work.roots[place] / path).unlink(missing_ok=True)
            result.removed.append((path, place))
        except OSError as error:
            report(f"FAILED removing {path} from {place}: {error}")
            result.failed.append((f"{path} in {place}", str(error)))
            left.setdefault(key, set()).add(place)
    if dry_run:
        return

    resolved = load_resolved()
    for key, gone in list(resolved.items()):
        if key in work.copies:  # edited after it was discarded, so it was synced as normal
            del resolved[key]
            continue
        # Every place reachable now is clean, unless its removal failed.
        places = {place for place in gone["places"] if place not in work.roots} | left.get(key, set())
        if places:
            gone["places"] = sorted(places)
        else:
            del resolved[key]
    save_resolved(resolved)


def reconcile(
    roots: dict[str, Path],
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
    work: YearPlan | None = None,
) -> YearResult:
    """Make every reachable copy of the 4th year folder hold the same files.

    Deletes nothing but conflict copies the user has already discarded.
    """

    report = on_progress or print
    work = work or plan(roots, report)
    result = YearResult(plan=work)
    for name, why in work.offline.items():
        report(f"{name}: left alone - {why}")
    if len(work.roots) < 2:
        report("Fewer than two copies of 4th year are reachable, so there is nothing to compare.")
        return result
    if not work.pending and not work.removals:
        report(f"4th year already matches in {' + '.join(work.roots)}.")

    _remove_discarded(work, dry_run, report, result)
    manifest = _load_manifest()

    def copy(source: Path, destination: Path, label: str, size: int) -> bool:
        report(f"{'Would copy' if dry_run else 'Copying'} {label} ({size / 1_048_576:.1f} MB)")
        try:
            if not dry_run:
                _copy(source, destination)
            result.bytes_sent += size
            return True
        except OSError as error:
            report(f"FAILED {label}: {error}")
            result.failed.append((label, str(error)))
            return False

    created: set[str] = set()  # conflict files made this run, which the scan never saw
    for action in work.actions:
        copies = work.copies[action.key]
        ok = True
        unsaved: set[str] = set()  # places whose edit could not be kept, so must not be overwritten

        # Save any edit that is about to be overwritten, before it is.
        for place, conflict in action.conflicts:
            loser = copies[place]
            report(f"{place} and another copy both changed {loser.path} - keeping {place}'s version as {conflict}")
            sent = [
                target
                for target, root in work.roots.items()
                if copy(work.roots[place] / loser.path, root / conflict, f"{conflict} -> {target}", loser.size)
            ]
            if place not in sent:
                unsaved.add(place)
            ok &= len(sent) == len(work.roots)
            result.conflicts.append(conflict)
            if len(sent) == len(work.roots) and not dry_run:
                manifest[conflict.lower()] = {target: [loser.size, loser.mtime] for target in sent}
                created.add(conflict.lower())

        source = work.roots[action.source] / action.path
        for target in action.targets:
            if target in unsaved:
                report(f"Not overwriting {action.path} in {target}, as its own edit could not be kept.")
                ok = False
                continue
            spelt = copies[target].path if target in copies else action.path
            if copy(source, work.roots[target] / spelt, f"{action.path} -> {target}", action.size):
                result.copied.append((action.path, target, action.size))
            else:
                ok = False

        for place in action.retime:  # identical contents: line the times up so later runs skip the hash
            if dry_run:
                continue
            try:
                path = work.roots[place] / copies[place].path
                os.utime(path, (path.stat().st_atime, action.mtime))
            except OSError:
                ok = False

        if ok and not dry_run:
            record = manifest.setdefault(action.key, {})
            for place in work.roots:  # places that are offline keep their old record
                record[place] = [action.size, action.mtime]

    if not dry_run:
        # Forget files that are gone from every place the record knows about.
        gone = [
            key
            for key, record in manifest.items()
            if key not in work.copies and key not in created and set(record) <= set(work.roots)
        ]
        for key in gone:
            del manifest[key]
        _save_manifest(manifest)

    report(("Preview: " if dry_run else "4th year: ") + result.summary())
    return result
