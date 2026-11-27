import json
import shutil
from pathlib import Path

import pytest

from xemfg.core import ini
from xemfg.core import launchopts as lo
from xemfg.core.ops import apply, diagnose, gc, health, install as ops, prune, uninstall
from xemfg.core.ops.adopt import build_adopt
from xemfg.core.ops.common import OpError
from xemfg.core.ops.run import run_plan
from xemfg.core.state import GameState

from fakes import ASI_LOADER_DLL, RESHADE_DLL, TIER_FILES
from fakeset import setup_sets, make_ctx, sha

DATA = Path(__file__).parent / "data"


@pytest.fixture
def installed(world, tmp_path):
    m, f = setup_sets(world)
    root = world.steam_game("700", "Verify Me", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    g = ctx.resolve("700")
    ops.install(ctx, g, apply.Request(), yes=True)
    return world, m, f, ctx, g, root


def statuses(ctx):
    return {h.title: h for h in health.list_health(ctx)}


def test_healthy_after_install(installed):
    world, m, f, ctx, g, root = installed
    h = statuses(ctx)["Verify Me"]
    assert h.status == "ok", h.findings


def test_health_and_repair_preserve_frame_generation_preferences(installed):
    _, _, _, ctx, game, root = installed
    path = root / "OptiScaler.ini"
    settings = ini.Ini(ini.read_text(path))
    settings.set("XeFG", "FramerateTargetDMFG", "180")
    settings.set("XeFG", "DynamicMFG", "false")
    settings.set("FSR", "UpscalerIndex", "auto")
    ini.write_text(path, settings.text())
    assert statuses(ctx)["Verify Me"].status == "ok"

    settings.set("FrameGen", "FGOutput", "auto")
    ini.write_text(path, settings.text())
    assert statuses(ctx)["Verify Me"].status == "drifted"
    assert ops.repair(ctx, game, yes=True).ok
    repaired = ini.Ini(ini.read_text(path))
    assert repaired.get("FrameGen", "FGOutput") == "XeFG"
    assert repaired.get("XeFG", "FramerateTargetDMFG") == "180"
    assert repaired.get("XeFG", "DynamicMFG") == "false"
    assert repaired.get("FSR", "UpscalerIndex") == "auto"


def test_steam_verify_wipe_doctor_then_repair(installed):
    world, m, f, ctx, g, root = installed
    # Steam verify deletes unknown DLLs and resets files it owns
    (root / "dxgi.dll").unlink()
    (root / "OptiScaler/plugins/MFGUnlocker.asi").unlink()
    path = root / "OptiScaler.ini"
    ini.write_text(path, ini.read_text(path).replace("FGOutput=XeFG", "FGOutput=auto")
                   .replace("[DLSSG]", "[Mine]\r\nTweak=1\r\n\r\n[DLSSG]"))
    world.launch_opts["700"] = "-dx12"
    world.write_steam()
    h = statuses(ctx)["Verify Me"]
    assert h.status == "broken"
    msgs = " | ".join(x.message for x in h.findings)
    assert "missing: dxgi.dll" in msgs and "FGOutput" in msgs and "launch option dxgi=n,b is missing" in msgs
    r = ops.repair(ctx, g, yes=True)
    assert r.ok
    assert sha((root / "dxgi.dll").read_bytes()) == m.get("t1").optiscaler.sha256
    i = ini.Ini(ini.read_text(path))
    assert i.get("FrameGen", "FGOutput") == "XeFG" and i.get("Mine", "Tweak") == "1"  # user tweak kept
    assert world.steam_launch_options("700") == 'WINEDLLOVERRIDES="dxgi=n,b" %command% -dx12'
    assert statuses(ctx)["Verify Me"].status == "ok"


def test_repair_refuses_when_other_mod_took_proxy(installed):
    world, m, f, ctx, g, root = installed
    (root / "dxgi.dll").write_bytes(RESHADE_DLL)
    h = statuses(ctx)["Verify Me"]
    assert any("now belongs to ReShade" in x.message for x in h.findings)
    with pytest.raises(OpError, match="ReShade"):
        ops.repair(ctx, g, yes=True)
    assert (root / "dxgi.dll").read_bytes() == RESHADE_DLL


def test_repair_reset_ini_and_clean(installed):
    world, m, f, ctx, g, root = installed
    path = root / "OptiScaler.ini"
    ini.write_text(path, "garbage\r\n")
    ops.repair(ctx, g, reset_ini=True, yes=True)
    i = ini.Ini(ini.read_text(path))
    assert i.get("FrameGen", "FGOutput") == "XeFG" and i.get("DLSSG", "FramerateTargetDMFG") == "auto"
    st = GameState.load(root)
    bk = [p for p in (root / ".xemfg/backups").rglob("OptiScaler.ini")]
    assert any(p.read_bytes() == b"garbage\r\n" for p in bk)
    m.current = "t2"
    m.save(world.paths.manifest_file)
    ctx.reload_manifest()
    ops.repair(ctx, g, clean=True, yes=True)
    st = GameState.load(root)
    assert st.set == "t2" and (root / "OptiScaler/new_in_t2.dll").exists()
    assert not (root / "OptiScaler/only_in_t1.dll").exists()


def test_outdated_status(installed):
    world, m, f, ctx, g, root = installed
    m.current = "t2"
    m.save(world.paths.manifest_file)
    ctx.reload_manifest()
    assert statuses(ctx)["Verify Me"].status == "outdated"


def test_deleted_game_prune(installed):
    world, m, f, ctx, g, root = installed
    shutil.rmtree(root)
    (world.steam_root / "steamapps/appmanifest_700.acf").unlink()
    ctx.library(refresh=True)
    assert statuses(ctx)["Verify Me"].status == "missing"
    items = prune.plan_prune(ctx)
    assert len(items) == 1 and items[0].relocated_to is None
    prune.drop(ctx, items[0])
    assert json.loads(world.paths.registry_file.read_text())["installs"] == {}


def test_moved_game_relocation(installed, tmp_path):
    world, m, f, ctx, g, root = installed
    lib2 = world.add_library(tmp_path / "lib2")
    new_root = lib2 / "steamapps/common" / root.name
    new_root.parent.mkdir(parents=True)
    shutil.move(str(root), str(new_root))
    acf = world.steam_root / "steamapps/appmanifest_700.acf"
    shutil.move(str(acf), str(lib2 / "steamapps/appmanifest_700.acf"))
    ctx.library(refresh=True)
    items = prune.plan_prune(ctx)
    assert items[0].relocated_to == new_root.resolve() / "Game.exe" or items[0].relocated_to.parent.name == root.name
    assert "another library" in items[0].how
    prune.repoint(ctx, items[0])
    h = statuses(ctx)["Verify Me"]
    assert h.status == "ok", h.findings


def test_gc_keeps_sets_in_use(installed, tmp_path):
    world, m, f, ctx, g, root = installed
    # another game stays on t1 while this one moves to t2 -> both kept
    other = world.faugus_game("o", "Other", tmp_path / "O", TIER_FILES["native"])
    ctx.library(refresh=True)
    ops.install(ctx, ctx.resolve("Other"), apply.Request(), yes=True)
    m.current = "t2"
    m.save(world.paths.manifest_file)
    ctx.reload_manifest()
    ops.update(ctx, g, yes=True)
    cache = world.paths.cache_dir
    stale = cache / "old-set"  # a per-set folder of xemfg 0.1 nobody uses
    (stale / "nightly").mkdir(parents=True)
    (stale / "nightly/x").write_bytes(b"x")
    unused = cache / "nightly/nightly-t0_deadbeef"  # a component version nobody uses
    unused.mkdir(parents=True)
    (unused / "Opti-t0.zip").write_bytes(b"z")
    (cache / "nightly/junk.part").write_bytes(b"p")
    res = gc.build_gc(ctx)
    assert res.kept["nightly"] >= {"nightly-t1", "nightly-t2"} and res.kept["cyboman"] >= {"dll-t1"}
    deleted = {o.dest for o in res.plan.ops if o.kind == "delete"}
    assert stale / "nightly/x" in deleted and unused / "Opti-t0.zip" in deleted and cache / "nightly/junk.part" in deleted
    in_use = [d for d in (cache / "nightly").iterdir() if d.name.startswith(("nightly-t1_", "nightly-t2_"))]
    assert len(in_use) == 2 and not any(str(d) in str(x) for d in in_use for x in deleted)
    run_plan(ctx, res.plan, yes=True)
    assert not stale.exists() and not unused.exists() and all(d.exists() for d in in_use)
    assert other.exists()


def test_gc_removes_unreferenced_backups_after_update(installed):
    world, m, f, ctx, g, root = installed
    st = GameState.load(root)
    junk = root / ".xemfg/backups/20260101T000000/OptiScaler.ini"
    junk.parent.mkdir(parents=True)
    junk.write_text("old")
    refs = {mf.backup for mf in st.files.values() if mf.backup}
    res = gc.build_gc(ctx)
    deleted = {o.dest for o in res.plan.ops if o.kind == "delete"}
    assert junk in deleted
    assert not any(str(root / r) == str(d) for r in refs for d in deleted)


def test_diagnose_sample_logs(installed):
    world, m, f, ctx, g, root = installed
    d = diagnose.diagnose(ctx, g)
    assert any("no RTXMFG log" in x.message for x in d.findings)
    ok = (DATA / "logs/ok.log").read_text()
    # the real log names dashdogy 7f382da9; our fake set's dll says something else
    (root / "RTXMFG-game-1.log").write_text(ok)
    d = diagnose.diagnose(ctx, g)
    msgs = " | ".join(x.message for x in d.findings)
    assert d.facts["plugin API"] == "accepted" and d.facts["last session"] == "ended normally"
    assert "the game loaded OptiScaler 10.0.0-dev (7f382da9)" in msgs
    crashed = "\n".join(l for l in ok.splitlines() if "Session end" not in l and "first-chance faults" not in l)
    crashed += "\n[02:01:00.000] Unhandled exception 0xC0000005 (access violation)\n[02:01:00.000]   #0 dxgi.dll+0x1234\n[02:01:00.000]   #1 ffxvi.exe+0x99\n"
    (root / "RTXMFG-game-1.log").write_text(crashed.replace("OptiScaler plugin API accepted", "x"))
    (root / "RTXMFG-game-1.crash.dmp").write_bytes(b"MDMP")
    d = diagnose.diagnose(ctx, g)
    msgs = " | ".join(x.message for x in d.findings)
    assert "Session end" in msgs and "crash.dmp" in msgs
    assert "plugin did not register" in msgs
    assert d.stack[0].startswith("[02:01:00.000] Unhandled") and "#1 ffxvi.exe" in d.stack[2]
    fix = next(x.fix for x in d.findings if "Session end" in x.message)
    assert "Dynamic MFG off" in fix


def test_diagnose_flags_proton_optiscaler_and_missing_override(world, tmp_path):
    m, f = setup_sets(world)
    world.faugus_game("p", "Pgame", tmp_path / "P", TIER_FILES["native"], launch_arguments="PROTON_USE_OPTISCALER=1")
    ctx = make_ctx(world, f)
    d = diagnose.diagnose(ctx, ctx.resolve("Pgame"))
    msgs = " | ".join(x.message for x in d.findings)
    assert "PROTON_USE_OPTISCALER" in msgs and "lack WINEDLLOVERRIDES" in msgs


def test_adopt_manual_setup(world, tmp_path):
    """A setup made by xemfg-linux.sh: same files as set t1, no state. status sees it, adopt manages it."""
    m, f = setup_sets(world)
    t1 = m.get("t1")
    import io
    import zipfile
    nightly = zipfile.ZipFile(io.BytesIO(f.routes[t1.nightly.url]))
    files = {n: nightly.read(n) for n in nightly.namelist() if n.startswith("OptiScaler/")}
    files["OptiScaler/amd_fidelityfx_upscaler_dx12.dll.orig"] = files["OptiScaler/amd_fidelityfx_upscaler_dx12.dll"]
    files["OptiScaler/amd_fidelityfx_upscaler_dx12.dll"] = t1.raw["cy"]["4.1.2l-exact"]
    files["OptiScaler/plugins/MFGUnlocker.asi"] = t1.raw["mfgu_dll"]
    files["dxgi.dll"] = t1.raw["opti"]
    files["dinput8.dll"] = ASI_LOADER_DLL
    files["OptiScaler.ini"] = (b"[FrameGen]\r\nEnabled = true\r\nFGInput = FSRFG\r\nFGOutput = XeFG\r\n"
                               b"[XeFG]\r\nDynamicMFG = true\r\nFramerateTargetDMFG = 169.000000\r\n"
                               b"[FSR]\r\nUpscalerIndex = auto\r\nFsr4GpuSupport = 2\r\nFsr4Model = 1\r\n"
                               b"[Plugins]\r\nLoadAsiPlugins = true\r\n")
    root = world.faugus_game("ff16", "FINAL FANTASY XVI", tmp_path / "FFXVI", {**TIER_FILES["native"], **files},
                             exe="ffxvi.exe", launch_arguments='PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dxgi,dinput8=n,b"')
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    games_json = (world.faugus_data / "games.json").read_bytes()
    ctx = make_ctx(world, f)
    g = ctx.resolve("ff16")
    u = health.inspect_unmanaged(ctx, g)
    assert u.matches_set == "t1" and u.fsr4_variant[2] == "4.1.2l-exact"
    run_plan(ctx, build_adopt(ctx, g), yes=True)
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".xemfg" not in p.parts}
    assert after == before  # adopt never touches game files
    assert (world.faugus_data / "games.json").read_bytes() == games_json
    st = GameState.load(root)
    assert st.adopted and st.options["fsr4"] == "exact-l" and st.options["dmfg_target"] == 169
    h = statuses(ctx)["FINAL FANTASY XVI"]
    assert h.status == "ok", h.findings
    # now it can be updated like any install
    m.current = "t2"
    m.save(world.paths.manifest_file)
    ctx.reload_manifest()
    ops.update(ctx, g, yes=True)
    assert sha((root / "dxgi.dll").read_bytes()) == m.get("t2").optiscaler.sha256
    assert ini.Ini(ini.read_text(root / "OptiScaler.ini")).get("XeFG", "FramerateTargetDMFG") == "169.000000"
    assert (root / "dinput8.dll").read_bytes() == ASI_LOADER_DLL


def test_mod_takes_over_proxy_then_move_and_uninstall(installed):
    """ReShade is installed over xemfg's dxgi.dll later: xemfg must never delete or overwrite it."""
    world, m, f, ctx, g, root = installed
    (root / "dxgi.dll").write_bytes(RESHADE_DLL)
    h = statuses(ctx)["Verify Me"]
    assert any(x.fix.endswith("--proxy winmm.dll") for x in h.findings if "ReShade" in x.message)
    r = ops.repair(ctx, g, yes=True, proxy="winmm.dll")
    assert r.ok and (root / "dxgi.dll").read_bytes() == RESHADE_DLL and (root / "winmm.dll").exists()
    launch = world.steam_launch_options(g.appid) if g.appid else world.faugus_entry(g.gameid)["launch_arguments"]
    assert lo.override_mode(launch, "winmm") == "n,b"
    assert lo.override_mode(launch, "dxgi") == "n,b"  # ReShade still needs it
    assert statuses(ctx)["Verify Me"].status in ("ok", "outdated")
    assert uninstall.uninstall(ctx, g, yes=True)
    assert (root / "dxgi.dll").read_bytes() == RESHADE_DLL and not (root / "winmm.dll").exists()


def test_uninstall_leaves_a_taken_over_proxy(installed):
    world, m, f, ctx, g, root = installed
    (root / "dxgi.dll").write_bytes(RESHADE_DLL)
    plan = uninstall.build_uninstall(ctx, g)
    assert not any(o.dest == root / "dxgi.dll" for o in plan.ops)
    assert any("dxgi.dll changed since xemfg installed it" in w for w in plan.warnings)
    run_plan(ctx, plan, yes=True)
    assert (root / "dxgi.dll").read_bytes() == RESHADE_DLL
    launch = world.steam_launch_options(g.appid) if g.appid else world.faugus_entry(g.gameid)["launch_arguments"]
    assert lo.override_mode(launch, "dxgi") == "n,b"
