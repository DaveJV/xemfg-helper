"""Per-game state: <exe dir>/.xemfg/state.json. The source of truth for rollback and uninstall."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .fsutil import read_json, write_json

STATE_DIR = ".xemfg"


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class ManagedFile:
    # added: did not exist before xemfg; replaced: original saved at `backup`;
    # kept: was already there with the right content and is left alone on uninstall
    action: str
    sha256: str  # what xemfg installed (or found, for kept)
    backup: str | None = None  # path relative to the exe dir
    original_sha256: str | None = None
    component: str = ""  # optiscaler | nightly | plugin | fsr4 | fsr4-stock | ini


@dataclass
class IniState:
    path: str = "OptiScaler.ini"
    created: bool = False
    backup: str | None = None
    keys: dict[str, dict[str, str]] = field(default_factory=dict)  # xemfg-owned keys and their values
    original: dict[str, dict[str, str | None]] = field(default_factory=dict)  # values before xemfg


@dataclass
class LaunchState:
    target: str | None = None  # steam | faugus | None
    dll: str = ""
    env: dict[str, str] = field(default_factory=dict)
    had_override: bool = False  # the dll was already overridden before xemfg
    original: str | None = None
    written: str | None = None
    queued: bool = False


@dataclass
class GameState:
    game_key: str
    title: str
    set: str
    options: dict
    files: dict[str, ManagedFile] = field(default_factory=dict)
    ini: IniState = field(default_factory=IniState)
    launch: LaunchState = field(default_factory=LaunchState)
    backend: str = "xemfg"
    installed_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    history: list[dict] = field(default_factory=list)  # [{set, at, action}]
    adopted: bool = False
    schema: int = 1

    @staticmethod
    def path(exe_dir: Path) -> Path:
        return exe_dir / STATE_DIR / "state.json"

    @classmethod
    def load(cls, exe_dir: Path) -> "GameState | None":
        d = read_json(cls.path(exe_dir), None)
        if not d:
            return None
        d["files"] = {k: ManagedFile(**v) for k, v in d.get("files", {}).items()}
        d["ini"] = IniState(**d.get("ini", {}))
        d["launch"] = LaunchState(**d.get("launch", {}))
        return cls(**d)

    def save(self, exe_dir: Path) -> None:
        write_json(self.path(exe_dir), asdict(self))

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def previous_sets(self) -> list[str]:
        out = []
        for h in self.history:
            if h.get("set") and h["set"] != self.set and h["set"] not in out:
                out.append(h["set"])
        return out
