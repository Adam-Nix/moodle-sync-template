"""The few things that differ between Windows, Linux and macOS.

Everything else in the program uses pathlib and plain Python, so it behaves the same
everywhere.  Keep the operating-system checks in this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


class OpenError(OSError):
    """A folder or file couldn't be shown; the message says why in plain words."""


# --------------------------------------------------------------------------- #
# opening things in the file manager
# --------------------------------------------------------------------------- #
def _launch(command: list[str]) -> None:
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise OpenError(f"'{command[0]}' isn't installed, so this can't be opened from here.") from error


def open_path(path: str | Path) -> None:
    """Open a folder or file the way a double-click would."""

    path = str(path)
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606 - the user's own file or folder
    elif IS_MAC:
        _launch(["open", path])
    else:
        _launch(["xdg-open", path])


def reveal(path: str | Path) -> None:
    """Show a file in its folder, selected where the file manager allows it."""

    path = Path(path)
    if IS_WINDOWS:
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif IS_MAC:
        _launch(["open", "-R", str(path)])
    else:
        _launch(["xdg-open", str(path.parent)])  # Linux file managers can't agree on "select"


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def is_absolute(path: str) -> bool:
    """A full path on this system (on Windows that includes \\\\server\\share)."""

    return os.path.isabs(path) or (IS_WINDOWS and path.startswith("\\\\"))


def example_folder() -> str:
    """A sample folder path to show in hints, written the way this system writes it."""

    if IS_WINDOWS:
        return "E:\\Moodle"
    return "/Volumes/Backup/Moodle" if IS_MAC else "/media/you/Backup/Moodle"


def example_share() -> str:
    if IS_WINDOWS:
        return "\\\\server\\share\\Moodle"
    return "/mnt/server/Moodle"  # a share has to be mounted first, then it is just a folder


def find_onedrive() -> str:
    """This PC's OneDrive folder, or "" if there isn't one."""

    if IS_WINDOWS:
        base = os.environ.get("OneDriveConsumer") or os.environ.get("OneDrive") or ""
        return base if base and os.path.isdir(base) else ""
    home = Path.home()
    candidates = [home / "OneDrive"]  # where the common Linux sync clients put it
    if IS_MAC:
        candidates += sorted((home / "Library" / "CloudStorage").glob("OneDrive*"))
    return next((str(p) for p in candidates if p.is_dir()), "")


def can_open_folders() -> bool:
    return IS_WINDOWS or shutil.which("open" if IS_MAC else "xdg-open") is not None
