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
    fsr4: str = "exact-l"
    gpu_family: str = "rx7000"
    # chosen exe per game key when exe detection was a tie
    exe_overrides: dict[str, str] = field(default_factory=dict)
    # use the OptiScaler wiki compatibility list (network, cached)
    wiki_lookup: bool = True

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, tomli_w.dumps(asdict(self)).encode())
