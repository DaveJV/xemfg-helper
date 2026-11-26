"""XeMFG backend: dashdogy OptiScaler + MFG Unlocker plugin + paired nightly + cyboman FSR4 (xemfg-linux.sh)."""
from __future__ import annotations

from pathlib import Path

from ..cache import SetCache
from ..models import CompatReport
from .base import Options, Placement

UPSCALER_DLL = "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"
PLUGIN = "OptiScaler/plugins/MFGUnlocker.asi"
FG_INPUTS = ("FSRFG", "FSRFG30", "Upscaler")


class XeMFGBackend:
    name = "xemfg"

    def placements(self, cache: SetCache, opts: Options, keep_fsr4: bool) -> list[Placement]:
        xs = cache.xset
        nightly = cache.nightly_dir()
        out: list[Placement] = []
        src_root = nightly / "OptiScaler"
        for p in sorted(src_root.rglob("*")):
            if p.is_file():
                rel = "OptiScaler/" + p.relative_to(src_root).as_posix()
                out.append(Placement(p, rel, "nightly"))
        out.append(Placement(cache.optiscaler_dll(), opts.proxy, "optiscaler", xs.optiscaler.sha256))
        out.append(Placement(cache.mfgu_dll(), PLUGIN, "plugin", xs.mfgu.inner_sha256.get("MFGUnlocker.dll")))
        if opts.fsr4 != "none" and not keep_fsr4:
            stock = src_root / "amd_fidelityfx_upscaler_dx12.dll"
            cy = cache.cyboman_dll(opts.gpu_family, opts.fsr4)
            out = [p for p in out if p.rel != UPSCALER_DLL]
            # stock DLL kept beside it, as xemfg-linux.sh did
            out.append(Placement(stock, UPSCALER_DLL + ".orig", "fsr4-stock"))
            out.append(Placement(cy, UPSCALER_DLL, "fsr4"))
        elif keep_fsr4:
            out = [p for p in out if p.rel != UPSCALER_DLL]
        return out

    def ini_template(self, cache: SetCache) -> Path:
        return cache.nightly_dir() / "OptiScaler.ini"

    def owned_ini_keys(self, opts: Options) -> dict[str, dict[str, str]]:
        int8 = opts.fsr4 != "none"
        return {
            "Plugins": {"LoadAsiPlugins": "true"},
            "FrameGen": {"Enabled": "true", "FGInput": opts.fg_input, "FGOutput": "XeFG"},
            "XeFG": {"DynamicMFG": "true", "FramerateTargetDMFG": str(opts.dmfg_target)},
            "FSR": {"UpscalerIndex": "0", "Fsr4GpuSupport": "2" if int8 else "1", "Fsr4Model": "1" if int8 else "auto"},
        }

    def fresh_install_ini_defaults(self, opts: Options) -> dict[str, dict[str, str]]:
        """Set once, only while still 'auto' (as xemfg-linux.sh did); never reset by repair."""
        d = {"FSR": {"Fsr4EnableWatermark": "true" if opts.fsr4 != "none" else "auto"}}
        if opts.fg_input in ("FSRFG", "FSRFG30"):
            d["Spoofing"] = {"Dxgi": "false"}
        return d

    def launch_env(self, opts: Options) -> dict[str, str]:
        return {"DXIL_SPIRV_CONFIG": "wmma_rdna3_workaround"} if opts.fsr4 == "none" else {}

    def default_fg_input(self, report: CompatReport) -> str | None:
        return report.fg_input


BACKENDS = {"xemfg": XeMFGBackend()}




