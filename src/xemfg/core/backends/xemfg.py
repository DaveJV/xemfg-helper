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
            # cyboman DLLs replace the INT8 model; with OptiScaler's own DLL, OptiScaler picks the model for the GPU
            "FSR": {"UpscalerIndex": "0", "Fsr4GpuSupport": "2" if int8 else "auto", "Fsr4Model": "1" if int8 else "auto"},
        }

    def fresh_install_ini_defaults(self, opts: Options) -> dict[str, dict[str, str]]:
        """Set once, only while still 'auto' (as xemfg-linux.sh did); never reset by repair."""
        d = {"FSR": {"Fsr4EnableWatermark": "true" if opts.fsr4 != "none" else "auto"}}
        if opts.fg_input in ("FSRFG", "FSRFG30"):
            d["Spoofing"] = {"Dxgi": "false"}
        return d

    def launch_env(self, opts: Options) -> dict[str, str]:
        # No env vars. DXIL_SPIRV_CONFIG=wmma_rdna3_workaround is only needed for FP8 on RDNA3 (MLFG),
        # which xemfg never selects.
        return {}

    def legacy_ini_keys(self, keys: dict[str, dict[str, str]], opts: Options) -> dict[str, dict[str, str]]:
        """Recorded keys from older xemfg versions that must change with the current launch env.

        xemfg used to force FP8 (Fsr4GpuSupport=1) on the stock DLL and add the WMMA workaround so RDNA3
        could emulate it. Without the workaround that setting breaks, so it goes together with the env var.
        """
        fsr = keys.get("FSR", {})
        if opts.fsr4 == "none" and fsr.get("Fsr4GpuSupport") == "1":
            new = self.owned_ini_keys(opts)["FSR"]
            keys = {**keys, "FSR": {**fsr, "Fsr4GpuSupport": new["Fsr4GpuSupport"], "Fsr4Model": new["Fsr4Model"]}}
        return keys

    def default_fg_input(self, report: CompatReport) -> str | None:
        return report.fg_input


BACKENDS = {"xemfg": XeMFGBackend()}




