from pathlib import Path

import pytest

from xemfg.core import manifest, upstream
from xemfg.core.upstream import Upstream, UpstreamError

from fakes import FakeFetcher

DATA = Path(__file__).parent / "data"
GH = "https://github.com"
API = "https://api.github.com/repos"


def routes(api=True):
    r = {
        f"{GH}/dashdogy/MFG-Unlocker/releases/latest": f"{GH}/dashdogy/MFG-Unlocker/releases/tag/v1.7.2",
        "https://raw.githubusercontent.com/dashdogy/MFG-Unlocker/v1.7.2/docs/optiscaler.md": (DATA / "optiscaler.md").read_text(),
        f"{GH}/dashdogy/MFG-Unlocker/releases/download/v1.7.2/SHA256SUMS.txt": (DATA / "mfgu-sums.txt").read_text(),
        f"{GH}/dashdogy/OptiScaler/releases/expanded_assets/mfgunlocker-v1.7.2": (DATA / "expanded_assets_dashdogy.html").read_text(),
        f"{GH}/optiscaler/OptiScaler-nightly/releases/expanded_assets/nightly-20261122":
            '<a href="/optiscaler/OptiScaler-nightly/releases/download/nightly-20261122/OptiScaler_v10.0.0-pre1_20261122_ab1f3d49.7z">x</a>'
            ' sha256:6de71eb7fbef58ac3d6621c4afce4032b5c1de82be691c5118aa032452ad1b88',
        f"{GH}/zgauthier2000/fsr4-shader-performance-overrides/releases":
            '<a href="/zgauthier2000/fsr4-shader-performance-overrides/releases/tag/dll-2026-11-22">',
        f"{GH}/zgauthier2000/fsr4-shader-performance-overrides/releases/download/dll-2026-11-22/SHA256SUMS":
            (DATA / "cy-sums.txt").read_text(),
    }
    if api:
        r[f"{API}/dashdogy/MFG-Unlocker/releases/latest"] = {"tag_name": "v1.7.2"}
    return r


def bundled():
    return manifest.Manifest.parse(manifest.bundled_text())


def test_resolve_with_api_blocked_matches_bundled():
    s = Upstream(FakeFetcher(routes(api=False))).resolve()
    assert s.same_content(bundled().current_set)
    assert s.optiscaler.verified_by == "html-digest"


def test_resolve_via_api():
    r = routes()
    r[f"{API}/dashdogy/OptiScaler/releases/tags/mfgunlocker-v1.7.2"] = {"assets": [
        {"name": "OptiScaler.dll", "digest": "sha256:473f7a3aef9632f797e7ab39492d5ad827822e80331103b53e12addee58a7bc1"}]}
    s = Upstream(FakeFetcher(r)).resolve()
    assert s.optiscaler.verified_by == "api-digest"
    assert s.same_content(bundled().current_set)


def test_tofu_when_no_digest():
    r = routes(api=False)
    r[f"{GH}/dashdogy/OptiScaler/releases/expanded_assets/mfgunlocker-v1.7.2"] = \
        '<a href="/dashdogy/OptiScaler/releases/download/mfgunlocker-v1.7.2/OptiScaler.dll">'
    r[f"{GH}/dashdogy/OptiScaler/releases/download/mfgunlocker-v1.7.2/OptiScaler.dll"] = b"dll-bytes"
    s = Upstream(FakeFetcher(r)).resolve()
    assert s.optiscaler.verified_by == "tofu"
    import hashlib
    assert s.optiscaler.sha256 == hashlib.sha256(b"dll-bytes").hexdigest()


def test_docs_must_name_the_pairing():
    with pytest.raises(UpstreamError):
        upstream.parse_which_build("## Which OptiScaler build\nuse any build\n## Next\n", "v1.7.2")
    with pytest.raises(UpstreamError):
        upstream.parse_which_build((DATA / "optiscaler.md").read_text(), "v1.8.0")  # docs talk about V1.7.2


def test_merge_new_cyboman_gets_suffixed_id():
    m = bundled()
    s = Upstream(FakeFetcher(routes())).resolve()
    sid, changed = upstream.merge_into(m, s)
    assert (sid, changed) == ("v1.7.2", False)
    s2 = Upstream(FakeFetcher(routes())).resolve()
    s2.cyboman_tag = "dll-2026-12-01"
    sid, changed = upstream.merge_into(m, s2)
    assert sid == "v1.7.2+dll-2026-12-01" and changed and m.current == sid


def test_manifest_roundtrip(tmp_path):
    m = bundled()
    m.save(tmp_path / "m.toml")
    m2 = manifest.load(tmp_path / "m.toml")
    assert m2.current_set.same_content(m.current_set)
    fam = m2.current_set.family("rx7000")
    assert fam.variant_dir("exact-l") == "4.1.2l-exact"
    assert fam.variant_dir("lossy") == "lossy"
    assert fam.variant_dir("none") is None
