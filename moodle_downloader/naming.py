"""Folder and file names, kept identical to the ones already on disk."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

from moodle_downloader.config import REPO_ROOT, current_year_category, master_folder

DATA_FOLDER = "_data"
ARCHIVE_FOLDER = "archive"

# The current year's category comes from [Courses] current_category in config.ini
# (chosen on the first run); blank puts every course in the master folder.

_ILLEGAL = re.compile(r'[/:*?"<>|%\\\x00-\x1f\x7f]')
_ACADEMIC_YEAR = re.compile(r"\(\d+-\d+\)")


def slugify(text: str) -> str:
    """'Lecture 1: Intro (v2).PDF' -> 'lecture-1-intro-v2.pdf'."""

    text = _ILLEGAL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).replace(" ", "-")
    text = re.sub(r"[()]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("- ").replace("-.", ".").lower()


def course_title(course: dict) -> str:
    """'Applied Cryptography (26-27) CRYP 08004' -> 'Applied Cryptography CRYP 08004(26-27)'."""

    name = unescape(course.get("fullname", "")).replace("&", "and")
    match = _ACADEMIC_YEAR.search(name)
    if not match:
        return name
    return name.replace(match.group(0), "").strip() + match.group(0)


def course_folder_name(course: dict) -> str:
    return slugify(f"{course_title(course)}-id-{course.get('id', '')}")


def course_folder(course: dict, root: Path = REPO_ROOT) -> Path:
    """Where a course's files go.

    The current year goes in the master folder inside the 4th year folder (C:\\4th year
    by default, outside this repo).  A course that has already been archived keeps
    downloading into the archive instead of reappearing in the root.
    """

    name = course_folder_name(course)
    current = current_year_category()
    if not current or course.get("coursecategory", "") == current:
        return master_folder() / name
    if (root / ARCHIVE_FOLDER / name).is_dir():
        return root / ARCHIVE_FOLDER / name
    return root / name


def numbered_file_name(number: int, file_name: str) -> str:
    return f"{number:02d}-{slugify(file_name)}"


def contents_json_path(course: dict, root: Path = REPO_ROOT) -> Path:
    return root / DATA_FOLDER / f"course-{slugify(course_title(course))}-contents.json"


def program_json_path(program: str, root: Path = REPO_ROOT) -> Path:
    return root / DATA_FOLDER / f"program-{slugify(program)}-courses.json"
