"""A small client for the Moodle web service REST API."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

REST_PATH = "/webservice/rest/server.php"


class MoodleError(RuntimeError):
    """Moodle answered, but with an error (bad token, no permission, ...)."""


class MoodleClient:
    """Calls web service functions with your token."""

    def __init__(self, server: str, token: str, timeout: float = 60.0) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, function: str, **params) -> dict | list:
        """Run one web service function and return its JSON."""

        # The token goes in the POST body, not the URL, so it never lands in logs.
        data = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}
        data.update(params)
        response = self.session.post(self.server + REST_PATH, data=data, timeout=self.timeout)
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and "exception" in result:
            raise MoodleError(f"{function}: {result.get('message', result['exception'])}")
        return result

    # ------------------------------------------------------------ functions
    def site_info(self) -> dict:
        """Who the token belongs to."""

        return self.call("core_webservice_get_site_info")

    def enrolled_courses(self, classification: str = "inprogress") -> list[dict]:
        """Courses you are enrolled on (in progress by default)."""

        result = self.call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification=classification,
        )
        return result.get("courses", []) if isinstance(result, dict) else []

    def course_contents(self, course_id: int) -> list[dict]:
        """Sections, activities and files of one course."""

        result = self.call("core_course_get_contents", courseid=course_id)
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------ files
    def file_url(self, file_url: str) -> str:
        """A pluginfile URL with the token attached, which Moodle needs for downloads."""

        parts = urlsplit(file_url)
        query = dict(parse_qsl(parts.query))
        query["token"] = self.token
        query.setdefault("forcedownload", "1")
        return urlunsplit(parts._replace(query=urlencode(query)))

    def download(self, file_url: str, destination: Path, modified: int | None = None) -> int:
        """Save a file, going via a .part file so a dropped download never looks complete."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        written = 0
        try:
            with self.session.get(self.file_url(file_url), stream=True, timeout=self.timeout) as response:
                response.raise_for_status()
                if "application/json" in response.headers.get("Content-Type", ""):
                    # Moodle reports download errors as JSON with a 200 status.
                    error = response.json()
                    raise MoodleError(error.get("error") or error.get("message") or str(error))
                with open(partial, "wb") as file:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        file.write(chunk)
                        written += len(chunk)
            os.replace(partial, destination)
        finally:
            if partial.exists():
                partial.unlink()

        if modified:
            os.utime(destination, (modified, modified))
        return written
