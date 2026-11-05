"""Fetch every current course: data, readmes and files."""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from moodle_downloader import master_guard
from moodle_downloader.client import MoodleClient, MoodleError
from moodle_downloader.config import REPO_ROOT, Settings, master_folder, year_root
from moodle_downloader.course_page import RemoteFile, build_course_page
from moodle_downloader.file_index import FileIndex, entry_for, place_files, unchanged
from moodle_downloader.naming import (
    SKIPPED_CATEGORIES,
    contents_json_path,
    course_folder,
    course_title,
    program_json_path,
)

Log = Callable[[str], None]
# Called with (path relative to the repo root or 4th year folder, bytes) after each file is saved.
OnFile = Callable[[str, int], None]
CLOCK_SLACK = 2  # seconds of timestamp difference still treated as "same file"


def _save_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


def _display_path(path: Path, root: Path) -> str:
    """A saved file's path relative to the repo, or to the 4th year folder for this year's courses."""

    for base in (root, year_root()):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _up_to_date(path: Path, remote: RemoteFile) -> bool:
    """Same size, and not older than the copy on Moodle."""

    try:
        stat = path.stat()
    except OSError:
        return False
    if remote.size and stat.st_size != remote.size:
        return False
    if remote.modified and stat.st_mtime + CLOCK_SLACK < remote.modified:
        return False
    return True


def download_all(
    settings: Settings,
    root: Path = REPO_ROOT,
    log: Log = print,
    on_file: OnFile | None = None,
) -> list[dict]:
    """Download data, readmes and new or changed files for every in-progress course."""

    client = MoodleClient(settings.server, settings.token)
    info = client.site_info()
    log(f"Logged in to {info.get('sitename', settings.server)} as {info.get('fullname', '?')}")

    courses = client.enrolled_courses()
    for course in courses:
        # Inline images are huge base64 blobs; drop them from the saved data.
        if str(course.get("courseimage", "")).startswith("data:image/"):
            course["courseimage"] = ""
    courses.sort(key=lambda course: course.get("fullname", ""))
    _save_json({"courses": courses}, program_json_path(settings.program, root))

    totals = {"new": 0, "skipped": 0, "duplicates": 0, "failed": 0, "bytes": 0}
    master_guard.sweep(master_folder(), root, log)  # start from a master folder with no duplicates
    index = FileIndex(root)
    for course in courses:
        if course.get("coursecategory") in SKIPPED_CATEGORIES:
            continue
        _download_course(client, settings, course, root, log, totals, index, on_file)

    _write_root_readme(settings.program, courses, root)
    log(
        f"Moodle: {totals['new']} downloaded, {totals['skipped']} already up to date, "
        f"{totals['duplicates']} duplicates dropped, "
        f"{totals['failed']} failed ({totals['bytes'] / 1_048_576:.1f} MB)"
    )
    return courses


def _download_course(
    client: MoodleClient,
    settings: Settings,
    course: dict,
    root: Path,
    log: Log,
    totals: dict,
    index: FileIndex,
    on_file: OnFile | None = None,
) -> None:
    title = course_title(course)
    folder = course_folder(course, root)
    log(f"\n{title}")

    try:
        contents = client.course_contents(course["id"])
    except (MoodleError, OSError) as error:
        log(f"  could not read course: {error}")
        totals["failed"] += 1
        return
    _save_json(contents, contents_json_path(course, root))

    root_readme = Path(os.path.relpath(root / "readme.md", folder)).as_posix()
    page = build_course_page(course, contents, title, settings.program, root_readme)
    folder.mkdir(parents=True, exist_ok=True)

    known = index.course(folder)
    places = place_files(folder, page.files, known)
    master = master_folder()
    # Only the master folder is kept free of duplicates; archived years may hold copies.
    in_master = master_guard.is_inside(folder, master)
    for remote in page.files:
        place = places[remote.relative_path]
        destination = folder / place
        entry = known.get(remote.key)
        kept = entry is not None and entry["path"] == place
        if (kept and unchanged(entry, remote)) or _up_to_date(destination, remote):
            totals["skipped"] += 1
            known[remote.key] = entry_for(place, remote, own=entry.get("own", True) if kept else True)
            continue

        log(f"  downloading {remote.relative_path} ({remote.size / 1_048_576:.1f} MB)")
        try:
            written = client.download(remote.url, destination, remote.modified)
        except (MoodleError, OSError) as error:
            log(f"  FAILED {remote.relative_path}: {error}")
            totals["failed"] += 1
            time.sleep(0.5)
            continue
        totals["bytes"] += written

        twin = master_guard.twin_of(destination, master) if in_master else None
        if twin:
            destination.unlink()
            # relative to this course folder, so it may lead into another course (../other/x.pdf)
            twin_place = Path(os.path.relpath(twin, folder)).as_posix()
            places[remote.relative_path] = twin_place
            known[remote.key] = entry_for(twin_place, remote, own=False)
            log(f"  {remote.relative_path} is the same as {twin.relative_to(master).as_posix()} - not kept")
            totals["duplicates"] += 1
            continue
        known[remote.key] = entry_for(place, remote)
        totals["new"] += 1
        if on_file:
            on_file(_display_path(destination, root), written)

    index.save()
    page = build_course_page(course, contents, title, settings.program, root_readme, places)
    (folder / "readme.md").write_text(page.markdown, encoding="utf-8")


def _write_root_readme(program: str, courses: list[dict], root: Path) -> None:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for course in courses:
        category = course.get("coursecategory", "")
        if category not in SKIPPED_CATEGORIES:
            by_category[category].append(course)

    lines = [f"# {program}", ""]
    for category, members in by_category.items():
        lines += [f"## {category}", ""]
        for course in members:
            title = course_title(course)
            # relpath, not relative_to: this year's courses live outside the repo (C:\4th year).
            readme = course_folder(course, root) / "readme.md"
            link = quote(Path(os.path.relpath(readme, root)).as_posix())
            lines.append(f'- [{title}]({link} "{title}")')
        lines.append("")
    lines += ["---", ""]
    (root / "readme.md").write_text("\n".join(lines), encoding="utf-8")
