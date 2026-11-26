"""check (read-only): latest upstream pairing + cyboman vs the local manifest and every install."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..context import Context
from ..manifest import Manifest, XSet
from ..upstream import Upstream, merge_into


@dataclass
class GameCheck:
    title: str
    key: str
    rows: list  # components.Row
    unverified: list[str]


@dataclass
class CheckResult:
    latest: XSet | None
    local_current: str
    up_to_date: bool
    differences: list[str] = field(default_factory=list)
    outdated_installs: list[tuple[str, str]] = field(default_factory=list)  # (title, set)
    error: str | None = None
    games: list[GameCheck] = field(default_factory=list)
    newest_nightly: str | None = None  # newest OptiScaler nightly upstream (may be newer than the paired one)
    newest_sdk: str | None = None  # newest AMD FSR SDK release
    sdk_known: bool = True  # whether xemfg's catalog already has that SDK's DLLs


def diff_sets(local: XSet, new: XSet) -> list[str]:
    out = []
    for k in ("nightly", "optiscaler", "mfgu"):
        a, b = local.files[k], new.files[k]
        if (a.tag, a.sha256) != (b.tag, b.sha256):
            out.append(f"{k}: {a.tag} -> {b.tag}" + (" (same tag, new file!)" if a.tag == b.tag else ""))
    if local.cyboman_tag != new.cyboman_tag:
        out.append(f"cyboman FSR4: {local.cyboman_tag} -> {new.cyboman_tag}")
    elif any(local.families.get(n) is None or local.families[n].dlls != f.dlls for n, f in new.families.items()):
        out.append("cyboman FSR4: same tag, DLL hashes changed")
    return out


def check(ctx: Context, log=lambda s: None) -> CheckResult:
    """Read-only: what upstream has now, and per game each component installed vs the newest.

    The per-game comparison uses upstream's current pairing when it could be read, otherwise the manifest.
    Nothing is saved; `xemfg update` (or `manifest refresh`) does that.
    """
    from .. import fsr4
    from .components import rows

    m = ctx.manifest
    res = CheckResult(None, m.current, True)
    res.outdated_installs = [(e.title, e.set) for e in ctx.registry.active() if e.set != m.current]
    up = Upstream(ctx.fetcher)
    newest = m.newest()
    try:
        latest = up.resolve(log)
        res.latest = latest
        known = next((sid for sid, s in m.sets.items() if s.same_content(latest)), None)
        if known != m.current:
            res.up_to_date = False
            res.differences = diff_sets(m.current_set, latest)
        tmp = Manifest(m.current, {k: dict(v) for k, v in m.components.items()}, dict(m.pairings), dict(m.latest))
        merge_into(tmp, latest)
        newest = tmp.newest()
    except Exception as e:
        res.error = str(e)
    for name, fn in (("newest_nightly", up.newest_nightly), ("newest_sdk", up.newest_sdk)):
        try:
            setattr(res, name, fn())
        except Exception as e:
            log(f"could not read the {name.replace('_', ' ')}: {e}")
    if res.newest_sdk:
        res.sdk_known = any(d.sdk_tag == res.newest_sdk for d in fsr4.OFFICIAL)
    for e in ctx.registry.active():
        st = ctx.state(Path(e.exe_dir))
        if st is None or not st.components:
            continue
        r, unverified = rows(ctx, st, newest)
        res.games.append(GameCheck(e.title, e.key, r, unverified))
    return res


def refresh(ctx: Context, log=lambda s: None) -> tuple[str, bool]:
    from ..upstream import merge_into

    m = ctx.manifest
    latest = Upstream(ctx.fetcher).resolve(log)
    sid, changed = merge_into(m, latest)
    if changed or not ctx.paths.manifest_file.exists():
        m.save(ctx.paths.manifest_file)
    return sid, changed
