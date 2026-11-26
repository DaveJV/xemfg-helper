"""Faugus Launcher discovery (games.json entries + games installed inside its prefixes)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..models import Game, Source
from .exefinder import find_exe, resolve_launcher_exe

# drive_c folders that never hold games
SYSTEM_DIRS = re.compile(r"^(windows|programdata|users|perflogs|\$recycle\.bin|recovery|"
                         r"system volume information|openxr|vulkan)$", re.I)
PROGRAM_FILES = re.compile(r"^program files( \(x86\))?$", re.I)
SKIP_PF_CHILDREN = re.compile(
    r"^(common files|windows .*|windows|microsoft.*|internet explorer|windowsapps|"
    r"reference assemblies|msbuild|dotnet|uninstall information|modifiablewindowsapps|"
    r"battle\.net|epic games|ea app|electronic arts|origin|ubisoft|gog galaxy|steam|"
    r"rockstar games launcher|amazon games|riot client|nvidia corporation|amd|intel|"
    r"wine|mono|openal|directx|vulkanrt|windows nt|windows defender|windows mail|"
    r"windows media player|windows photo viewer|windows sidebar|windowspowershell|"
    r"anticheatexpert|easyanticheat.*|battleye|tencent|wellbia|xigncode.*|nprotect)$",
    re.I,
)
# Store/launcher subfolders that hold games one level deeper
STORE_GAME_DIRS = [
    ("epic games",), ("ea games",), ("gog galaxy", "games"), ("ubisoft", "ubisoft game launcher", "games"),
    ("rockstar games",), ("electronic arts",), ("amazon games", "library"),
]


@dataclass
class FaugusInstall:
    games_json: Path
    config_json: Path | None

    def load_entries(self) -> list[dict]:
        try:
            data = json.loads(self.games_json.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def config(self) -> dict:
        if not self.config_json or not self.config_json.exists():
            return {}
        try:
            return json.loads(self.config_json.read_text())
        except (OSError, json.JSONDecodeError):
            return {}


def find_faugus(data_dirs: list[Path], config_dirs: list[Path]) -> FaugusInstall | None:
    for i, d in enumerate(data_dirs):
        gj = d / "games.json"
        if gj.exists():
            cj = None
            cands = config_dirs[i:i + 1] + config_dirs
            for c in cands:
                if (c / "config.json").exists():
                    cj = c / "config.json"
                    break
            return FaugusInstall(gj, cj)
    return None


def resolve_runner(entry_runner: str, cfg: dict) -> str:
    if not entry_runner or entry_runner == "Default":
        return cfg.get("default-runner") or "Default"
    return entry_runner


def discover(fi: FaugusInstall, exe_overrides: dict[str, str] | None = None, walk_prefixes: bool = True) -> list[Game]:
    exe_overrides = exe_overrides or {}
    cfg = fi.config()
    games: list[Game] = []
    covered: set[Path] = set()
    prefixes: dict[Path, dict] = {}
    for e in fi.load_entries():
        gameid, path = e.get("gameid"), e.get("path")
        if not gameid or not path:
            continue
        given = Path(path)
        key = f"faugus:{gameid}"
        g = Game(
            key=key, title=e.get("title") or gameid, source=Source.FAUGUS,
            install_dir=given.parent, gameid=gameid,
            prefix=Path(e["prefix"]) if e.get("prefix") else None,
            runner=resolve_runner(e.get("runner", ""), cfg),
            launch_options=e.get("launch_arguments", "") or "",
        )
        ov = exe_overrides.get(key)
        if ov and Path(ov).exists():
            g.exe, g.exe_reason = Path(ov), "chosen earlier"
        else:
            r = resolve_launcher_exe(given)
            g.exe, g.exe_candidates, g.exe_reason = r.exe, r.candidates, r.reason
        if g.exe:
            # install dir = the folder the entry points into, widened to include the real exe
            g.install_dir = Path(os.path.commonpath([given.parent, g.exe.parent]))
        games.append(g)
        covered.add(given.parent.resolve() if given.parent.exists() else given.parent)
        if g.prefix:
            prefixes.setdefault(g.prefix, e)

    if walk_prefixes:
        for prefix, owner in prefixes.items():
            for root in _prefix_game_roots(prefix):
                rr = root.resolve()
                if any(rr == c or rr in c.parents or c in rr.parents for c in covered):
                    continue
                key = f"faugus-prefix:{owner['gameid']}:{root.relative_to(prefix).as_posix()}"
                ov = exe_overrides.get(key)
                if ov and Path(ov).exists():
                    exe, cands, reason = Path(ov), [], "chosen earlier"
                else:
                    r = find_exe(root)
                    exe, cands, reason = r.exe, r.candidates, r.reason
                if not exe and not cands:
                    continue
                games.append(Game(
                    key=key, title=root.name, source=Source.FAUGUS_PREFIX, install_dir=root,
                    exe=exe, exe_candidates=cands, exe_reason=f"{reason} (inside {owner.get('title')} prefix)",
                    gameid=owner["gameid"], prefix=prefix,
                    runner=resolve_runner(owner.get("runner", ""), cfg),
                    launch_options=owner.get("launch_arguments", "") or "",
                ))
    return games


def _children(d: Path) -> list[Path]:
    try:
        return [c for c in d.iterdir() if c.is_dir() and not c.is_symlink()]
    except OSError:
        return []


def _prefix_game_roots(prefix: Path) -> list[Path]:
    """Candidate game folders inside a Wine prefix's drive_c."""
    drive_c = prefix / "drive_c"
    if not drive_c.is_dir():
        drive_c = prefix / "pfx/drive_c"
    if not drive_c.is_dir():
        return []
    roots: list[Path] = []
    for top in _children(drive_c):
        name = top.name
        if SYSTEM_DIRS.match(name):
            continue
        if PROGRAM_FILES.match(name):
            for c in _children(top):
                low = c.name.lower()
                store = [s for s in STORE_GAME_DIRS if s[0] == low]
                if store:
                    for s in store:
                        rel = _match_case(top, s)
                        if rel is not None:
                            roots += _children(top / rel)
                    continue
                if not SKIP_PF_CHILDREN.match(c.name):
                    roots.append(c)
        else:
            # e.g. drive_c/Games/<game> or drive_c/<game>
            kids = _children(top)
            if name.lower() in ("games", "game") and kids:
                roots += kids
            else:
                roots.append(top)
    return roots


def _match_case(base: Path, parts: tuple[str, ...]) -> Path | None:
    cur = base
    for p in parts:
        hit = next((c for c in _children(cur) if c.name.lower() == p), None)
        if hit is None:
            return None
        cur = hit
    return cur.relative_to(base)
