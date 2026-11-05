"""Export lecture slides published on a course website to PDF.

Some modules put their slides on the lecturer's own site instead of Moodle, as
reveal.js decks that only exist as web pages.  Each `[Webpage ...]` section in
config.ini says where a module's slides live and which course folder in the
master folder the PDFs belong in, so once they are printed the usual sync sends
them to the server and OneDrive like any other course file.

A site that asks for a login reads it from secrets.ini, which is ignored by git,
so config.ini can be shared without handing over any passwords.

Printing needs a real browser, so this drives Playwright's Chromium:

    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import asyncio
import re
from configparser import ConfigParser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

from moodle_downloader.config import CONFIG_FILE, SECRETS_FILE, master_folder
from moodle_downloader.naming import slugify

Log = Callable[[str], None]
# Called with (path relative to the 4th year folder, bytes) after each PDF is saved.
OnFile = Callable[[str, int], None]

SECTION_PREFIX = "Webpage"  # e.g. [Webpage snet08008]
DEFAULT_SUBFOLDER = "slides"
DEFAULT_PRINT_QUERY = "print-pdf"  # reveal.js' printable view
PAGE_TIMEOUT_MS = 60_000
SETTLE_MS = 2_500  # let the deck finish laying itself out before printing
HTTP_TIMEOUT = 20
CLOCK_SLACK = 2  # seconds of timestamp difference still treated as "same file"

SECRETS_HEADER = """; Logins for the course websites in config.ini - one section per site.
; This file is ignored by git, so the passwords stay on this PC.
"""

INSTALL_HINT = (
    "Printing web slides needs Playwright's Chromium. Run:  "
    "pip install playwright  and then  python -m playwright install chromium"
)

_HREF = re.compile(r"""href\s*=\s*["']([^"'#?]+\.html?)""", re.IGNORECASE)
_INDEX_NAMES = {"index.html", "index.htm"}


class WebpageError(RuntimeError):
    """Something stopped a site from being pulled: no browser, no login, no pages."""


@dataclass
class Site:
    """One course website whose slides are printed into one course folder."""

    name: str
    course: str  # course folder name inside the master folder
    index: str = ""  # page that links to the slides, used to find new ones
    pages: list[str] = field(default_factory=list)  # slide URLs, or names relative to index
    subfolder: str = DEFAULT_SUBFOLDER
    username: str = ""
    password: str = ""
    print_query: str = DEFAULT_PRINT_QUERY

    @property
    def auth(self) -> tuple[str, str] | None:
        return (self.username, self.password) if (self.username or self.password) else None

    def folder(self, master: Path | None = None) -> Path:
        """Where this site's PDFs are saved."""

        root = (master or master_folder()) / self.course
        return root / self.subfolder if self.subfolder else root


@dataclass
class Page:
    """One slide deck: where to read it, and where its PDF goes."""

    site: Site
    url: str
    destination: Path
    relative: str  # as the dashboard shows it, e.g. master/<course>/slides/week01a.pdf
    modified: datetime | None = None


@dataclass
class Plan:
    """What a pull would print, and what it would leave alone."""

    pages: list[Page] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # printed already and still current
    errors: list[str] = field(default_factory=list)  # a site that could not be read at all
    warnings: list[str] = field(default_factory=list)  # e.g. no index, but decks are listed


@dataclass
class Result:
    saved: list[Page] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (relative path, why)
    skipped: int = 0

    def summary(self) -> str:
        parts = [f"{len(self.saved)} printed"]
        if self.skipped:
            parts.append(f"{self.skipped} already current")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        text = ", ".join(parts)
        # When everything failed for the same reason, say it here rather than only in the log.
        reasons = {why for _, why in self.failed}
        return f"{text} - {reasons.pop()}" if len(reasons) == 1 else text


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def get_sites(
    config_file: Path | str = CONFIG_FILE,
    secrets_file: Path | str = SECRETS_FILE,
) -> list[Site]:
    """Every `[Webpage <name>]` section in config.ini, in the order they appear.

    Logins live in secrets.ini instead, in a section of the same name, so config.ini
    holds nothing private.  A login still written in config.ini is used as a fallback.
    """

    parser = ConfigParser()
    parser.read(config_file, encoding="utf-8")
    logins = _read_logins(secrets_file)

    sites = []
    for section in parser.sections():
        head, _, name = section.partition(" ")
        if head.lower() != SECTION_PREFIX.lower():
            continue

        def get(key: str, fallback: str = "", _section: str = section) -> str:
            # raw=True: a password may contain a % sign, which is not interpolation here.
            return parser.get(_section, key, raw=True, fallback=fallback).strip()

        username, password = logins.get(section.lower(), ("", ""))
        sites.append(
            Site(
                name=name.strip() or section,
                course=get("course"),
                index=get("index"),
                pages=[part.strip() for part in re.split(r"[,\n]", get("pages")) if part.strip()],
                subfolder=get("subfolder", DEFAULT_SUBFOLDER),
                username=username or get("username"),
                password=password or get("password"),
                print_query=get("print_query", DEFAULT_PRINT_QUERY),
            )
        )
    return sites


def _read_logins(secrets_file: Path | str = SECRETS_FILE) -> dict[str, tuple[str, str]]:
    """The username and password for each site in secrets.ini, keyed by section name."""

    parser = ConfigParser()
    parser.read(secrets_file, encoding="utf-8")
    return {
        section.lower(): (
            parser.get(section, "username", raw=True, fallback="").strip(),
            parser.get(section, "password", raw=True, fallback="").strip(),
        )
        for section in parser.sections()
    }


def ensure_secrets(
    sites: list[Site] | None = None, secrets_file: Path | str = SECRETS_FILE
) -> Path:
    """Make sure secrets.ini exists with an empty login waiting for every site.

    Nothing already in the file is touched, so filled-in logins survive and only
    a site that has no section yet gets one added.
    """

    path = Path(secrets_file)
    sites = get_sites(secrets_file=path) if sites is None else sites
    existing = _read_logins(path)
    missing = [site for site in sites if f"{SECTION_PREFIX} {site.name}".lower() not in existing]

    if not path.exists():
        path.write_text(SECRETS_HEADER, encoding="utf-8")
    if missing:
        with path.open("a", encoding="utf-8") as file:
            for site in missing:
                file.write(f"\n[{SECTION_PREFIX} {site.name}]\nusername = \npassword = \n")
    return path


def _check_course(site: Site) -> None:
    """Tidy up the course folder name, refusing anything that could escape the master folder."""

    site.course = site.course.strip().strip("/\\")
    if not site.course or Path(site.course).is_absolute() or ".." in Path(site.course).parts:
        raise WebpageError(
            f"no course folder set - add `course = ...` to [{SECTION_PREFIX} {site.name}]."
        )


# --------------------------------------------------------------------------- #
# finding the slides
# --------------------------------------------------------------------------- #
def _get(url: str, site: Site) -> requests.Response:
    response = requests.get(url, auth=site.auth, timeout=HTTP_TIMEOUT)
    if response.status_code in (401, 403):
        raise WebpageError(
            f"{url} wants a login - fill in username and password under "
            f"[{SECTION_PREFIX} {site.name}] in {SECRETS_FILE.name}."
        )
    response.raise_for_status()
    return response


def _index_links(site: Site) -> list[str]:
    """Every slide page the site's index links to, alongside the index itself.

    Only pages in the same folder count, so a link to the module home page or to
    someone else's site is left alone.
    """

    base = site.index
    folder = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/"
    links = []
    for href in _HREF.findall(_get(base, site).text):
        url = urljoin(base, href)
        name = url[len(folder):]
        if url.startswith(folder) and "/" not in name and name.lower() not in _INDEX_NAMES:
            links.append(url)
    return links


def discover(site: Site, on_problem: Log | None = None) -> list[str]:
    """Slide URLs for a site: the configured ones, plus any others its index links to.

    An index that can't be read is only fatal when nothing is listed in config.ini;
    otherwise it is reported through `on_problem` and the listed decks are used.
    """

    base = site.index or ""
    found = [page if urlparse(page).scheme else urljoin(base, page) for page in site.pages]

    if base:
        try:
            found += _index_links(site)
        except (WebpageError, requests.RequestException) as error:
            if not found or on_problem is None:
                raise
            on_problem(f"{site.name}: couldn't read {base} ({error}) - using the listed decks.")

    seen, ordered = set(), []
    for url in found:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def pdf_name(url: str) -> str:
    """'.../week01a.html' -> 'week01a.pdf'."""

    stem = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.html?$", "", stem, flags=re.IGNORECASE)
    return slugify(stem or "slides") + ".pdf"


def _remote_modified(url: str, site: Site) -> datetime | None:
    """When the page last changed, if the server says so; None when it doesn't."""

    try:
        response = requests.head(url, auth=site.auth, timeout=HTTP_TIMEOUT, allow_redirects=True)
        stamp = response.headers.get("Last-Modified", "")
        return parsedate_to_datetime(stamp) if stamp else None
    except (requests.RequestException, ValueError, TypeError):
        return None


def _needs_printing(destination: Path, modified: datetime | None) -> bool:
    """Print when there is no PDF yet, or the page has changed since the PDF was made."""

    try:
        printed = destination.stat().st_mtime
    except OSError:
        return True
    if modified is None:
        return False  # no way to tell it apart; delete the PDF to print it again
    return printed + CLOCK_SLACK < modified.astimezone(timezone.utc).timestamp()


def plan(sites: list[Site] | None = None, master: Path | None = None) -> Plan:
    """Look at every site and work out which decks are missing or out of date."""

    master = master or master_folder()
    prepared = Plan()
    for site in get_sites() if sites is None else sites:
        try:
            _check_course(site)
            urls = discover(site, on_problem=prepared.warnings.append)
        except (WebpageError, requests.RequestException) as error:
            prepared.errors.append(f"{site.name}: {error}")
            continue
        if not urls:
            prepared.errors.append(
                f"{site.name}: no slides found - set `pages` or `index` in config.ini."
            )
            continue

        folder = site.folder(master)
        for url in urls:
            destination = folder / pdf_name(url)
            modified = _remote_modified(url, site)
            page = Page(
                site=site,
                url=url,
                destination=destination,
                relative=destination.relative_to(master.parent).as_posix(),
                modified=modified,
            )
            if _needs_printing(destination, modified):
                prepared.pages.append(page)
            else:
                prepared.skipped.append(page.relative)
    return prepared


# --------------------------------------------------------------------------- #
# printing
# --------------------------------------------------------------------------- #
def export(pages: list[Page], log: Log = print, on_file: OnFile | None = None) -> Result:
    """Print every page to its PDF with Chromium; one bad deck never stops the rest."""

    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise WebpageError(INSTALL_HINT) from error

    return asyncio.run(_export(async_playwright, pages, log, on_file))


async def _export(async_playwright, pages: list[Page], log: Log, on_file: OnFile | None) -> Result:
    result = Result()
    by_site: dict[str, list[Page]] = {}
    for page in pages:
        by_site.setdefault(page.site.name, []).append(page)

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as error:  # installed, but the browser itself is missing
            raise WebpageError(f"Chromium would not start. {INSTALL_HINT} ({error})") from error
        try:
            for name, site_pages in by_site.items():
                site = site_pages[0].site
                credentials = (
                    {"username": site.username, "password": site.password} if site.auth else None
                )
                context = await browser.new_context(http_credentials=credentials)
                try:
                    log(f"{name}: printing {len(site_pages)} deck(s)")
                    for page in site_pages:
                        await _print_one(context, page, log, on_file, result)
                finally:
                    await context.close()
        finally:
            await browser.close()
    return result


def _print_url(page: Page) -> str:
    """The deck's printable view, e.g. `week01a.html?print-pdf` for reveal.js."""

    query = page.site.print_query
    if not query:
        return page.url
    return page.url + ("&" if "?" in page.url else "?") + query


async def _print_one(context, page: Page, log: Log, on_file: OnFile | None, result: Result) -> None:
    tab = await context.new_page()
    try:
        log(f"  printing {page.relative}")
        response = await tab.goto(_print_url(page), wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        if response is not None and response.status in (401, 403):
            raise WebpageError(
                f"the site wants a login - fill in username and password under "
                f"[{SECTION_PREFIX} {page.site.name}] in {SECRETS_FILE.name}"
            )
        if response is not None and response.status >= 400:
            raise WebpageError(f"the site answered {response.status}")
        await tab.wait_for_timeout(SETTLE_MS)
        page.destination.parent.mkdir(parents=True, exist_ok=True)
        await tab.pdf(
            path=str(page.destination),
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        result.saved.append(page)
        if on_file:
            on_file(page.relative, page.destination.stat().st_size)
    except Exception as error:  # one unreachable deck must not stop the others
        log(f"FAILED {page.relative}: {error}")
        result.failed.append((page.relative, str(error)))
    finally:
        await tab.close()


def pull_all(
    sites: list[Site] | None = None,
    master: Path | None = None,
    log: Log = print,
    on_file: OnFile | None = None,
) -> Result:
    """Find every site's slides and print the ones that are new or have changed."""

    master = master or master_folder()
    prepared = plan(sites, master)
    for note in prepared.warnings:
        log(note)
    for problem in prepared.errors:
        log(f"FAILED {problem}")
    result = export(prepared.pages, log=log, on_file=on_file) if prepared.pages else Result()
    result.skipped = len(prepared.skipped)
    result.failed += [("", problem) for problem in prepared.errors]
    log(f"Webpage slides: {result.summary()}")
    return result


if __name__ == "__main__":  # python -m moodle_downloader.webpages
    sites = get_sites()
    if not sites:
        raise SystemExit(
            f"No [{SECTION_PREFIX} <name>] section in config.ini - see config.example.ini."
        )
    print(f"Logins: {ensure_secrets(sites)}")
    pull_all(sites)
