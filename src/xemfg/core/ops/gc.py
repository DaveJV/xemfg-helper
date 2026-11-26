"""gc: delete cache sets no install uses, stale downloads, and xemfg backups that are no longer referenced."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..context import Context
from ..cache import component_dir
from ..manifest import COMPONENTS, FILE_COMPONENTS
from ..plan import Plan
from ..state import STATE_DIR


@dataclass
class GcResult:
    plan: Plan
    kept: dict[str, set[str]] = field(default_factory=dict)  # component -> versions kept
    kept_sets: set[str] = field(default_factory=set)  # old per-set cache folders kept
    freed: int = 0

    def summary(self) -> str:
        parts = [f"{k} {', '.join(sorted(v))}" for k, v in sorted(self.kept.items()) if v]
        if self.kept_sets:
            parts.append(f"old set folders {', '.join(sorted(self.kept_sets))}")
        return "; ".join(parts)


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def versions_in_use(ctx: Context) -> tuple[dict[str, set[str]], set[str]]:
    """Every component version an installed game uses, had (its history: rollback targets) or is pinned to,
    plus the newest known ones. Returns (component -> versions, set ids still referenced)."""
    m = ctx.manifest
    keep: dict[str, set[str]] = {k: set() for k in COMPONENTS}
    sets: set[str] = set()

    def add(versions: dict) -> None:
        for k, v in versions.items():
            if v:
                keep.setdefault(k, set()).add(v)

    add(m.newest())
    for e in ctx.registry.active():
        sets.update([e.set, *e.previous_sets])
        st = ctx.state(Path(e.exe_dir))
        comps = st.components if st else e.components
        add({k: c.get("version") for k, c in comps.items() if k in FILE_COMPONENTS})
        fsr = comps.get("fsr4", {})
        if fsr.get("kind") == "cyboman":
            add({"cyboman": fsr.get("version")})
        for h in (st.history if st else []):
            sets.add(h.get("set", ""))
            hc = h.get("components") or {}
            add({k: v for k, v in hc.items() if k in COMPONENTS})
    for sid in list(sets):
        if sid in m.pairings:
            add(m.versions_of(sid))
    sets.discard("")
    return keep, sets


def _delete_tree(plan: Plan, d: Path, note: str) -> None:
    for f in sorted(d.rglob("*"), reverse=True):
        if f.is_file() or f.is_symlink():
            plan.delete(f)
        else:
            plan.rmdir(f)
    plan.rmdir(d, note=note)


def build_gc(ctx: Context) -> GcResult:
    m = ctx.manifest
    keep, sets = versions_in_use(ctx)
    plan = Plan("gc: clean xemfg's cache and backups")
    res = GcResult(plan, keep)
    cache = ctx.paths.cache_dir
    if cache.is_dir():
        for kind in FILE_COMPONENTS:
            kdir = cache / kind
            wanted = {component_dir(cache, m.component(kind, v)).name
                      for v in keep.get(kind, ()) if v in m.components.get(kind, {})}
            for d in sorted(kdir.iterdir()) if kdir.is_dir() else []:
                if d.is_dir() and d.name not in wanted:
                    size = _size(d)
                    _delete_tree(plan, d, f"unused {kind} {d.name} ({size >> 20} MB)")
                    res.freed += size
        cdir = cache / "cyboman"
        for d in sorted(cdir.iterdir()) if cdir.is_dir() else []:
            if d.is_dir() and d.name not in keep.get("cyboman", set()):
                size = _size(d)
                _delete_tree(plan, d, f"unused cyboman {d.name} ({size >> 20} MB)")
                res.freed += size
        for d in sorted(cache.iterdir()):  # per-set folders of xemfg 0.1 and xemfg-linux.sh
            if not d.is_dir() or d.name in (*FILE_COMPONENTS, "cyboman"):
                continue
            is_set = (d / "nightly").is_dir() or (d / "OptiScaler-mfgu.dll").exists() or d.name in m.pairings
            if not is_set:
                continue
            if d.name in sets:
                res.kept_sets.add(d.name)
                continue
            size = _size(d)
            _delete_tree(plan, d, f"unused old set folder ({size >> 20} MB)")
            res.freed += size
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
