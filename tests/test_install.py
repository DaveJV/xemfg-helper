import json

import pytest

from xemfg.core import ini
from xemfg.core.ops import apply, install as ops, uninstall
from xemfg.core.ops.common import ConflictError, OpError
from xemfg.core.state import GameState

from fakes import ASI_LOADER_DLL, RESHADE_DLL, TIER_FILES
from fakeset import STOCK_INI, setup_sets, make_ctx, sha


@pytest.fixture
def env(world):
    m, fetcher = setup_sets(world)
    return world, m, fetcher


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_install_faugus_native_fg(env, tmp_path):
    world, m, f = env
    root = world.faugus_game("ff16", "FINAL FANTASY XVI", tmp_path / "FF16", TIER_FILES["native"],
                             launch_arguments='PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dinput8=n,b"')
    world._files(root, {"dinput8.dll": ASI_LOADER_DLL})
    before = snapshot(root)
    ctx = make_ctx(world, f)
    g = ctx.resolve("ff16")
    r = ops.install(ctx, g, apply.Request(), yes=True)
    assert r.ok
    t1 = m.get("t1")
    assert sha((root / "dxgi.dll").read_bytes()) == t1.optiscaler.sha256
    assert (root / "OptiScaler/plugins/MFGUnlocker.asi").read_bytes() == t1.raw["mfgu_dll"]
    assert (root / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll").read_bytes() == t1.raw["cy"]["4.1.2l-exact"]
    assert (root / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll.orig").read_bytes() == b"stock fsr t1"
    text = ini.read_text(root / "OptiScaler.ini")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    i = ini.Ini(text)
    assert i.get("FrameGen", "FGInput") == "FSRFG" and i.get("FrameGen", "FGOutput") == "XeFG"
    assert i.get("XeFG", "FramerateTargetDMFG") == "171" and i.get("DLSSG", "FramerateTargetDMFG") == "auto"
    assert i.get("Plugins", "LoadAsiPlugins") == "true" and i.get("Spoofing", "Dxgi") == "false"
    assert i.get("FSR", "Fsr4GpuSupport") == "2" and i.get("FSR", "Fsr4Model") == "1"
    assert world.faugus_entry("ff16")["launch_arguments"] == \
        'PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dinput8,dxgi=n,b"'
    assert (root / "dinput8.dll").read_bytes() == ASI_LOADER_DLL  # other mod untouched
    st = GameState.load(root)
    assert st.set == "t1" and st.files["dxgi.dll"].action == "added" and st.ini.created
    reg = json.loads(world.paths.registry_file.read_text())
    e = reg["installs"]["faugus:ff16"]
    assert e["status"] == "installed" and e["set"] == "t1" and e["options"]["proxy"] == "dxgi.dll"
    assert e["runner"] == "proton-cachyos" and e["exe_sha256"]

    # reinstall: nothing to change
    prep = apply.build(ctx, g, apply.Request())
    assert prep.plan.ops == [] and prep.plan.texts == []

    # uninstall restores the folder exactly and removes only our override
    assert uninstall.uninstall(ctx, g, yes=True)
    assert snapshot(root) == before
    assert world.faugus_entry("ff16")["launch_arguments"] == 'PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dinput8=n,b"'
    reg = json.loads(world.paths.registry_file.read_text())
    assert reg["installs"]["faugus:ff16"]["status"] == "removed"


def test_dry_run_writes_nothing(env, tmp_path):
    world, m, f = env
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    before = snapshot(world.home)
    ctx = make_ctx(world, f)
    r = ops.install(ctx, ctx.resolve("Game"), apply.Request(), dry_run=True)
    assert r.ok
    assert snapshot(world.home) == before
    assert not world.paths.cache_dir.exists() or not any(world.paths.cache_dir.rglob("*.zip"))
    assert any("would download" in m for m in ctx.ui.messages)


def test_existing_ini_edited_and_restored(env, tmp_path):
    world, m, f = env
    mine = STOCK_INI.replace("Dxgi=auto", "Dxgi=true").replace("[Plugins]", "[Mine]\r\nTweak=5\r\n\r\n[Plugins]")
    root = world.faugus_game("g", "Game", tmp_path / "G", {**TIER_FILES["upscaler"], "OptiScaler.ini": mine.encode()})
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    ops.install(ctx, g, apply.Request(), yes=True)
    i = ini.Ini(ini.read_text(root / "OptiScaler.ini"))
    assert i.get("FrameGen", "FGInput") == "Upscaler"
    assert i.get("Spoofing", "Dxgi") == "true"  # user's non-auto value kept (Upscaler input anyway)
    assert i.get("Mine", "Tweak") == "5"
    uninstall.uninstall(ctx, g, yes=True)
    assert ini.read_text(root / "OptiScaler.ini") == mine


def test_conflict_refusal_suggests_free_proxy(env, tmp_path):
    world, m, f = env
    root = world.faugus_game("g", "Game", tmp_path / "G", {**TIER_FILES["native"], "dxgi.dll": RESHADE_DLL})
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    with pytest.raises(ConflictError) as e:
        apply.build(ctx, g, apply.Request())
    assert "ReShade" in str(e.value) and "--proxy winmm.dll" in str(e.value)
    assert (root / "dxgi.dll").read_bytes() == RESHADE_DLL
    ops.install(ctx, g, apply.Request(proxy="winmm.dll"), yes=True)
    assert (root / "dxgi.dll").read_bytes() == RESHADE_DLL
    assert 'winmm=n,b' in world.faugus_entry("g")["launch_arguments"]


def test_incompatible_and_dlssg_refused(env, tmp_path):
    world, m, f = env
    world.faugus_game("a", "AcGame", tmp_path / "A", {**TIER_FILES["native"], "EasyAntiCheat/x": b""})
    world.faugus_game("b", "SlGame", tmp_path / "B", TIER_FILES["dlssg"])
    ctx = make_ctx(world, f)
    with pytest.raises(OpError, match="anti-cheat"):
        apply.build(ctx, ctx.resolve("AcGame"), apply.Request())
    prep = apply.build(ctx, ctx.resolve("AcGame"), apply.Request(allow_anticheat=True))
    assert any("ANTI-CHEAT OVERRIDE" in w for w in prep.plan.warnings)
    with pytest.raises(OpError, match="DLSS FG only"):
        apply.build(ctx, ctx.resolve("SlGame"), apply.Request())


def test_steam_running_queues_launch_options(env):
    world, m, f = env
    world.steam_game("500", "Steamy", TIER_FILES["native"], launch_options="-dx12")
    ctx = make_ctx(world, f, steam_running=True)
    g = ctx.resolve("500")
    ops.install(ctx, g, apply.Request(), yes=True)
    assert world.steam_launch_options("500") == "-dx12"  # untouched while Steam runs
    q = json.loads(world.paths.registry_file.read_text())["steam_queue"]
    assert q["500"]["options"] == 'WINEDLLOVERRIDES="dxgi=n,b" %command% -dx12'
    ctx2 = make_ctx(world, f, steam_running=False)
    from xemfg.core.ops import launch
    launch.apply_queued(ctx2)
    assert world.steam_launch_options("500") == 'WINEDLLOVERRIDES="dxgi=n,b" %command% -dx12'
    assert json.loads(world.paths.registry_file.read_text())["steam_queue"] == {}


def test_steam_closed_writes_directly(env):
    world, m, f = env
    world.steam_game("500", "Steamy", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    ops.install(ctx, ctx.resolve("500"), apply.Request(), yes=True)
    assert world.steam_launch_options("500") == 'WINEDLLOVERRIDES="dxgi=n,b" %command%'


def test_batch_install_all_compatible(env, tmp_path):
    world, m, f = env
    world.faugus_game("a", "Alpha", tmp_path / "A", TIER_FILES["native"])
    world.faugus_game("b", "Beta", tmp_path / "B", TIER_FILES["native30"])
    world.faugus_game("c", "Gamma", tmp_path / "C", TIER_FILES["upscaler"])
    world.faugus_game("d", "Delta", tmp_path / "D", {**TIER_FILES["native"], "dxgi.dll": RESHADE_DLL})
    ctx = make_ctx(world, f)
    games = ops.compatible_games(ctx)
    assert sorted(g.title for g in games) == ["Alpha", "Beta", "Delta"]
    res = {r.game.title: r for r in ops.install_batch(ctx, games, apply.Request())}
    assert res["Alpha"].ok and res["Beta"].ok
    assert not res["Delta"].ok and "ReShade" in res["Delta"].message
    assert ini.Ini(ini.read_text(tmp_path / "B/OptiScaler.ini")).get("FrameGen", "FGInput") == "FSRFG30"
    assert ctx.ui.confirms == ["Install XeMFG into 2 game(s)?"]


def test_update_then_rollback(env, tmp_path):
    world, m, f = env
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    ops.install(ctx, g, apply.Request(), yes=True)
    assert (root / "OptiScaler/only_in_t1.dll").exists()
    # user tweak outside xemfg keys survives
    path = root / "OptiScaler.ini"
    ini.write_text(path, ini.read_text(path).replace("[DLSSG]\r\nFramerateTargetDMFG=auto", "[DLSSG]\r\nFramerateTargetDMFG=99"))
    m.current = "t2"
    m.set_latest_from("t2")  # what `manifest refresh` does
    m.save(world.paths.manifest_file)
    ctx.reload_manifest()
    r = ops.update(ctx, g, yes=True)
    assert r.ok and r.message == ("nightly nightly-t1 -> nightly-t2; optiscaler mfgu-t1 -> mfgu-t2; "
                                  "mfgu v-t1 -> v-t2; cyboman dll-t1 -> dll-t2")
    t2 = m.get("t2")
    assert sha((root / "dxgi.dll").read_bytes()) == t2.optiscaler.sha256
    assert (root / "OptiScaler/new_in_t2.dll").exists() and not (root / "OptiScaler/only_in_t1.dll").exists()
    assert (root / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll").read_bytes() == t2.raw["cy"]["4.1.2l-exact"]
    st = GameState.load(root)
    assert st.set == "t2" and st.previous_sets == ["t1"]
    reg = json.loads(world.paths.registry_file.read_text())["installs"]["faugus:g"]
    assert reg["set"] == "t2" and reg["previous_sets"] == ["t1"]
    r = ops.rollback(ctx, g, yes=True)
    assert r.message == ("nightly nightly-t2 -> nightly-t1; optiscaler mfgu-t2 -> mfgu-t1; "
                         "mfgu v-t2 -> v-t1; cyboman dll-t2 -> dll-t1")
    assert sha((root / "dxgi.dll").read_bytes()) == m.get("t1").optiscaler.sha256
    assert (root / "OptiScaler/only_in_t1.dll").exists() and not (root / "OptiScaler/new_in_t2.dll").exists()
    assert ini.Ini(ini.read_text(path)).get("DLSSG", "FramerateTargetDMFG") == "99"


def test_keeps_existing_cyboman_unless_told(env, tmp_path):
    world, m, f = env
    existing = b"x 4.1.2l-lossy-r3-cyboman someone-elses"
    root = world.faugus_game("g", "Game", tmp_path / "G", {**TIER_FILES["native"],
                             "OptiScaler/amd_fidelityfx_upscaler_dx12.dll": existing})
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    ops.install(ctx, g, apply.Request(), yes=True)
    assert (root / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll").read_bytes() == existing
    ops.install(ctx, g, apply.Request(fsr4="exact", fsr4_explicit=True), yes=True)
    assert (root / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll").read_bytes() == m.get("t1").raw["cy"]["exact"]
    uninstall.uninstall(ctx, g, yes=True)
    assert (root / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll").read_bytes() == existing


def test_hash_mismatch_aborts_and_deletes(env, tmp_path):
    world, m, f = env
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    f.routes[m.get("t1").optiscaler.url] = b"tampered"
    ctx = make_ctx(world, f)
    from xemfg.core.cache import HashMismatch
    with pytest.raises(HashMismatch):
        ops.install(ctx, ctx.resolve("Game"), apply.Request(), yes=True)
    assert not (root / "dxgi.dll").exists()
    assert not list(world.paths.cache_dir.rglob("OptiScaler-mfgu.dll"))
