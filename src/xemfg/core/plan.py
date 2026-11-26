"""Change plans: every write is described first, confirmed, then executed transactionally.

A Plan lists file operations, ini diffs and launch option changes. `render()` shows exactly
what changes (this is also the --dry-run output). `execute()` saves every file it is about to
overwrite or delete into a journal and restores them all if any step fails.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class PlanError(RuntimeError):
    pass


@dataclass
class FileOp:
    kind: str  # copy | delete | write | mkdir | rmdir
    dest: Path
    src: Path | None = None
    data: bytes | None = None
    note: str = ""


@dataclass
class TextChange:
    label: str  # e.g. "OptiScaler.ini" or "Faugus launch options"
    diff: str


@dataclass
class Plan:
    title: str
    ops: list[FileOp] = field(default_factory=list)
    texts: list[TextChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    after: list[Callable[[], None]] = field(default_factory=list)  # state/registry/launcher writes
    base: Path | None = None  # for shorter paths in render

    def copy(self, src: Path, dest: Path, note: str = "") -> None:
        self.ops.append(FileOp("copy", dest, src=src, note=note))

    def write(self, dest: Path, data: bytes, note: str = "") -> None:
        self.ops.append(FileOp("write", dest, data=data, note=note))

    def delete(self, dest: Path, note: str = "") -> None:
        self.ops.append(FileOp("delete", dest, note=note))

    def rmdir(self, dest: Path, note: str = "") -> None:
        self.ops.append(FileOp("rmdir", dest, note=note))

    @property
    def empty(self) -> bool:
        return not self.ops and not self.texts and not self.after

    @property
    def destructive(self) -> bool:
        return any(o.kind in ("delete", "rmdir") or (o.kind in ("copy", "write") and o.dest.exists())
                   for o in self.ops) or bool(self.texts)

    def _rel(self, p: Path) -> str:
        if self.base:
            try:
                return str(p.relative_to(self.base))
            except ValueError:
                pass
        return str(p)

    def render(self, verbose: bool = False) -> str:
        out = [self.title]
        if self.base:
            out.append(f"  in {self.base}")
        groups: dict[str, list[str]] = {}
        sym = {"copy": "+", "write": "+", "delete": "-", "rmdir": "-", "mkdir": "+"}
        for o in self.ops:
            verb = o.kind
            if o.kind in ("copy", "write"):
                verb = "replace" if o.dest.exists() else "add"
            line = f"  {sym.get(o.kind, '*') if verb != 'replace' else '~'} {verb:8} {self._rel(o.dest)}"
            if o.note:
                line += f"  ({o.note})"
            groups.setdefault(o.dest.parent.name if self._is_bulk(o) and not verbose else "", []).append(line)
        for g, lines in groups.items():
            if g and len(lines) > 6:
                out += lines[:3] + [f"    ... and {len(lines) - 3} more files in {g}/"]
            else:
                out += lines
        for t in self.texts:
            out.append(f"  ~ {t.label}:")
            out += ["    " + line for line in t.diff.rstrip("\n").splitlines()]
        for n in self.notes:
            out.append(f"  * {n}")
        for w in self.warnings:
            out.append(f"  ! {w}")
        if self.empty:
            out.append("  (nothing to change)")
        return "\n".join(out)

    def _is_bulk(self, o: FileOp) -> bool:
        return "OptiScaler" in o.dest.parts and o.dest.parent.name != "plugins"

    def execute(self, journal_root: Path | None = None) -> None:
        jroot = Path(tempfile.mkdtemp(prefix="xemfg-txn-", dir=journal_root if journal_root and journal_root.exists() else None))
        undo: list[Callable[[], None]] = []
        try:
            for i, o in enumerate(self.ops):
                self._do(o, jroot / str(i), undo)
            for fn in self.after:
                fn()
        except BaseException:
            for fn in reversed(undo):
                try:
                    fn()
                except Exception:
                    pass
            raise
        finally:
            shutil.rmtree(jroot, ignore_errors=True)

    @staticmethod
    def _save(dest: Path, jfile: Path, undo: list) -> None:
        if dest.is_file():
            jfile.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, jfile)
            undo.append(lambda: (dest.parent.mkdir(parents=True, exist_ok=True), shutil.copy2(jfile, dest)))
        else:
            undo.append(lambda: dest.unlink(missing_ok=True))

    def _do(self, o: FileOp, jfile: Path, undo: list) -> None:
        if o.kind in ("copy", "write"):
            created = []
            p = o.dest.parent
            while not p.exists():
                created.append(p)
                p = p.parent
            o.dest.parent.mkdir(parents=True, exist_ok=True)
            for d in created:
                undo.append(lambda d=d: d.rmdir() if d.exists() and not any(d.iterdir()) else None)
            self._save(o.dest, jfile, undo)
            if o.kind == "copy":
                shutil.copy2(o.src, o.dest)
            else:
                o.dest.write_bytes(o.data)
        elif o.kind == "delete":
            if o.dest.is_file() or o.dest.is_symlink():
                self._save(o.dest, jfile, undo)
                o.dest.unlink()
        elif o.kind == "rmdir":
            if o.dest.is_dir() and not any(o.dest.iterdir()):
                o.dest.rmdir()
                undo.append(lambda d=o.dest: d.mkdir(parents=True, exist_ok=True))
        else:
            raise PlanError(f"unknown op {o.kind}")
