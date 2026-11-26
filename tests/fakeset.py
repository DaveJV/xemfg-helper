"""A tiny but real XeMFG 'set': zip archives served by FakeFetcher, with a matching manifest."""
from __future__ import annotations

import hashlib
import io
import zipfile

from xemfg.core.context import Context, ScriptedUI
from xemfg.core.manifest import CybomanFamily, FileSpec, Manifest, XSet

from fakes import FakeFetcher

STOCK_INI = (
    "; OptiScaler.ini\r\n\r\n[FrameGen]\r\nEnabled=auto\r\nFGInput=auto\r\nFGOutput=auto\r\n\r\n"
    "[XeFG]\r\nDynamicMFG=auto\r\nFramerateTargetDMFG=auto\r\n\r\n[DLSSG]\r\nFramerateTargetDMFG=auto\r\n\r\n"
    "[FSR]\r\nUpscalerIndex=auto\r\n"
    "; 0 = No override | 1 = FP8 | 2 = INT8 | 3 = INT8-2 | 4 = INT8 Lite\r\nFsr4GpuSupport=auto\r\n"
    "; 0 = FP8 | 1 = INT8 | 2 = INT8 RDNA2 | 3 = INT8 Lite\r\nFsr4Model=auto\r\nFsr4EnableWatermark=auto\r\n\r\n"
    "[Spoofing]\r\nDxgi=auto\r\n\r\n[Plugins]\r\nLoadAsiPlugins=auto\r\n"
)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def zipbytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


def make_set(sid: str, created: str, routes: dict) -> XSet:
    nightly_files = {
        "OptiScaler/libxess.dll": b"xess " + sid.encode(),
        "OptiScaler/amd_fidelityfx_upscaler_dx12.dll": b"stock fsr " + sid.encode(),
        "OptiScaler/D3D12_OptiScaler/D3D12Core.dll": b"core",
        "OptiScaler.dll": b"MZ OptiScaler v10.0.0-dev (00000000) nightly " + sid.encode(),
        "OptiScaler.ini": STOCK_INI.encode(),
        "setup_linux.sh": b"#!/bin/sh",
    }
    if sid == "t2":
        nightly_files["OptiScaler/new_in_t2.dll"] = b"new"
    else:
        nightly_files["OptiScaler/only_in_t1.dll"] = b"old"
    nightly = zipbytes(nightly_files)
    opti = b"MZ OptiScaler v10.0.0-dev (dd00" + sid.encode().hex()[:4].encode() + b") dashdogy " + sid.encode()
    mfgu_dll = b"MZ RTXMFG plugin " + sid.encode()
    mfgu = zipbytes({"MFGUnlocker.dll": mfgu_dll, "README.txt": b"r"})
    cy_dlls = {d: f"x 4.1.2l-{d}-r3-cyboman {sid}".encode() if "l-" in d else f"x {d}-cyboman {sid}".encode()
               for d in ("4.1.2l-exact", "4.1.2l-lossy", "exact", "lossy")}
    cy = zipbytes({f"{d}/amd_fidelityfx_upscaler_dx12.dll": b for d, b in cy_dlls.items()})
    tag = f"dll-{sid}"
    xs = XSet(
        id=sid, mfgu_version=f"v-{sid}", created=created,
        files={
            "nightly": FileSpec("nightly", "optiscaler/OptiScaler-nightly", f"nightly-{sid}", f"Opti-{sid}.zip", sha(nightly)),
            "optiscaler": FileSpec("optiscaler", "dashdogy/OptiScaler", f"mfgu-{sid}", "OptiScaler.dll", sha(opti),
                                   cache_name="OptiScaler-mfgu.dll"),
            "mfgu": FileSpec("mfgu", "dashdogy/MFG-Unlocker", f"v-{sid}", f"MFGUnlocker-{sid}.zip", sha(mfgu),
                             inner_sha256={"MFGUnlocker.dll": sha(mfgu_dll)}),
        },
        cyboman_repo="zgauthier2000/fsr4", cyboman_tag=tag,
        families={"rx7000": CybomanFamily("rx7000", [(f"cy-rx7000-{sid}.zip", sha(cy))],
                                          {d: sha(b) for d, b in cy_dlls.items()})},
    )
    for spec, data in ((xs.nightly, nightly), (xs.optiscaler, opti), (xs.mfgu, mfgu)):
        routes[spec.url] = data
    routes[xs.cyboman_url(f"cy-rx7000-{sid}.zip")] = cy
    xs.raw = {"opti": opti, "mfgu_dll": mfgu_dll, "cy": cy_dlls}  # type: ignore[attr-defined]
    return xs


class _TestManifest(Manifest):
    """The real manifest, whose set views also carry the raw test file contents (`.raw`)."""

    raws: dict = {}

    def __post_init__(self):
        self.raws = {}

    def get(self, sid):
        xs = super().get(sid)
        xs.raw = self.raws.get(sid)  # type: ignore[attr-defined]
        return xs


def setup_sets(world, current: str = "t1") -> tuple[Manifest, FakeFetcher]:
    routes: dict = {}
    t1 = make_set("t1", "2026-11-01", routes)
    t2 = make_set("t2", "2026-11-20", routes)
    m = _TestManifest(current)
    for xs in (t1, t2):
        m.add_set(xs)
        m.raws[xs.id] = xs.raw  # type: ignore[attr-defined]
    m.set_latest_from(current)
    add_officials(m, routes)
    m.save(world.paths.manifest_file)
    return m, FakeFetcher(routes)


def default_sysfs(world):
    """The world's GPUs, or one RX 7600 (RDNA 3) when the test did not add any."""
    if any((world.sysfs / "class/drm").glob("card[0-9]")):
        return world.sysfs
    from fakes import World

    w = World.__new__(World)
    w.sysfs = world.tmp / "sys-default"
    if not w.sysfs.exists():
        (w.sysfs / "class/drm").mkdir(parents=True)
        World.add_gpu(w, 1, "0000:03:00.0", 0x1002, 0x7480, (11, 0, 2))
    return w.sysfs


def make_ctx(world, fetcher, ui=None, steam_running=False) -> Context:
    flag = {"steam": steam_running}
    ctx = Context(world.paths, world.locations(), ui or ScriptedUI(), lambda: fetcher,
                  steam_running_fn=lambda: flag["steam"], faugus_running_fn=lambda: False, sysfs=default_sysfs(world))
    ctx.steam_flag = flag  # type: ignore[attr-defined]
    return ctx


ALL_MODELS = ("fp8", "int8", "int8-rdna2", "int8-lite")
OFFICIAL_DLLS = {  # fake official DLLs, newer than the real ones xemfg knows, so they are what tests pick
    ("4.1.2.9000", "dx12"): ALL_MODELS, ("4.1.2.9000", "vk"): ALL_MODELS, ("4.1.1.9000", "dx12"): ("fp8", "int8"),
    ("4.1.0.9000", "dx12"): ("fp8",),
}


def official_bytes(version, api):
    import fakepe

    return fakepe.upscaler(version, OFFICIAL_DLLS[(version, api)], None) + api.encode()


def add_officials(m, routes):
    from xemfg.core.fsr4 import OfficialDll

    for (version, api), models in OFFICIAL_DLLS.items():
        data = official_bytes(version, api)
        d = OfficialDll(version, api, f"v-sdk-{version}", "c0ffee" + version.replace(".", ""), sha(data),
                        "2026-11-01", models)
        m.components.setdefault("fsr4_official", {})[d.key] = d
        routes[d.url] = data
