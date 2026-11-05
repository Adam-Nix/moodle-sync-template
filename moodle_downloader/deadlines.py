"""Upcoming assignments, quizzes and other hand-ins, with whether you have done them.

Three Moodle sources are merged, one entry per activity:

* assignments in your in-progress courses, plus each one's submission status;
* quizzes in those courses that have a closing date, plus your attempts;
* Moodle's own to-do list (calendar action events) for those same courses, which
  catches other activity types; old courses' leftovers are skipped.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qs, urlparse

from moodle_downloader.client import MoodleClient, MoodleError

PAST_DAYS = 60  # how far back to look for overdue work
AHEAD_DAYS = 400  # some lecturers set due dates a year out by mistake - still show them

_ACADEMIC_YEAR = re.compile(r"\s*\(\d+-\d+\)\s*")
_CODE = re.compile(r"^(.*?)\s+([A-Z]{4}\s?\d{5})$")


def _course(fullname: str) -> tuple[str, str]:
    """'Digital Forensics (26-27) COMP 08083' -> ('Digital Forensics', 'COMP 08083')."""

    name = _ACADEMIC_YEAR.sub(" ", unescape(fullname or "")).strip()
    match = _CODE.match(name)
    return (match.group(1), match.group(2)) if match else (name, "")


def _iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp).isoformat() if timestamp else None


def _cmid(url: str) -> int | None:
    """The course-module id from a /mod/<x>/view.php?id=<cmid> link."""

    value = parse_qs(urlparse(url or "").query).get("id", [None])[0]
    return int(value) if value and value.isdigit() else None


def _entry(kind: str, cmid: int | None, name: str, fullname: str, due: int | None, url: str) -> dict:
    course, code = _course(fullname)
    return {
        "key": f"{kind}-{cmid}",
        "kind": kind,
        "name": unescape(name or ""),
        "course": course,
        "code": code,
        "due": _iso(due),
        "cutoff": None,
        "url": url,
        "status": "todo",  # or "draft" / "submitted"
        "submittedAt": None,
        "graded": False,
        "grade": "",
    }


def _assignments(client: MoodleClient, courses: list[dict]) -> list[dict]:
    params = {f"courseids[{i}]": course["id"] for i, course in enumerate(courses)}
    result = client.call("mod_assign_get_assignments", **params)
    names = {course["id"]: course.get("fullname", "") for course in courses}

    entries = []
    for course in result.get("courses", []):
        for assign in course.get("assignments", []):
            cmid = assign.get("cmid")
            entry = _entry(
                "assign",
                cmid,
                assign.get("name", ""),
                names.get(course.get("id"), course.get("fullname", "")),
                assign.get("duedate") or None,
                f"{client.server}/mod/assign/view.php?id={cmid}",
            )
            entry["cutoff"] = _iso(assign.get("cutoffdate") or None)
            try:
                _add_submission(client, assign["id"], entry)
            except (MoodleError, OSError, ValueError):
                pass  # keep the deadline even if its status can't be read
            entries.append(entry)
    return entries


def _add_submission(client: MoodleClient, assign_id: int, entry: dict) -> None:
    status = client.call("mod_assign_get_submission_status", assignid=assign_id)
    attempt = status.get("lastattempt") or {}
    submission = attempt.get("submission") or attempt.get("teamsubmission") or {}

    state = submission.get("status")
    if state == "submitted":
        entry["status"] = "submitted"
        entry["submittedAt"] = _iso(submission.get("timemodified"))
    elif state == "draft":
        entry["status"] = "draft"
    if attempt.get("extensionduedate"):
        entry["due"] = _iso(attempt["extensionduedate"])

    feedback = status.get("feedback") or {}
    grade = re.sub(r"<[^>]+>", "", unescape(feedback.get("gradefordisplay") or "")).strip()
    entry["graded"] = attempt.get("gradingstatus") == "graded" or bool(grade)
    entry["grade"] = grade


def _quizzes(client: MoodleClient, courses: list[dict]) -> list[dict]:
    params = {f"courseids[{i}]": course["id"] for i, course in enumerate(courses)}
    result = client.call("mod_quiz_get_quizzes_by_courses", **params)
    names = {course["id"]: course.get("fullname", "") for course in courses}

    entries = []
    for quiz in result.get("quizzes", []):
        if not quiz.get("timeclose"):
            continue  # a quiz that never closes isn't a deadline
        cmid = quiz.get("coursemodule")
        entry = _entry(
            "quiz",
            cmid,
            quiz.get("name", ""),
            names.get(quiz.get("course"), ""),
            quiz["timeclose"],
            f"{client.server}/mod/quiz/view.php?id={cmid}",
        )
        try:
            attempts = client.call("mod_quiz_get_user_attempts", quizid=quiz["id"], status="finished")
            finished = attempts.get("attempts", [])
            if finished:
                entry["status"] = "submitted"
                entry["submittedAt"] = _iso(max(a.get("timefinish") or 0 for a in finished))
        except (MoodleError, OSError, ValueError):
            pass
        entries.append(entry)
    return entries


def _action_events(client: MoodleClient, now: int, course_ids: set[int]) -> list[dict]:
    result = client.call(
        "core_calendar_get_action_events_by_timesort",
        timesortfrom=now - PAST_DAYS * 86400,
        timesortto=now + AHEAD_DAYS * 86400,
        limitnum=50,
    )
    entries = []
    for event in result.get("events", []):
        if (event.get("course") or {}).get("id") not in course_ids:
            continue  # e.g. a repeat assessment left over in last year's course
        action = event.get("action") or {}
        url = event.get("url") or action.get("url") or ""
        kind = event.get("modulename") or "event"
        entries.append(
            _entry(
                kind,
                _cmid(url) or event.get("id"),
                event.get("activityname") or event.get("name", ""),
                (event.get("course") or {}).get("fullname", ""),
                event.get("timesort"),
                url.split("&action=")[0],
            )
        )
    return entries


def fetch_deadlines(client: MoodleClient, now: float | None = None) -> list[dict]:
    """Every dated hand-in from the last few weeks onward, soonest first."""

    now = int(now or time.time())
    courses = client.enrolled_courses("inprogress")

    course_ids = {course["id"] for course in courses}
    sources = [lambda: _action_events(client, now, course_ids)]
    if courses:
        sources[:0] = [lambda: _assignments(client, courses), lambda: _quizzes(client, courses)]

    merged: dict[str, dict] = {}
    for source in sources:
        try:
            found = source()
        except (MoodleError, OSError, ValueError):
            continue  # one broken source shouldn't hide the others
        for entry in found:
            merged.setdefault(entry["key"], entry)  # the first source has the richer status

    earliest = datetime.fromtimestamp(now - PAST_DAYS * 86400).isoformat()
    dated = [e for e in merged.values() if e["due"] and e["due"] >= earliest]
    undated = [e for e in merged.values() if not e["due"] and e["status"] != "submitted"]
    dated.sort(key=lambda e: e["due"])
    return dated + undated
