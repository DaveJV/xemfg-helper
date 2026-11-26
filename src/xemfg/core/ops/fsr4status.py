"""Read-only FSR4 report for one game: the DLLs in its folder, which one runs, and how it compares."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import fsr4
from ..context import Context
from ..gpu import MODEL_LABEL, Gpu, override_warning
from ..models import Game

# cyboman name suffix -> archive family
CYBOMAN_FAMILY = [(r"-igpu-r3-", "igpu-rdna3"), (r"-igpu-r2-", "igpu-rdna2"), (r"-r3-", "rx7000"), (r"-r2-", "rx6000")]


def cyboman_family(name: str | None) -> str | None:
    for pat, fam in CYBOMAN_FAMILY:
        if name and re.search(pat, name + "-"):
            return fam
    return None


@dataclass
class ApiReport:
    api: str
    runtime: fsr4.Runtime
    verdict: str  # current | older | wrong-gpu | cyboman | unknown | none | no-gpu
    message: str
    newest: fsr4.OfficialDll | None = None


@dataclass
class Fsr4Report:
    game: Game
    gpu: Gpu | None
    gpu_note: str
    dlls: list[fsr4.DllInfo]
    apis: list[ApiReport] = field(default_factory=list)
    unused: list[fsr4.DllInfo] = field(default_factory=list)  # game copies OptiScaler does not load
    driver: fsr4.DriverFsr | None = None
    driver_note: str = ""
    log: fsr4.LogEvidence | None = None
    log_note: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One line for scan/list."""
        parts = []
        for a in self.apis:
            d = a.runtime.dll
            label = d.label() if d else "none"
            tag = {"older": " (older)", "wrong-gpu": " (not for this GPU)"}.get(a.verdict, "")
            parts.append(f"{a.api}: {label}{tag}")
        return "FSR4 " + "; ".join(parts) if parts else ""


def apis_for(ctx: Context, game: Game) -> list[str]:
    r = ctx.compat(game)
    out = [api for api, name in (("dx12", "d3d12"), ("vk", "vulkan")) if name in r.graphics_api]
    return out or ["dx12"]


def prefix_of(game: Game):
    if game.prefix:
        return game.prefix
    return fsr4.steam_prefix(game.library, game.appid)


def _judge(a_api: str, rt: fsr4.Runtime, g: Gpu | None) -> ApiReport:
    d = rt.dll
    if d is None:
        return ApiReport(a_api, rt, "none", rt.how)
    if g is None:
        return ApiReport(a_api, rt, "no-gpu", f"{d.label()}; pick a GPU to compare (`xemfg gpu --pick`)")
    p = g.profile
    newest = fsr4.newest_official(p, a_api)
    if p.model is None:
        return ApiReport(a_api, rt, "wrong-gpu", f"{d.label()}; {p.label}: {p.note or 'no FSR4 for this GPU'}")
    if d.cyboman:
        fam = cyboman_family(d.cyboman)
        if p.cyboman is None:
            return ApiReport(a_api, rt, "wrong-gpu",
                             f"cyboman {d.cyboman} on {p.label}: upstream says not to install it there", newest)
        extra = f"; built for {fam}, the archive for this GPU is {p.cyboman}" if fam and fam != p.cyboman else ""
        nv = f"; newest official for this GPU: {newest.version}" if newest else ""
        return ApiReport(a_api, rt, "cyboman" if not extra else "wrong-gpu",
                         f"{d.label()}, AMD's 4.1.1.2740 file with faster shaders{extra}{nv}", newest)
    if not fsr4.runs_on(d, p):
        need = MODEL_LABEL[p.model]
        nv = f"; newest official with it: {newest.version}" if newest else ""
        return ApiReport(a_api, rt, "wrong-gpu",
                         f"{d.label()} has no {need} model, which this GPU runs{nv}", newest)
    if d.official and newest and fsr4.vtuple(d.official.version) < fsr4.vtuple(newest.version):
        issue = fsr4.official_issue(d.official, p)
        return ApiReport(a_api, rt, "older",
                         f"official {d.official.version}, newest official for this GPU is {newest.version}"
                         + (f" ({issue})" if issue else ""), newest)
    if d.official:
        return ApiReport(a_api, rt, "current", f"official {d.official.version}, the newest for this GPU", newest)
    newer = newest and fsr4.vtuple(d.version)[:3] < fsr4.vtuple(newest.version)[:3]
    return ApiReport(a_api, rt, "older" if newer else "unknown",
                     f"{d.label()}" + (f"; newest official for this GPU is {newest.version}" if newer else ""), newest)


def report(ctx: Context, game: Game) -> Fsr4Report:
    exe_dir = game.exe.parent
    sel = ctx.gpu_selection()
    g = sel.gpu
    note = (g.describe() + f" ({sel.reason})") if g else sel.reason
    dlls = fsr4.scan_game(exe_dir, game.install_dir, ctx.manifest.cyboman_by_hash())
    rep = Fsr4Report(game, g, note, dlls)
    if g and (w := override_warning(g)):
        rep.warnings.append(w)
    installed = ctx.state(exe_dir) is not None or bool(ctx.compat(game).optiscaler_proxy)
    prefix = prefix_of(game)
    for api in apis_for(ctx, game):
        rt = fsr4.runtime_dll(exe_dir, api, dlls, prefix, optiscaler_installed=installed)
        rep.apis.append(_judge(api, rt, g))
    running = {a.runtime.dll.path for a in rep.apis if a.runtime.dll}
    rep.unused = [d for d in dlls if d.role == "upscaler" and d.location == "game" and d.path not in running]
    rep.driver = fsr4.driver_fsr(prefix)
    if rep.driver:
        dx = next((a for a in rep.apis if a.api == "dx12"), None)
        take = fsr4.driver_takes_over(rep.driver, dx.runtime.dll if dx else None)
        what = f"Proton's driver FSR (amdxcffx64.dll {rep.driver.version}" + \
            (f", FSR {rep.driver.fsr})" if rep.driver.fsr else ")")
        rep.driver_note = {True: f"{what} is newer than the DLL above, so OptiScaler would run the driver's instead",
                           False: f"{what} is not newer, so it is not used",
                           None: f"{what}: xemfg cannot tell its FSR version; check OptiScaler's log"}[take]
        if take:
            rep.warnings.append(rep.driver_note)
    rep.log = fsr4.log_evidence(exe_dir)
    if rep.log is None:
        rep.log_note = "no OptiScaler.log (LogToFile is off): the DLL above is predicted from OptiScaler's load order"
    else:
        found = [f"{api}: {p}" for api, p in rep.log.loaded.items()]
        rep.log_note = (f"{rep.log.log.name} says " + "; ".join(found)) if found else \
            f"{rep.log.log.name} has no upscaler load line (the game may not have started FSR yet)"
    return rep
