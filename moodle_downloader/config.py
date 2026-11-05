"""Read the Moodle login from config.ini, asking for it the first time."""

from __future__ import annotations

import os
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config.ini"
# Logins for course websites, kept out of config.ini so it can be shared more freely.
SECRETS_FILE = REPO_ROOT / "secrets.ini"
SECTION = "User"
DEFAULT_YEAR_ROOT = r"C:\4th year"
MASTER_FOLDER = "master"


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
        server = input("Moodle address (e.g. https://moodle.midwest.tus.ie): ").strip().rstrip("/")
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

    return Settings(
        program=parser.get(SECTION, "program").strip(),
        server=parser.get(SECTION, "server").strip().rstrip("/"),
        token=parser.get(SECTION, "token").strip(),
    )


def year_root(config_file: Path = CONFIG_FILE) -> Path:
    """This PC's 4th year folder, from [Folders] local; created if it is missing."""

    parser = ConfigParser()
    parser.read(config_file, encoding="utf-8")
    value = parser.get("Folders", "local", raw=True, fallback="").strip() or DEFAULT_YEAR_ROOT
    root = Path(os.path.expandvars(value))
    root.mkdir(parents=True, exist_ok=True)
    return root


def master_folder(config_file: Path = CONFIG_FILE) -> Path:
    """Where the downloader puts this year's Moodle courses: the master folder in 4th year."""

    return year_root(config_file) / MASTER_FOLDER
