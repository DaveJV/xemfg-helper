"""check (read-only): latest upstream pairing + cyboman vs the local manifest and every install."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..context import Context
from ..manifest import XSet
from ..upstream import Upstream


@dataclass
class CheckResult:
    latest: XSet | None
    local_current: str
    up_to_date: bool
    differences: list[str] = field(default_factory=list)
    outdated_installs: list[tuple[str, str]] = field(default_factory=list)  # (title, set)
    error: str | None = None


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
    m = ctx.manifest
    res = CheckResult(None, m.current, True)
    res.outdated_installs = [(e.title, e.set) for e in ctx.registry.active() if e.set != m.current]
    try:
        latest = Upstream(ctx.fetcher).resolve(log)
    except Exception as e:
        res.error = str(e)
        return res
    res.latest = latest
    known = next((sid for sid, s in m.sets.items() if s.same_content(latest)), None)
    if known != m.current:
        res.up_to_date = False
        res.differences = diff_sets(m.current_set, latest)
    return res


def refresh(ctx: Context, log=lambda s: None) -> tuple[str, bool]:
    from ..upstream import merge_into

    m = ctx.manifest
    latest = Upstream(ctx.fetcher).resolve(log)
    sid, changed = merge_into(m, latest)
    if changed or not ctx.paths.manifest_file.exists():
        m.save(ctx.paths.manifest_file)
    return sid, changed
