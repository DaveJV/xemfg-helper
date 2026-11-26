"""FSR4 DLL choice: parsing, per-GPU filtering and ini keys, install default, swap, store, update, rollback."""
import json

import pytest
from typer.testing import CliRunner

from xemfg import cli
from xemfg.core import fsr4choice as fc
from xemfg.core import ini, upstream
from xemfg.core.config import Config
from xemfg.core.context import ScriptedUI
from xemfg.core.gpu import PROFILES
from xemfg.core.ops import apply, components, fsr4swap, install as ops, uninstall
from xemfg.core.ops.common import OpError
from xemfg.core.ops.fsr4status import report

import fakepe
from fakes import FakeFetcher, TIER_FILES
from fakeset import ALL_MODELS, make_ctx, official_bytes, setup_sets, sha

UP = "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"
VK = "OptiScaler/amd_fidelityfx_upscaler_vk.dll"
AMD = 0x1002


def slot(root):
    return (root / UP).read_bytes() if (root / UP).exists() else None


def fsr(root):
    return json.loads((root / ".xemfg/state.json").read_text())["components"]["fsr4"]


def fsr_keys(root):
    i = ini.Ini(ini.read_text(root / "OptiScaler.ini"))
    return i.get("FSR", "Fsr4GpuSupport"), i.get("FSR", "Fsr4Model")


@pytest.fixture
def game(world, tmp_path):
    m, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    return world, m, f, ctx, ctx.resolve("Game"), root


# ------------------------------------------------------------------ parsing and ini keys

def test_parse():
    assert str(fc.parse(None)) == "official"
    assert str(fc.parse("exact-l")) == "cyboman:exact-l" and str(fc.parse("none")) == "nightly"
    assert fc.parse("official:4.1.1.2740") == fc.Choice("official", "4.1.1.2740")
    for bad in ("cyboman", "fsr3", "keep:1"):
        with pytest.raises(fc.ChoiceError):
            fc.parse(bad)


@pytest.mark.parametrize("profile,keys", [
    ("rdna4", ("auto", "0")), ("rdna3", ("2", "1")), ("strix-halo", ("2", "1")), ("rdna2", ("3", "2")),
    ("van-gogh", ("4", "3")), ("strix-point", ("4", "3")), ("rdna3-igpu", ("4", "3")), ("rdna2-igpu", ("4", "3")),
])
def test_ini_keys_official(profile, keys):
    k = fc.ini_keys(fc.Choice("official"), PROFILES[profile])
    assert (k["Fsr4GpuSupport"], k["Fsr4Model"]) == keys


def test_ini_keys_other_kinds():
    assert fc.ini_keys(fc.Choice("cyboman", "exact"), PROFILES["rdna2"]) == {"Fsr4GpuSupport": "2", "Fsr4Model": "1"}
    for kind in ("nightly", "original"):
        assert fc.ini_keys(fc.Choice(kind), PROFILES["rdna3"]) == {"Fsr4GpuSupport": "auto", "Fsr4Model": "auto"}


def test_template_check():
    documented = ("; INT8-2 is the RDNA2 model\r\n; 0 = No override | 1 = FP8 | 2 = INT8 | 3 = INT8-2 | 4 = INT8 Lite\r\n"
                  "Fsr4GpuSupport=auto\r\n; 0 = FP8 | 1 = INT8 | 2 = INT8 RDNA2 | 3 = INT8 Lite\r\nFsr4Model=auto\r\n")
    assert fc.template_supports(documented, {"Fsr4GpuSupport": "4", "Fsr4Model": "3"}) is None
    old = "Fsr4GpuSupport=auto\r\nFsr4Model=auto\r\n"
    assert fc.template_supports(old, {"Fsr4GpuSupport": "2", "Fsr4Model": "1"}) is None  # INT8: any OptiScaler 10
    assert "2026-10-20" in fc.template_supports(old, {"Fsr4GpuSupport": "3", "Fsr4Model": "2"})


# ------------------------------------------------------------------ the list per GPU

def options(world, game_, gc):
    world.add_gpu(1, "0000:03:00.0", AMD, 0x1234, gc)
    w, m, f, ctx, g, root = game_
    return fsr4swap.options_for(make_ctx(world, f), g, for_install=True)


def test_options_rdna3(world, game):
    o = options(world, game, (11, 0, 2))
    labels = [str(x.choice) for x in o.options]
    assert labels[0] == "official" and o.options[0].default and o.options[0].version == "4.1.2.9000"
    assert "official:4.1.1.9000" in labels and "official:4.1.1.2740" in labels
    assert [x for x in labels if x.startswith("cyboman")] == [
        "cyboman:exact", "cyboman:lossy", "cyboman:4.1.2l-exact", "cyboman:4.1.2l-lossy"]
    lossy = next(x for x in o.options if str(x.choice) == "cyboman:lossy")
    assert "WARNING.txt" in lossy.warning and "rx7000" in lossy.gpu
    lite = next(x for x in o.options if str(x.choice) == "cyboman:4.1.2l-exact")
    assert "Lite" in lite.tradeoff
    assert any(x.startswith("official 4.1.0.9000") for x in o.excluded)  # FP8 only


def test_options_rdna4_never_cyboman(world, game):
    o = options(world, game, (12, 0, 1))
    assert not any(str(x.choice).startswith("cyboman") for x in o.options)
    assert any("not for RX 9000" in x for x in o.excluded)
    assert "official:4.1.0.9000" in [str(x.choice) for x in o.options]  # FP8 DLLs are fine there


def test_options_rdna2_leaves_out_shimmering_411(world, game):
    o = options(world, game, (10, 3, 0))
    labels = [str(x.choice) for x in o.options]
    assert "official:4.1.1.9000" not in labels and any("shimmers" in x for x in o.excluded), o.excluded
    # the test cyboman release only has an rx7000 archive: said, not silently skipped or swapped for another
    assert not any(x.choice.kind == "cyboman" for x in o.options)
    assert "cyboman: release dll-t1 has no rx6000 archive" in o.excluded


# ------------------------------------------------------------------ install and swap

def test_default_is_newest_official_and_cyboman_gets_the_gpu_archive(game):
    world, m, f, ctx, g, root = game
    assert ops.install(ctx, g, apply.Request(), yes=True).ok
    assert slot(root) == official_bytes("4.1.2.9000", "dx12")
    assert fsr(root) | {} == {"kind": "official", "version": "4.1.2.9000", "choice": "official:4.1.2.9000",
                              "sha256": sha(official_bytes("4.1.2.9000", "dx12")), "pinned": False}
    st = json.loads((root / ".xemfg/state.json").read_text())
    assert st["options"]["fsr4"] == "official:4.1.2.9000" and st["options"]["gpu_profile"] == "rdna3"


def test_swap_cyboman_official_original_and_back(game):
    world, m, f, ctx, g, root = game
    ops.install(ctx, g, apply.Request(), yes=True)
    r = fsr4swap.swap(ctx, [g], "cyboman:exact", yes=True)
    assert r[0].ok and "cyboman exact from the rx7000 archive" in r[0].message
    assert slot(root) == m.get("t1").raw["cy"]["exact"] and (root / UP).with_suffix(".dll.orig").exists()
    assert fsr_keys(root) == ("2", "1") and fsr(root)["variant"] == "exact"
    assert not (root / VK).exists()  # cyboman has no Vulkan DLL; the (test) nightly has none either
    fsr4swap.swap(ctx, [g], "official:4.1.1.9000", yes=True)
    assert slot(root) == official_bytes("4.1.1.9000", "dx12") and not (root / UP).with_suffix(".dll.orig").exists()
    assert fsr_keys(root) == ("2", "1")
    fsr4swap.swap(ctx, [g], "original", yes=True)
    assert slot(root) is None and not (root / VK).exists()  # nothing was there before xemfg
    assert fsr(root)["kind"] == "original" and fsr_keys(root) == ("auto", "auto")
    rep = report(ctx, g)
    assert rep.apis[0].runtime.source in ("game", "none")
    # every DLL that was in the slot is kept
    kept = {e.meta.get("version") for e in ctx.store.entries()}
    assert {"4.1.2.9000", "4.1.1.9000", "dll-t1"} <= kept
    # rollback of the DLL alone, offline and with the cache gone
    import shutil

    shutil.rmtree(world.paths.cache_dir / "fsr4_official")
    ctx2 = make_ctx(world, FakeFetcher({k: v for k, v in f.routes.items() if "FidelityFX-SDK" not in k}))
    assert ops.rollback(ctx2, ctx2.resolve("Game"), yes=True, component="fsr4").ok
    assert slot(root) == official_bytes("4.1.1.9000", "dx12")
    uninstall.uninstall(ctx2, ctx2.resolve("Game"), yes=True)
    assert not (root / "OptiScaler").exists()


def test_original_restores_a_replaced_dll(world, tmp_path):
    m, f = setup_sets(world)
    mine = b"MZ some upscaler DLL that was here first"
    root = world.faugus_game("g", "Game", tmp_path / "G", {**TIER_FILES["native"], UP: mine})
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    ops.install(ctx, g, apply.Request(), yes=True)  # not cyboman: replaced (backed up), not kept
    assert slot(root) == official_bytes("4.1.2.9000", "dx12")
    fsr4swap.swap(ctx, [g], "original", yes=True)
    assert slot(root) == mine


def test_swap_several_games_one_confirmation(world, tmp_path):
    m, f = setup_sets(world)
    roots = [world.faugus_game(x, x.upper(), tmp_path / x, TIER_FILES["native"]) for x in ("a", "b")]
    ui = ScriptedUI()
    ctx = make_ctx(world, f, ui=ui)
    games = [ctx.resolve(x.upper()) for x in ("a", "b")]
    for g in games:
        ops.install(ctx, g, apply.Request(), yes=True)
    res = fsr4swap.swap(ctx, games, "cyboman:4.1.2l-lossy")
    assert all(r.ok for r in res) and ui.confirms[-1] == "Change the FSR4 DLL in 2 game(s)?"
    assert all(slot(r) == m.get("t1").raw["cy"]["4.1.2l-lossy"] for r in roots)
    assert any("changes the picture" in x for x in ui.messages)  # the lossy warning was in the plans


def test_gpu_rules(world, tmp_path):
    m, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    world.add_gpu(1, "0000:03:00.0", AMD, 0x7550, (12, 0, 1))  # RX 9070 XT
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    with pytest.raises(OpError, match="not offered for RDNA 4"):
        ops.install(ctx, g, apply.Request(fsr4="cyboman:exact", fsr4_explicit=True), yes=True)
    ops.install(ctx, g, apply.Request(), yes=True)
    assert fsr_keys(root) == ("auto", "0")  # FP8, detected by OptiScaler


def test_unknown_gpu_and_several_gpus(world, tmp_path):
    m, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    world.add_gpu(1, "0000:03:00.0", 0x10DE, 0x2684, None, driver="nvidia")
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    with pytest.raises(OpError, match="will not guess"):
        ops.install(ctx, g, apply.Request(), yes=True)
    assert ops.install(ctx, g, apply.Request(fsr4="nightly", fsr4_explicit=True), yes=True).ok
    assert fsr(root)["kind"] == "nightly" and fsr_keys(root) == ("auto", "auto")

    world.add_gpu(0, "0000:05:00.0", AMD, 0x15BF, (11, 0, 1))  # a 780M too: ask
    ui = ScriptedUI(picks=[0])
    ctx = make_ctx(world, f, ui=ui)
    fsr4swap.swap(ctx, [ctx.resolve("Game")], "official", yes=True)
    assert ctx.config.gpu == "0000:05:00.0" and fsr_keys(root) == ("4", "3")  # Lite on the 780M (AMD default)


def test_kept_dll_and_anticheat(world, tmp_path):
    m, f = setup_sets(world)
    mine = b"x 4.1.2l-lossy-r3-cyboman mine"
    root = world.faugus_game("g", "Game", tmp_path / "G", {**TIER_FILES["native"], UP: mine})
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    ops.install(ctx, g, apply.Request(), yes=True)
    assert slot(root) == mine and fsr(root)["kind"] == "kept"
    assert not fsr4swap.swap(ctx, [g], "official", yes=True)[0].ok
    assert slot(root) == mine
    assert fsr4swap.swap(ctx, [g], "official", replace_kept=True, yes=True)[0].ok
    assert slot(root) == official_bytes("4.1.2.9000", "dx12")
    fsr4swap.swap(ctx, [g], "original", yes=True)
    assert slot(root) == mine  # it was backed up

    (root / "EasyAntiCheat").mkdir()
    ctx = make_ctx(world, f)
    r = fsr4swap.swap(ctx, [ctx.resolve("Game")], "official", yes=True)
    assert not r[0].ok and "anti-cheat" in r[0].message


def test_update_and_pin_official(game, monkeypatch):
    world, m, f, ctx, g, root = game
    ops.install(ctx, g, apply.Request(fsr4="official:4.1.1.9000", fsr4_explicit=True), yes=True)
    rows, _ = components.rows(ctx, ctx.state(root))
    assert next(r for r in rows if r.component == "fsr4").status == "update"
    # repair keeps the exact version
    ops.repair(ctx, g, yes=True)
    assert slot(root) == official_bytes("4.1.1.9000", "dx12")
    components.pin(ctx, g, ["fsr4"])
    assert "fsr4 is pinned" in ops.update(ctx, g, yes=True, only=["fsr4"]).message
    components.pin(ctx, g, ["fsr4"], on=False)
    r = ops.update(ctx, g, yes=True, only=["fsr4"])
    assert r.message == "fsr4 official:4.1.1.9000 -> official:4.1.2.9000"
    assert slot(root) == official_bytes("4.1.2.9000", "dx12")


def test_existing_cyboman_install_is_never_switched(game):
    world, m, f, ctx, g, root = game
    ops.install(ctx, g, apply.Request(fsr4="exact-l", fsr4_explicit=True), yes=True)
    ops.update(ctx, g, yes=True)
    ops.repair(ctx, g, yes=True)
    assert fsr(root)["kind"] == "cyboman" and fsr(root)["variant"] == "4.1.2l-exact"
    rows, _ = components.rows(ctx, ctx.state(root))
    r = next(x for x in rows if x.component == "cyboman")
    assert r.status == "current"


def test_config_old_default_becomes_official(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('fsr4 = "exact-l"\ngpu_family = "rx7000"\n')
    assert Config.load(p).fsr4 == "official"
    p.write_text('schema = 2\nfsr4 = "cyboman:exact-l"\n')
    assert Config.load(p).fsr4 == "cyboman:exact-l"


def test_manifest_add_fsr4_checks_git_blob(world):
    import hashlib

    m, _ = setup_sets(world)
    data = fakepe.upscaler("4.1.3.1", ALL_MODELS)
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    sdk = "GPUOpen-LibrariesAndSDKs/FidelityFX-SDK"
    routes = {
        f"https://api.github.com/repos/{sdk}/commits/v9.9.9": {"sha": "abc", "commit": {"committer": {"date": "2027-01-01T0"}}},
        f"https://api.github.com/repos/{sdk}/contents/Kits/FidelityFX/signedbin?ref=abc":
            [{"name": "amd_fidelityfx_upscaler_dx12.dll", "sha": blob}],
        f"https://raw.githubusercontent.com/{sdk}/abc/Kits/FidelityFX/signedbin/amd_fidelityfx_upscaler_dx12.dll": data,
    }
    [d] = upstream.Upstream(FakeFetcher(routes)).official_dlls("v9.9.9")
    assert (d.version, d.models, d.sha256) == ("4.1.3.1", ALL_MODELS, sha(data))
    assert upstream.add_component(m, "fsr4", [d]) == ("4.1.3.1", True)
    routes[list(routes)[1]] = [{"name": "amd_fidelityfx_upscaler_dx12.dll", "sha": "0" * 40}]
    with pytest.raises(upstream.UpstreamError, match="git blob"):
        upstream.Upstream(FakeFetcher(routes)).official_dlls("v9.9.9")


def test_cli_fsr4(world, tmp_path, monkeypatch):
    m, f = setup_sets(world)
    for x in ("a", "b"):
        world.faugus_game(x, x.upper(), tmp_path / x, TIER_FILES["native"])
    from xemfg.core.context import Context

    monkeypatch.setattr(Context, "default", staticmethod(lambda ui: make_ctx(world, f, ui=ui)))
    run = CliRunner().invoke
    assert run(cli.app, ["install", "A", "--yes"]).exit_code == 0
    assert run(cli.app, ["install", "B", "--yes", "--fsr4", "cyboman:exact"]).exit_code == 0
    r = run(cli.app, ["install", "A", "--fsr4", "fsr3"])
    assert r.exit_code == 1 and "unknown FSR4 choice" in r.output
    r = run(cli.app, ["fsr4", "list", "A"])
    assert r.exit_code == 0 and "official:4.1.1.9000" in r.output and "cyboman:4.1.2l-lossy" in r.output
    assert "(installed)" in r.output and "not offered: official 4.1.0.9000" in r.output
    r = run(cli.app, ["fsr4", "set", "A", "B", "cyboman:lossy", "--yes"])
    assert "A: ok" in r.output and "B: ok" in r.output, r.output
    r = run(cli.app, ["list"])
    assert r.output.count("cyboman lossy") == 2
    r = run(cli.app, ["fsr4", "store"])
    assert "official" in r.output and "cyboman" in r.output
    r = run(cli.app, ["fsr4", "set", "cyboman:lossy"])
    assert r.exit_code == 1 and "--all" in r.output


def test_swap_changes_only_the_dll_keys(game):
    world, m, f, ctx, g, root = game
    ops.install(ctx, g, apply.Request(fsr4="cyboman:exact", fsr4_explicit=True), yes=True)
    path = root / "OptiScaler.ini"
    text = ini.read_text(path)
    ini.write_text(path, ini.plan_edit(text, {"FrameGen": {"Enabled": "false"}, "FSR": {"UpscalerIndex": "auto"}})[0])
    world.add_gpu(1, "0000:03:00.0", AMD, 0x73BF, (10, 3, 0))  # moved to an RX 6800
    ctx = make_ctx(world, f)
    fsr4swap.swap(ctx, [ctx.resolve("Game")], "official", yes=True)
    i = ini.Ini(ini.read_text(path))
    assert (i.get("FSR", "Fsr4GpuSupport"), i.get("FSR", "Fsr4Model")) == ("3", "2")  # RDNA2 model
    assert i.get("FrameGen", "Enabled") == "false" and i.get("FSR", "UpscalerIndex") == "auto"  # the user's


def test_dry_run_before_download_does_not_list_deletes(game):
    import shutil

    world, m, f, ctx, g, root = game
    ops.install(ctx, g, apply.Request(), yes=True)
    shutil.rmtree(world.paths.cache_dir)
    prep = fsr4swap.build_swap(make_ctx(world, f), g, "cyboman:exact", dry_run=True)
    deletes = [o.dest for o in prep.plan.ops if o.kind == "delete"]
    assert not any("libxess" in str(d) or "only_in_t1" in str(d) for d in deletes)
    assert any("compared once they are downloaded" in n for n in prep.plan.notes)
    assert not world.paths.cache_dir.exists() or not any(world.paths.cache_dir.rglob("*.zip"))
