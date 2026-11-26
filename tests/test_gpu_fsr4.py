"""GPU detection, FSR4 DLL detection, which DLL runs, and the per-GPU filters."""
import pytest
from typer.testing import CliRunner

from xemfg import cli
from xemfg.core import fsr4, gpu
from xemfg.core.mods import cyboman_build
from xemfg.core.ops import apply, install as ops
from xemfg.core.ops.fsr4status import cyboman_family, report

import fakepe
from fakes import TIER_FILES
from fakeset import make_ctx, setup_sets

ALL = ("fp8", "int8", "int8-rdna2", "int8-lite")
AMD = 0x1002


# ------------------------------------------------------------------ GPU detection

@pytest.mark.parametrize("gc,profile,integrated,model,support,cyboman", [
    ((11, 0, 2), "rdna3", False, "int8", "official", "rx7000"),
    ((11, 0, 0), "rdna3", False, "int8", "official", "rx7000"),
    ((12, 0, 1), "rdna4", False, "fp8", "official", None),
    ((10, 3, 0), "rdna2", False, "int8-rdna2", "official", "rx6000"),
    ((10, 3, 1), "van-gogh", True, "int8-lite", "official", "igpu-rdna2"),
    ((11, 5, 0), "strix-point", True, "int8-lite", "official", "igpu-rdna3"),
    ((11, 5, 1), "strix-halo", True, "int8", "official", "igpu-rdna3"),
    ((11, 0, 1), "rdna3-igpu", True, "int8-lite", "optiscaler", "igpu-rdna3"),
    ((11, 5, 2), "rdna3-igpu", True, "int8-lite", "optiscaler", "igpu-rdna3"),
    ((10, 3, 3), "rdna2-igpu", True, "int8-lite", "forced", "igpu-rdna2"),
    ((10, 1, 10), "rdna1", False, None, None, None),
])
def test_classify_amd(world, gc, profile, integrated, model, support, cyboman):
    world.add_gpu(1, "0000:03:00.0", AMD, 0x7480, gc)
    [g] = gpu.detect(world.sysfs)
    p = g.profile
    assert (g.detected, p.integrated, p.model, p.support, p.cyboman) == (profile, integrated, model, support, cyboman)
    assert g.known and g.slot == "0000:03:00.0" and g.driver == "amdgpu"


def test_unknown_and_non_amd_are_not_guessed(world):
    world.add_gpu(0, "0000:01:00.0", AMD, 0x1234, (11, 7, 0))  # a GC version xemfg does not know
    world.add_gpu(1, "0000:02:00.0", AMD, 0x5678, None)  # no ip_discovery (old kernel)
    world.add_gpu(2, "0000:03:00.0", 0x10DE, 0x2684, None, driver="nvidia")
    gs = gpu.detect(world.sysfs)
    assert [g.detected for g in gs] == ["other", "other", "other"]
    assert not any(g.known for g in gs) and gs[2].vendor == "NVIDIA"
    assert all(g.profile.model is None and g.profile.cyboman is None for g in gs)


def test_several_gpus_need_a_choice(world):
    world.add_gpu(0, "0000:05:00.0", AMD, 0x15BF, (11, 0, 1), boot_vga=True)  # 780M
    world.add_gpu(1, "0000:03:00.0", AMD, 0x7480, (11, 0, 2), boot_vga=False)  # RX 7600 dGPU
    gs = gpu.detect(world.sysfs)
    assert len(gs) == 2  # connector nodes (card0-DP-1) are skipped
    s = gpu.select(gs)
    assert s.gpu is None and s.needs_choice and "pick" in s.reason
    s = gpu.select(gs, "0000:03:00.0")
    assert s.gpu.detected == "rdna3"
    s = gpu.select(gs, "0000:09:00.0")
    assert s.gpu is None and "not present" in s.reason


def test_override_with_warning(world):
    world.add_gpu(0, "0000:03:00.0", 0x10DE, 0x2684, None)
    s = gpu.select(gpu.detect(world.sysfs), override="rdna3")
    assert s.gpu.profile.key == "rdna3" and "manual override" in s.reason
    assert "treated as" in gpu.override_warning(s.gpu)
    with pytest.raises(ValueError):
        gpu.select(gpu.detect(world.sysfs), override="rdna9")


# ------------------------------------------------------------------ DLL version detection

def test_dll_version_models_and_cyboman_name(tmp_path):
    p = tmp_path / "a.dll"
    p.write_bytes(fakepe.upscaler("4.1.2.4555", ALL))
    i = fsr4.inspect_file(p, "a.dll", "upscaler", "dx12", "game")
    assert i.version == "4.1.2.4555" and i.models == ALL and i.cyboman is None and i.kind == "unknown build"
    p.write_bytes(fakepe.upscaler("4.1.1.2740", ("fp8", "int8"), "4.1.2l-exact-igpu-r3-cyboman"))
    i = fsr4.inspect_file(p, "a.dll", "upscaler", "dx12", "game")
    assert i.version == "4.1.1.2740" and i.cyboman == "4.1.2l-exact-igpu-r3-cyboman" and i.kind == "cyboman"
    p.write_bytes(fakepe.make_dll(None, b"nothing"))
    assert fsr4.pe_version(p) is None
    p.write_bytes(b"not a PE")
    assert fsr4.pe_version(p) is None and fsr4._scan(p) == ((), None)


def test_known_hashes(tmp_path, monkeypatch):
    p = tmp_path / "a.dll"
    p.write_bytes(fakepe.upscaler("4.1.1.2740", ("fp8", "int8")))
    from xemfg.core.fsutil import sha256_or_none

    h = sha256_or_none(p)
    i = fsr4.inspect_file(p, "a.dll", "upscaler", "dx12", "game", cyboman_hashes={h: "exact (rx7000, dll-x)"})
    assert i.kind == "cyboman" and i.cyboman == "exact (rx7000, dll-x)"
    off = fsr4.OfficialDll("4.1.1.2740", "dx12", "v2.3.0", "c", h, "2026-06-24", ("fp8", "int8"))
    monkeypatch.setitem(fsr4.OFFICIAL_BY_SHA, h, off)
    i = fsr4.inspect_file(p, "a.dll", "upscaler", "dx12", "game")
    assert i.kind == "official" and i.label() == "official 4.1.1.2740 (FSR SDK v2.3.0)"


def test_cyboman_names():
    for name in ("4.1.2-exact-r3-cyboman", "4.1.2l-lossy-igpu-r2-cyboman", "4.1.1-igpu-lossy2-cyboman",
                 "4.1.1-cyboman", "4.1.2-EXACT-R3-CYBOMAN"):
        assert fsr4.CYBOMAN_RE.search(b"x\0" + name.encode() + b"\0").group(1).decode().lower() == name.lower()
    assert cyboman_family("4.1.2-exact-r3-cyboman") == "rx7000"
    assert cyboman_family("4.1.2-lossy-r2-cyboman") == "rx6000"
    assert cyboman_family("4.1.2l-exact-igpu-r3-cyboman") == "igpu-rdna3"
    assert cyboman_family("4.1.2-exact-igpu-r2-cyboman") == "igpu-rdna2"


def test_mods_cyboman_build_reads_igpu_names(tmp_path):
    p = tmp_path / "a.dll"
    p.write_bytes(b"MZ\0" + b"4.1.2-exact-igpu-r3-cyboman\0")
    assert cyboman_build(p) == "4.1.2-exact-igpu-r3-cyboman"


# ------------------------------------------------------------------ which DLL a GPU can use

def test_filters_per_gpu():
    P = gpu.PROFILES
    assert fsr4.newest_official(P["rdna3"]).version == "4.1.2.4555"
    assert fsr4.newest_official(P["rdna2"]).version == "4.1.2.4555"
    assert fsr4.newest_official(P["rdna4"]).version == "4.1.2.4555"
    assert fsr4.newest_official(P["rdna1"]) is None and fsr4.newest_official(P["other"]) is None
    by = {d.version: d for d in fsr4.OFFICIAL if d.api == "dx12"}
    assert fsr4.runs_on(by["4.1.1.2740"], P["rdna3"]) and not fsr4.runs_on(by["4.1.1.2740"], P["rdna2"])
    assert not fsr4.runs_on(by["4.1.0.0"], P["rdna3"]) and fsr4.runs_on(by["4.0.2.44888"], P["rdna4"])
    assert not fsr4.runs_on(by["4.1.1.2740"], P["van-gogh"]) and fsr4.runs_on(by["4.1.2.4555"], P["van-gogh"])
    cy = fsr4.DllInfo(None, "x", "upscaler", "dx12", "optiscaler", None, "4.1.1.2740", ("fp8", "int8"),
                      "4.1.2-exact-r3-cyboman")
    assert fsr4.runs_on(cy, P["rdna3"]) and not fsr4.runs_on(cy, P["rdna4"])
    assert fsr4.official_issue(by["4.1.1.2740"], P["rdna2"])  # shimmer on RX 6000
    assert fsr4.newest_official(P["rdna3"], "vk").api == "vk"


# ------------------------------------------------------------------ which DLL runs

def _game(tmp_path, files):
    exe = tmp_path / "G"
    for rel, data in files.items():
        (exe / rel).parent.mkdir(parents=True, exist_ok=True)
        (exe / rel).write_bytes(data)
    return exe


def test_runtime_optiscaler_folder_wins(tmp_path):
    exe = _game(tmp_path, {"amd_fidelityfx_upscaler_dx12.dll": fakepe.upscaler("4.0.3.604"),
                           "OptiScaler/amd_fidelityfx_upscaler_dx12.dll": fakepe.upscaler("4.1.2.4555", ALL),
                           ".xemfg/backups/1/OptiScaler/amd_fidelityfx_upscaler_dx12.dll": b"x"})
    dlls = fsr4.scan_game(exe)
    assert [(d.location, d.rel) for d in dlls] == [("optiscaler", "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"),
                                                   ("game", "amd_fidelityfx_upscaler_dx12.dll")]
    rt = fsr4.runtime_dll(exe, "dx12", dlls)
    assert rt.source == "optiscaler" and rt.dll.version == "4.1.2.4555" and "before the game's own" in rt.how
    rt = fsr4.runtime_dll(exe, "dx12", dlls, optiscaler_installed=False)
    assert rt.source == "game" and rt.dll.version == "4.0.3.604"


def test_runtime_falls_back_to_game_copy_or_none(tmp_path):
    exe = _game(tmp_path, {"OptiScaler/libxess.dll": b"x", "bin/amd_fidelityfx_upscaler_dx12.dll": fakepe.upscaler("4.0.2.44888")})
    rt = fsr4.runtime_dll(exe, "dx12", fsr4.scan_game(exe))
    assert rt.source == "game" and rt.dll.rel == "bin/amd_fidelityfx_upscaler_dx12.dll"
    exe2 = _game(tmp_path / "b", {"OptiScaler/libxess.dll": b"x", "amd_fidelityfx_dx12.dll": fakepe.make_dll("1.0.0.1")})
    dlls = fsr4.scan_game(exe2)
    assert dlls[0].role == "combined" and dlls[0].version == "1.0.0.1"
    rt = fsr4.runtime_dll(exe2, "dx12", dlls)
    assert rt.source == "none" and rt.dll is None and "combined FSR 3.1 DLL" in rt.how


def test_runtime_ini_paths(tmp_path):
    exe = _game(tmp_path, {"OptiScaler/amd_fidelityfx_upscaler_dx12.dll": fakepe.upscaler("4.1.2.4555", ALL),
                           "mine/amd_fidelityfx_upscaler_dx12.dll": fakepe.upscaler("4.1.1.2740", ("fp8", "int8")),
                           "OptiScaler.ini": b"[Libraries]\r\nOptiDllPath=auto\r\nFfxDx12SRPath=mine\r\n"})
    rt = fsr4.runtime_dll(exe, "dx12", fsr4.scan_game(exe))
    assert rt.source == "override" and rt.dll.version == "4.1.1.2740" and "FfxDx12SRPath" in rt.how
    zpath = "Z:" + str(exe / "mine").replace("/", "\\")
    (exe / "OptiScaler.ini").write_text(f"[Libraries]\r\nOptiDllPath={zpath}\r\nFfxDx12SRPath=auto\r\n")
    rt = fsr4.runtime_dll(exe, "dx12", fsr4.scan_game(exe))
    assert rt.source == "optiscaler" and rt.dll.version == "4.1.1.2740" and "OptiDllPath" in rt.how
    (exe / "OptiScaler.ini").write_text("[Libraries]\r\nFfxDx12SRPath=D:\\elsewhere\r\n")
    rt = fsr4.runtime_dll(exe, "dx12", fsr4.scan_game(exe))
    assert rt.source == "override" and rt.dll is None and "cannot map" in rt.how


def test_windows_paths(tmp_path):
    pfx = tmp_path / "pfx"
    (pfx / "drive_c").mkdir(parents=True)
    assert fsr4.windows_to_host("Z:\\home\\x\\a.dll", tmp_path, None) == fsr4.Path("/home/x/a.dll")
    assert fsr4.windows_to_host("C:\\Games\\a", tmp_path, pfx) == pfx / "drive_c/Games/a"
    assert fsr4.windows_to_host("D:\\a", tmp_path, None) is None
    assert fsr4.windows_to_host("sub\\x", tmp_path, None) == (tmp_path / "sub/x").resolve()


def test_driver_fsr(tmp_path):
    sys32 = tmp_path / "pfx/drive_c/windows/system32"
    sys32.mkdir(parents=True)
    (sys32 / "amdxcffx64.dll").write_bytes(fakepe.make_dll("2.3.0.2913"))
    drv = fsr4.driver_fsr(tmp_path / "pfx")
    assert drv.version == "2.3.0.2913" and drv.fsr == "4.1.1"
    off = fsr4.DllInfo(None, "x", "upscaler", "dx12", "optiscaler", None, "4.1.2.4555", ALL)
    cy = fsr4.DllInfo(None, "x", "upscaler", "dx12", "optiscaler", None, "4.1.1.2740", ("fp8", "int8"), "4.1.2-exact-r3-cyboman")
    assert fsr4.driver_takes_over(drv, off) is False and fsr4.driver_takes_over(drv, cy) is False
    (sys32 / "amdxcffx64.dll").write_bytes(fakepe.make_dll("2.4.0.1"))
    assert fsr4.driver_takes_over(fsr4.driver_fsr(tmp_path / "pfx"), cy) is True
    (sys32 / "amdxcffx64.dll").write_bytes(fakepe.make_dll("9.0.0.1"))
    assert fsr4.driver_takes_over(fsr4.driver_fsr(tmp_path / "pfx"), off) is None
    assert fsr4.driver_fsr(None) is None


def test_log_evidence(tmp_path):
    exe = _game(tmp_path, {"OptiScaler.log": b"[info] amd_fidelityfx_upscaler_dx12.dll loaded from Opti dll path: "
                                             b"Z:\\g\\OptiScaler\\amd_fidelityfx_upscaler_dx12.dll\r\n"})
    ev = fsr4.log_evidence(exe)
    assert ev.loaded == {"dx12": "Z:\\g\\OptiScaler\\amd_fidelityfx_upscaler_dx12.dll"}
    assert fsr4.log_evidence(tmp_path) is None


# ------------------------------------------------------------------ report on an installed game

def test_report_installed_game(world, tmp_path, monkeypatch):
    _, f = setup_sets(world)
    world.add_gpu(1, "0000:03:00.0", AMD, 0x7480, (11, 0, 2))
    files = dict(TIER_FILES["native"])
    files["amd_fidelityfx_upscaler_dx12.dll"] = fakepe.upscaler("4.0.3.604")
    root = world.faugus_game("g", "Game", tmp_path / "G", files)
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    prep = apply.build(ctx, g, apply.Request(), dry_run=True)
    assert any(n.startswith("GPU: AMD Navi 33") for n in prep.plan.notes)
    assert any("the game ships amd_fidelityfx_upscaler_dx12.dll" in n for n in prep.plan.notes)
    assert any("amd_fidelityfx_dx12.dll" in n and "left alone" in n for n in prep.plan.notes)
    ops.install(ctx, g, apply.Request(fsr4="none", fsr4_explicit=True), yes=True)

    # put an "official 4.1.1" into OptiScaler's slot
    slot = root / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"
    slot.write_bytes(fakepe.upscaler("4.1.1.2740", ("fp8", "int8")))
    from xemfg.core.fsutil import sha256_or_none

    h = sha256_or_none(slot)
    monkeypatch.setitem(fsr4.OFFICIAL_BY_SHA, h, fsr4.OfficialDll("4.1.1.2740", "dx12", "v2.3.0", "c", h, "", ("fp8", "int8")))
    rep = report(ctx, g)
    [a] = rep.apis
    assert a.verdict == "older" and "4.1.2.4555" in a.message and a.runtime.source == "optiscaler"
    assert [d.rel for d in rep.unused] == ["amd_fidelityfx_upscaler_dx12.dll"]
    assert "LogToFile is off" in rep.log_note

    slot.write_bytes(fakepe.upscaler("4.1.1.2740", ("fp8", "int8"), "4.1.2-exact-r3-cyboman"))
    [a] = report(ctx, g).apis
    assert a.verdict == "cyboman"
    ctx.config.gpu_override = "rdna4"
    rep = report(ctx, g)
    assert rep.apis[0].verdict == "wrong-gpu" and rep.warnings
    ctx.config.gpu_override = "rdna2"
    assert "the archive for this GPU is rx6000" in report(ctx, g).apis[0].message


def test_cli_gpu_and_status(world, tmp_path, monkeypatch):
    _, f = setup_sets(world)
    world.add_gpu(0, "0000:05:00.0", AMD, 0x15BF, (11, 0, 1))
    world.add_gpu(1, "0000:03:00.0", AMD, 0x7480, (11, 0, 2), boot_vga=False)
    world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    from xemfg.core.context import Context

    monkeypatch.setattr(Context, "default", staticmethod(lambda ui: make_ctx(world, f, ui=ui)))
    r = CliRunner().invoke(cli.app, ["gpu"])
    assert r.exit_code == 0 and "2 GPUs" in r.output and "Navi 33" in r.output
    monkeypatch.setattr(cli.TerminalUI, "pick", lambda self, prompt, options: 1)
    r = CliRunner().invoke(cli.app, ["gpu", "--pick"])
    assert "profile   rdna3" in r.output and "rx7000" in r.output
    r = CliRunner().invoke(cli.app, ["gpu", "--override", "rdna4"])
    assert "treated as" in r.output and "cyboman   not offered" in r.output
    CliRunner().invoke(cli.app, ["gpu", "--clear-override"])
    r = CliRunner().invoke(cli.app, ["install", "Game", "--yes"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(cli.app, ["status", "Game"])
    assert r.exit_code == 0, r.output
    assert "gpu       AMD Navi 33" in r.output and "fsr4 dx12" in r.output and "why this file" in r.output
    assert "amd_fidelityfx_dx12.dll" in r.output and "left alone" in r.output
