"""The manifest: versions, URLs and SHA-256s of every XeMFG set (TOML data, not code)."""
from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import tomli_w

from .fsutil import atomic_write_bytes

# CLI --fsr4 choice -> regex matching the cyboman archive's variant folder
FSR4_CHOICES: dict[str, str | None] = {
    "exact-l": r"^[\d.]+l-exact$",
    "lossy-l": r"^[\d.]+l-lossy$",
    "exact": r"^exact$",
    "lossy": r"^lossy$",
    "none": None,
}


class ManifestError(RuntimeError):
    pass


@dataclass
class FileSpec:
    key: str
    repo: str
    tag: str
    asset: str
    sha256: str
    verified_by: str = "pinned"
    cache_name: str | None = None
    inner_sha256: dict[str, str] = field(default_factory=dict)
    created: str = ""

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/releases/download/{self.tag}/{self.asset}"

    @property
    def filename(self) -> str:
        return self.cache_name or self.asset

    def to_dict(self) -> dict:
        d = {"repo": self.repo, "tag": self.tag, "asset": self.asset, "sha256": self.sha256,
             "verified_by": self.verified_by}
        if self.cache_name:
            d["cache_name"] = self.cache_name
        if self.inner_sha256:
            d["inner_sha256"] = dict(self.inner_sha256)
        return d


@dataclass
class CybomanFamily:
    name: str
    archives: list[tuple[str, str]]  # (asset, sha256), preferred first
    dlls: dict[str, str]  # variant folder -> sha256 of amd_fidelityfx_upscaler_dx12.dll

    def variant_dir(self, choice: str) -> str | None:
        pat = FSR4_CHOICES.get(choice)
        if pat is None:
            return None
        hits = [d for d in self.dlls if re.match(pat, d)]
        if not hits:
            raise ManifestError(f"cyboman {self.name}: no variant matching '{choice}' (have {sorted(self.dlls)})")
        return sorted(hits)[-1]


@dataclass
class XSet:
    id: str
    mfgu_version: str
    created: str
    files: dict[str, FileSpec]
    cyboman_repo: str
    cyboman_tag: str
    families: dict[str, CybomanFamily]
    notes: str = ""

    @property
    def nightly(self) -> FileSpec:
        return self.files["nightly"]

    @property
    def optiscaler(self) -> FileSpec:
        return self.files["optiscaler"]

    @property
    def mfgu(self) -> FileSpec:
        return self.files["mfgu"]

    def family(self, name: str) -> CybomanFamily:
        if name not in self.families:
            raise ManifestError(f"set {self.id}: no cyboman archive for GPU family '{name}' "
                                f"(have {', '.join(self.families)})")
        return self.families[name]

    def cyboman_url(self, asset: str) -> str:
        return f"https://github.com/{self.cyboman_repo}/releases/download/{self.cyboman_tag}/{asset}"

    @classmethod
    def from_dict(cls, sid: str, d: dict) -> "XSet":
        files = {k: FileSpec(key=k, **v) for k, v in d["files"].items()}
        cy = d.get("cyboman", {})
        fams = {n: CybomanFamily(n, [(a["asset"], a["sha256"]) for a in f["archives"]], dict(f["dlls"]))
                for n, f in cy.get("families", {}).items()}
        return cls(sid, d["mfgu_version"], d.get("created", ""), files, cy.get("repo", ""), cy.get("tag", ""),
                   fams, d.get("notes", ""))

    def to_dict(self) -> dict:
        return {
            "mfgu_version": self.mfgu_version, "created": self.created, "notes": self.notes,
            "files": {k: f.to_dict() for k, f in self.files.items()},
            "cyboman": {"repo": self.cyboman_repo, "tag": self.cyboman_tag, "verified_by": "sha256sums",
                        "families": {n: {"archives": [{"asset": a, "sha256": h} for a, h in f.archives],
                                         "dlls": dict(f.dlls)} for n, f in self.families.items()}},
        }

    def same_content(self, other: "XSet") -> bool:
        a, b = self.to_dict(), other.to_dict()
        for x in (a, b):
            x.pop("created", None)
            x.pop("notes", None)
            for f in x["files"].values():
                f.pop("verified_by", None)
        return a == b


@dataclass
class CybomanRelease:
    tag: str
    repo: str
    families: dict[str, CybomanFamily]
    created: str = ""
    verified_by: str = "sha256sums"

    def to_dict(self) -> dict:
        return {"repo": self.repo, "tag": self.tag, "created": self.created, "verified_by": self.verified_by,
                "families": {n: {"archives": [{"asset": a, "sha256": h} for a, h in f.archives],
                                 "dlls": dict(f.dlls)} for n, f in self.families.items()}}

    @classmethod
    def from_dict(cls, tag: str, d: dict) -> "CybomanRelease":
        fams = {n: CybomanFamily(n, [(a["asset"], a["sha256"]) for a in f["archives"]], dict(f["dlls"]))
                for n, f in d.get("families", {}).items()}
        return cls(d.get("tag", tag), d.get("repo", ""), fams, d.get("created", ""), d.get("verified_by", "sha256sums"))


# components that come from a release file; cyboman is a release of per-GPU archives
FILE_COMPONENTS = ("nightly", "optiscaler", "mfgu")
COMPONENTS = (*FILE_COMPONENTS, "cyboman")


@dataclass
class Pairing:
    """A combination upstream documents as working together (MFG Unlocker's docs/optiscaler.md)."""

    id: str
    mfgu: str
    optiscaler: str
    nightly: str
    cyboman: str = ""  # the cyboman release that was current when the pairing was resolved
    created: str = ""
    notes: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if k != "id"}


@dataclass
class Manifest:
    """Schema 2: components (each with its versions) + the pairings upstream documents.

    `sets` is a read-only view: one XSet per pairing, for the code that installs a whole pairing.
    """

    current: str  # pairing id
    components: dict[str, dict[str, FileSpec | CybomanRelease]] = field(default_factory=dict)
    pairings: dict[str, Pairing] = field(default_factory=dict)
    latest: dict[str, str] = field(default_factory=dict)  # component -> newest known version
    source: Path | None = None

    # ------------------------------------------------------------ views
    @property
    def sets(self) -> dict[str, XSet]:
        return {pid: self._view(p) for pid, p in self.pairings.items()}

    def _view(self, p: Pairing) -> XSet:
        # copies: a view must never change the manifest behind it
        files = {k: copy.deepcopy(self.component(k, getattr(p, k))) for k in FILE_COMPONENTS}
        cy = self.components.get("cyboman", {}).get(p.cyboman)
        return XSet(p.id, p.mfgu, p.created, files, cy.repo if cy else "", cy.tag if cy else "",
                    copy.deepcopy(cy.families) if cy else {}, p.notes)

    def component(self, kind: str, version: str) -> FileSpec | CybomanRelease:
        try:
            return self.components[kind][version]
        except KeyError:
            raise ManifestError(f"unknown {kind} version '{version}' "
                                f"(known: {', '.join(self.components.get(kind, {})) or 'none'})") from None

    def versions(self, kind: str) -> list[str]:
        """Known versions of a component, oldest first."""
        vs = self.components.get(kind, {})
        return sorted(vs, key=lambda v: (getattr(vs[v], "created", ""), v))

    def pairing_for(self, versions: dict[str, str]) -> str | None:
        """The pairing these nightly/optiscaler/mfgu versions match, if any."""
        for pid, p in self.pairings.items():
            if all(versions.get(k) == getattr(p, k) for k in FILE_COMPONENTS):
                return pid
        return None

    def compose(self, versions: dict[str, str]) -> XSet:
        """The files of any combination of component versions (nightly, optiscaler, mfgu, cyboman).

        Its id is the pairing it matches, or "custom" for a mix upstream does not document.
        """
        pid = self.pairing_for(versions)
        files = {k: copy.deepcopy(self.component(k, versions[k])) for k in FILE_COMPONENTS}
        cy = self.components.get("cyboman", {}).get(versions.get("cyboman", ""))
        if versions.get("cyboman") and cy is None:
            raise ManifestError(f"unknown cyboman version '{versions['cyboman']}'")
        p = self.pairings.get(pid) if pid else None
        mfgu = files["mfgu"].tag
        return XSet(pid or "custom", p.mfgu if p else mfgu, p.created if p else "", files,
                    cy.repo if cy else "", cy.tag if cy else "", copy.deepcopy(cy.families) if cy else {},
                    p.notes if p else "custom combination")

    def newest(self) -> dict[str, str]:
        """The versions an update moves to: the current pairing's nightly, OptiScaler.dll and MFG Unlocker
        (a combination upstream documents), and the newest cyboman release (it does not depend on them)."""
        out = self.versions_of(self.current)
        if self.latest.get("cyboman"):
            out["cyboman"] = self.latest["cyboman"]
        return out

    def versions_of(self, pid: str) -> dict[str, str]:
        p = self.pairings[pid]
        return {k: getattr(p, k) for k in COMPONENTS if getattr(p, k)}

    def unverified(self, versions: dict[str, str]) -> list[str]:
        """Why a combination is not one upstream documents ([] when it is).

        Upstream (MFG Unlocker's docs/optiscaler.md and dashdogy's release notes) pairs each MFG Unlocker
        release with one dashdogy OptiScaler.dll, built from one nightly's commit. Those two links are the
        dependencies; cyboman's DLLs do not depend on them.
        """
        if self.pairing_for(versions):
            return []
        out = []
        o, n, m = versions.get("optiscaler"), versions.get("nightly"), versions.get("mfgu")
        if not any(p.optiscaler == o and p.nightly == n for p in self.pairings.values()):
            paired = sorted({p.nightly for p in self.pairings.values() if p.optiscaler == o})
            out.append(f"dashdogy OptiScaler {o} is documented with {' / '.join(paired) or 'no known nightly'}, "
                       f"not {n}")
        if not any(p.optiscaler == o and p.mfgu == m for p in self.pairings.values()):
            paired = sorted({p.mfgu for p in self.pairings.values() if p.optiscaler == o})
            out.append(f"dashdogy OptiScaler {o} is documented with MFG Unlocker {' / '.join(paired) or '?'}, "
                       f"not {m}")
        return out

    @property
    def current_set(self) -> XSet:
        return self.get(self.current)

    def get(self, sid: str) -> XSet:
        if sid not in self.pairings:
            raise ManifestError(f"unknown set '{sid}' (known: {', '.join(self.pairings)})")
        return self._view(self.pairings[sid])

    def optiscaler_hashes(self) -> set[str]:
        return {f.sha256 for f in self.components.get("optiscaler", {}).values()}

    def optiscaler_set_by_hash(self) -> dict[str, str]:
        return {s.optiscaler.sha256: sid for sid, s in self.sets.items()}

    def cyboman_by_hash(self) -> dict[str, str]:
        out = {}
        for tag in reversed(self.versions("cyboman")):  # newest wins a tie
            rel = self.components["cyboman"][tag]
            for fam in rel.families.values():
                for d, h in fam.dlls.items():
                    out.setdefault(h, f"{d} ({fam.name}, {rel.tag})")
        return out

    def ordered_ids(self) -> list[str]:
        return sorted(self.pairings, key=lambda k: (self.pairings[k].created, k))

    # ------------------------------------------------------------ building
    def add_set(self, xs: XSet, source: str = "") -> None:
        """Split a resolved set into its components and a pairing with the set's id."""
        comps = self.components
        versions = {}
        for k in FILE_COMPONENTS:
            f = xs.files[k]
            v = f.tag
            have = comps.get(k, {}).get(v)
            if have is not None and have.sha256 != f.sha256:  # same tag, new file upstream
                v = f"{f.tag}+{f.sha256[:8]}"
            spec = FileSpec(**{**vars(f), "key": k})
            spec.created = getattr(have, "created", "") or xs.created
            comps.setdefault(k, {}).setdefault(v, spec)
            versions[k] = v
        if xs.cyboman_tag:
            comps.setdefault("cyboman", {}).setdefault(
                xs.cyboman_tag, CybomanRelease(xs.cyboman_tag, xs.cyboman_repo, xs.families, xs.created))
        self.pairings[xs.id] = Pairing(xs.id, xs.mfgu_version, versions["optiscaler"], versions["nightly"],
                                       xs.cyboman_tag, xs.created, xs.notes, source)

    def set_latest_from(self, pid: str) -> None:
        p = self.pairings[pid]
        self.latest.update({k: getattr(p, k) for k in COMPONENTS if getattr(p, k)})

    # ------------------------------------------------------------ (de)serialising
    @classmethod
    def parse(cls, text: str, source: Path | None = None) -> "Manifest":
        d = tomllib.loads(text)
        schema = d.get("schema")
        if schema == 1:
            return cls._from_v1(d, source)
        if schema != 2:
            raise ManifestError(f"unsupported manifest schema {schema}")
        m = cls(d["current"], source=source)
        for k, vs in d.get("components", {}).items():
            for v, spec in vs.items():
                if k == "cyboman":
                    m.components.setdefault(k, {})[v] = CybomanRelease.from_dict(v, spec)
                elif k in FILE_COMPONENTS:
                    m.components.setdefault(k, {})[v] = FileSpec(key=k, **spec)
        for pid, p in d.get("pairings", {}).items():
            m.pairings[pid] = Pairing(pid, **p)
        m.latest = dict(d.get("latest", {}))
        if m.current not in m.pairings:
            raise ManifestError(f"current set {m.current} is not defined")
        for p in m.pairings.values():  # every pairing must resolve
            m._view(p)
        return m

    @classmethod
    def _from_v1(cls, d: dict, source: Path | None) -> "Manifest":
        m = cls(d["current"], source=source)
        for sid, sd in d.get("sets", {}).items():
            m.add_set(XSet.from_dict(sid, sd), source="manifest schema 1")
        if m.current not in m.pairings:
            raise ManifestError(f"current set {m.current} is not defined")
        m.set_latest_from(m.current)
        return m

    def to_dict(self) -> dict:
        return {"schema": 2, "current": self.current, "latest": dict(self.latest),
                "pairings": {pid: p.to_dict() for pid, p in self.pairings.items()},
                "components": {k: {v: c.to_dict() | ({"created": c.created} if isinstance(c, FileSpec) else {})
                                   for v, c in vs.items()} for k, vs in self.components.items()}}

    def dumps(self) -> str:
        return tomli_w.dumps(self.to_dict())

    def save(self, path: Path) -> None:
        header = "# xemfg manifest (written by `xemfg manifest refresh`). Data only.\n"
        if path.is_file():  # keep the first copy of an older format
            try:
                if tomllib.loads(path.read_text()).get("schema") == 1:
                    bk = path.with_name(path.stem + ".v1.toml")
                    if not bk.exists():
                        atomic_write_bytes(bk, path.read_bytes())
            except (OSError, tomllib.TOMLDecodeError):
                pass
        atomic_write_bytes(path, (header + self.dumps()).encode())
        self.source = path


def bundled_text() -> str:
    return resources.files("xemfg").joinpath("data/manifest.toml").read_text()


def load(user_path: Path) -> Manifest:
    """User copy if present (it accumulates refreshed versions), merged with the bundled data."""
    bundled = Manifest.parse(bundled_text())
    if not user_path.exists():
        return bundled
    user = Manifest.parse(user_path.read_text(), user_path)
    for k, vs in bundled.components.items():
        for v, c in vs.items():
            user.components.setdefault(k, {}).setdefault(v, c)
    for pid, p in bundled.pairings.items():
        user.pairings.setdefault(pid, p)
    for k, v in bundled.latest.items():
        user.latest.setdefault(k, v)
    return user
