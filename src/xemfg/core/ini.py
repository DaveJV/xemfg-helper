"""OptiScaler.ini editing in place: section-scoped, CRLF and comments preserved.

Files are read and written with newline='' so line endings are never translated.
Existing lines keep their 'Key = value' / 'Key=value' style.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
KEYLINE = re.compile(r"^(\s*)([^=;#\[\s][^=]*?)(\s*=\s*)(.*?)(\s*)$")

USER_PREFERENCES = {
    ("xefg", "dynamicmfg"),
    ("xefg", "frameratetargetdmfg"),
    ("fsr", "upscalerindex"),
}


def read_text(path: Path) -> str:
    with open(path, newline="", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    from .fsutil import atomic_write_bytes

    atomic_write_bytes(path, text.encode("utf-8", errors="surrogateescape"))


def values_equal(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    a, b = a.strip(), b.strip()
    if a.lower() == b.lower():
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


@dataclass
class Change:
    section: str
    key: str
    old: str | None
    new: str


class Ini:
    def __init__(self, text: str):
        self.lines = text.splitlines(keepends=True)
        self.eol = "\r\n" if "\r\n" in text else "\n"

    def text(self) -> str:
        return "".join(self.lines)

    def _find(self, section: str, key: str) -> tuple[int | None, int | None, int | None]:
        """(index of key line, index of section header, index of the section's last line)."""
        sec = None
        hdr = last = None
        hit = None
        for i, line in enumerate(self.lines):
            m = SECTION.match(line.rstrip("\r\n"))
            if m:
                sec = m.group(1).strip()
                if sec.lower() == section.lower() and hdr is None:
                    hdr = i
                continue
            if sec is not None and sec.lower() == section.lower():
                last = i
                if hit is None:
                    km = KEYLINE.match(line.rstrip("\r\n"))
                    if km and km.group(2).strip().lower() == key.lower():
                        hit = i
        return hit, hdr, last

    def get(self, section: str, key: str) -> str | None:
        i, _, _ = self._find(section, key)
        if i is None:
            return None
        return KEYLINE.match(self.lines[i].rstrip("\r\n")).group(4)

    def set(self, section: str, key: str, value: str) -> Change | None:
        i, hdr, last = self._find(section, key)
        if i is not None:
            line = self.lines[i]
            body = line.rstrip("\r\n")
            eol = line[len(body):] or self.eol
            m = KEYLINE.match(body)
            old = m.group(4)
            if values_equal(old, value):
                return None
            self.lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{value}{m.group(5)}{eol}"
            return Change(section, key, old, value)
        # key missing: add it at the end of its section (after the last non-blank line)
        new_line = f"{key}={value}{self.eol}"
        if hdr is None:
            if self.lines and not self.lines[-1].endswith(("\n", "\r")):
                self.lines[-1] += self.eol
            self.lines += [self.eol, f"[{section}]{self.eol}", new_line]
        else:
            pos = hdr + 1
            if last is not None:
                pos = last + 1
                while pos - 1 > hdr and not self.lines[pos - 1].strip():
                    pos -= 1
            self.lines.insert(pos, new_line)
        return Change(section, key, None, value)


def plan_edit(text: str, wanted: dict[str, dict[str, str]]) -> tuple[str, list[Change]]:
    ini = Ini(text)
    changes = []
    for sec, kv in wanted.items():
        for k, v in kv.items():
            c = ini.set(sec, k, v)
            if c:
                changes.append(c)
    return ini.text(), changes


def read_values(text: str, keys: dict[str, list[str]]) -> dict[str, dict[str, str | None]]:
    ini = Ini(text)
    return {s: {k: ini.get(s, k) for k in ks} for s, ks in keys.items()}


def unified_diff(old: str, new: str, name: str) -> str:
    a = [line.rstrip("\r\n") + "\n" for line in old.splitlines(keepends=True)]
    b = [line.rstrip("\r\n") + "\n" for line in new.splitlines(keepends=True)]
    return "".join(difflib.unified_diff(a, b, f"a/{name}", f"b/{name}", n=1))
