"""Schema 1 -> 2 for per-game state and registry entries.

Schema 1 recorded one "set" (a pairing of nightly + dashdogy OptiScaler.dll + MFG Unlocker + cyboman release)
and an --fsr4 option. Schema 2 records each component's version on its own, so components can be updated,
pinned and rolled back separately, plus what is in OptiScaler's FSR4 slot.

Migration happens in memory when a state or the registry is loaded; it is written on the next change, and
the schema 1 file is kept beside it (state.v1.json, registry.v1.json). No game file is touched.
"""
from __future__ import annotations

from .fsr4 import OFFICIAL_BY_SHA
from .manifest import FILE_COMPONENTS, Manifest, ManifestError
from .registry import Entry, Registry
from .state import GameState

UPSCALER_DLL = "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"


def file_components(manifest: Manifest, set_id: str) -> dict[str, dict]:
    p = manifest.pairings.get(set_id)
    if p is None:
        return {}
    return {k: {"version": getattr(p, k), "pinned": False} for k in FILE_COMPONENTS}


def fsr4_component(manifest: Manifest, set_id: str, options: dict, slot_action: str | None,
                   slot_sha: str | None, xset=None, nightly: str | None = None) -> dict:
    """What OptiScaler's FSR4 slot holds, from the --fsr4 option and the recorded file.

    `xset`/`nightly` describe the files being installed; without them the pairing `set_id` is used.
    """
    rec = {"pinned": False, "sha256": slot_sha}
    choice = options.get("fsr4") or "none"
    family = options.get("gpu_family")
    if slot_action == "kept":
        label = manifest.cyboman_by_hash().get(slot_sha or "")
        return {**rec, "kind": "kept", "version": label or "your own DLL"}
    if choice != "none":
        variant, tag = None, ""
        try:
            xs = xset or manifest.get(set_id)
            tag = xs.cyboman_tag
            variant = xs.family(family).variant_dir(choice)
        except ManifestError:
            pass
        return {**rec, "kind": "cyboman", "version": tag, "variant": variant or choice, "family": family}
    off = OFFICIAL_BY_SHA.get(slot_sha or "")
    if off:
        return {**rec, "kind": "official", "version": off.version}
    p = manifest.pairings.get(set_id)
    return {**rec, "kind": "nightly", "version": nightly or (p.nightly if p else "")}


def upgrade_state(st: GameState | None, manifest: Manifest) -> GameState | None:
    if st is None or st.schema >= 2 or st.components:
        return st
    comps = file_components(manifest, st.set)
    if not comps:  # a set this manifest does not know: leave it as it is, say so in doctor
        return st
    slot = st.files.get(UPSCALER_DLL)
    comps["fsr4"] = fsr4_component(manifest, st.set, st.options, slot.action if slot else None,
                                   slot.sha256 if slot else None)
    st.components = comps
    if st.options.get("gpu_family"):
        st.gpu = {"cyboman_family": st.options["gpu_family"], "source": "xemfg config (before GPU detection)"}
    return st


def upgrade_entry(e: Entry, manifest: Manifest) -> Entry:
    if e.components:
        return e
    comps = file_components(manifest, e.set)
    if not comps:
        return e
    sha = e.files.get(UPSCALER_DLL)
    kept = sha is not None and e.options.get("fsr4", "none") != "none" and sha not in _cyboman_set_hashes(manifest, e)
    comps["fsr4"] = fsr4_component(manifest, e.set, e.options, "kept" if kept else None, sha)
    e.components = comps
    return e


def _cyboman_set_hashes(manifest: Manifest, e: Entry) -> set[str]:
    try:
        xs = manifest.get(e.set)
    except ManifestError:
        return set()
    return {h for f in xs.families.values() for h in f.dlls.values()}


def upgrade_registry(reg: Registry, manifest: Manifest) -> Registry:
    for e in reg.entries.values():
        upgrade_entry(e, manifest)
    return reg


def pending(st: GameState | None) -> bool:
    """True when the state on disk is still schema 1."""
    return st is not None and st.schema < 2


def describe_fsr4(c: dict | None, short: bool = False) -> str:
    """One line for what OptiScaler's FSR4 slot holds (schema 2 component)."""
    if not c:
        return "-"
    kind, v = c.get("kind"), c.get("version") or "?"
    if kind == "cyboman":
        return f"cyboman {c.get('variant')}" if short else f"cyboman {c.get('variant')} {v} ({c.get('family')})"
    if kind == "nightly" and short:
        return "nightly's own"
    if kind == "official":
        return f"official {v}"
    if kind == "nightly":
        return f"the nightly's own DLL ({v})"
    if kind == "kept":
        return f"kept: {v}"
    return f"{kind} {v}"


def describe_components(comps: dict) -> list[str]:
    out = []
    for k in ("nightly", "optiscaler", "mfgu"):
        if k in comps:
            out.append(f"{k} {comps[k].get('version')}" + (" (pinned)" if comps[k].get("pinned") else ""))
    if "fsr4" in comps:
        out.append("fsr4 " + describe_fsr4(comps["fsr4"]) + (" (pinned)" if comps["fsr4"].get("pinned") else ""))
    return out
