"""Read the Moodle login from config.ini, asking for it the first time."""

from __future__ import annotations

import os
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config.ini"
# Logins for course websites, kept out of config.ini so it can be shared more freely.
SECRETS_FILE = REPO_ROOT / "secrets.ini"
SECTION = "User"
DEFAULT_YEAR_ROOT = str(Path.home() / "Documents" / "Moodle")
MASTER_FOLDER = "materials"  # every downloaded Moodle file lives in here


@dataclass
class Settings:
    """Who to log in as and what to call the programme."""

    program: str
    server: str
    token: str


def load_settings(config_file: Path = CONFIG_FILE) -> Settings:
    """Read [User] from config.ini, prompting and saving it if it is missing."""

    parser = ConfigParser()
    parser.read(config_file, encoding="utf-8")

    if not all(parser.has_option(SECTION, key) for key in ("program", "server", "token")):
        print("No Moodle login saved yet.")
        server = input("Moodle address (e.g. https://moodle.example.ie): ").strip().rstrip("/")
        print(f"Get your 'Moodle mobile web service' key from {server}/user/managetoken.php")
        token = input("Web service key: ").strip()
        program = input("Programme name (used as the readme title): ").strip()

        if not parser.has_section(SECTION):
            parser.add_section(SECTION)
        parser.set(SECTION, "program", program)
        parser.set(SECTION, "server", server)
        parser.set(SECTION, "token", token)
        with open(config_file, "w", encoding="utf-8") as file:
            parser.write(file)

    _ask_for_folder(parser, config_file)

    return Settings(
        program=parser.get(SECTION, "program").strip(),
        server=parser.get(SECTION, "server").strip().rstrip("/"),
        token=parser.get(SECTION, "token").strip(),
    )


def _ask_for_folder(parser: ConfigParser, config_file: Path) -> None:
    """First run only: ask where the local folder should live (Enter keeps the default)."""

    if parser.get("Folders", "local", raw=True, fallback="").strip() or not sys.stdin.isatty():
        return
    answer = input(f"Folder for your course files [{DEFAULT_YEAR_ROOT}]: ").strip().strip('"')
    if not parser.has_section("Folders"):
        parser.add_section("Folders")
    parser.set("Folders", "local", answer or DEFAULT_YEAR_ROOT)
    with open(config_file, "w", encoding="utf-8") as file:
        parser.write(file)


def _courses_option(name: str, config_file: Path) -> str:
    parser = ConfigParser()
    parser.read(config_file, encoding="utf-8")
    return parser.get("Courses", name, raw=True, fallback="").strip()


def current_year_category(config_file: Path = CONFIG_FILE) -> str:
    """[Courses] current_category: the Moodle category whose courses go in the master folder.

    Blank means every course goes there.
    """

    return _courses_option("current_category", config_file)


def skipped_categories(config_file: Path = CONFIG_FILE) -> set[str]:
    """[Courses] skip_categories (comma separated): Moodle categories to ignore."""

    return {c.strip() for c in _courses_option("skip_categories", config_file).split(",") if c.strip()}


def choose_current_category(categories: list[str], config_file: Path = CONFIG_FILE) -> None:
    """First run only: let the user pick which Moodle category is the current year."""

    parser = ConfigParser()
    parser.read(config_file, encoding="utf-8")
    if parser.has_option("Courses", "current_category") or not sys.stdin.isatty():
        return
    categories = sorted({c for c in categories if c})
    chosen = ""
    if len(categories) > 1:
        print("\nYour courses are in these Moodle categories:")
        for number, name in enumerate(categories, 1):
            print(f"  {number}. {name}")
        answer = input("Which one is the current year? (number, or Enter for all): ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(categories):
            chosen = categories[int(answer) - 1]
    if not parser.has_section("Courses"):
        parser.add_section("Courses")
    parser.set("Courses", "current_category", chosen)
    with open(config_file, "w", encoding="utf-8") as file:
        parser.write(file)


def year_root(config_file: Path = CONFIG_FILE) -> Path:
    """This PC's 4th year folder, from [Folders] local; created if it is missing."""

    parser = ConfigParser()
    parser.read(config_file, encoding="utf-8")
    value = parser.get("Folders", "local", raw=True, fallback="").strip() or DEFAULT_YEAR_ROOT
    root = Path(os.path.expanduser(os.path.expandvars(value)))
    root.mkdir(parents=True, exist_ok=True)
    return root


def master_folder(config_file: Path = CONFIG_FILE) -> Path:
    """Where the downloader puts this year's Moodle courses: the master folder in 4th year."""

    return year_root(config_file) / MASTER_FOLDER
