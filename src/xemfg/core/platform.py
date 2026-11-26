"""Platform-specific locations and names.

Everything that differs between operating systems lives here, so a Windows
adapter can be added later without touching the rest of the core.
Only Linux is implemented.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Proxy file names OptiScaler can be installed under, in preference order.
# Same list as OptiScaler's own setup scripts.
PROXY_NAMES = (
    "dxgi.dll",
    "winmm.dll",
    "version.dll",
    "dbghelp.dll",
    "d3d12.dll",
    "wininet.dll",
    "winhttp.dll",
)
# Names other mods commonly use; never ours, but relevant for conflict reporting.
OTHER_PROXY_NAMES = ("dinput8.dll", "d3d11.dll", "d3d9.dll", "xinput1_3.dll", "xinput1_4.dll", "dsound.dll")


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else Path.home() / default


@dataclass
class Paths:
    """Where xemfg keeps its own files."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path

    @classmethod
    def default(cls) -> "Paths":
        if sys.platform == "win32":  # pragma: no cover - placeholder for a future port
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "xemfg"
            return cls(base / "config", base / "data", base / "cache")
        return cls(
            _xdg("XDG_CONFIG_HOME", ".config") / "xemfg",
            _xdg("XDG_DATA_HOME", ".local/share") / "xemfg",
            _xdg("XDG_CACHE_HOME", ".cache") / "xemfg",
        )

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def registry_file(self) -> Path:
        return self.data_dir / "registry.json"

    @property
    def manifest_file(self) -> Path:
        return self.data_dir / "manifest.toml"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def library_snapshot(self) -> Path:
        return self.cache_dir / "library.json"

    def set_cache(self, set_id: str) -> Path:
        # Same layout as xemfg-linux.sh (~/.cache/xemfg/v1.7.2), so its downloads are reused
        # after their SHA-256 checks pass.
        return self.cache_dir / set_id


@dataclass
class LauncherLocations:
    """Candidate locations of the launchers' own files."""

    steam_roots: list[Path] = field(default_factory=list)
    faugus_data: list[Path] = field(default_factory=list)  # dirs holding games.json
    faugus_config: list[Path] = field(default_factory=list)  # dirs holding config.json

    @classmethod
    def default(cls) -> "LauncherLocations":
        home = Path.home()
        data = _xdg("XDG_DATA_HOME", ".local/share")
        conf = _xdg("XDG_CONFIG_HOME", ".config")
        flatpak_steam = home / ".var/app/com.valvesoftware.Steam"
        flatpak_faugus = home / ".var/app/io.github.Faugus.faugus-launcher"
        return cls(
            steam_roots=[
                data / "Steam",
                home / ".steam/steam",
                home / ".steam/root",
                flatpak_steam / ".local/share/Steam",
            ],
            faugus_data=[data / "faugus-launcher", flatpak_faugus / "data/faugus-launcher"],
            faugus_config=[conf / "faugus-launcher", flatpak_faugus / "config/faugus-launcher"],
        )


def dll_override_name(proxy: str) -> str:
    """'dxgi.dll' -> 'dxgi' (the form WINEDLLOVERRIDES uses)."""
    return proxy[:-4] if proxy.lower().endswith(".dll") else proxy


def process_running(names: tuple[str, ...]) -> bool:
    """True if a process whose comm/cmdline matches one of `names` is running (Linux /proc)."""
    proc = Path("/proc")
    if not proc.is_dir():  # pragma: no cover
        return False
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            comm = (p / "comm").read_text().strip()
            cmd = (p / "cmdline").read_bytes().split(b"\0")[0].decode(errors="ignore")
        except OSError:
            continue
        base = os.path.basename(cmd)
        if comm in names or base in names:
            return True
    return False
