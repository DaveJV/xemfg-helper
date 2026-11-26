"""The manifest: versions, URLs and SHA-256s of every XeMFG set (TOML data, not code)."""
from __future__ import annotations

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
class Manifest:
    current: str
    sets: dict[str, XSet]
    source: Path | None = None

    @property
    def current_set(self) -> XSet:
        return self.get(self.current)

    def get(self, sid: str) -> XSet:
        if sid not in self.sets:
            raise ManifestError(f"unknown set '{sid}' (known: {', '.join(self.sets)})")
        return self.sets[sid]

    def optiscaler_hashes(self) -> set[str]:
        return {s.optiscaler.sha256 for s in self.sets.values()}

    def optiscaler_set_by_hash(self) -> dict[str, str]:
        return {s.optiscaler.sha256: sid for sid, s in self.sets.items()}

    def cyboman_by_hash(self) -> dict[str, str]:
        out = {}
        for s in self.sets.values():
            for fam in s.families.values():
                for d, h in fam.dlls.items():
                    out.setdefault(h, f"{d} ({fam.name}, {s.cyboman_tag})")
        return out

    def ordered_ids(self) -> list[str]:
        return sorted(self.sets, key=lambda k: (self.sets[k].created, k))

    @classmethod
    def parse(cls, text: str, source: Path | None = None) -> "Manifest":
        d = tomllib.loads(text)
        if d.get("schema") != 1:
            raise ManifestError("unsupported manifest schema")
        sets = {sid: XSet.from_dict(sid, sd) for sid, sd in d.get("sets", {}).items()}
        if d["current"] not in sets:
            raise ManifestError(f"current set {d['current']} is not defined")
        return cls(d["current"], sets, source)

    def dumps(self) -> str:
        return tomli_w.dumps({"schema": 1, "current": self.current,
                              "sets": {k: v.to_dict() for k, v in self.sets.items()}})

    def save(self, path: Path) -> None:
        header = ("# xemfg manifest (written by `xemfg manifest refresh`). Data only.\n")
        atomic_write_bytes(path, (header + self.dumps()).encode())
        self.source = path


def bundled_text() -> str:
    return resources.files("xemfg").joinpath("data/manifest.toml").read_text()


def load(user_path: Path) -> Manifest:
    """User copy if present (it accumulates refreshed sets), merged with the bundled sets."""
    bundled = Manifest.parse(bundled_text())
    if not user_path.exists():
        return bundled
    user = Manifest.parse(user_path.read_text(), user_path)
    for sid, s in bundled.sets.items():
        user.sets.setdefault(sid, s)
    return user
