"""FSR4 upscaler DLLs: what a game folder holds, what each file is, and which one actually runs.

Which DLL runs (OptiScaler source at nightly-20261122, commit ab1f3d49):
- proxies/FfxApi_Proxy.h (InitFfxDx12_SR) asks Util::LoadProxyLibrary for
  amd_fidelityfx_upscaler_dx12.dll: first `FfxDx12SRPath` from the ini, then OptiScaler's dll folder
  (`OptiDllPath`, default `.\\OptiScaler`, or OptiScaler's own folder when that does not exist).
  Only when neither has the file does it fall back to the copy the game loaded itself.
  Vulkan games do the same with amd_fidelityfx_upscaler_vk.dll and `FfxVkSRPath`.
- fsr4/FSR4DriverProvider.cpp (AddDriverToVersionList): the driver's FSR (amdxcffx64.dll, which Proton
  puts into the prefix) is only listed when it is newer than every FSR4 model in that DLL.
So while xemfg's OptiScaler is installed, the game's own upscaler DLL is not what runs.
"""
from __future__ import annotations

import mmap
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ini as ini_mod
from .fsutil import sha256_or_none
from .mods import CYBOMAN as CYBOMAN_RE
from .gpu import FP8, INT8, INT8_LITE, INT8_RDNA2, Profile

UPSCALER = {"dx12": "amd_fidelityfx_upscaler_dx12.dll", "vk": "amd_fidelityfx_upscaler_vk.dll"}
COMBINED = {"amd_fidelityfx_dx12.dll": "dx12", "amd_fidelityfx_vk.dll": "vk"}  # FSR 3.1 era, all effects in one
LOADER = {"amd_fidelityfx_loader_dx12.dll": "dx12", "amd_fidelityfx_loader_vk.dll": "vk"}
OPTI_DIR = "OptiScaler"
PATH_KEYS = {"dx12": "FfxDx12SRPath", "vk": "FfxVkSRPath"}

# FFX provider class names inside the DLL -> model (OptiScaler fsr4/FSR4Models.cpp, modelProviders)
PROVIDERS = {b"ffxProvider_FSR4_Int8_Navi2": INT8_RDNA2, b"ffxProvider_FSR4Lite_Int8": INT8_LITE,
             b"ffxProvider_FSR4_Int8": INT8, b"ffxProvider_FSR4": FP8}
_PROVIDER_RE = re.compile(rb"ffxProvider_FSR4(?:_Int8_Navi2|Lite_Int8|_Int8|_Common)?(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class OfficialDll:
    """An AMD-signed upscaler DLL from GPUOpen-LibrariesAndSDKs/FidelityFX-SDK at a release tag."""

    version: str
    api: str
    sdk_tag: str
    commit: str
    sha256: str
    released: str
    models: tuple[str, ...]

    @property
    def repo_path(self) -> str:
        return f"Kits/FidelityFX/signedbin/{UPSCALER[self.api]}"

    @property
    def url(self) -> str:
        return f"https://raw.githubusercontent.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/{self.commit}/{self.repo_path}"


SDK = "GPUOpen-LibrariesAndSDKs/FidelityFX-SDK"
# Verified 2026-11-26: downloaded at each tag's commit, version resource and providers read from the file.
OFFICIAL: tuple[OfficialDll, ...] = (
    OfficialDll("4.1.2.4555", "dx12", "v2.4.0", "7539ae8b6568ad29ed78a753404351daf3b5cca7",
                "aad4fccccb29084a655f9a4f9f613bd22c8c03fb4a0f8840148cc6543b4ccace", "2026-10-28",
                (FP8, INT8, INT8_RDNA2, INT8_LITE)),
    OfficialDll("4.1.2.4555", "vk", "v2.4.0", "7539ae8b6568ad29ed78a753404351daf3b5cca7",
                "4a7e895af6e0b615d40a84d59eb8c2b1054ad91cdd0c739567940270d51bdab7", "2026-10-28",
                (FP8, INT8, INT8_RDNA2, INT8_LITE)),
    OfficialDll("4.1.1.2740", "dx12", "v2.3.0", "60f4ea81909200d8542eca14dccb2628b763a9a3",
                "d0dcccc74a43c44ba435b7a369b456e0970d8a4464e4bd683119b374f2c9fb46", "2026-06-24", (FP8, INT8)),
    OfficialDll("4.1.0.0", "dx12", "v2.2.0", "e236f2304dcda35f282fdddd085f41e2ff48c86a",
                "ec7ed3ca674e288240e6f04b986342aece47454c41d9b0959449e82e22bd7f6d", "2026-03-23", (FP8,)),
    OfficialDll("4.0.3.604", "dx12", "v2.1.0", "0836aa7f24058b3f8d035a88c0774457d78fc98e",
                "3eab9a448db1ca09d5aba17d11df5b789649c11d60f1281f4be97bd007ac7d6d", "2025-12-10", (FP8,)),
    OfficialDll("4.0.2.44888", "dx12", "v2.0.0", "f4c1da8e92f3fe563b5c28c44e6267ce6b6b8eb2",
                "241e6e5e4d848424eb8ec9a6b22c43fe34cf0cf52d30002ca435ba42e53a9ca0", "2025-08-20", (FP8,)),
)
OFFICIAL_BY_SHA = {d.sha256: d for d in OFFICIAL}

# Proton's driver-side FSR (amdxcffx64.dll) carries the FSR SDK version. Proton-CachyOS 11.0-20260702 calls
# its 2.3.0.2913 copy "AMD's FSR 4.1.1"; the SDK's own notes pair SDK 2.3.0 with FSR 4.1.1.
DRIVER_SDK_TO_FSR = {(2, 0): "4.0.2", (2, 1): "4.0.3", (2, 2): "4.1.0", (2, 3): "4.1.1", (2, 4): "4.1.2"}

# What AMD and upstream say about older official versions per GPU (shown, and used to filter choices)
KNOWN_ISSUES = {
    ("4.1.1", INT8_RDNA2): "AMD's 4.1.1 shimmers on RX 6000 cards; 4.1.2 fixes it (cyboman docs/gpu-support.md)",
}


def vtuple(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (v or "").split("."))
    except ValueError:
        return ()


def pe_version(path: Path) -> str | None:
    """FileVersion from the PE version resource, e.g. 4.1.2.4555."""
    try:
        import pefile

        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        info = getattr(pe, "VS_FIXEDFILEINFO", None)
        pe.close()
        if not info:
            return None
        f = info[0]
        return f"{f.FileVersionMS >> 16}.{f.FileVersionMS & 0xFFFF}.{f.FileVersionLS >> 16}.{f.FileVersionLS & 0xFFFF}"
    except Exception:
        return None


def _scan(path: Path) -> tuple[tuple[str, ...], str | None]:
    """(FSR4 models, cyboman version name) found in the file's bytes."""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            names = {bytes(x.group(0)) for x in _PROVIDER_RE.finditer(m)}
            hit = CYBOMAN_RE.search(m)
            cy = bytes(hit.group(1)).decode().lower() if hit else None
    except (OSError, ValueError):
        return (), None
    models = tuple(model for name, model in PROVIDERS.items() if name in names)
    order = (FP8, INT8, INT8_RDNA2, INT8_LITE)
    return tuple(sorted(models, key=order.index)), cy


@dataclass
class DllInfo:
    path: Path
    rel: str  # relative to the exe folder when inside it, else to the install folder
    role: str  # upscaler | combined | loader
    api: str  # dx12 | vk
    location: str  # optiscaler | game
    sha256: str | None
    version: str | None
    models: tuple[str, ...] = ()
    cyboman: str | None = None  # cyboman version name, or the variant from a known hash
    official: OfficialDll | None = None

    @property
    def kind(self) -> str:
        if self.cyboman:
            return "cyboman"
        if self.official:
            return "official"
        return "unknown build" if self.role == "upscaler" else "fsr3" if self.role == "combined" else "loader"

    def label(self) -> str:
        if self.role == "combined":
            return f"combined FidelityFX DLL {self.version or '?'} (FSR 3.1 era)"
        if self.role == "loader":
            return f"FidelityFX loader {self.version or '?'}"
        if self.cyboman:
            lite = " (FSR 4.1.2 Lite network)" if re.match(r"\d+\.\d+\.\d+l-", self.cyboman) else ""
            return f"cyboman {self.cyboman}{lite}"
        if self.official:
            return f"official {self.official.version} (FSR SDK {self.official.sdk_tag})"
        return f"{self.version or 'unknown version'} (not a known official or cyboman file)"


def inspect_file(path: Path, rel: str, role: str, api: str, location: str,
                 cyboman_hashes: dict[str, str] | None = None) -> DllInfo:
    sha = sha256_or_none(path)
    info = DllInfo(path, rel, role, api, location, sha, pe_version(path))
    if role == "upscaler":
        info.models, info.cyboman = _scan(path)
        info.official = OFFICIAL_BY_SHA.get(sha or "")
        if cyboman_hashes and sha in cyboman_hashes:
            info.cyboman = info.cyboman or cyboman_hashes[sha]
    return info


def scan_game(exe_dir: Path, install_dir: Path | None = None, cyboman_hashes: dict[str, str] | None = None,
              max_depth: int = 6) -> list[DllInfo]:
    """Every FidelityFX upscaler, combined and loader DLL in the game folder, OptiScaler's included."""
    roots = [exe_dir] + ([install_dir] if install_dir and install_dir != exe_dir and install_dir in exe_dir.parents
                         else [])
    names = {**{v: ("upscaler", k) for k, v in UPSCALER.items()},
             **{k: ("combined", v) for k, v in COMBINED.items()}, **{k: ("loader", v) for k, v in LOADER.items()}}
    out: dict[Path, DllInfo] = {}
    for root in reversed(roots):  # the install folder first, so exe-folder entries win on rel naming
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            dirnames[:] = [x for x in dirnames if x != ".xemfg"]
            if len(d.parts) - base >= max_depth:
                dirnames[:] = []
            for f in filenames:
                hit = names.get(f.lower())
                if not hit:
                    continue
                p = d / f
                inside_exe = exe_dir == d or exe_dir in d.parents
                rel = p.relative_to(exe_dir if inside_exe else root).as_posix()
                location = "optiscaler" if inside_exe and rel.lower().startswith(OPTI_DIR.lower() + "/") else "game"
                out[p] = inspect_file(p, rel, hit[0], hit[1], location, cyboman_hashes)
    return sorted(out.values(), key=lambda i: (i.location != "optiscaler", i.rel.lower()))


# ------------------------------------------------------------------ which DLL runs

def windows_to_host(value: str, base: Path, prefix: Path | None) -> Path | None:
    """An ini path as OptiScaler sees it -> a host path. Relative paths are based on OptiScaler's folder."""
    v = value.strip().strip('"')
    if re.match(r"^[A-Za-z]:[\\/]", v):
        drive, rest = v[0].lower(), v[3:].replace("\\", "/")
        if drive == "z":
            return Path("/") / rest
        if prefix is not None:
            dd = prefix / "dosdevices" / f"{drive}:"
            if dd.exists():
                return Path(os.path.realpath(dd)) / rest
            if drive == "c":
                return prefix / "drive_c" / rest
        return None
    return (base / v.replace("\\", "/")).resolve() if v else None


@dataclass
class Runtime:
    api: str
    dll: DllInfo | None
    source: str  # override | optiscaler | game | none
    how: str  # plain explanation
    notes: list[str] = field(default_factory=list)


def _ini_value(ini: ini_mod.Ini | None, section: str, key: str) -> str | None:
    if ini is None:
        return None
    v = ini.get(section, key)
    return None if v is None or v.strip().lower() in ("", "auto") else v


def runtime_dll(exe_dir: Path, api: str, dlls: list[DllInfo], prefix: Path | None = None,
                optiscaler_installed: bool = True) -> Runtime:
    """Which upscaler DLL runs for `api`, by OptiScaler's lookup order (see the module docstring)."""
    name = UPSCALER[api]
    by_path = {i.path.resolve(): i for i in dlls}
    game_copies = [i for i in dlls if i.role == "upscaler" and i.api == api and i.location == "game"]

    def game_fallback(why: str) -> Runtime:
        if game_copies:
            g = min(game_copies, key=lambda i: (i.path.parent != exe_dir, len(i.rel)))
            return Runtime(api, g, "game", f"{why}; the game's own copy runs ({g.rel})")
        combined = [i for i in dlls if i.role == "combined" and i.api == api]
        extra = " The game only ships the combined FSR 3.1 DLL, which has no FSR4." if combined else ""
        return Runtime(api, None, "none", f"{why}; the game has no {name} either, so FSR4 cannot run.{extra}")

    if not optiscaler_installed:
        return game_fallback("OptiScaler is not installed")
    ip = exe_dir / "OptiScaler.ini"
    ini = ini_mod.Ini(ini_mod.read_text(ip)) if ip.is_file() else None
    override = _ini_value(ini, "Libraries", PATH_KEYS[api])
    if override:
        p = windows_to_host(override, exe_dir, prefix)
        if p is None:
            return Runtime(api, None, "override", f"{PATH_KEYS[api]}={override} is set; xemfg cannot map that path")
        if p.is_dir():
            p = p / name
        if p.is_file():
            info = by_path.get(p.resolve()) or inspect_file(p, str(p), "upscaler", api, "optiscaler")
            return Runtime(api, info, "override", f"{PATH_KEYS[api]} in OptiScaler.ini points at it")
    opti = _ini_value(ini, "Libraries", "OptiDllPath")
    folder = windows_to_host(opti, exe_dir, prefix) if opti else None
    if folder is None:
        folder = exe_dir / OPTI_DIR if (exe_dir / OPTI_DIR).is_dir() else exe_dir
    p = folder / name
    if p.is_file():
        info = by_path.get(p.resolve()) or inspect_file(p, p.name, "upscaler", api, "optiscaler")
        where = "OptiDllPath in OptiScaler.ini" if opti else f"OptiScaler's dll folder ({folder.name}/)"
        return Runtime(api, info, "optiscaler", f"OptiScaler loads it from {where}, before the game's own copy")
    return game_fallback(f"OptiScaler's dll folder has no {name}")


# ------------------------------------------------------------------ the driver's FSR in the Wine prefix

@dataclass
class DriverFsr:
    path: Path
    version: str | None
    fsr: str | None  # FSR version it carries, when known


def driver_fsr(prefix: Path | None) -> DriverFsr | None:
    if prefix is None:
        return None
    p = prefix / "drive_c/windows/system32/amdxcffx64.dll"
    if not p.is_file():
        return None
    v = pe_version(p)
    t = vtuple(v)
    return DriverFsr(p, v, DRIVER_SDK_TO_FSR.get(t[:2]) if len(t) >= 2 else None)


def driver_takes_over(drv: DriverFsr | None, dll: DllInfo | None) -> bool | None:
    """True when the driver's FSR is newer than the DLL's (OptiScaler then lists it first). None: unknown."""
    if drv is None or dll is None:
        return False if drv is None else None
    if drv.fsr is None:
        return None
    mine = "4.1.1" if dll.cyboman else (dll.version or "")  # cyboman builds are AMD's 4.1.1.2740 file
    return vtuple(drv.fsr)[:3] > vtuple(mine)[:3]


def steam_prefix(library: Path | None, appid: str | None) -> Path | None:
    if library is None or not appid:
        return None
    p = library / "steamapps/compatdata" / appid / "pfx"
    return p if p.is_dir() else None


# ------------------------------------------------------------------ OptiScaler.log confirmation

_LOG_LOAD = re.compile(r"amd_fidelityfx_upscaler_(dx12|vk)\.dll loaded from (?:Opti dll path|override path): (.+?)\s*$",
                       re.M)


@dataclass
class LogEvidence:
    log: Path
    loaded: dict[str, str]  # api -> Windows path from the log


def log_evidence(exe_dir: Path) -> LogEvidence | None:
    """What the last OptiScaler run logged about the upscaler DLL (only when LogToFile is on)."""
    ip = exe_dir / "OptiScaler.ini"
    ini = ini_mod.Ini(ini_mod.read_text(ip)) if ip.is_file() else None
    name = _ini_value(ini, "Log", "LogFileName") or "OptiScaler.log"
    log = windows_to_host(name, exe_dir, None)
    if log is None or not log.is_file():
        return None
    text = log.read_text(errors="replace")
    loaded = {m.group(1): m.group(2) for m in _LOG_LOAD.finditer(text)}
    return LogEvidence(log, loaded)


# ------------------------------------------------------------------ what a GPU can use

def runs_on(dll: DllInfo | OfficialDll, profile: Profile) -> bool:
    """Whether the DLL holds the FSR4 model the GPU class runs (cyboman: INT8 slot, never on RDNA 4)."""
    if profile.model is None:
        return False
    if isinstance(dll, DllInfo) and dll.cyboman:
        return profile.cyboman is not None
    return profile.model in dll.models


def newest_official(profile: Profile, api: str = "dx12") -> OfficialDll | None:
    ok = [d for d in OFFICIAL if d.api == api and profile.model in d.models]
    return max(ok, key=lambda d: vtuple(d.version)) if ok and profile.model else None


def official_issue(d: OfficialDll, profile: Profile) -> str | None:
    return KNOWN_ISSUES.get((".".join(d.version.split(".")[:3]), profile.model or ""))
