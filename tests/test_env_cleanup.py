"""DXIL_SPIRV_CONFIG=wmma_rdna3_workaround: never added any more, removed where an older xemfg added it."""
from xemfg.core import ini
from xemfg.core import launchopts as lo
from xemfg.core.launchers import faugus_writer, steam_writer
from xemfg.core.ops import apply, install as ops, uninstall
from xemfg.core.ops.health import list_health
from xemfg.core.state import GameState

from fakes import TIER_FILES
from fakeset import make_ctx, setup_sets

DXIL = "DXIL_SPIRV_CONFIG"
WMMA = "wmma_rdna3_workaround"


def _legacy(ctx, g, root, launch_text):
    """Make an install look like one from an older xemfg with --fsr4 none: forced FP8 plus the WMMA env var."""
    st = GameState.load(root)
    st.launch.env = {DXIL: WMMA}
    st.ini.keys["FSR"]["Fsr4GpuSupport"] = "1"
    st.save(root)
    path = root / "OptiScaler.ini"
    text = ini.read_text(path)
    new, _ = ini.plan_edit(text, {"FSR": {"Fsr4GpuSupport": "1"}})
    path.write_bytes(new.encode())
    if g.launch_target == "faugus":
        faugus_writer.write_launch_args(ctx.library().faugus.games_json, g.gameid, launch_text, ctx.paths.backups_dir)
    else:
        steam_writer.write_launch_options(ctx.library().steam.userdata_configs()[0], g.appid, launch_text,
                                          ctx.paths.backups_dir)


def test_none_path_adds_no_env_and_lets_optiscaler_pick(world, tmp_path):
    _, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"], launch_arguments="A=1")
    ctx = make_ctx(world, f)
    assert ops.install(ctx, ctx.resolve("Game"), apply.Request(fsr4="none", fsr4_explicit=True), yes=True).ok
    assert DXIL not in world.faugus_entry("g")["launch_arguments"]
    assert GameState.load(root).launch.env == {}
    i = ini.Ini(ini.read_text(root / "OptiScaler.ini"))
    assert i.get("FSR", "Fsr4GpuSupport") == "auto" and i.get("FSR", "Fsr4Model") == "auto"


def test_repair_removes_flag_xemfg_added_and_unforces_fp8(world, tmp_path):
    _, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"], launch_arguments="A=1")
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    ops.install(ctx, g, apply.Request(fsr4="none", fsr4_explicit=True), yes=True)
    _legacy(ctx, g, root, f'A=1\nWINEDLLOVERRIDES="dxgi=n,b"\n{DXIL}={WMMA}')

    h = list_health(ctx)[0]
    assert any(DXIL in x.message and x.level == "drifted" for x in h.findings)

    assert ops.repair(ctx, g, yes=True).ok
    assert world.faugus_entry("g")["launch_arguments"] == 'A=1\nWINEDLLOVERRIDES="dxgi=n,b"'
    i = ini.Ini(ini.read_text(root / "OptiScaler.ini"))
    assert i.get("FSR", "Fsr4GpuSupport") == "auto" and i.get("FSR", "Fsr4Model") == "auto"
    st = GameState.load(root)
    assert st.launch.env == {} and st.ini.keys["FSR"]["Fsr4GpuSupport"] == "auto"
    assert list_health(ctx)[0].status == "ok"


def test_user_set_flag_is_left_alone(world, tmp_path):
    _, f = setup_sets(world)
    mine = f"{DXIL}={WMMA}\nB=2"
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"], launch_arguments=mine)
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    ops.install(ctx, g, apply.Request(fsr4="none", fsr4_explicit=True), yes=True)
    # an older xemfg recorded the variable as its own even though it was already there
    _legacy(ctx, g, root, mine + '\nWINEDLLOVERRIDES="dxgi=n,b"')
    assert not any(DXIL in x.message for x in list_health(ctx)[0].findings)

    ops.repair(ctx, g, yes=True)
    assert lo.env_value(world.faugus_entry("g")["launch_arguments"], DXIL) == WMMA
    assert uninstall.uninstall(ctx, g, yes=True)
    assert world.faugus_entry("g")["launch_arguments"] == mine


def test_steam_update_path_removes_flag(world, tmp_path):
    _, f = setup_sets(world)
    world.steam_game("100", "Steam Game", TIER_FILES["native"], launch_options="-dx12")
    world.write_steam()
    ctx = make_ctx(world, f)
    g = ctx.resolve("100")
    ops.install(ctx, g, apply.Request(fsr4="none", fsr4_explicit=True), yes=True)
    _legacy(ctx, g, g.exe.parent, f'{DXIL}={WMMA} WINEDLLOVERRIDES="dxgi=n,b" %command% -dx12')
    ops.repair(ctx, g, yes=True)
    assert world.steam_launch_options("100") == 'WINEDLLOVERRIDES="dxgi=n,b" %command% -dx12'


def test_xemfg_added_env():
    assert lo.xemfg_added_env({DXIL: WMMA}, None) == [DXIL]
    assert lo.xemfg_added_env({DXIL: WMMA}, f"{DXIL}={WMMA} %command%") == []
    assert lo.xemfg_added_env({DXIL: WMMA}, f"A=1\n{DXIL}=x") == []
