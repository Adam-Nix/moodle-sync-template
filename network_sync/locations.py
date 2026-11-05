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

from moodle_downloader import platform_utils
from moodle_downloader.ini_edit import drop_section, read_lines, set_option, write_lines
from network_sync import sync as ns

SECTION_PREFIX = "Location"
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")  # also safe inside a file name


class LocationError(ValueError):
    """Something the person can fix: a bad name, a path that can't be used, ..."""


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
    path = os.path.expanduser(os.path.expandvars(path.strip().strip('"')))
    path = path.rstrip("\\/") if len(path) > 1 else path
    examples = f"{platform_utils.example_folder()} or {platform_utils.example_share()}"
    if not path:
        raise LocationError(f"Enter the folder to copy to, e.g. {examples}.")
    if not platform_utils.is_absolute(path):
        raise LocationError(f"Use a full path, like {examples}.")
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
    if not folder.is_dir() and not (platform_utils.IS_WINDOWS and path.startswith("\\\\")):
        try:
            if not folder.parent.is_dir():
                raise OSError(f"{folder.parent} doesn't exist")
            folder.mkdir()
        except OSError as error:
            raise LocationError(f"Can't use that folder: {error}") from error

    status = ns.check_remote(path)
    if not status.available:
        raise LocationError(f"{status.message or 'That folder cannot be used right now.'} Nothing was saved.")

    lines = set_option(read_lines(config_file), f"{SECTION_PREFIX} {name}", "path", path)
    write_lines(config_file, lines)
    return ns.Destination(name, path, kind="location")


def remove(name: str, config_file: str = ns.CONFIG_FILE) -> None:
    """Stop copying to a location.  Files already there are left untouched."""

    found = next((d for d in ns.get_destinations(config_file) if d.name == name), None)
    if found is None:
        raise LocationError(f"There is no location called '{name}'.")

    lines = read_lines(config_file)
    if found.kind == "location":
        lines = drop_section(lines, f"{SECTION_PREFIX} {name}")
    elif found.kind == "share":
        if os.environ.get(ns.SHARE_ENV):
            raise LocationError(f"This one comes from the {ns.SHARE_ENV} environment variable; unset it first.")
        lines = set_option(lines, "Network", "share", "")
    else:  # onedrive: blank (not missing) so it isn't detected again
        lines = set_option(lines, "Network", "onedrive", "")
    write_lines(config_file, lines)


def suggestions(config_file: str = ns.CONFIG_FILE) -> list[dict]:
    """Places worth offering: OneDrive, when this PC has it and it isn't one of the locations."""

    have = {os.path.normcase(d.root) for d in ns.get_destinations(config_file)}
    found = []
    onedrive = ns.detect_onedrive()
    if onedrive and os.path.normcase(onedrive) not in have:
        found.append({"name": "OneDrive", "path": onedrive})
    return found
