"""Web dashboard for the 4th year master folder and the network share.

Run with `python -m network_sync.ui` from the repo root, or run this file
directly (e.g. VS Code's Run button).  A small server starts on this PC only
and the dashboard opens in your browser.  Closing the tab stops the server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from configparser import ConfigParser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    # Run as a plain script, so the repo root is not on the path yet.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moodle_downloader import master_guard, webpages  # noqa: E402
from moodle_downloader.client import MoodleClient  # noqa: E402
from moodle_downloader.config import CONFIG_FILE, SECTION, Settings  # noqa: E402
from moodle_downloader.deadlines import fetch_deadlines  # noqa: E402
from moodle_downloader.downloader import download_all  # noqa: E402
from network_sync import conflicts, state, year_sync  # noqa: E402
from network_sync import sync as ns  # noqa: E402

HOST = "127.0.0.1"
PORTS = range(8765, 8785)
STATIC = Path(__file__).resolve().parent / "static"
IDLE_SHUTDOWN_SECONDS = 90  # the page checks in every 20s, so this means "tab closed"
LOG_LIMIT = 300
RUN_LIMIT = 500  # files listed in the "this run" panel
BULK_WARNING = 25  # restoring more files than this is worth a second look
DEADLINES_MAX_AGE = 15 * 60  # re-ask Moodle at most every 15 minutes


class Dashboard:
    """Everything the page can see or ask for, shared across request threads."""

    def __init__(self) -> None:
        self.destinations = ns.get_destinations()
        self.webpage_sites = webpages.get_sites()
        self.share_path = self.destinations[0].path if self.destinations else ""
        self.master_folder = ns.get_master_folder()
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.status: ns.RemoteStatus | None = None
        self.plans: dict[str, dict] = {}  # destination name -> {"status", "entries"}
        self.entries: list[ns.FileEntry] = []
        self.scanned_at: datetime | None = None
        self.log: list[dict] = []
        self.job: dict = {"running": False}
        self.last_seen = time.monotonic()
        self.in_flight = 0  # requests being handled; never shut down under one
        self.run: list[dict] = []  # files moved in this dashboard session
        self.deadlines_lock = threading.Lock()
        self.deadlines: dict = {"items": [], "fetchedAt": None, "error": None, "loading": False}
        self.deadlines_fetched = 0.0

    def touch(self, delta: int) -> None:
        with self.lock:
            self.in_flight += delta
            self.last_seen = time.monotonic()

    # ---------------------------------------------------------------- logging
    def add_log(self, message: str, level: str = "info") -> None:
        with self.lock:
            self.log.append({"time": f"{datetime.now():%H:%M:%S}", "text": message, "level": level})
            del self.log[:-LOG_LIMIT]

    def add_run_file(self, direction: str, path: str, size: int, where: str = "") -> None:
        """Record one file as downloaded from Moodle or copied to a destination."""

        with self.lock:
            self.run.append(
                {
                    "direction": direction,
                    "path": path,
                    "name": path.rsplit("/", 1)[-1],
                    "size": size,
                    "where": where,
                    "at": datetime.now().isoformat(),
                }
            )
            del self.run[:-RUN_LIMIT]

    # --------------------------------------------------------------- scanning
    def refresh(self, check_connection: bool = True) -> None:
        """Re-test every destination and re-compare the files (skipped while a sync runs)."""

        if self.job.get("running"):
            return
        with self.refresh_lock:  # one scan at a time; the page and the startup scan can overlap
            # Whatever put it there - by hand, OneDrive, printed slides - the master folder keeps no duplicates.
            master_guard.sweep(self.master_folder, log=lambda message: self.add_log(message, "warn"))
            plans = {}
            for destination in self.destinations:
                old = self.plans.get(destination.name, {})
                status = (
                    ns.check_remote(destination.path)
                    if check_connection or not old.get("status")
                    else old["status"]
                )
                plans[destination.name] = {
                    "status": status,
                    "entries": ns.build_plan(self.master_folder, destination.path, status),
                }
            entries = _merge_entries(plans)

            baseline = state.load_counts()
            for name, plan in plans.items():
                present = sum(1 for e in plan["entries"] if e.remote_size is not None)
                plan["guard"] = state.guard(name, present, baseline) if plan["status"].available else {
                    "suspect": False, "present": present, "baseline": baseline.get(name, {}).get("files"), "lost": 0, "message": "",
                }
                # Only a healthy, reachable copy updates its baseline.
                if plan["status"].available and not plan["guard"]["suspect"]:
                    baseline[name] = {"files": present, "seenAt": datetime.now().isoformat()}
            state.save_counts(baseline)

        with self.lock:
            was = {name: plan["status"].available for name, plan in self.plans.items()}
            self.plans, self.entries, self.scanned_at = plans, entries, datetime.now()
            self.status = plans[self.destinations[0].name]["status"] if self.destinations else None
        for name, plan in plans.items():
            if plan["guard"]["suspect"]:
                self.add_log(plan["guard"]["message"], "error")
            status = plan["status"]
            if was.get(name) != status.available:
                self.add_log(
                    f"{name} {'online' if status.available else 'unavailable'}: {status.message}",
                    "ok" if status.available else "warn",
                )

    # ---------------------------------------------------------------- syncing
    def start_sync(self, dry_run: bool) -> tuple[bool, str]:
        with self.lock:
            if self.job.get("running"):
                return False, "A sync is already running."
            ready = self._ready_destinations()
            if not ready:
                first = self.plans.get(self.destinations[0].name, {}) if self.destinations else {}
                reason = (first.get("guard") or {}).get("message") or (
                    first["status"].message if first.get("status") else "Not checked yet."
                )
                return False, f"Nowhere to sync to - {reason}"
            pending = [
                entry
                for destination in ready
                for entry in self.plans[destination.name]["entries"]
                if entry.state in (ns.NEW, ns.CHANGED)
            ]
            if not pending:
                return False, "Everything is already in both places - nothing to send."
            self.job = {
                "running": True,
                "kind": "sync",
                "dry_run": dry_run,
                "total": len(pending),
                "done": 0,
                "current": "",
                "summary": None,
                "failed": 0,
            }

        threading.Thread(target=self._run_sync, args=(dry_run,), daemon=True).start()
        verb = "Previewing" if dry_run else "Syncing"
        return True, f"{verb} {len(pending)} file(s)..."

    def _run_sync(self, dry_run: bool, finish: bool = True) -> tuple[str, int]:
        """Push the master folder out; with finish=False the job is left running for a next step."""

        def progress(message: str) -> None:
            if message.startswith(("Copying ", "Would copy ")):
                with self.lock:
                    self.job["current"] = message.split(" ", 2)[-1] if dry_run else message[8:]
                    self.job["done"] += 1
            level = "error" if message.startswith("FAILED") else "info"
            self.add_log(message, level)

        parts, failed = [], 0
        for destination in self._ready_destinations():
            plan = self.plans.get(destination.name, {})
            self.add_log(f"{'Previewing' if dry_run else 'Sending to'} {destination.name}...")
            # Work on a copy so a preview never changes what the page shows.
            entries = [ns.FileEntry(**vars(entry)) for entry in plan["entries"]]
            try:
                result = ns.push(
                    master_folder=self.master_folder,
                    share_path=destination.path,
                    dry_run=dry_run,
                    on_progress=progress,
                    entries=entries,
                    status=ns.check_remote(destination.path),
                )
                parts.append(f"{destination.name}: {result.summary()}")
                failed += len(result.failed)
                if not dry_run:
                    sizes = {entry.relative_path: entry.size for entry in entries}
                    for path in result.copied + result.updated:
                        self.add_run_file("up", path, sizes.get(path, 0), destination.name)
            except Exception as error:  # a dropped share must not kill the server
                parts.append(f"{destination.name}: stopped - {error}")
                failed += 1
                self.add_log(f"{destination.name}: sync stopped - {error}", "error")

        summary = " | ".join(parts) or "Nothing to send."
        if not finish:
            return summary, failed
        with self.lock:
            self.job.update(running=False, summary=summary, failed=failed, current="")
        if not dry_run:
            self.refresh(check_connection=True)
        self.add_log(("Preview: " if dry_run else "Done: ") + summary, "error" if failed else "ok")
        return summary, failed

    # -------------------------------------------------------- making identical
    def _ready_destinations(self) -> list:
        """Destinations that are reachable and not suspected of having lost files."""

        ready = []
        for destination in self.destinations:
            plan = self.plans.get(destination.name, {})
            status, guard = plan.get("status"), plan.get("guard") or {}
            if status and status.available and not guard.get("suspect"):
                ready.append(destination)
        return ready

    def accept_count(self, name: str) -> tuple[bool, str]:
        """Tell the guard that a destination really does hold this many files now."""

        plan = self.plans.get(name)
        if not plan:
            return False, "No such destination."
        present = plan.get("guard", {}).get("present", 0)
        baseline = state.load_counts()
        baseline[name] = {"files": present, "seenAt": datetime.now().isoformat()}
        state.save_counts(baseline)
        with self.lock:
            plan["guard"] = {"baseline": present, "present": present, "lost": 0, "suspect": False, "message": ""}
        self.add_log(f"{name}: now treating {present} files as normal.", "warn")
        return True, f"{name} baseline updated to {present} files."

    def identical_plan(self) -> dict:
        """What "make identical" would do, so the page can ask before anything moves."""

        # The rest of 4th year: scanned here, outside the lock, as the share can be slow.
        year = year_sync.plan(year_sync.places(self.destinations)).to_json()
        with self.lock:
            ready = self._ready_destinations()
            fetch: dict[str, str] = {}  # path -> where it would come from
            send: dict[str, list[str]] = {}  # path -> destinations it would go to
            for destination in ready:
                for entry in self.plans[destination.name]["entries"]:
                    if entry.state == ns.REMOTE_ONLY:
                        fetch.setdefault(entry.relative_path, destination.name)
                    elif entry.state in (ns.NEW, ns.CHANGED):
                        send.setdefault(entry.relative_path, []).append(destination.name)

            local_count = len([e for e in self.entries if e.state != ns.REMOTE_ONLY]) or 1
            big = len(fetch) > BULK_WARNING or len(fetch) > local_count / 3
            return {
                "ready": [d.name for d in ready],
                "offline": [d.name for d in self.destinations if d not in ready],
                "fetch": [{"path": path, "from": where} for path, where in sorted(fetch.items())],
                "send": [{"path": path, "to": to} for path, to in sorted(send.items())],
                "bulkWarning": big,
                "running": bool(self.job.get("running")),
                "year": year,
            }

    def start_identical(self) -> tuple[bool, str]:
        """Fetch anything missing here, then send everything everywhere. Never deletes."""

        plan = self.identical_plan()
        with self.lock:
            if self.job.get("running"):
                return False, "Something is already running."
            year = plan["year"]
            if not plan["ready"] and len(year["ready"]) < 2:
                first = self.plans.get(self.destinations[0].name, {}) if self.destinations else {}
                reason = (first.get("guard") or {}).get("message") or (
                    first["status"].message if first.get("status") else "Not checked yet."
                )
                return False, f"Nothing usable right now - {reason}"
            if not plan["fetch"] and not plan["send"] and not year["total"] and not year["removals"]:
                return False, "Everything already matches."
            self.job = {
                "running": True,
                "kind": "identical",
                "dry_run": False,
                "total": len(plan["fetch"]) + sum(len(s["to"]) for s in plan["send"]) + year["total"],
                "done": 0,
                "current": "",
                "summary": None,
                "failed": 0,
            }

        threading.Thread(target=self._run_identical, daemon=True).start()
        return True, (
            f"Fetching {len(plan['fetch'])} and sending {len(plan['send'])} master file(s), "
            f"and copying {len(plan['year']['copies'])} other 4th year file(s)..."
        )

    def _run_identical(self) -> None:
        def progress(message: str) -> None:
            if message.startswith(("Copying ", "Fetching ")):
                with self.lock:
                    self.job["current"] = message.split(" ", 1)[1]
                    self.job["done"] += 1
            self.add_log(message, "error" if message.startswith("FAILED") else "info")

        fetched, failed = 0, 0
        for destination in self._ready_destinations():
            plan = self.plans.get(destination.name, {})
            entries = [ns.FileEntry(**vars(entry)) for entry in plan.get("entries", [])]
            try:
                result = ns.pull(
                    master_folder=self.master_folder,
                    share_path=destination.path,
                    on_progress=progress,
                    entries=entries,
                    status=plan["status"],
                )
                sizes = {entry.relative_path: entry.remote_size or 0 for entry in entries}
                for path in result.copied:
                    self.add_run_file("down", path, sizes.get(path, 0), destination.name)
                fetched += len(result.copied)
                failed += len(result.failed)
            except Exception as error:  # a dropped share must not kill the server
                failed += 1
                self.add_log(f"{destination.name}: fetch stopped - {error}", "error")

        if fetched:
            self.add_log(f"Restored {fetched} file(s) that were missing on this laptop.", "ok")

        # Rescan with the fetched files in place, then push everything out.
        with self.lock:
            self.job["running"] = False
        self.refresh(check_connection=False)
        with self.lock:
            self.job.update(running=True, current="")

        master_summary, master_failed = self._run_sync(dry_run=False, finish=False)
        failed += master_failed

        # Then everything else in 4th year: FYP, CV, lab reports...
        self.add_log("Making the rest of 4th year identical...")
        try:
            result = year_sync.reconcile(year_sync.places(self.destinations), on_progress=progress)
            for path, place, size in result.copied:
                self.add_run_file("up", path, size, place)
            year_summary = result.summary()
            failed += len(result.failed)
        except Exception as error:  # a dropped share must not kill the server
            year_summary = f"stopped - {error}"
            failed += 1
            self.add_log(f"4th year sync stopped - {error}", "error")

        summary = f"{fetched} restored here | {master_summary} | 4th year: {year_summary}"
        with self.lock:
            self.job.update(running=False, summary=summary, failed=failed, current="")
        self.refresh(check_connection=True)
        self.add_log("Done: " + summary, "error" if failed else "ok")

    # ------------------------------------------------------------ downloading
    def start_download(self) -> tuple[bool, str]:
        with self.lock:
            if self.job.get("running"):
                return False, "Something is already running."
            self.job = {
                "running": True,
                "kind": "download",
                "dry_run": False,
                "total": 0,  # Moodle only tells us as it goes
                "done": 0,
                "current": "",
                "summary": None,
                "failed": 0,
            }

        threading.Thread(target=self._run_download, daemon=True).start()
        return True, "Checking Moodle for new files..."

    def _run_download(self) -> None:
        def on_file(path: str, size: int) -> None:
            self.add_run_file("down", path, size)
            with self.lock:
                self.job["done"] += 1
                self.job["current"] = path

        def log(message: str) -> None:
            text = message.strip()
            if text:
                self.add_log(text, "error" if text.startswith("FAILED") else "info")

        try:
            summary = "Moodle download finished."
            download_all(_settings(), log=log, on_file=on_file)
            failed = 0
        except Exception as error:  # no login, Moodle down, disk full...
            summary, failed = f"Download stopped - {error}", 1
            self.add_log(summary, "error")

        with self.lock:
            done = self.job["done"]
            self.job.update(running=False, summary=summary, failed=failed, current="")
        self.add_log(
            f"Done: {done} file(s) downloaded from Moodle.", "error" if failed else "ok"
        )
        self.refresh(check_connection=True)

    # ------------------------------------------------------- webpage slides
    def start_web_pull(self) -> tuple[bool, str]:
        """Print the slides from every course website in config.ini into the master folder."""

        sites = webpages.get_sites()
        if not sites:
            return False, (
                "No course websites set up yet - add a [Webpage <name>] section to "
                "config.ini (see config.example.ini)."
            )
        try:
            # So a site that needs a login has somewhere to put it before it is asked for.
            webpages.ensure_secrets(sites)
        except OSError as error:
            self.add_log(f"Couldn't write {webpages.SECRETS_FILE.name}: {error}", "warn")

        with self.lock:
            if self.job.get("running"):
                return False, "Something is already running."
            self.job = {
                "running": True,
                "kind": "webpull",
                "dry_run": False,
                "total": 0,  # filled in once the slide pages have been listed
                "done": 0,
                "current": "",
                "summary": None,
                "failed": 0,
            }

        threading.Thread(target=self._run_web_pull, args=(sites,), daemon=True).start()
        names = ", ".join(site.name for site in sites)
        return True, f"Looking for new slides on {names}..."

    def _run_web_pull(self, sites: list) -> None:
        def on_file(path: str, size: int) -> None:
            self.add_run_file("down", path, size, "webpage")
            with self.lock:
                self.job["done"] += 1
                self.job["current"] = path

        def log(message: str) -> None:
            text = message.strip()
            if text:
                self.add_log(text, "error" if text.startswith("FAILED") else "info")

        failed = 0
        try:
            prepared = webpages.plan(sites, self.master_folder)
            for note in prepared.warnings:
                self.add_log(note, "warn")
            for problem in prepared.errors:
                self.add_log(f"FAILED {problem}", "error")
            failed += len(prepared.errors)
            with self.lock:
                self.job["total"] = len(prepared.pages)
            if not prepared.pages:
                summary = (
                    f"{len(prepared.skipped)} deck(s) already printed - nothing new."
                    if prepared.skipped and not prepared.errors
                    else "No slides printed."
                )
            else:
                result = webpages.export(prepared.pages, log=log, on_file=on_file)
                result.skipped = len(prepared.skipped)
                summary = result.summary()
                failed += len(result.failed)
        except Exception as error:  # no browser, no login, site down...
            summary, failed = f"Webpage pull stopped - {error}", failed + 1
            self.add_log(summary, "error")

        with self.lock:
            self.job.update(running=False, summary=summary, failed=failed, current="")
        self.add_log("Done: " + summary, "error" if failed else "ok")
        self.refresh(check_connection=True)

    # -------------------------------------------------------------- conflicts
    def conflicts(self) -> dict:
        try:
            return {"items": conflicts.find(), "error": None}
        except OSError as error:
            return {"items": [], "error": f"Couldn't read the 4th year folder - {error}"}

    def compare_conflict(self, path: str) -> dict:
        try:
            return conflicts.compare(path)
        except (ValueError, OSError) as error:
            return {"error": str(error)}

    def resolve_conflict(self, path: str, keep: str) -> tuple[bool, str]:
        """Keep one version everywhere; runs as a job so no sync can start halfway through."""

        with self.lock:
            if self.job.get("running"):
                return False, "Wait for the current job to finish first."
            idle = {"running": False, "kind": "resolve", "dry_run": False, "total": 0, "done": 0,
                    "current": "", "summary": None, "failed": 0}
            self.job = {**idle, "running": True, "current": path}
        try:
            message = conflicts.resolve(path, keep, year_sync.places(self.destinations))
            self.add_log(message, "ok")
            return True, message
        except (conflicts.ResolveError, OSError) as error:
            self.add_log(f"Conflict in {path} not resolved: {error}", "warn")
            return False, str(error)
        finally:
            with self.lock:
                self.job = idle

    # -------------------------------------------------------------- deadlines
    def refresh_deadlines(self, force: bool = False) -> dict:
        """Ask Moodle for upcoming hand-ins, unless the last answer is still fresh."""

        with self.deadlines_lock:  # one Moodle fetch at a time
            fresh = time.monotonic() - self.deadlines_fetched < DEADLINES_MAX_AGE
            if self.deadlines["fetchedAt"] and fresh and not force:
                return self.deadlines_snapshot()
            with self.lock:
                self.deadlines["loading"] = True
            try:
                items, error = fetch_deadlines(_moodle_client()), None
            except Exception as err:  # offline, bad token, Moodle down - show it, don't crash
                items, error = self.deadlines["items"], str(err)
            with self.lock:
                self.deadlines = {
                    "items": items,
                    "fetchedAt": datetime.now().isoformat() if not error else self.deadlines["fetchedAt"],
                    "error": error,
                    "loading": False,
                }
            if not error:
                self.deadlines_fetched = time.monotonic()
            else:
                self.add_log(f"Couldn't load deadlines: {error}", "warn")
        return self.deadlines_snapshot()

    def deadlines_snapshot(self) -> dict:
        with self.lock:
            return {**self.deadlines, "semesterEnd": _semester_end()}

    # ---------------------------------------------------------------- reading
    def snapshot(self) -> dict:
        with self.lock:
            status = self.status
            return {
                "share": self.share_path,
                "master": str(self.master_folder),
                "yearRoot": str(self.master_folder.parent),
                "destinations": [
                    {
                        "name": destination.name,
                        "path": destination.path,
                        "isUnc": destination.is_unc,
                        "guard": self.plans.get(destination.name, {}).get("guard") or {},
                        **_status_json(self.plans.get(destination.name, {}).get("status")),
                    }
                    for destination in self.destinations
                ],
                "scannedAt": self.scanned_at.isoformat() if self.scanned_at else None,
                "webpages": [
                    {"name": site.name, "course": site.course, "index": site.index}
                    for site in self.webpage_sites
                ],
                "status": _status_json(status)["status"],
                "files": [
                    {
                        "path": e.relative_path,
                        "course": e.course,
                        "name": e.name,
                        "size": e.size,
                        "modified": e.modified.isoformat() if e.modified else None,
                        "state": e.state,
                        "duplicateOf": e.duplicate_of,
                    }
                    for e in self.entries
                ],
                "job": dict(self.job),
                "run": list(self.run),
                "log": list(self.log[-100:]),
            }


def _status_json(status: ns.RemoteStatus | None) -> dict:
    return {
        "status": None
        if status is None
        else {
            "available": status.available,
            "hostUp": status.host_up,
            "shareMounted": status.share_mounted,
            "writable": status.writable,
            "latencyMs": status.latency_ms,
            "message": status.message,
            "host": status.host,
            "checkedAt": status.checked_at.isoformat(),
        }
    }


def _merge_entries(plans: dict[str, dict]) -> list[ns.FileEntry]:
    """One row per file, showing the worst state across every destination.

    A file counts as synced only once every destination has it, so "waiting to
    sync" means "missing from at least one of them".
    """

    order = [ns.DUPLICATE, ns.NEW, ns.CHANGED, ns.REMOTE_ONLY, ns.SYNCED]
    merged: dict[str, ns.FileEntry] = {}
    states: dict[str, list[str]] = {}

    for plan in plans.values():
        for entry in plan["entries"]:
            states.setdefault(entry.relative_path, []).append(entry.state)
            kept = merged.get(entry.relative_path)
            if kept is None or (kept.state == ns.REMOTE_ONLY and entry.state != ns.REMOTE_ONLY):
                merged[entry.relative_path] = ns.FileEntry(**vars(entry))

    for path, entry in merged.items():
        seen = states[path]
        if all(state == ns.REMOTE_ONLY for state in seen):
            entry.state = ns.REMOTE_ONLY
            continue
        entry.state = next((state for state in order if state in seen), entry.state)
    return sorted(merged.values(), key=lambda e: e.relative_path)


def _settings() -> Settings:
    """The Moodle login from config.ini - never prompts, as the dashboard has no console."""

    parser = ConfigParser()
    parser.read(CONFIG_FILE, encoding="utf-8")
    if not all(parser.has_option(SECTION, key) for key in ("program", "server", "token")):
        raise RuntimeError("No Moodle login in config.ini - run `python main.py` once to set it up.")
    return Settings(
        program=parser.get(SECTION, "program").strip(),
        server=parser.get(SECTION, "server").strip().rstrip("/"),
        token=parser.get(SECTION, "token").strip(),
    )


def _moodle_client() -> MoodleClient:
    settings = _settings()
    return MoodleClient(settings.server, settings.token, timeout=20)


def _semester_end() -> str | None:
    """[Deadlines] semester_end = YYYY-MM-DD from config.ini, if set and valid."""

    parser = ConfigParser()
    parser.read(CONFIG_FILE, encoding="utf-8")
    value = parser.get("Deadlines", "semester_end", fallback="").strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(hour=23, minute=59).isoformat()
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard
    allowed_hosts: set[str]
    timeout = 30  # drop idle browser connections so they don't count as activity

    def log_message(self, *_args) -> None:  # keep the console quiet
        pass

    def handle_one_request(self) -> None:
        self.dashboard.touch(+1)
        try:
            super().handle_one_request()
        finally:
            self.dashboard.touch(-1)

    # Only this PC's browser, on this exact address, may talk to the server.
    def _trusted(self) -> bool:
        if self.headers.get("Host", "") not in self.allowed_hosts:
            return False
        if self.command == "POST":
            # A custom header forces a CORS preflight, which other sites can't pass.
            return self.headers.get("X-Sync-Dashboard") == "1"
        return True

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if not self._trusted():
            return self._send(403, b"Forbidden", "text/plain")
        url = urlparse(self.path)

        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            query = parse_qs(url.query)
            if "check" in query:
                self.dashboard.refresh(check_connection=True)
            return self._json(self.dashboard.snapshot())
        if url.path == "/api/identical-plan":
            return self._json(self.dashboard.identical_plan())
        if url.path == "/api/deadlines":
            force = "refresh" in parse_qs(url.query)
            return self._json(self.dashboard.refresh_deadlines(force=force))
        if url.path == "/api/conflicts":
            return self._json(self.dashboard.conflicts())
        if url.path == "/api/conflict":
            path = parse_qs(url.query).get("path", [""])[0]
            return self._json(self.dashboard.compare_conflict(path))
        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if not self._trusted():
            return self._send(403, b"Forbidden", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}

        path = urlparse(self.path).path
        if path == "/api/sync":
            ok, message = self.dashboard.start_sync(bool(body.get("dryRun")))
            return self._json({"ok": ok, "message": message}, 200 if ok else 409)
        if path == "/api/accept-count":
            ok, message = self.dashboard.accept_count(str(body.get("name", "")))
            return self._json({"ok": ok, "message": message}, 200 if ok else 404)
        if path == "/api/identical":
            ok, message = self.dashboard.start_identical()
            return self._json({"ok": ok, "message": message}, 200 if ok else 409)
        if path == "/api/web-pull":
            ok, message = self.dashboard.start_web_pull()
            return self._json({"ok": ok, "message": message}, 200 if ok else 409)
        if path == "/api/download":
            ok, message = self.dashboard.start_download()
            return self._json({"ok": ok, "message": message}, 200 if ok else 409)
        if path == "/api/rescan":
            self.dashboard.refresh(check_connection=bool(body.get("check", True)))
            return self._json(self.dashboard.snapshot())
        if path == "/api/open":
            return self._json(self._open(body.get("target")))
        if path == "/api/open-file":
            return self._json(self._open_file(str(body.get("path", ""))))
        if path == "/api/resolve":
            ok, message = self.dashboard.resolve_conflict(str(body.get("path", "")), str(body.get("keep", "")))
            return self._json({"ok": ok, "message": message}, 200 if ok else 409)
        self._send(404, b"Not found", "text/plain")

    def _open_file(self, relative: str) -> dict:
        """Open one version of a conflicting file, so the two can be compared by eye."""

        try:
            target = conflicts.local_file(relative)
        except ValueError as error:
            return {"ok": False, "message": str(error)}
        if not target.is_file():
            return {"ok": False, "message": "That file isn't there any more."}
        if target.suffix.lower() in conflicts.RUNNABLE:  # show it rather than run it
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            os.startfile(target)  # noqa: S606 - opens in its usual app
        return {"ok": True}

    def _open(self, target: str | None) -> dict:
        dashboard = self.dashboard
        if target == "local" and dashboard.master_folder.parent.is_dir():
            os.startfile(dashboard.master_folder.parent)  # noqa: S606 - Windows explorer; C:\4th year
            return {"ok": True}
        if target == "bin":
            conflicts.BIN.mkdir(parents=True, exist_ok=True)
            os.startfile(conflicts.BIN)  # noqa: S606
            return {"ok": True}
        if target == "duplicate-bin":
            master_guard.bin_folder().mkdir(parents=True, exist_ok=True)
            os.startfile(master_guard.bin_folder())  # noqa: S606
            return {"ok": True}
        for destination in dashboard.destinations:
            plan = dashboard.plans.get(destination.name, {})
            if target == destination.name and plan.get("status") and plan["status"].share_mounted:
                subprocess.Popen(["explorer", destination.root])
                return {"ok": True}
        return {"ok": False, "message": "That folder isn't reachable right now."}


class _Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second dashboard bind a port that is already
    # taken, so the browser can end up on the old one.  Fail and try the next port.
    allow_reuse_address = False


def _serve() -> tuple[ThreadingHTTPServer, str]:
    for port in PORTS:
        try:
            server = _Server((HOST, port), Handler)
        except OSError:
            continue
        Handler.allowed_hosts = {f"{HOST}:{port}", f"localhost:{port}"}
        return server, f"http://{HOST}:{port}/"
    raise SystemExit(f"No free port between {PORTS.start} and {PORTS.stop - 1}.")


def _stop_when_idle(server: ThreadingHTTPServer, dashboard: Dashboard) -> None:
    while True:
        time.sleep(10)
        idle = time.monotonic() - dashboard.last_seen
        busy = dashboard.in_flight or dashboard.job.get("running")
        if idle > IDLE_SHUTDOWN_SECONDS and not busy:
            print("Dashboard tab closed - stopping.")
            server.shutdown()
            return


def main() -> None:
    """Start the local server and open the dashboard in the browser."""

    dashboard = Dashboard()
    dashboard.add_log(f"Watching {dashboard.master_folder}")
    Handler.dashboard = dashboard
    server, url = _serve()

    threading.Thread(target=dashboard.refresh, daemon=True).start()
    threading.Thread(target=dashboard.refresh_deadlines, daemon=True).start()
    threading.Thread(target=_stop_when_idle, args=(server, dashboard), daemon=True).start()

    print(f"Sync dashboard running at {url}  (close the tab or press Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
