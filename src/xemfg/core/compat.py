"""Static, read-only compatibility check of a game folder."""
from __future__ import annotations

import os
import struct
from pathlib import Path

from .models import CompatReport, ForeignProxy, Game, Tier
from .mods import classify, cyboman_build, optiscaler_version
from .platform import OTHER_PROXY_NAMES, PROXY_NAMES

ANTI_CHEAT_DIRS = {"easyanticheat": "EasyAntiCheat", "easyanticheat_eos": "EasyAntiCheat",
                   "battleye": "BattlEye", "anticheatexpert": "ACE", "xigncode3": "XIGNCODE3",
                   "nprotect": "nProtect", "gameguard": "GameGuard"}
ANTI_CHEAT_FILES = {"start_protected_game.exe": "EasyAntiCheat", "easyanticheat_eos_setup.exe": "EasyAntiCheat",
                    "easyanticheat_setup.exe": "EasyAntiCheat", "easyanticheat_x64.dll": "EasyAntiCheat",
                    "easyanticheat_eos.sys": "EasyAntiCheat", "beservice_x64.exe": "BattlEye",
                    "beclient_x64.dll": "BattlEye", "eac_launcher.exe": "EasyAntiCheat",
                    "mhyprot2.sys": "mhyprot", "vgk.sys": "Vanguard", "ace-base64.dll": "ACE",
                    "faceit.exe": "FACEIT", "pnkbstra.exe": "PunkBuster"}
MAX_FILES = 60000
WALK_DEPTH = 8


def _walk_files(root: Path) -> dict[str, list[Path]]:
    """lowercase file name -> paths, excluding our own OptiScaler folder and backups."""
    out: dict[str, list[Path]] = {}
    n = 0
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [x for x in dirnames if x.lower() not in ("optiscaler", ".xemfg")
                       and not x.lower().startswith(("_backup", "_original"))]
        if len(d.parts) - base >= WALK_DEPTH:
            dirnames[:] = []
        for f in filenames:
            out.setdefault(f.lower(), []).append(d / f)
            n += 1
            if n > MAX_FILES:
                return out
        for x in list(dirnames):
            out.setdefault(x.lower() + "/", []).append(d / x)
    return out


def pe_machine(exe: Path) -> int | None:
    """COFF machine type (0x8664 x64, 0x14c x86) or None if not a PE."""
    try:
        with open(exe, "rb") as f:
            head = f.read(0x40)
            if head[:2] != b"MZ":
                return None
            (off,) = struct.unpack_from("<I", head, 0x3C)
            f.seek(off)
            sig = f.read(6)
            if sig[:4] != b"PE\0\0":
                return None
            return struct.unpack_from("<H", sig, 4)[0]
    except (OSError, struct.error):
        return None


def graphics_apis(exe: Path) -> list[str]:
    """Graphics APIs the exe imports (pefile), falling back to a string scan."""
    names: set[str] = set()
    try:
        import pefile

        pe = pefile.PE(str(exe), fast_load=True)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
        ])
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attr, []) or []:
                names.add(entry.dll.decode(errors="ignore").lower())
        pe.close()
    except Exception:
        pass
    apis = []
    want = {"d3d12.dll": "d3d12", "d3d11.dll": "d3d11", "vulkan-1.dll": "vulkan", "d3d9.dll": "d3d9",
            "opengl32.dll": "opengl", "d3d10.dll": "d3d10", "d3d10_1.dll": "d3d10"}
    for dll, api in want.items():
        if dll in names and api not in apis:
            apis.append(api)
    if "d3d12" not in apis:
        # games often load D3D12 dynamically; look for the name in the binary
        try:
            with open(exe, "rb") as f:
                data = f.read(512 << 20).lower()
            for dll, api in want.items():
                if api not in apis and (dll.encode() in data or dll.encode("utf-16-le") in data):
                    apis.append(api)
        except OSError:
            pass
    return apis


def detect_engine(files: dict[str, list[Path]], exe: Path | None) -> str | None:
    if "unityplayer.dll" in files:
        return "Unity"
    if exe and exe.name.lower().endswith("-shipping.exe"):
        return "Unreal"
    if any(k.endswith("-shipping.exe") for k in files) or "engine/" in files:
        return "Unreal"
    return None


def _owned_by_optiscaler_folder(p: Path, exe_dir: Path) -> bool:
    return "optiscaler" in [x.lower() for x in p.relative_to(exe_dir).parts[:-1]] if exe_dir in p.parents else False


def analyze(game: Game, optiscaler_hashes: set[str] | None = None,
            cyboman_hashes: dict[str, str] | None = None) -> CompatReport:
    if game.exe is None:
        if game.exe_candidates:
            return CompatReport(Tier.UNKNOWN, None, ["several possible game exes; pick one"])
        elf = _has_elf(game.install_dir)
        reason = "Linux-native build (no Windows exe)" if elf else game.exe_reason or "no game exe found"
        return CompatReport(Tier.INCOMPATIBLE, None, [reason])
    exe_dir = game.exe.parent
    files = _walk_files(game.install_dir if game.install_dir in exe_dir.parents or game.install_dir == exe_dir else exe_dir)
    if game.install_dir != exe_dir:
        for k, v in _walk_files(exe_dir).items():
            out = files.setdefault(k, [])
            out += [p for p in v if p not in out]

    r = CompatReport(Tier.UNKNOWN, None)
    r.engine = detect_engine(files, game.exe)

    # --- inputs
    has = lambda *names: any(n in files for n in names)  # noqa: E731
    if has("amd_fidelityfx_dx12.dll", "amd_fidelityfx_framegeneration_dx12.dll"):
        r.inputs.append("fsrfg")
    if any(k.startswith(("ffx_fsr3", "ffx_frameinterpolation")) for k in files):
        r.inputs.append("fsr30fg")
    if has("amd_fidelityfx_upscaler_dx12.dll") or (
            has("amd_fidelityfx_dx12.dll") and "fsrfg" not in r.inputs):
        r.inputs.append("fsr31")
    if has("nvngx_dlss.dll"):
        r.inputs.append("dlss")
    if has("libxess.dll"):
        r.inputs.append("xess")
    if has("sl.dlss_g.dll", "nvngx_dlssg.dll"):
        r.inputs.append("dlssg")
    if any(k.startswith("sl.") and k.endswith(".dll") for k in files):
        r.flags.append("Streamline (sl.*.dll) present")

    # --- anti-cheat
    for k, label in ANTI_CHEAT_FILES.items():
        if k in files and label not in r.anti_cheat:
            r.anti_cheat.append(label)
    for k, label in ANTI_CHEAT_DIRS.items():
        if k + "/" in files and label not in r.anti_cheat:
            r.anti_cheat.append(label)

    # --- exe architecture and API
    machine = pe_machine(game.exe)
    r.graphics_api = graphics_apis(game.exe)
    if r.engine == "Unity" and (exe_dir / "UnityPlayer.dll").is_file():
        r.graphics_api = graphics_apis(exe_dir / "UnityPlayer.dll")

    # --- proxies already present in the exe folder
    for name in (*PROXY_NAMES, *OTHER_PROXY_NAMES):
        p = exe_dir / name
        if p.is_file():
            kind = classify(p, optiscaler_hashes)
            if kind == "optiscaler":
                r.optiscaler_proxy = name
                r.optiscaler_version = optiscaler_version(p)
            else:
                r.foreign_proxies.append(ForeignProxy(name, kind))
    if (exe_dir / "OptiScaler.asi").is_file():
        r.optiscaler_proxy = r.optiscaler_proxy or "OptiScaler.asi"
    up = exe_dir / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"
    if up.is_file():
        from .fsutil import sha256_or_none

        h = sha256_or_none(up)
        r.cyboman = (cyboman_hashes or {}).get(h or "") or cyboman_build(up)

    # --- tier
    if machine == 0x14C:
        r.tier, r.reasons = Tier.INCOMPATIBLE, ["32-bit exe (OptiScaler needs 64-bit)"]
    elif r.anti_cheat:
        r.tier, r.reasons = Tier.INCOMPATIBLE, [f"anti-cheat: {', '.join(r.anti_cheat)}"]
    elif r.graphics_api and "d3d12" not in r.graphics_api and "vulkan" not in r.graphics_api:
        r.tier, r.reasons = Tier.INCOMPATIBLE, [f"{'/'.join(r.graphics_api).upper()} only (no D3D12)"]
    elif "fsrfg" in r.inputs or "fsr30fg" in r.inputs:
        r.tier = Tier.NATIVE_FG
        r.fg_input = "FSRFG" if "fsrfg" in r.inputs else "FSRFG30"
        r.reasons = ["game ships FSR frame generation" + (" (FSR 3.0)" if r.fg_input == "FSRFG30" else "")]
    elif any(i in r.inputs for i in ("dlss", "xess", "fsr31")):
        r.tier, r.fg_input = Tier.UPSCALER, "Upscaler"
        found = [i.upper() for i in r.inputs if i in ("dlss", "xess", "fsr31")]
        r.reasons = [f"upscaler input: {', '.join(found)} (OptiFG)"]
        r.warnings.append("FG from upscaler inputs: HUD may ghost (no HUD-less image)")
    elif "dlssg" in r.inputs:
        r.tier = Tier.DLSSG_ONLY
        r.reasons = ["only DLSS frame generation (Streamline); needs spoofing, risky on AMD"]
    elif not r.graphics_api:
        r.tier, r.reasons = Tier.UNKNOWN, ["could not tell the graphics API and no upscaler found"]
    else:
        r.tier, r.reasons = Tier.INCOMPATIBLE, ["nothing to hook (no DLSS/XeSS/FSR upscaler or FSR FG)"]
    if r.fg_input is None:  # what install would use if forced (e.g. anti-cheat override)
        if "fsrfg" in r.inputs:
            r.fg_input = "FSRFG"
        elif "fsr30fg" in r.inputs:
            r.fg_input = "FSRFG30"
        elif any(i in r.inputs for i in ("dlss", "xess", "fsr31")):
            r.fg_input = "Upscaler"
    if "vulkan" in r.graphics_api and "d3d12" not in r.graphics_api and r.installable:
        r.warnings.append("Vulkan game: XeFG works only with dashdogy's OptiScaler build")

    # --- extra flags
    if r.engine == "Unity":
        r.flags.append("Unity: may need -force-d3d12 as a game argument")
    elif r.engine:
        r.flags.append(f"engine: {r.engine}")
    if r.anti_cheat and r.tier != Tier.INCOMPATIBLE:
        r.warnings.append(f"anti-cheat present: {', '.join(r.anti_cheat)}")
    for fp in r.foreign_proxies:
        from .mods import KIND_LABELS

        r.flags.append(f"{fp.name}: {KIND_LABELS.get(fp.kind, fp.kind)}")
    if r.optiscaler_proxy:
        r.flags.append(f"OptiScaler present as {r.optiscaler_proxy}" +
                       (f" ({r.optiscaler_version})" if r.optiscaler_version else ""))
    if r.cyboman:
        r.flags.append(f"cyboman FSR4 DLL: {r.cyboman}")
    return r


def _has_elf(root: Path) -> bool:
    if not root.is_dir():
        return False
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - len(root.parts) > 4:
            dirnames[:] = []
        for f in filenames:
            p = Path(dirpath) / f
            try:
                if p.stat().st_size > 100_000 and p.read_bytes()[:4] == b"\x7fELF":
                    return True
            except OSError:
                continue
    return False
