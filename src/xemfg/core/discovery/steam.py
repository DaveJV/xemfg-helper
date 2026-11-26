"""Steam library discovery (libraryfolders.vdf, appmanifest_*.acf, compatdata)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import vdf
from ..models import Game, Source
from .exefinder import find_exe

# Steam's own tools/runtimes; never games.
TOOL_APPIDS = {"228980", "1070560", "1391110", "1628350", "4183110", "1493710", "2180100", "961940",
               "1054830", "1113280", "1245040", "1420170", "1580130", "1887720", "2230260", "2348590",
               "2805730", "3658110"}
TOOL_NAME_PREFIXES = ("proton", "steam linux runtime", "steamworks common", "steamvr")


@dataclass
class SteamInstall:
    root: Path
    libraries: list[Path] = field(default_factory=list)

    @property
    def pid_file(self) -> Path:
        return Path.home() / ".steam/steam.pid"

    def userdata_configs(self) -> list[Path]:
        ud = self.root / "userdata"
        out = []
        if ud.is_dir():
            for u in ud.iterdir():
                f = u / "config/localconfig.vdf"
                if u.name.isdigit() and u.name != "0" and f.exists():
                    out.append(f)
        # most recently used account first
        out.sort(key=lambda p: -p.stat().st_mtime)
        return out


def find_steam(candidates: list[Path]) -> SteamInstall | None:
    seen = set()
    for c in candidates:
        try:
            real = c.resolve()
        except OSError:
            continue
        if real in seen or not (real / "steamapps").is_dir():
            continue
        seen.add(real)
        libs = [real]
        lf = real / "steamapps/libraryfolders.vdf"
        if lf.exists():
            data = vdf.loads(lf.read_text(errors="replace"))
            for _, entry in (vdf.get_ci(data, "libraryfolders") or {}).items():
                if isinstance(entry, dict) and entry.get("path"):
                    p = Path(entry["path"])
                    try:
                        p = p.resolve()
                    except OSError:
                        pass
                    if p not in libs:
                        libs.append(p)
        return SteamInstall(real, libs)
    return None


def read_manifest(acf: Path) -> dict:
    try:
        return vdf.get_ci(vdf.loads(acf.read_text(errors="replace")), "AppState") or {}
    except (OSError, vdf.VdfError):
        return {}


def compat_tools(steam: SteamInstall) -> dict[str, str]:
    """appid -> compat tool name from config.vdf CompatToolMapping ('0' = global default)."""
    f = steam.root / "config/config.vdf"
    if not f.exists():
        return {}
    try:
        data = vdf.loads(f.read_text(errors="replace"))
    except vdf.VdfError:
        return {}
    m = vdf.get_ci(data, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping") or {}
    return {k: v.get("name", "") for k, v in m.items() if isinstance(v, dict)}


def launch_options(steam: SteamInstall) -> dict[str, str]:
    """appid -> LaunchOptions from the most recent user's localconfig.vdf."""
    cfgs = steam.userdata_configs()
    if not cfgs:
        return {}
    try:
        data = vdf.loads(cfgs[0].read_text(errors="replace"))
    except vdf.VdfError:
        return {}
    apps = vdf.get_ci(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
    return {k: v.get("LaunchOptions", "") for k, v in apps.items() if isinstance(v, dict) and "LaunchOptions" in v}


def is_tool(appid: str, name: str) -> bool:
    return appid in TOOL_APPIDS or name.lower().startswith(TOOL_NAME_PREFIXES)


def discover(steam: SteamInstall, exe_overrides: dict[str, str] | None = None) -> list[Game]:
    exe_overrides = exe_overrides or {}
    tools = compat_tools(steam)
    opts = launch_options(steam)
    games: list[Game] = []
    for lib in steam.libraries:
        sa = lib / "steamapps"
        if not sa.is_dir():
            continue
        for acf in sorted(sa.glob("appmanifest_*.acf")):
            m = read_manifest(acf)
            appid, name, idir = m.get("appid"), m.get("name", ""), m.get("installdir")
            if not appid or not idir or is_tool(appid, name):
                continue
            install = sa / "common" / idir
            if not install.is_dir():
                continue
            key = f"steam:{appid}"
            prefix = sa / "compatdata" / appid
            if not prefix.is_dir():
                prefix = steam.root / "steamapps/compatdata" / appid
            g = Game(
                key=key, title=name, source=Source.STEAM, install_dir=install, appid=appid,
                prefix=prefix if prefix.is_dir() else None,
                runner=tools.get(appid) or tools.get("0") or None,
                launch_options=opts.get(appid, ""), library=lib,
            )
            ov = exe_overrides.get(key)
            if ov and Path(ov).exists():
                g.exe, g.exe_reason = Path(ov), "chosen earlier"
            else:
                r = find_exe(install)
                g.exe, g.exe_candidates, g.exe_reason = r.exe, r.candidates, r.reason
            games.append(g)
    return games


def steam_running(steam: SteamInstall | None) -> bool:
    """Steam rewrites localconfig.vdf on exit, so we only write it while Steam is closed."""
    from ..platform import process_running

    if steam is None:
        return False
    pid_file = steam.pid_file
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip() or 0)
            if pid and os.path.exists(f"/proc/{pid}"):
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
                if comm.startswith("steam"):
                    return True
        except (ValueError, OSError):
            pass
    return process_running(("steam", "steamwebhelper", "steam.sh"))
