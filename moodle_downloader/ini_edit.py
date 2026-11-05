"""Edit an .ini file as plain text, so the comments and layout in it survive.

ConfigParser can read these files but would throw away every comment when it writes
them back, so settings are changed line by line instead.
"""

from __future__ import annotations

import re
from pathlib import Path

def read_lines(config_file: str) -> list[str]:
    try:
        return Path(config_file).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def write_lines(config_file: str, lines: list[str]) -> None:
    Path(config_file).write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    """(header index, index after the section's last line), or None if it isn't there."""

    header = re.compile(r"^\s*\[(.*?)\]\s*$")
    start = None
    for number, line in enumerate(lines):
        found = header.match(line)
        if found and start is not None:
            return start, number
        if found and found.group(1).strip().lower() == section.lower():
            start = number
    return (start, len(lines)) if start is not None else None


def set_option(lines: list[str], section: str, key: str, value: str) -> list[str]:
    """Set `key = value` in a section, replacing the line or adding it; comments are kept."""

    span = section_span(lines, section)
    if span is None:
        return [*lines, *([""] if lines else []), f"[{section}]", f"{key} = {value}"]
    start, end = span
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*[=:]", re.IGNORECASE)
    for number in range(start + 1, end):
        if pattern.match(lines[number]):
            lines[number] = f"{key} = {value}"
            return lines
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # keep the blank line that separates sections
    lines.insert(insert_at, f"{key} = {value}")
    return lines


def drop_section(lines: list[str], section: str) -> list[str]:
    span = section_span(lines, section)
    if span is None:
        return lines
    start, end = span
    trimmed = lines[:start] + lines[end:]
    while start > 0 and start <= len(trimmed) and not trimmed[start - 1].strip() and (
        start == len(trimmed) or not trimmed[start].strip()
    ):
        del trimmed[start - 1]  # don't leave a gap of blank lines
        start -= 1
    return trimmed
