"""Download current Moodle courses; copy them to other locations only if you've added some.

    python main.py
"""

from __future__ import annotations

import time

from moodle_downloader import download_all, load_settings
from moodle_downloader.naming import course_title
from network_sync import sync_everything
from network_sync.sync import get_destinations


def is_active(course: dict, now: float) -> bool:
    return course.get("startdate", 0) <= now <= (course.get("enddate") or now)


def print_courses(heading: str, courses: list[dict]) -> None:
    print(f"\n{heading} ({len(courses)})")
    if not courses:
        print("  none")
        return
    width = max(len(course_title(course)) for course in courses)
    for course in courses:
        print(f"  {course_title(course):<{width}}  {course.get('viewurl', '')}")


def main() -> None:
    settings = load_settings()
    courses = download_all(settings)

    now = time.time()
    print_courses("Courses: all", courses)
    print_courses("Courses: active", [c for c in courses if is_active(c, now)])
    print_courses("Courses: favourite", [c for c in courses if c.get("isfavourite")])

    if get_destinations():
        print("\nCopying Moodle files to your other locations")
        sync_everything()


if __name__ == "__main__":
    main()
