"""Turn one course's Moodle contents into a readme and a list of files to fetch.

Numbering: every downloadable file gets the next number (01-, 02-, ...) in page
order, which is the naming already used on disk.  A folder activity with five
or more files becomes one numbered sub-folder holding the files by name.
A file whose number changes keeps the name it was first saved under (see file_index.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import quote, urlsplit

from moodle_downloader.naming import numbered_file_name, slugify

BIG_FOLDER = 5

# "Password: x", "<strong>Username</strong>: y" - logins lecturers post in labels.
CREDENTIAL = re.compile(
    r"(?i)\b(password|passwd|pwd|username|user|login)\b"
    r"((?:\s*</?\w+[^>]*>)*\s*:\s*(?:<\w+[^>]*>\s*)*)"
    r"([^\s<,;]+)"
)


@dataclass
class RemoteFile:
    """A file on Moodle and where it should be saved inside the course folder."""

    relative_path: str
    url: str
    size: int
    modified: int | None
    key: str = ""  # Moodle's own id for the file, which survives reordering (see file_index.py)


@dataclass
class CoursePage:
    """The rendered readme plus the files it links to."""

    markdown: str
    files: list[RemoteFile] = field(default_factory=list)


def _link(text: str, target: str, title: str = "") -> str:
    title = (title or text).replace('"', "'")
    return f'[{text}]({target} "{title}")'


def _local(path: str) -> str:
    return quote(path, safe="/")


def redact_credentials(text: str) -> str:
    """Blank out any username or password so the readme is safe to push."""
    return CREDENTIAL.sub(r"\1\2[removed]", text)


def _files_in(contents: list[dict]) -> list[dict]:
    return [item for item in contents if item.get("type") == "file" and item.get("fileurl")]


def build_course_page(
    course: dict,
    contents: list[dict],
    title: str,
    program: str,
    root_readme: str,
    paths: dict[str, str] | None = None,
) -> CoursePage:
    """Walk the course sections in order and number each file as it appears.

    `paths` maps a file's numbered name to the name it is actually saved under,
    when that differs, so the readme links to the file on disk.
    """

    def saved(relative: str) -> str:
        return (paths or {}).get(relative, relative)

    page = CoursePage(markdown="")
    lines = [
        f"# {_link(program, root_readme)}",
        "",
        f"## {title}",
        "",
        f"Course URL: <{course.get('viewurl', '')}>",
        "",
    ]
    number = 1

    for section in contents:
        lines += [f"### {unescape(section.get('name', ''))}", ""]
        if section.get("visible", 1) == 0:
            lines += ["*Not available.*", ""]
            continue
        modules = section.get("modules") or []
        if not modules:
            lines += ["*No content.*", ""]
            continue

        for module in modules:
            name = unescape(module.get("name", ""))
            moodle_url = module.get("url", "")
            kind = module.get("modname", "")
            items = module.get("contents") or []
            files = _files_in(items)

            if kind == "label":
                text = redact_credentials(unescape(module.get("description", "")).strip())
                if text:
                    lines += [text, ""]
                continue

            if kind == "url" or not files:
                target = next(
                    (item.get("fileurl") for item in items if item.get("type") == "url"),
                    moodle_url,
                )
                lines += [f"- {_link(f'`{name}`', target, name)}", ""]
                continue

            original = _link("`moodle`", moodle_url, name)

            if len(items) >= BIG_FOLDER:
                # Windows drops trailing dots and spaces from folder names.
                folder = numbered_file_name(number, name).rstrip(". ")
                number += 1
                relatives = []
                for item in files:
                    sub_path = (item.get("filepath") or "/").strip("/")
                    parts = [folder] + [slugify(p) for p in sub_path.split("/") if p]
                    relative = "/".join(parts + [slugify(item.get("filename", ""))])
                    relatives.append(relative)
                    page.files.append(_remote(relative, item))
                # A folder saved before its number changed stays where it is.
                saved_folders = {saved(relative).split("/", 1)[0] for relative in relatives}
                if len(saved_folders) == 1:
                    folder = saved_folders.pop()
                lines.append(f"- **{_link(name, _local(folder) + '/')}**: original at {original}")
                lines.append("")
                continue

            if len(items) > 1:
                lines.append(f"- **{name}**")
                for item in files:
                    relative = numbered_file_name(number, item.get("filename", ""))
                    number += 1
                    label = item.get("filename", "")
                    lines.append(
                        f"  - **{_link(label, _local(saved(relative)))}**: original at "
                        f"{_link('`moodle`', item.get('fileurl', ''), label)}"
                    )
                    page.files.append(_remote(relative, item))
                lines.append("")
                continue

            item = files[0]
            relative = numbered_file_name(number, item.get("filename", ""))
            number += 1
            lines += [f"- **{_link(name, _local(saved(relative)))}**: original at {original}", ""]
            page.files.append(_remote(relative, item))

    lines += ["---", ""]
    page.markdown = "\n".join(lines)
    return page


def moodle_key(item: dict) -> str:
    """'1609567/mod_resource/content/notes.pdf' for .../pluginfile.php/1609567/mod_resource/content/3/notes.pdf.

    The first number belongs to the activity, so it stays the same when the page
    is reordered.  The revision number after the file area changes on every
    upload, so it is left out and an updated file keeps its key.
    """

    after = urlsplit(item.get("fileurl", "")).path.partition("/pluginfile.php/")[2]
    context, component, area = (after.split("/") + ["", "", ""])[:3]
    file_path = item.get("filepath") or "/"
    return f"{context}/{component}/{area}{file_path}{item.get('filename', '')}"


def _remote(relative_path: str, item: dict) -> RemoteFile:
    return RemoteFile(
        relative_path=relative_path,
        url=item.get("fileurl", ""),
        size=int(item.get("filesize") or 0),
        modified=item.get("timemodified") or None,
        key=moodle_key(item),
    )
