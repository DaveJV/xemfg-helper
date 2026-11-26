"""Frame generation backend interface. Only the xemfg backend is implemented."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..cache import SetCache
from ..models import CompatReport


@dataclass
class Placement:
    src: Path  # verified file in the cache
    rel: str  # destination relative to the exe dir
    component: str  # optiscaler | nightly | plugin | fsr4 | fsr4-stock | ini-template
    sha256: str | None = None


@dataclass
class Options:
    proxy: str
    fg_input: str
    fsr4: str
    gpu_family: str
    dmfg_target: int
    allow_anticheat: bool = False
    gpu_profile: str = ""  # gpu.PROFILES key the FSR4 choice was made for ("" = unknown, xemfg 0.1)

    def to_dict(self) -> dict:
        return {"proxy": self.proxy, "fg_input": self.fg_input, "fsr4": self.fsr4,
                "gpu_family": self.gpu_family, "dmfg_target": self.dmfg_target,
                "allow_anticheat": self.allow_anticheat, "gpu_profile": self.gpu_profile}

    @classmethod
    def from_dict(cls, d: dict) -> "Options":
        return cls(**{k: d[k] for k in ("proxy", "fg_input", "fsr4", "gpu_family", "dmfg_target") if k in d},
                   allow_anticheat=d.get("allow_anticheat", False), gpu_profile=d.get("gpu_profile", ""))


class FrameGenBackend(Protocol):
    name: str

    def placements(self, cache: SetCache, opts: Options, keep_fsr4: bool,
                   slot: dict | None = None) -> list[Placement]: ...
    def owned_ini_keys(self, opts: Options) -> dict[str, dict[str, str]]: ...
    def fresh_install_ini_defaults(self, opts: Options) -> dict[str, dict[str, str]]: ...
    def launch_env(self, opts: Options) -> dict[str, str]: ...
    def default_fg_input(self, report: CompatReport) -> str | None: ...
