"""Read and change everything in config.ini and secrets.ini, for the dashboard's Settings screen.

Nothing here prints or prompts.  A problem the person can fix raises SettingsError with a
plain-English message.  The Moodle token and website passwords are only ever written;
the page is told whether one is saved, never what it is.
"""

from __future__ import annotations

import os
import re
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path

from moodle_downloader import platform_utils, webpages
from moodle_downloader.client import MoodleClient
from moodle_downloader.config import CONFIG_FILE, DEFAULT_YEAR_ROOT, SECRETS_FILE, SECTION, master_folder
from moodle_downloader.ini_edit import drop_section, read_lines, set_option, write_lines

SITE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")


class SettingsError(ValueError):
    """Something the person can fix: a missing token, a folder that can't be made, ..."""


def _parser(path: Path | str) -> ConfigParser:
    parser = ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser


def _single_line(value: object, label: str) -> str:
    text = str(value or "").strip()
    if "\n" in text or "\r" in text:
        raise SettingsError(f"{label} must be on one line.")
    return text


def is_configured(config_file: Path | str = CONFIG_FILE) -> bool:
    parser = _parser(config_file)
    return all(parser.get(SECTION, key, raw=True, fallback="").strip() for key in ("server", "token"))


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def read(config_file: Path | str = CONFIG_FILE, secrets_file: Path | str = SECRETS_FILE) -> dict:
    """Everything the Settings screen shows, with no secret in it."""

    parser = _parser(config_file)

    def get(section: str, key: str) -> str:
        return parser.get(section, key, raw=True, fallback="").strip()

    skip = [c.strip() for c in get("Courses", "skip_categories").split(",") if c.strip()]
    folder = get("Folders", "local")
    master = master_folder(Path(config_file)) if folder else None
    courses = sorted(p.name for p in master.iterdir() if p.is_dir()) if master and master.is_dir() else []
    return {
        "configured": is_configured(config_file),
        "moodle": {
            "server": get(SECTION, "server"),
            "program": get(SECTION, "program").replace("%%", "%"),
            "tokenSet": bool(get(SECTION, "token")),
        },
        "folder": folder,
        "defaultFolder": DEFAULT_YEAR_ROOT,
        "category": get("Courses", "current_category"),
        "skip": skip,
        "semesterEnd": get("Deadlines", "semester_end"),
        "syncOther": get("Network", "sync_other_files").lower() in ("1", "true", "yes", "on"),
        "courseFolders": courses,
        "sites": [
            {
                "name": site.name,
                "course": site.course,
                "index": site.index,
                "pages": ", ".join(site.pages),
                "subfolder": site.subfolder,
                "printQuery": site.print_query,
                "username": site.username,
                "passwordSet": bool(site.password),
            }
            for site in webpages.get_sites(config_file, secrets_file)
        ],
    }


# --------------------------------------------------------------------------- #
# Moodle
# --------------------------------------------------------------------------- #
def clean_server(server: str) -> str:
    server = _single_line(server, "The Moodle address").rstrip("/")
    if "%" in server:
        raise SettingsError("The Moodle address can't contain a % sign.")
    if server and not re.match(r"^https?://[^/\s]+", server):
        server = "https://" + server.removeprefix("//")
    if not re.match(r"^https?://[^/\s]+\.[^/\s]+", server) and "localhost" not in server:
        raise SettingsError("Enter your Moodle address, like https://moodle.yourcollege.ie")
    return server


def saved_token(config_file: Path | str = CONFIG_FILE) -> str:
    return _parser(config_file).get(SECTION, "token", raw=True, fallback="").strip()


def test_moodle(server: str, token: str = "", config_file: Path | str = CONFIG_FILE) -> dict:
    """Log in with these details (or the saved token if none is typed) and list the course categories."""

    server = clean_server(server)
    token = _single_line(token, "The key") or saved_token(config_file)
    if not token:
        raise SettingsError("Paste your Moodle key first.")
    client = MoodleClient(server, token, timeout=20)
    try:
        info = client.site_info()
        courses = client.enrolled_courses()
    except Exception as error:  # wrong address, bad key, offline - all the same to the person
        raise SettingsError(
            f"Couldn't log in to {server}. Check the address and the key. ({str(error)[:150]})"
        ) from error
    return {
        "site": info.get("sitename", server),
        "name": info.get("fullname", ""),
        "categories": sorted({c.get("coursecategory", "") for c in courses if c.get("coursecategory")}),
        "courses": len(courses),
    }


# --------------------------------------------------------------------------- #
# saving
# --------------------------------------------------------------------------- #
def save_general(data: dict, config_file: Path | str = CONFIG_FILE) -> None:
    """Save the Moodle login, folder, course categories and semester end."""

    config_file = str(config_file)
    moodle = data.get("moodle") or {}
    server = clean_server(moodle.get("server", ""))
    token = _single_line(moodle.get("token", ""), "The key") or saved_token(config_file)
    if not token:
        raise SettingsError("Paste your Moodle key - it is how the program logs in for you.")
    if "%" in token:
        raise SettingsError("That doesn't look like a Moodle key (it has a % sign).")
    program = _single_line(moodle.get("program", ""), "The programme name") or "My courses"

    folder = _single_line(data.get("folder", ""), "The folder").strip('"') or DEFAULT_YEAR_ROOT
    expanded = Path(os.path.expanduser(os.path.expandvars(folder)))
    if not expanded.is_absolute():
        raise SettingsError(f"Use a full folder path, like {DEFAULT_YEAR_ROOT}.")
    try:
        expanded.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SettingsError(f"Can't use that folder: {error}") from error

    end = _single_line(data.get("semesterEnd", ""), "The semester end date")
    if end:
        try:
            datetime.strptime(end, "%Y-%m-%d")
        except ValueError as error:
            raise SettingsError("Pick the semester end date from the calendar.") from error

    category = _single_line(data.get("category", ""), "The category")
    skip = [_single_line(c, "A category").replace(",", " ") for c in data.get("skip", []) if str(c).strip()]
    skip = [c for c in skip if c != category]

    lines = read_lines(config_file)
    for section, key, value in (
        (SECTION, "program", program.replace("%", "%%")),
        (SECTION, "server", server),
        (SECTION, "token", token),
        ("Folders", "local", folder),
        ("Courses", "current_category", category),
        ("Courses", "skip_categories", ", ".join(skip)),
        ("Deadlines", "semester_end", end),
        ("Network", "sync_other_files", "true" if data.get("syncOther") else "false"),
    ):
        lines = set_option(lines, section, key, value)
    write_lines(config_file, lines)


# --------------------------------------------------------------------------- #
# course websites
# --------------------------------------------------------------------------- #
def save_site(
    data: dict,
    original: str = "",
    config_file: Path | str = CONFIG_FILE,
    secrets_file: Path | str = SECRETS_FILE,
) -> str:
    """Add a course website, or change the one called `original`; returns its name."""

    config_file, secrets_file = str(config_file), str(secrets_file)
    name = " ".join(_single_line(data.get("name", ""), "The name").split())
    if not SITE_NAME.match(name):
        raise SettingsError("Give the website a short name using letters, numbers, spaces, - or _.")
    others = {s.name.lower() for s in webpages.get_sites(config_file, secrets_file) if s.name != original}
    if name.lower() in others:
        raise SettingsError(f"You already have a website called '{name}'.")

    course = _single_line(data.get("course", ""), "The course folder").strip().strip("/\\")
    if not course or Path(course).is_absolute() or ".." in Path(course).parts:
        raise SettingsError("Say which course folder the slides go in, e.g. the one Moodle made for the module.")
    index = _single_line(data.get("index", ""), "The slides page")
    pages = [p.strip() for p in re.split(r"[,\n]", str(data.get("pages", ""))) if p.strip()]
    if not index and not pages:
        raise SettingsError("Enter the web address of the page that lists the slides (or list the slides yourself).")
    if index and not re.match(r"^https?://", index):
        raise SettingsError("The slides page must start with https://")

    section = f"{webpages.SECTION_PREFIX} {name}"
    old_section = f"{webpages.SECTION_PREFIX} {original}" if original else ""
    lines = read_lines(config_file)
    if old_section and old_section != section:
        lines = drop_section(lines, old_section)
    lines = drop_section(lines, section)  # rewritten whole, so a removed field really goes
    fields = [
        ("course", course),
        ("index", index),
        ("pages", ", ".join(pages)),
        ("subfolder", _single_line(data.get("subfolder", webpages.DEFAULT_SUBFOLDER), "The subfolder").strip("/\\")),
        ("print_query", _single_line(data.get("printQuery", webpages.DEFAULT_PRINT_QUERY), "The print view")),
    ]
    for key, value in fields:
        lines = set_option(lines, section, key, value)
    write_lines(config_file, lines)

    # Logins go in secrets.ini; a blank password keeps the one already saved.
    username = _single_line(data.get("username", ""), "The username")
    password = _single_line(data.get("password", ""), "The password")
    previous = webpages._read_logins(secrets_file)
    kept = previous.get(old_section.lower() or section.lower(), ("", ""))[1] if not password else ""
    secret_lines = read_lines(secrets_file) or webpages.SECRETS_HEADER.strip().splitlines()
    if old_section and old_section != section:
        secret_lines = drop_section(secret_lines, old_section)
    secret_lines = set_option(secret_lines, section, "username", username)
    secret_lines = set_option(secret_lines, section, "password", password or kept)
    write_lines(secrets_file, secret_lines)
    return name


def remove_site(
    name: str, config_file: Path | str = CONFIG_FILE, secrets_file: Path | str = SECRETS_FILE
) -> None:
    """Forget a course website and its login.  Slides already printed stay where they are."""

    section = f"{webpages.SECTION_PREFIX} {name}"
    if not any(s.name == name for s in webpages.get_sites(config_file, secrets_file)):
        raise SettingsError(f"There is no website called '{name}'.")
    write_lines(str(config_file), drop_section(read_lines(str(config_file)), section))
    if Path(secrets_file).exists():
        write_lines(str(secrets_file), drop_section(read_lines(str(secrets_file)), section))
