"""User configuration (~/.config/xemfg/config.toml)."""
from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

from .fsutil import atomic_write_bytes


@dataclass
class Config:
    roots: list[str] = field(default_factory=list)
    dmfg_target: int = 171
    default_proxy: str = "dxgi.dll"
    fsr4: str = "official"  # FSR4 DLL for new installs (see core/fsr4choice.py); the newest official one
    gpu_family: str = "rx7000"  # cyboman archive before GPU detection; now taken from the detected GPU
    gpu: str = ""  # PCI address of the GPU games run on, when there are several (`xemfg gpu --pick`)
    gpu_override: str = ""  # GPU profile to use instead of the detected one (`xemfg gpu --override`)
    # chosen exe per game key when exe detection was a tie
    exe_overrides: dict[str, str] = field(default_factory=dict)
    schema: int = 2

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        if "schema" not in data and data.get("fsr4") == "exact-l":
            # xemfg 0.1 wrote its default (cyboman exact-l) into every saved config; it was never a choice
            data["fsr4"] = "official"
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, tomli_w.dumps(asdict(self)).encode())
