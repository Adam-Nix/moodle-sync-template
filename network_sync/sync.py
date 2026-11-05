"""Push the 4th year master folder to the network share, without making duplicates.

The downloader already writes Year 4 courses into the local master folder,
C:\\4th year\\master (see moodle_downloader/naming.py).  This module mirrors that
folder out to the master folder in each other copy of 4th year (the SMB share,
OneDrive) when it happens to be reachable, and is a no-op everywhere else.
The rest of the 4th year folder is kept in step by year_sync.py.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import time
import uuid
from configparser import ConfigParser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from moodle_downloader import master_guard
from moodle_downloader.config import MASTER_FOLDER, REPO_ROOT, master_folder
from network_sync import state

CONFIG_FILE = str(REPO_ROOT / "config.ini")
SMB_PORT = 445
CONNECT_TIMEOUT = 3.0
PROBE_PREFIX = ".write-test-"  # the temporary file a write check leaves for a moment

ProgressCallback = Callable[[str], None]


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def get_share_path(config_filepath: str = CONFIG_FILE) -> str:
    """The server's 4th year folder, from $TUS_SHARE_PATH, then config.ini; empty if neither is set."""

    from_environment = os.environ.get("TUS_SHARE_PATH")
    if from_environment:
        return from_environment.rstrip("\\/")

    config_parser = ConfigParser()
    config_parser.read(config_filepath)
    if config_parser.has_option("Network", "share"):
        return config_parser.get("Network", "share", raw=True).strip().rstrip("\\/")

    return ""


def get_share_master(config_filepath: str = CONFIG_FILE) -> str:
    """The master folder inside the server's 4th year folder; empty if no share is set."""

    share = get_share_path(config_filepath)
    return os.path.join(share, MASTER_FOLDER) if share else ""


@dataclass
class Destination:
    """One other copy of the 4th year folder; its master folder sits inside it."""

    name: str
    root: str

    @property
    def path(self) -> str:
        """The master folder in this copy, which the Moodle files are mirrored to."""

        return os.path.join(self.root, MASTER_FOLDER)

    @property
    def is_unc(self) -> bool:
        """True for \\\\server\\share paths, which need a server to be reachable first."""

        return self.root.replace("/", "\\").startswith("\\\\")


def get_destinations(config_filepath: str = CONFIG_FILE) -> list[Destination]:
    """Every other copy of the 4th year folder: the home server, OneDrive, ..."""

    config_parser = ConfigParser()
    config_parser.read(config_filepath)

    found = []
    share = get_share_path(config_filepath)
    if share:
        found.append(Destination("Home server", share))
    if config_parser.has_option("Network", "onedrive"):
        # raw=True: a path like %OneDrive%\... is not ConfigParser interpolation.
        onedrive = config_parser.get("Network", "onedrive", raw=True).strip().rstrip("\\/")
        if onedrive:
            found.append(Destination("OneDrive", os.path.expandvars(onedrive)))
    return found


def get_master_folder() -> Path:
    """The local master folder that holds every 4th year course (C:\\4th year\\master)."""

    return master_folder()


def host_of(share_path: str) -> str:
    """Pull the server name out of a UNC path, e.g. `\\\\server\\share\\x` -> `server`."""

    parts = share_path.replace("/", "\\").lstrip("\\").split("\\")
    return parts[0] if parts and parts[0] else ""


# --------------------------------------------------------------------------- #
# connection state
# --------------------------------------------------------------------------- #
@dataclass
class RemoteStatus:
    """A snapshot of whether the share can be used right now."""

    share_path: str
    host: str
    host_up: bool = False
    share_mounted: bool = False
    writable: bool = False
    latency_ms: float | None = None
    message: str = ""
    checked_at: datetime = field(default_factory=datetime.now)

    @property
    def available(self) -> bool:
        """True when files can actually be pushed."""

        return self.host_up and self.share_mounted and self.writable


def check_remote(
    share_path: str | None = None, timeout: float = CONNECT_TIMEOUT
) -> RemoteStatus:
    """Test the server, the share and write access, in that order, and never raise."""

    share_path = share_path or get_share_path()
    status = RemoteStatus(share_path=share_path, host=host_of(share_path))

    if not share_path:
        status.message = "No network share set - add 'share' under [Network] in config.ini."
        return status

    if Destination("", share_path).is_unc:
        if not status.host:
            status.message = "No server name in the share path."
            return status
        started = time.perf_counter()
        try:
            with socket.create_connection((status.host, SMB_PORT), timeout=timeout):
                status.host_up = True
                status.latency_ms = (time.perf_counter() - started) * 1000
        except OSError as error:
            status.message = f"Cannot reach {status.host}:{SMB_PORT} - {error}"
            return status
    else:
        # A local folder (e.g. the OneDrive folder) has no server to reach.
        status.host_up = True

    share = Path(share_path)
    try:
        status.share_mounted = share.is_dir()
    except OSError as error:  # a dead handle to a mapped share can still throw
        status.message = f"Share path unreadable - {error}"
        return status

    if not status.share_mounted:
        status.message = f"'{share_path}' is not available."
        return status

    # Unique per call, so two checks running at once can't trip over each other.
    probe = share / f"{PROBE_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        status.writable = True
        status.message = "Connected."
    except OSError as error:
        status.message = f"Read-only or access was denied - {error}"
    finally:
        try:
            probe.unlink()
        except OSError:
            pass

    return status


# --------------------------------------------------------------------------- #
# comparing local and remote
# --------------------------------------------------------------------------- #
NEW = "new"
SYNCED = "synced"
CHANGED = "changed"
DUPLICATE = "duplicate"
REMOTE_ONLY = "remote-only"


@dataclass
class FileEntry:
    """One file in the master folder, and how it compares to the share."""

    relative_path: str
    size: int
    modified: datetime | None
    state: str = NEW
    remote_size: int | None = None
    duplicate_of: str | None = None

    @property
    def course(self) -> str:
        """The course folder the file lives in."""

        head, _, tail = self.relative_path.replace("\\", "/").partition("/")
        return head if tail else "(root)"

    @property
    def name(self) -> str:
        """The file name on its own."""

        return self.relative_path.replace("\\", "/").rsplit("/", 1)[-1]


def _walk(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        # .part files are half-copied; .write-test files belong to a check running right now.
        if path.is_file() and not path.name.endswith(".part") and not path.name.startswith(PROBE_PREFIX):
            yield path


_digest_cache: dict[tuple[str, int, int], str] = {}


def _digest(path: Path) -> str:
    # Cached on size + modified time, so rescans don't re-read unchanged files off the share.
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _digest_cache:
        _digest_cache[key] = _hash_file(path)
    return _digest_cache[key]


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1_048_576), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _mark_duplicates(entries: list[FileEntry], master_folder: Path, share: Path | None) -> None:
    """Flag new files whose contents already exist in the same course folder.

    The downloader numbers files by their position on the Moodle page, so when a
    lecturer reorders things the same file comes down again as `04-x.pptx`
    beside `02-x.pptx`.  Only files of equal size are hashed, which keeps reads
    over the network to a minimum.
    """

    # Files that are, or will be, on the share: (course, size) -> [(label, path)]
    kept: dict[tuple[str, int], list[tuple[str, Path]]] = {}
    for entry in entries:
        if entry.state in (SYNCED, CHANGED, REMOTE_ONLY) and share is not None:
            source = share / entry.relative_path
            size = entry.remote_size if entry.remote_size is not None else entry.size
            kept.setdefault((entry.course, size), []).append((entry.relative_path, source))

    hashes: dict[Path, str] = {}

    def digest_of(path: Path) -> str | None:
        if path not in hashes:
            try:
                hashes[path] = _digest(path)
            except OSError:
                return None
        return hashes[path]

    for entry in entries:
        if entry.state != NEW:
            continue
        local = master_folder / entry.relative_path
        candidates = kept.setdefault((entry.course, entry.size), [])
        if candidates:
            local_digest = digest_of(local)
            for label, other in candidates:
                if local_digest and digest_of(other) == local_digest:
                    entry.state = DUPLICATE
                    entry.duplicate_of = label
                    break
        if entry.state == NEW:
            candidates.append((entry.relative_path, local))


def scan_local(master_folder: Path | None = None) -> list[FileEntry]:
    """Every file sitting in the local master folder."""

    master_folder = Path(master_folder or get_master_folder())
    if not master_folder.is_dir():
        return []

    entries = []
    for path in _walk(master_folder):
        stat = path.stat()
        entries.append(
            FileEntry(
                relative_path=str(path.relative_to(master_folder)).replace("\\", "/"),
                size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    return entries


def build_plan(
    master_folder: Path | None = None,
    share_path: str | None = None,
    status: RemoteStatus | None = None,
) -> list[FileEntry]:
    """Mark every local file as new, synced or changed, and list remote-only extras.

    Matching is by the course folder name and file name, which is how the
    downloader names things on both sides.  Nothing is ever deleted.
    """

    master_folder = Path(master_folder or get_master_folder())
    share_path = share_path or get_share_master()
    entries = scan_local(master_folder)

    status = status or check_remote(share_path)
    if not status.share_mounted:
        _mark_duplicates(entries, master_folder, None)
        return entries  # everything else stays "new" until the share comes back

    share = Path(share_path)
    remote: dict[str, int] = {}
    for path in _walk(share):
        remote[str(path.relative_to(share)).replace("\\", "/")] = path.stat().st_size

    for entry in entries:
        if entry.relative_path not in remote:
            entry.state = NEW
            continue
        entry.remote_size = remote[entry.relative_path]
        entry.state = SYNCED if entry.remote_size == entry.size else CHANGED

    local_paths = {entry.relative_path for entry in entries}
    for relative_path, size in sorted(remote.items()):
        if relative_path not in local_paths:
            entries.append(
                FileEntry(
                    relative_path=relative_path,
                    size=size,
                    modified=None,
                    state=REMOTE_ONLY,
                    remote_size=size,
                )
            )

    _mark_duplicates(entries, master_folder, share)
    _mark_remote_duplicates(entries, master_folder, share)
    return entries


def _mark_remote_duplicates(entries: list[FileEntry], master_folder: Path, share: Path) -> None:
    """Never copy a file back into the master folder when its bytes are already there.

    The master folder must hold no duplicates (see master_guard.py), so a copy
    that exists only out there under another name - a duplicate removed here
    before - stays out there.  Only files of a size found locally are read.
    """

    local_by_size: dict[int, list[FileEntry]] = {}
    for entry in entries:
        if entry.state != REMOTE_ONLY:
            local_by_size.setdefault(entry.size, []).append(entry)

    for entry in entries:
        if entry.state != REMOTE_ONLY:
            continue
        for local in local_by_size.get(entry.size, []):
            try:
                if _digest(share / entry.relative_path) == _digest(master_folder / local.relative_path):
                    entry.state = DUPLICATE
                    entry.duplicate_of = local.relative_path
                    break
            except OSError:
                break


# --------------------------------------------------------------------------- #
# pushing
# --------------------------------------------------------------------------- #
@dataclass
class SyncResult:
    """What a push actually did."""

    copied: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    bytes_sent: int = 0
    status: RemoteStatus | None = None

    def summary(self) -> str:
        """A one-line report."""

        if self.status and not self.status.available:
            return f"Share unavailable - {self.status.message}"
        megabytes = self.bytes_sent / 1_048_576
        return (
            f"{len(self.copied)} new, {len(self.updated)} updated, "
            f"{len(self.skipped)} already there, {len(self.duplicates)} duplicates skipped, "
            f"{len(self.failed)} failed "
            f"({megabytes:.1f} MB sent)"
        )


def _copy(source: Path, destination: Path) -> None:
    """Copy through a .part file so an interrupted transfer never looks finished."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        shutil.copy2(source, partial)
        os.replace(partial, destination)
    finally:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass


def push(
    master_folder: Path | None = None,
    share_path: str | None = None,
    include_changed: bool = True,
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
    entries: list[FileEntry] | None = None,
    status: RemoteStatus | None = None,
) -> SyncResult:
    """Copy anything missing from the share, and skip anything already there.

    A file that exists remotely with the same size is left alone, so re-running
    this never creates duplicates.  A file whose size differs is overwritten in
    place when `include_changed` is set, which replaces it rather than adding a
    second copy.
    """

    master_folder = Path(master_folder or get_master_folder())
    share_path = share_path or get_share_master()
    report = on_progress or (lambda _message: None)

    status = status or check_remote(share_path)
    result = SyncResult(status=status)

    if not status.available:
        report(f"Skipping push: {status.message}")
        return result

    if not master_folder.is_dir():
        report(f"No local master folder at '{master_folder}'.")
        return result

    entries = entries if entries is not None else build_plan(master_folder, share_path, status)
    share = Path(share_path)

    for entry in entries:
        if entry.state == REMOTE_ONLY:
            continue
        if entry.state == DUPLICATE:
            result.duplicates.append(entry.relative_path)
            continue
        if entry.state == SYNCED or (entry.state == CHANGED and not include_changed):
            result.skipped.append(entry.relative_path)
            continue

        source = master_folder / entry.relative_path
        destination = share / entry.relative_path
        verb = "Would copy" if dry_run else "Copying"
        report(f"{verb} {entry.relative_path} ({entry.size / 1_048_576:.1f} MB)")

        try:
            if not dry_run:
                _copy(source, destination)
            result.bytes_sent += entry.size
            if entry.state == CHANGED:
                result.updated.append(entry.relative_path)
            else:
                result.copied.append(entry.relative_path)
            if not dry_run:
                entry.state = SYNCED
                entry.remote_size = entry.size
        except OSError as error:
            report(f"FAILED {entry.relative_path}: {error}")
            result.failed.append((entry.relative_path, str(error)))

    report(result.summary())
    return result


def pull(
    master_folder: Path | None = None,
    share_path: str | None = None,
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
    entries: list[FileEntry] | None = None,
    status: RemoteStatus | None = None,
) -> SyncResult:
    """Copy anything that exists only in the destination back to the laptop.

    Together with `push` this makes both sides hold the same files.  Nothing is
    ever deleted: a file missing locally is treated as one to fetch, not as one
    to remove from the destination.
    """

    master_folder = Path(master_folder or get_master_folder())
    share_path = share_path or get_share_master()
    report = on_progress or (lambda _message: None)

    status = status or check_remote(share_path)
    result = SyncResult(status=status)
    if not status.available:
        report(f"Skipping pull: {status.message}")
        return result

    entries = entries if entries is not None else build_plan(master_folder, share_path, status)
    share = Path(share_path)

    for entry in entries:
        if entry.state != REMOTE_ONLY:
            continue
        source = share / entry.relative_path
        destination = master_folder / entry.relative_path
        size = entry.remote_size or 0
        report(f"{'Would fetch' if dry_run else 'Fetching'} {entry.relative_path} ({size / 1_048_576:.1f} MB)")
        try:
            if not dry_run:
                _copy(source, destination)
                entry.state = SYNCED
                entry.size = size
            result.copied.append(entry.relative_path)
            result.bytes_sent += size
        except OSError as error:
            report(f"FAILED {entry.relative_path}: {error}")
            result.failed.append((entry.relative_path, str(error)))

    return result


def ready_destinations(
    destinations: list[Destination], report: ProgressCallback
) -> list[tuple[Destination, RemoteStatus]]:
    """Destinations whose master folder is reachable and not suspected of having lost files."""

    baseline = state.load_counts()
    ready = []
    for destination in destinations:
        status = check_remote(destination.path)
        if not status.available:
            report(f"{destination.name}: not available - {status.message}")
            continue
        present = sum(1 for entry in build_plan(get_master_folder(), destination.path, status)
                      if entry.remote_size is not None)
        verdict = state.guard(destination.name, present, baseline)
        if verdict["suspect"]:
            report(verdict["message"])
            continue
        baseline[destination.name] = {"files": present, "seenAt": datetime.now().isoformat()}
        ready.append((destination, status))
    state.save_counts(baseline)
    return ready


def sync_everything(dry_run: bool = False, on_progress: ProgressCallback | None = None) -> None:
    """Make every copy of the 4th year folder the same: the master folder first, then the rest.

    Used by main.py after a download and by `python -m network_sync`.  Places that
    are offline are skipped and catch up next time.  Nothing is deleted from the
    other copies; duplicates in the local master folder go to the duplicate bin first.
    """

    from network_sync import year_sync  # year_sync builds on this module

    report = on_progress or print
    master = get_master_folder()
    destinations = get_destinations()

    report(f"Master folder: {master}")
    if not dry_run:
        master_guard.sweep(master, log=report)
    ready = ready_destinations(destinations, report)
    # Fetch anything that is only out there first, so it is then sent to every other copy.
    for destination, status in ready:
        fetched = pull(master, destination.path, dry_run=dry_run, on_progress=report, status=status)
        if fetched.copied:
            report(f"{destination.name}: {len(fetched.copied)} file(s) {'would be ' if dry_run else ''}copied back here")
    for destination, status in ready:
        report(f"{destination.name}: {'previewing' if dry_run else 'sending'} master folder...")
        push(master, destination.path, dry_run=dry_run, on_progress=report, status=status)

    report("\nRest of the 4th year folder")
    year_sync.reconcile(year_sync.places(destinations), dry_run=dry_run, on_progress=report)
