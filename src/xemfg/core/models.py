"""Data types shared by the core, CLI and TUI."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class Source(str, enum.Enum):
    STEAM = "steam"
    FAUGUS = "faugus"
    FAUGUS_PREFIX = "faugus-prefix"  # found by walking a Faugus prefix's drive_c
    ROOT = "root"  # `xemfg config add-root` folder
    PATH = "path"  # raw path given on the command line


class Tier(str, enum.Enum):
    NATIVE_FG = "native-fg"
    UPSCALER = "upscaler"
    DLSSG_ONLY = "dlssg-only"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "native-fg": "Native FG",
            "upscaler": "Upscaler only",
            "dlssg-only": "DLSS FG only",
            "incompatible": "Not compatible",
            "unknown": "Unknown",
        }[self.value]

    @property
    def rank(self) -> int:
        return ["native-fg", "upscaler", "dlssg-only", "unknown", "incompatible"].index(self.value)


@dataclass
class Game:
    key: str  # stable id: steam:<appid> | faugus:<gameid> | faugus-prefix:<gameid>:<rel> | root:<path>
    title: str
    source: Source
    install_dir: Path  # top folder of the game
    exe: Path | None = None  # the real game exe (None if not found / tie)
    exe_candidates: list[Path] = field(default_factory=list)  # set when detection tied
    exe_reason: str = ""
    appid: str | None = None
    gameid: str | None = None  # Faugus entry that launches it (owner of launch options)
    prefix: Path | None = None
    runner: str | None = None
    launch_options: str = ""  # current raw launch options (Faugus: newline separated)
    library: Path | None = None  # Steam library root

    @property
    def exe_dir(self) -> Path | None:
        return self.exe.parent if self.exe else None

    @property
    def launch_target(self) -> str | None:
        """Where launch options are written: 'steam' or 'faugus', None if nowhere."""
        if self.source == Source.STEAM:
            return "steam"
        if self.source in (Source.FAUGUS, Source.FAUGUS_PREFIX) and self.gameid:
            return "faugus"
        return None

    def short(self) -> str:
        ident = self.appid or self.gameid or ""
        return f"{self.title} [{self.source.value}{':' + ident if ident else ''}]"


@dataclass
class ForeignProxy:
    name: str
    kind: str  # reshade | asi-loader | specialk | dxvk | mfgu-standalone | unknown


@dataclass
class CompatReport:
    tier: Tier
    fg_input: str | None  # FSRFG | FSRFG30 | Upscaler | None
    reasons: list[str] = field(default_factory=list)  # why this tier
    warnings: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # extra info (engine, inputs found ...)
    inputs: list[str] = field(default_factory=list)  # dlss, xess, fsr31, fsrfg, fsr30fg, dlssg
    graphics_api: list[str] = field(default_factory=list)
    engine: str | None = None
    anti_cheat: list[str] = field(default_factory=list)
    foreign_proxies: list[ForeignProxy] = field(default_factory=list)
    optiscaler_proxy: str | None = None  # proxy file name holding OptiScaler
    optiscaler_version: str | None = None
    cyboman: str | None = None  # variant name or "unknown cyboman"

    @property
    def installable(self) -> bool:
        return self.tier in (Tier.NATIVE_FG, Tier.UPSCALER)
