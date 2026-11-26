"""Find the real game exe inside an install folder."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Executables that are never the game itself.
NON_GAME_EXE = re.compile(
    r"(launcher|crash(handler|report|pad|sender|dump|_?reporter)|unins|setup|install|redist|vc_?redist|dxsetup|dxwebsetup|dotnet|"
    r"easyanticheat|eac_|beservice|battleye|start_protected_game|update|patcher|helper|"
    r"report|cefprocess|qtwebengine|webhelper|uploader|bugsplat|sentry|ue4prereq|ueprereq|"
    r"prereq|vcruntime|directx|physx|touchup|cleanup|config(urator)?|settings|benchmark_tool|"
    r"unitycrashhandler|crashpad|errorreporter|7z|python|node|java|ace-|service|winedbg|browser|tbs_)",
    re.I,
)
SKIP_DIRS = re.compile(
    r"^(_commonredist|commonredist|redist|redistributables?|__installer|_installer|installers?|"
    r"directx|dotnet|vcredist|support|easyanticheat|battleye|prereq(uisite)?s?|thirdparty|"
    r"\.xemfg|optiscaler|_backup.*|_original.*|_crack)$",
    re.I,
)
LIBRARY_DIRS = {"common", "games", "steamapps", "program files", "program files (x86)", "drive_c"}
TIE_RATIO = 0.10  # two candidates within 10% size and in different folders = tie
MAX_DEPTH = 6


@dataclass
class ExeResult:
    exe: Path | None
    reason: str
    candidates: list[Path] = field(default_factory=list)

    @property
    def tie(self) -> bool:
        return self.exe is None and len(self.candidates) > 1


def _walk(root: Path, max_depth: int = MAX_DEPTH):
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        depth = len(d.parts) - root_depth
        dirnames[:] = [x for x in dirnames if not SKIP_DIRS.match(x)]
        if depth >= max_depth:
            dirnames[:] = []
        yield d, filenames


def is_non_game_exe(name: str) -> bool:
    stem = name.rsplit(".", 1)[0]
    return bool(NON_GAME_EXE.search(stem))


def find_exe(root: Path) -> ExeResult:
    if not root.is_dir():
        return ExeResult(None, f"folder missing: {root}")
    shipping: list[Path] = []
    unity: list[Path] = []
    exes: list[tuple[int, Path]] = []
    for d, files in _walk(root):
        lower = {f.lower(): f for f in files}
        in_win64 = d.name.lower() == "win64" and d.parent.name.lower() == "binaries"
        for low, f in lower.items():
            if not low.endswith(".exe"):
                continue
            p = d / f
            if in_win64 and low.endswith("-shipping.exe"):
                # Engine/Binaries/Win64 holds tools, not the game
                if "engine" not in [x.lower() for x in p.relative_to(root).parts[:-3]]:
                    shipping.append(p)
                continue
            if is_non_game_exe(f):
                continue
            try:
                exes.append((p.stat().st_size, p))
            except OSError:
                continue
        if "unityplayer.dll" in lower:
            unity += [d / f for low, f in lower.items() if low.endswith(".exe") and not is_non_game_exe(f)]

    if shipping:
        if len(shipping) == 1:
            return ExeResult(shipping[0], "Unreal *-Shipping.exe")
        shipping.sort(key=lambda p: -p.stat().st_size)
        return ExeResult(None, "several Unreal *-Shipping.exe", shipping)
    if unity:
        if len(unity) == 1:
            return ExeResult(unity[0], "Unity (UnityPlayer.dll)")
        unity.sort(key=lambda p: -p.stat().st_size)
        return ExeResult(None, "several Unity exes", unity)
    if not exes:
        return ExeResult(None, "no game .exe found")
    exes.sort(key=lambda t: -t[0])
    top_size, top = exes[0]
    ties = [p for s, p in exes[1:] if s >= top_size * (1 - TIE_RATIO) and p.parent != top.parent]
    if ties:
        return ExeResult(None, "several exes of similar size", [top, *ties])
    return ExeResult(top, "largest exe")


def resolve_launcher_exe(given: Path) -> ExeResult:
    """For an exe a launcher entry points at, find the real game exe.

    Unreal stubs (Game.exe next to Game/Binaries/Win64/Game-Shipping.exe) resolve to the
    shipping exe; an obvious launcher resolves by searching one folder up; otherwise the
    given exe is used as-is.
    """
    folder = given.parent
    if folder.is_dir():
        for d, files in _walk(folder, 4):
            if d.name.lower() == "win64" and d.parent.name.lower() == "binaries":
                ship = [d / f for f in files if f.lower().endswith("-shipping.exe")]
                rel = [x.lower() for x in d.relative_to(folder).parts]
                if ship and "engine" not in rel:
                    return ExeResult(ship[0], "Unreal *-Shipping.exe next to launcher entry")
    if is_non_game_exe(given.name):
        # the launcher's own folder first (mod launchers sit next to the game), then one level
        # up (NIKKE/Launcher/ -> NIKKE/), never into a whole library folder
        cands = (folder.parent,) if is_non_game_exe(folder.name) else (folder, folder.parent)
        for cand in cands:
            if cand.name.lower() in LIBRARY_DIRS or cand == Path.home() or cand == cand.parent:
                break
            r = find_exe(cand)
            if r.exe or r.tie:
                r.reason = f"entry points at a launcher; {r.reason} in {cand}"
                return r
    if given.exists():
        return ExeResult(given, "exe from launcher entry")
    return ExeResult(None, f"exe from launcher entry is missing: {given}")
