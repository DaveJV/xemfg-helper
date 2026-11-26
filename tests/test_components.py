"""Per-component update, pin and rollback, check rows, gc keeping used versions, cache reuse."""
import json

import pytest
from typer.testing import CliRunner

from xemfg import cli
from xemfg.core import upstream
from xemfg.core.ops import apply, components, gc, install as ops
from xemfg.core.ops.common import OpError
from xemfg.core.ops.health import list_health
from xemfg.core.ops.run import run_plan

from fakes import FakeFetcher, TIER_FILES
from fakeset import make_ctx, setup_sets, sha
from test_upstream import routes as upstream_routes

UP = "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"


@pytest.fixture
def on_t1(world, tmp_path):
    """A game installed on t1; the manifest then moves on to t2 (as `manifest refresh` would)."""
    m, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    assert ops.install(ctx, g, apply.Request(fsr4="cyboman:exact-l"), yes=True).ok
    m.current = "t2"
    m.set_latest_from("t2")
    m.save(world.paths.manifest_file)
    ctx.reload_manifest()
    return world, m, f, ctx, g, root


def versions(root):
    d = json.loads((root / ".xemfg/state.json").read_text())
    return {k: v["version"] for k, v in d["components"].items()}, d


def test_update_one_component_is_an_unverified_mix(on_t1):
    world, m, f, ctx, g, root = on_t1
    up = components.plan_update(ctx, g, only=["mfgu"])
    assert up.changes == {"mfgu": ("v-t1", "v-t2")} and up.prep.set_id == "custom"
    assert any("UNVERIFIED COMBINATION" in w for w in up.prep.plan.warnings)
    assert "documented with MFG Unlocker v-t1, not v-t2" in up.prep.unverified[0]
    with pytest.raises(OpError, match="--allow-unverified"):
        ops.update(ctx, g, yes=True, only=["mfgu"])
    assert versions(root)[0]["mfgu"] == "v-t1"  # nothing changed
    r = ops.update(ctx, g, yes=True, only=["mfgu"], allow_unverified=True)
    assert r.ok and r.message == "mfgu v-t1 -> v-t2"
    v, d = versions(root)
    assert v == {"nightly": "nightly-t1", "optiscaler": "mfgu-t1", "mfgu": "v-t2", "fsr4": "dll-t1"}
    assert d["set"] == "custom"
    assert (root / "OptiScaler/plugins/MFGUnlocker.asi").read_bytes() == m.get("t2").raw["mfgu_dll"]
    assert sha((root / "dxgi.dll").read_bytes()) == m.get("t1").optiscaler.sha256
    assert (root / "OptiScaler/only_in_t1.dll").exists()  # nightly untouched
    h = list_health(ctx)[0]
    assert any("unverified combination" in x.message for x in h.findings)


def test_cyboman_alone_needs_no_pairing(on_t1):
    world, m, f, ctx, g, root = on_t1
    r = ops.update(ctx, g, yes=True, only=["cyboman"])
    assert r.message == "cyboman dll-t1 -> dll-t2"
    assert (root / UP).read_bytes() == m.get("t2").raw["cy"]["4.1.2l-exact"]
    v, d = versions(root)
    assert v["nightly"] == "nightly-t1" and d["set"] == "t1"  # still the documented t1 pairing
    assert d["components"]["fsr4"]["variant"] == "4.1.2l-exact"


def test_pin_skips_and_stops_nagging(on_t1):
    world, m, f, ctx, g, root = on_t1
    assert components.pin(ctx, g, ["optiscaler", "nightly", "mfgu"]) == [
        "optiscaler mfgu-t1", "nightly nightly-t1", "mfgu v-t1"]
    r = ops.update(ctx, g, yes=True)
    assert r.message.startswith("cyboman dll-t1 -> dll-t2") and "optiscaler is pinned at mfgu-t1" in r.message
    v, d = versions(root)
    assert v["optiscaler"] == "mfgu-t1" and v["fsr4"] == "dll-t2" and d["components"]["optiscaler"]["pinned"]
    rows, unverified = components.rows(ctx, ctx.state(root))
    assert {r.component: r.status for r in rows} == {"nightly": "pinned", "optiscaler": "pinned", "mfgu": "pinned",
                                                     "cyboman": "current"}
    assert list_health(ctx)[0].status == "ok"
    reg = json.loads(world.paths.registry_file.read_text())["installs"]["faugus:g"]
    assert reg["components"]["nightly"]["pinned"]
    # an explicit version still goes through, and unpinning brings updates back
    components.pin(ctx, g, ["optiscaler", "nightly", "mfgu"], on=False)
    assert list_health(ctx)[0].status == "outdated"
    assert ops.update(ctx, g, yes=True).ok
    assert versions(root)[0]["optiscaler"] == "mfgu-t2"


def test_rollback_one_component_then_all(on_t1):
    world, m, f, ctx, g, root = on_t1
    ops.update(ctx, g, yes=True)
    assert versions(root)[0] == {"nightly": "nightly-t2", "optiscaler": "mfgu-t2", "mfgu": "v-t2", "fsr4": "dll-t2"}
    r = ops.rollback(ctx, g, yes=True, component="cyboman")
    assert r.message == "cyboman dll-t2 -> dll-t1" and (root / UP).read_bytes() == m.get("t1").raw["cy"]["4.1.2l-exact"]
    with pytest.raises(OpError, match="allow-unverified"):
        ops.rollback(ctx, g, yes=True, component="mfgu")
    r = ops.rollback(ctx, g, yes=True, component="mfgu", allow_unverified=True)
    assert r.message == "mfgu v-t2 -> v-t1"
    assert versions(root)[0] == {"nightly": "nightly-t2", "optiscaler": "mfgu-t2", "mfgu": "v-t1", "fsr4": "dll-t1"}
    with pytest.raises(OpError, match="not in the manifest"):
        ops.rollback(ctx, g, yes=True, component="nightly", to="nightly-t9")


def test_gc_keeps_every_version_a_game_used(on_t1):
    world, m, f, ctx, g, root = on_t1
    ops.update(ctx, g, yes=True)  # the only game is now on t2; t1 is in its history
    keep, sets = gc.versions_in_use(ctx)
    assert keep["nightly"] == {"nightly-t1", "nightly-t2"} and keep["cyboman"] == {"dll-t1", "dll-t2"}
    res = gc.build_gc(ctx)
    deleted = [str(o.dest) for o in res.plan.ops if o.kind == "delete"]
    assert not any("nightly-t1_" in d or "/cyboman/dll-t1" in d for d in deleted)
    run_plan(ctx, res.plan, yes=True)
    ctx2 = make_ctx(world, FakeFetcher({}))  # offline: rollback must come from the kept cache
    assert ops.rollback(ctx2, ctx2.resolve("Game"), yes=True).ok
    assert versions(root)[0]["nightly"] == "nightly-t1"


def test_reuses_files_from_old_per_set_folders(world, tmp_path):
    m, f = setup_sets(world)
    t1 = m.get("t1")
    old = world.paths.cache_dir / "v1.7.2"  # xemfg 0.1 / xemfg-linux.sh layout
    old.mkdir(parents=True)
    for spec in (t1.nightly, t1.optiscaler, t1.mfgu):
        (old / spec.filename).write_bytes(f.routes[spec.url])
    (old / "cyboman.zip").write_bytes(f.routes[t1.cyboman_url("cy-rx7000-t1.zip")])
    world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    ctx = make_ctx(world, FakeFetcher({}))  # no network at all
    assert ops.install(ctx, ctx.resolve("Game"), apply.Request(fsr4="cyboman:exact-l"), yes=True).ok
    assert (world.paths.cache_dir / "optiscaler").is_dir()
    # gc keeps the old folder while an install's history names it, and drops it otherwise
    assert "v1.7.2" not in gc.versions_in_use(ctx)[1]
    assert any(str(o.dest).startswith(str(old)) for o in gc.build_gc(ctx).plan.ops)


def test_manifest_add_component():
    m = __import__("xemfg.core.manifest", fromlist=["x"]).Manifest.parse(
        __import__("xemfg.core.manifest", fromlist=["x"]).bundled_text())
    up = upstream.Upstream(FakeFetcher(upstream_routes(api=False)))
    spec = up.component("nightly", "nightly-20261122")
    assert upstream.add_component(m, "nightly", spec) == ("nightly-20261122", False)
    spec.sha256 = "e" * 64
    assert upstream.add_component(m, "nightly", spec) == ("nightly-20261122+eeeeeeee", True)
    cy = up.component("cyboman", "dll-2026-11-22")
    assert upstream.add_component(m, "cyboman", cy) == ("dll-2026-11-22", False)
    with pytest.raises(upstream.UpstreamError):
        up.component("nope", "x")


def test_parse_components():
    assert components.parse_components("nightly, plugin,opti") == ["nightly", "mfgu", "optiscaler"]
    assert components.parse_components(["cyboman"], components.PINNABLE) == ["fsr4"]
    with pytest.raises(OpError):
        components.parse_components("ffx")


def test_cli_check_pin_update(on_t1, monkeypatch):
    world, m, f, ctx, g, root = on_t1
    from xemfg.core.context import Context

    monkeypatch.setattr(Context, "default", staticmethod(lambda ui: make_ctx(world, f, ui=ui)))
    run = CliRunner().invoke
    r = run(cli.app, ["check"])
    assert r.exit_code == 0, r.output
    assert "could not read" in r.output and "Game" in r.output
    assert "nightly-t1" in r.output and "nightly-t2" in r.output and "update" in r.output
    r = run(cli.app, ["pin", "Game", "-c", "nightly", "-c", "cyboman"])
    assert "pinned nightly nightly-t1; fsr4 cyboman 4.1.2l-exact dll-t1 (rx7000)" in r.output
    r = run(cli.app, ["update", "Game", "--only", "nightly", "--no-refresh", "--yes"])
    assert "nightly is pinned" in r.output
    r = run(cli.app, ["update", "Game", "--only", "cyboman", "--to", "dll-t2", "--yes"])
    assert "cyboman dll-t1 -> dll-t2" in r.output  # an explicit version goes through a pin
    r = run(cli.app, ["update", "Game", "--to", "x"])
    assert r.exit_code == 1 and "--to needs exactly one component" in r.output
    r = run(cli.app, ["rollback", "Game", "-c", "cyboman", "--yes"])
    assert "cyboman dll-t2 -> dll-t1" in r.output
    r = run(cli.app, ["manifest", "show"])
    assert "nightly-t1, nightly-t2" in r.output
