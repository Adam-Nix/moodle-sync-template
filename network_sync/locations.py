"""Add, test and remove the places your files are copied to, from the dashboard.

A location is any folder: a network share, a OneDrive folder, a USB drive.  Each one
is saved in config.ini as its own section, which this module edits as plain text so
the comments in the file survive:

    [Location Backup drive]
    path = E:\\Moodle

The older `share` and `onedrive` lines under [Network] still work and show up as
"Home server" and "OneDrive"; removing one of those blanks its line.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from network_sync import sync as ns

SECTION_PREFIX = "Location"
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")  # also safe inside a file name


class LocationError(ValueError):
    """Something the person can fix: a bad name, a path that can't be used, ..."""


# --------------------------------------------------------------------------- #
# editing config.ini as text
# --------------------------------------------------------------------------- #
def _read(config_file: str) -> list[str]:
    try:
        return Path(config_file).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write(config_file: str, lines: list[str]) -> None:
    Path(config_file).write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    """(header index, index after the section's last line), or None if it isn't there."""

    header = re.compile(r"^\s*\[(.*?)\]\s*$")
    start = None
    for number, line in enumerate(lines):
        found = header.match(line)
        if found and start is not None:
            return start, number
        if found and found.group(1).strip().lower() == section.lower():
            start = number
    return (start, len(lines)) if start is not None else None


def _set_option(lines: list[str], section: str, key: str, value: str) -> list[str]:
    """Set `key = value` in a section, replacing the line or adding it; comments are kept."""

    span = _section_span(lines, section)
    if span is None:
        return [*lines, *([""] if lines else []), f"[{section}]", f"{key} = {value}"]
    start, end = span
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*[=:]", re.IGNORECASE)
    for number in range(start + 1, end):
        if pattern.match(lines[number]):
            lines[number] = f"{key} = {value}"
            return lines
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # keep the blank line that separates sections
    lines.insert(insert_at, f"{key} = {value}")
    return lines


def _drop_section(lines: list[str], section: str) -> list[str]:
    span = _section_span(lines, section)
    if span is None:
        return lines
    start, end = span
    trimmed = lines[:start] + lines[end:]
    while start > 0 and start <= len(trimmed) and not trimmed[start - 1].strip() and (
        start == len(trimmed) or not trimmed[start].strip()
    ):
        del trimmed[start - 1]  # don't leave a gap of blank lines
        start -= 1
    return trimmed


# --------------------------------------------------------------------------- #
# the operations the dashboard uses
# --------------------------------------------------------------------------- #
def clean_name(name: str, existing: list[ns.Destination]) -> str:
    name = " ".join(name.split())
    if not NAME_OK.match(name):
        raise LocationError("Give it a short name using letters, numbers, spaces, - or _.")
    if name.lower() in {"local", "this laptop", "this pc"} | {d.name.lower() for d in existing}:
        raise LocationError(f"There is already a location called '{name}'.")
    return name


def clean_path(path: str) -> str:
    path = os.path.expandvars(path.strip().strip('"')).rstrip("\\/")
    if not path:
        raise LocationError("Enter the folder to copy to, e.g. E:\\Moodle or \\\\server\\share\\Moodle.")
    if not (os.path.isabs(path) or path.startswith("\\\\")):
        raise LocationError("Use a full path, like E:\\Moodle or \\\\server\\share\\Moodle.")
    return path


def test(path: str) -> ns.RemoteStatus:
    """Check a path the way a sync would (server up, folder there, writable)."""

    return ns.check_remote(clean_path(path))


def add(name: str, path: str, config_file: str = ns.CONFIG_FILE) -> ns.Destination:
    """Save a new location after checking it can be used; returns it."""

    existing = ns.get_destinations(config_file)
    name, path = clean_name(name, existing), clean_path(path)
    if any(os.path.normcase(d.root) == os.path.normcase(path) for d in existing):
        raise LocationError("That folder is already one of your locations.")

    folder = Path(path)
    if not folder.is_dir() and not path.startswith("\\\\"):
        try:
            if not folder.parent.is_dir():
                raise OSError(f"{folder.parent} doesn't exist")
            folder.mkdir()
        except OSError as error:
            raise LocationError(f"Can't use that folder: {error}") from error

    status = ns.check_remote(path)
    if not status.available:
        raise LocationError(f"{status.message or 'That folder cannot be used right now.'} Nothing was saved.")

    lines = _set_option(_read(config_file), f"{SECTION_PREFIX} {name}", "path", path)
    _write(config_file, lines)
    return ns.Destination(name, path, kind="location")


def remove(name: str, config_file: str = ns.CONFIG_FILE) -> None:
    """Stop copying to a location.  Files already there are left untouched."""

    found = next((d for d in ns.get_destinations(config_file) if d.name == name), None)
    if found is None:
        raise LocationError(f"There is no location called '{name}'.")

    lines = _read(config_file)
    if found.kind == "location":
        lines = _drop_section(lines, f"{SECTION_PREFIX} {name}")
    elif found.kind == "share":
        if os.environ.get(ns.SHARE_ENV):
            raise LocationError(f"This one comes from the {ns.SHARE_ENV} environment variable; unset it first.")
        lines = _set_option(lines, "Network", "share", "")
    else:  # onedrive: blank (not missing) so it isn't detected again
        lines = _set_option(lines, "Network", "onedrive", "")
    _write(config_file, lines)


def suggestions(config_file: str = ns.CONFIG_FILE) -> list[dict]:
    """Places worth offering: OneDrive, when this PC has it and it isn't one of the locations."""

    have = {os.path.normcase(d.root) for d in ns.get_destinations(config_file)}
    found = []
    onedrive = ns.detect_onedrive()
    if onedrive and os.path.normcase(onedrive) not in have:
        found.append({"name": "OneDrive", "path": onedrive})
    return found
