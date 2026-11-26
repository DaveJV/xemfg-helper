"""Global registry of every install xemfg made: ~/.local/share/xemfg/registry.json.

Entries are only dropped by `xemfg prune`; uninstall marks them removed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .fsutil import read_json, write_json
from .state import now


@dataclass
class Entry:
    key: str
    title: str
    source: str
    exe: str
    exe_dir: str
    exe_sha256: str | None
    set: str
    options: dict
    files: dict[str, str]  # rel path -> sha256 installed
    appid: str | None = None
    gameid: str | None = None
    runner: str | None = None
    launch_target: str | None = None
    status: str = "installed"  # installed | removed
    previous_sets: list[str] = field(default_factory=list)
    installed_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    removed_at: str | None = None
    adopted: bool = False

    @property
    def exe_dir_path(self) -> Path:
        return Path(self.exe_dir)


@dataclass
class Registry:
    path: Path
    entries: dict[str, Entry] = field(default_factory=dict)
    steam_queue: dict[str, dict] = field(default_factory=dict)  # appid -> {options, key, title, queued_at}

    @classmethod
    def load(cls, path: Path) -> "Registry":
        d = read_json(path, {})
        entries = {k: Entry(**v) for k, v in d.get("installs", {}).items()}
        return cls(path, entries, d.get("steam_queue", {}))

    def save(self) -> None:
        write_json(self.path, {"schema": 1, "installs": {k: asdict(v) for k, v in self.entries.items()},
                               "steam_queue": self.steam_queue})

    def active(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.status == "installed"]

    def get(self, key: str) -> Entry | None:
        return self.entries.get(key)

    def sets_in_use(self) -> set[str]:
        out = set()
        for e in self.active():
            out.add(e.set)
            out.update(e.previous_sets[-1:])  # the set rollback would return to
        return out
