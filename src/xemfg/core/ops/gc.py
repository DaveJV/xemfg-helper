"""gc: delete cache sets no install uses, stale downloads, and xemfg backups that are no longer referenced."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..context import Context
from ..plan import Plan
from ..state import STATE_DIR


@dataclass
class GcResult:
    plan: Plan
    kept_sets: set[str] = field(default_factory=set)
    freed: int = 0


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def build_gc(ctx: Context) -> GcResult:
    m = ctx.manifest
    keep = set(ctx.registry.sets_in_use())
    keep.add(m.current)
    ids = m.ordered_ids()
    if m.current in ids and ids.index(m.current) > 0:
        keep.add(ids[ids.index(m.current) - 1])  # the previous current, for rollback
    plan = Plan("gc: clean xemfg's cache and backups")
    res = GcResult(plan, keep)
    cache = ctx.paths.cache_dir
    if cache.is_dir():
        for d in sorted(cache.iterdir()):
            if not d.is_dir():
                continue
            is_set = (d / "nightly").is_dir() or (d / "OptiScaler-mfgu.dll").exists() or d.name in m.sets
            if is_set and d.name not in keep:
                for f in sorted(d.rglob("*"), reverse=True):
                    if f.is_file() or f.is_symlink():
                        plan.delete(f)
                    else:
                        plan.rmdir(f)
                plan.rmdir(d, note=f"unused set ({_size(d) >> 20} MB)")
                res.freed += _size(d)
        for part in cache.rglob("*.part"):
            plan.delete(part, note="stale download")
            res.freed += _size(part)
    # backups xemfg made that the state no longer points to (left after successful updates)
    for e in ctx.registry.active():
        exe_dir = Path(e.exe_dir)
        st = ctx.state(exe_dir)
        bdir = exe_dir / STATE_DIR / "backups"
        if not st or not bdir.is_dir():
            continue
        refs = {mf.backup for mf in st.files.values() if mf.backup} | ({st.ini.backup} if st.ini.backup else set())
        for f in sorted(bdir.rglob("*"), reverse=True):
            rel = f.relative_to(exe_dir).as_posix()
            if f.is_file() and rel not in refs:
                plan.delete(f, note=f"unreferenced backup ({e.title})")
                res.freed += _size(f)
            elif f.is_dir() and not any(r.startswith(rel + "/") for r in refs):
                plan.rmdir(f)
    return res
