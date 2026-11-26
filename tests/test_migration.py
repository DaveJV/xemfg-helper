"""Schema 1 -> 2: manifest, per-game state and registry. Old files are kept; game files are never touched."""
import json

import pytest

from xemfg.core import fsr4, manifest
from xemfg.core.manifest import Manifest, ManifestError
from xemfg.core.ops import apply, install as ops
from xemfg.core.ops.health import list_health

from fakes import TIER_FILES
from fakeset import make_ctx, setup_sets, sha

UP = "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".xemfg" not in p.parts}


def to_v1(world, root):
    """Rewrite state.json and registry.json as xemfg 0.1.0 wrote them."""
    sp = root / ".xemfg/state.json"
    d = json.loads(sp.read_text())
    for k in ("components", "gpu"):
        d.pop(k)
    d["schema"] = 1
    for h in d["history"]:
        h.pop("components", None)
    sp.write_text(json.dumps(d))
    rp = world.paths.registry_file
    r = json.loads(rp.read_text())
    r["schema"] = 1
    for e in r["installs"].values():
        e.pop("components")
    rp.write_text(json.dumps(r))
    return sp.read_text(), rp.read_text()


@pytest.fixture
def v1_install(world, tmp_path):
    m, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    assert ops.install(ctx, ctx.resolve("Game"), apply.Request(fsr4="cyboman:exact-l"), yes=True).ok
    old_state, old_reg = to_v1(world, root)
    return world, m, f, root, old_state, old_reg


def test_new_installs_are_schema_2(world, tmp_path):
    m, f = setup_sets(world)
    world.add_gpu(1, "0000:03:00.0", 0x1002, 0x7480, (11, 0, 2))
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    ops.install(ctx, ctx.resolve("Game"), apply.Request(fsr4="cyboman:exact-l"), yes=True)
    d = json.loads((root / ".xemfg/state.json").read_text())
    assert d["schema"] == 2
    assert {k: v["version"] for k, v in d["components"].items()} == {
        "nightly": "nightly-t1", "optiscaler": "mfgu-t1", "mfgu": "v-t1", "fsr4": "dll-t1"}
    assert d["components"]["fsr4"] | {} == {"kind": "cyboman", "version": "dll-t1", "variant": "4.1.2l-exact",
                                             "family": "rx7000", "pinned": False, "choice": "cyboman:exact-l",
                                             "sha256": sha(m.get("t1").raw["cy"]["4.1.2l-exact"])}
    assert d["gpu"] == {"profile": "rdna3", "slot": "0000:03:00.0", "chip": "Navi 33", "override": False}
    assert d["history"][-1]["components"] == {"nightly": "nightly-t1", "optiscaler": "mfgu-t1", "mfgu": "v-t1",
                                              "fsr4_kind": "cyboman", "fsr4": "cyboman:exact-l", "cyboman": "dll-t1",
                                              "cyboman_variant": "4.1.2l-exact"}
    reg = json.loads(world.paths.registry_file.read_text())
    assert reg["schema"] == 2 and reg["installs"]["faugus:g"]["components"]["fsr4"]["variant"] == "4.1.2l-exact"
    assert not (root / ".xemfg/state.v1.json").exists() and not world.paths.registry_file.with_name(
        "registry.v1.json").exists()


def test_v1_state_and_registry_read_in_memory_only(v1_install):
    world, m, f, root, old_state, old_reg = v1_install
    ctx = make_ctx(world, f)
    st = ctx.state(root)
    assert st.schema == 1 and st.components["nightly"]["version"] == "nightly-t1"
    assert st.components["fsr4"]["kind"] == "cyboman" and st.components["fsr4"]["variant"] == "4.1.2l-exact"
    assert st.gpu["cyboman_family"] == "rx7000"
    e = ctx.registry.get("faugus:g")
    assert e.components["optiscaler"]["version"] == "mfgu-t1"
    h = list_health(ctx)[0]
    assert h.status == "ok" and any("old format" in x.message for x in h.findings)
    # reading wrote nothing
    assert (root / ".xemfg/state.json").read_text() == old_state
    assert world.paths.registry_file.read_text() == old_reg


def test_first_write_upgrades_and_keeps_v1_files(v1_install):
    world, m, f, root, old_state, old_reg = v1_install
    before = snapshot(root)
    ctx = make_ctx(world, f)
    g = ctx.resolve("Game")
    prep = apply.build(ctx, g, apply.Request(mode="repair"))
    assert prep.plan.ops == []  # nothing in the game folder changes
    prep.plan.execute()
    assert snapshot(root) == before
    d = json.loads((root / ".xemfg/state.json").read_text())
    assert d["schema"] == 2 and d["components"]["mfgu"]["version"] == "v-t1"
    assert (root / ".xemfg/state.v1.json").read_text() == old_state
    assert world.paths.registry_file.with_name("registry.v1.json").read_text() == old_reg
    assert json.loads(world.paths.registry_file.read_text())["schema"] == 2
    # the backups are written once only
    apply.build(make_ctx(world, f), g, apply.Request(mode="repair")).plan.execute()
    assert (root / ".xemfg/state.v1.json").read_text() == old_state


def test_v1_none_and_kept_slots(world, tmp_path, monkeypatch):
    m, f = setup_sets(world)
    root = world.faugus_game("g", "Game", tmp_path / "G", TIER_FILES["native"])
    ctx = make_ctx(world, f)
    ops.install(ctx, ctx.resolve("Game"), apply.Request(fsr4="none", fsr4_explicit=True), yes=True)
    to_v1(world, root)
    st = make_ctx(world, f).state(root)
    assert st.components["fsr4"] == {"kind": "nightly", "version": "nightly-t1", "pinned": False,
                                     "sha256": sha(b"stock fsr t1"), "choice": "nightly"}
    h = sha(b"stock fsr t1")
    monkeypatch.setitem(fsr4.OFFICIAL_BY_SHA, h, fsr4.OfficialDll("4.1.2.4555", "dx12", "v2.4.0", "c", h, "", ()))
    assert make_ctx(world, f).state(root).components["fsr4"]["kind"] == "official"

    d = json.loads((root / ".xemfg/state.json").read_text())
    d["files"][UP]["action"] = "kept"
    d["options"]["fsr4"] = "exact-l"
    (root / ".xemfg/state.json").write_text(json.dumps(d))
    assert make_ctx(world, f).state(root).components["fsr4"]["kind"] == "kept"


def test_unknown_set_is_left_alone(v1_install):
    world, m, f, root, *_ = v1_install
    d = json.loads((root / ".xemfg/state.json").read_text())
    d["set"] = "v0.9-gone"
    (root / ".xemfg/state.json").write_text(json.dumps(d))
    ctx = make_ctx(world, f)
    st = ctx.state(root)
    assert st.components == {} and st.schema == 1
    st.save(root)  # a save without components does not claim schema 2
    assert json.loads((root / ".xemfg/state.json").read_text())["schema"] == 1
    e = ctx.registry.entries["faugus:g"]
    e.set = "v0.9-gone"
    assert any("not in the manifest" in x.message for x in list_health(ctx)[0].findings)


# ------------------------------------------------------------------ manifest

def test_manifest_v1_user_copy_converts_and_keeps_backup(world, tmp_path):
    v1 = (tmp_path / "v1.toml")
    v1.write_text(
        'schema = 1\ncurrent = "a"\n[sets.a]\nmfgu_version = "v1"\ncreated = "2026-01-01"\n'
        '[sets.a.files.nightly]\nrepo = "o/n"\ntag = "nightly-1"\nasset = "n.7z"\nsha256 = "' + "1" * 64 + '"\n'
        '[sets.a.files.optiscaler]\nrepo = "d/o"\ntag = "mfgunlocker-v1"\nasset = "OptiScaler.dll"\nsha256 = "' + "2" * 64 + '"\n'
        '[sets.a.files.mfgu]\nrepo = "d/m"\ntag = "v1"\nasset = "m.zip"\nsha256 = "' + "3" * 64 + '"\n'
        '[sets.a.cyboman]\nrepo = "z/c"\ntag = "dll-1"\n'
        '[sets.a.cyboman.families.rx7000]\narchives = [{asset = "c.7z", sha256 = "' + "4" * 64 + '"}]\n'
        'dlls = {exact = "' + "5" * 64 + '"}\n')
    m = manifest.load(v1)
    assert m.current == "a" and m.pairings["a"].nightly == "nightly-1"
    assert m.latest == {"nightly": "nightly-1", "optiscaler": "mfgunlocker-v1", "mfgu": "v1", "cyboman": "dll-1"}
    assert "v1.7.2" in m.pairings  # bundled pairings merged in
    assert m.get("a").family("rx7000").dlls == {"exact": "5" * 64}
    m.save(v1)
    assert v1.with_name("v1.v1.toml").exists()
    m2 = Manifest.parse(v1.read_text())
    assert m2.get("a").same_content(m.get("a")) and m2.latest == m.latest


def test_manifest_same_tag_new_file_and_errors(world):
    m, _ = setup_sets(world)
    xs = m.get("t2")
    xs.id = "t3"
    xs.files["nightly"].sha256 = "f" * 64  # upstream replaced the asset under the same tag
    m.add_set(xs)
    assert m.pairings["t3"].nightly == "nightly-t2+ffffffff"
    assert m.pairings["t2"].nightly == "nightly-t2"
    assert m.pairing_for({"nightly": "nightly-t1", "optiscaler": "mfgu-t1", "mfgu": "v-t1"}) == "t1"
    assert m.pairing_for({"nightly": "nightly-t2", "optiscaler": "mfgu-t1", "mfgu": "v-t1"}) is None
    with pytest.raises(ManifestError):
        Manifest.parse('schema = 3\ncurrent = "x"\n')
    with pytest.raises(ManifestError):
        m.component("nightly", "nope")


def test_bundled_manifest_is_schema_2():
    m = Manifest.parse(manifest.bundled_text())
    p = m.pairings["v1.7.2"]
    assert (p.nightly, p.optiscaler, p.mfgu, p.cyboman) == ("nightly-20261122", "mfgunlocker-v1.7.2", "v1.7.2",
                                                           "dll-2026-11-22")
    assert sorted(m.get("v1.7.2").families) == ["igpu-rdna2", "igpu-rdna3", "rx6000", "rx7000"]
