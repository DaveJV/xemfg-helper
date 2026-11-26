"""Resolve the current XeMFG pairing from upstream. Never guesses: every piece must be found.

Order of sources per release: GitHub API (asset list + digests) -> releases/expanded_assets/<tag>
HTML (asset list + digests) -> download the asset and hash it (trust on first download).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import CybomanFamily, FileSpec, Manifest, XSet
from .net import Fetcher, FetchError

MFGU_REPO = "dashdogy/MFG-Unlocker"
OPTI_REPO = "dashdogy/OptiScaler"
NIGHTLY_REPO = "optiscaler/OptiScaler-nightly"
CYBOMAN_REPO = "zgauthier2000/fsr4-shader-performance-overrides"


class UpstreamError(RuntimeError):
    pass


@dataclass
class Asset:
    name: str
    sha256: str | None
    source: str  # api-digest | html-digest | none


@dataclass
class Pairing:
    mfgu_tag: str
    optiscaler_tag: str
    nightly_tag: str
    cyboman_tag: str
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- parsing helpers (pure)

def parse_which_build(md: str, mfgu_tag: str) -> tuple[str, str]:
    """From docs/optiscaler.md "Which OptiScaler build": (dashdogy OptiScaler tag, nightly tag)."""
    m = re.search(r"^##\s+Which OptiScaler build\s*$(.*?)(?=^##\s)", md, re.S | re.M)
    if not m:
        raise UpstreamError("docs/optiscaler.md has no 'Which OptiScaler build' section")
    sec = m.group(1)
    ver = mfgu_tag.lstrip("vV")
    if not re.search(rf"MFG Unlocker V{re.escape(ver)}\b", sec, re.I):
        raise UpstreamError(f"'Which OptiScaler build' does not mention MFG Unlocker V{ver}")
    opti = re.search(r"dashdogy/OptiScaler/releases/tag/([\w.\-]+)", sec)
    nightly = re.search(r"\b(nightly-\d{8})\b", sec)
    if not opti or not nightly:
        raise UpstreamError("could not find both the dashdogy OptiScaler tag and the nightly tag in the docs")
    return opti.group(1), nightly.group(1)


def parse_sha256sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def parse_expanded_assets(html: str) -> list[Asset]:
    """Asset names with the digest GitHub shows after each one."""
    links = [(m.start(), m.group(1)) for m in
             re.finditer(r'href="[^"]*/releases/download/[^/"]+/([^"]+)"', html)]
    out = []
    for i, (pos, name) in enumerate(links):
        end = links[i + 1][0] if i + 1 < len(links) else len(html)
        d = re.search(r"sha256:([0-9a-f]{64})", html[pos:end])
        out.append(Asset(name, d.group(1) if d else None, "html-digest" if d else "none"))
    return out


def cyboman_families(sums: dict[str, str]) -> dict[str, CybomanFamily]:
    """Group cyboman SHA256SUMS into GPU families: archives + per-variant DLL hashes."""
    fams: dict[str, CybomanFamily] = {}
    for name, h in sums.items():
        m = re.match(r"^fsr[\d.]+-(?:(igpu)-)?cyboman-([\w]+)-[\d-]+\.(7z|zip)$", name)
        if m:
            fam = f"igpu-{m.group(2)}" if m.group(1) else m.group(2)
            f = fams.setdefault(fam, CybomanFamily(fam, [], {}))
            f.archives.append((name, h))
            continue
        m = re.match(r"^([\w-]+)/([^/]+)/amd_fidelityfx_upscaler_dx12\.dll$", name)
        if m:
            fams.setdefault(m.group(1), CybomanFamily(m.group(1), [], {})).dlls[m.group(2)] = h
    for f in fams.values():
        f.archives.sort(key=lambda a: (not a[0].endswith(".7z"), a[0]))  # .7z first (smaller)
    return {k: v for k, v in fams.items() if v.archives and v.dlls}


# ---------------------------------------------------------------- network

class Upstream:
    def __init__(self, fetcher: Fetcher):
        self.f = fetcher

    def latest_tag(self, repo: str, prefix: str | None = None) -> str:
        try:
            if prefix is None:
                return self.f.get_json(f"https://api.github.com/repos/{repo}/releases/latest")["tag_name"]
            for r in self.f.get_json(f"https://api.github.com/repos/{repo}/releases?per_page=30"):
                if r["tag_name"].startswith(prefix) and not r.get("draft"):
                    return r["tag_name"]
        except (FetchError, KeyError, TypeError, ValueError):
            pass
        if prefix is None:
            url = self.f.final_url(f"https://github.com/{repo}/releases/latest")
            m = re.search(r"/releases/tag/([^/?#]+)", url)
            if m:
                return m.group(1)
        else:
            html = self.f.get_text(f"https://github.com/{repo}/releases")
            tags = re.findall(rf"/releases/tag/({re.escape(prefix)}[^\"/?#]+)", html)
            if tags:
                return tags[0]
        raise UpstreamError(f"could not find the latest release of {repo}")

    def assets(self, repo: str, tag: str) -> list[Asset]:
        try:
            rel = self.f.get_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
            out = []
            for a in rel.get("assets", []):
                dg = a.get("digest") or ""
                sha = dg.split(":", 1)[1] if dg.startswith("sha256:") else None
                out.append(Asset(a["name"], sha, "api-digest" if sha else "none"))
            if out:
                return out
        except (FetchError, KeyError, TypeError, ValueError):
            pass
        html = self.f.get_text(f"https://github.com/{repo}/releases/expanded_assets/{tag}")
        out = parse_expanded_assets(html)
        if not out:
            raise UpstreamError(f"no assets found for {repo}@{tag}")
        return out

    def raw(self, repo: str, ref: str, path: str) -> str:
        return self.f.get_text(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")

    def release_file(self, repo: str, tag: str, asset: str) -> str:
        return self.f.get_text(f"https://github.com/{repo}/releases/download/{tag}/{asset}")

    def tofu_hash(self, repo: str, tag: str, asset: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / asset
            self.f.download(f"https://github.com/{repo}/releases/download/{tag}/{asset}", p)
            h = hashlib.sha256(p.read_bytes()).hexdigest()
        return h

    # ------------------------------------------------------------ resolve

    def resolve(self, log=lambda s: None) -> XSet:
        mfgu_tag = self.latest_tag(MFGU_REPO)
        log(f"MFG Unlocker latest: {mfgu_tag}")
        md = self.raw(MFGU_REPO, mfgu_tag, "docs/optiscaler.md")
        opti_tag, nightly_tag = parse_which_build(md, mfgu_tag)
        log(f"paired with {OPTI_REPO}@{opti_tag} and {NIGHTLY_REPO}@{nightly_tag} (docs/optiscaler.md)")

        # MFG Unlocker zip + inner dll from SHA256SUMS.txt
        sums = parse_sha256sums(self.release_file(MFGU_REPO, mfgu_tag, "SHA256SUMS.txt"))
        zips = [n for n in sums if n.lower().endswith(".zip") and "mfgunlocker" in n.lower()]
        if len(zips) != 1:
            raise UpstreamError(f"expected one MFGUnlocker zip in SHA256SUMS.txt, found {zips}")
        inner = {n: h for n, h in sums.items() if n.lower() == "mfgunlocker.dll"}
        mfgu = FileSpec("mfgu", MFGU_REPO, mfgu_tag, zips[0], sums[zips[0]], "sha256sums",
                        inner_sha256={"MFGUnlocker.dll": h for h in inner.values()})

        # dashdogy OptiScaler.dll
        opti_assets = {a.name: a for a in self.assets(OPTI_REPO, opti_tag)}
        if "OptiScaler.dll" not in opti_assets:
            raise UpstreamError(f"{OPTI_REPO}@{opti_tag} has no OptiScaler.dll")
        oa = opti_assets["OptiScaler.dll"]
        if oa.sha256:
            opti = FileSpec("optiscaler", OPTI_REPO, opti_tag, oa.name, oa.sha256, oa.source,
                            cache_name="OptiScaler-mfgu.dll")
        else:
            log("no published digest for dashdogy OptiScaler.dll; hashing the download (tofu)")
            opti = FileSpec("optiscaler", OPTI_REPO, opti_tag, oa.name, self.tofu_hash(OPTI_REPO, opti_tag, oa.name),
                            "tofu", cache_name="OptiScaler-mfgu.dll")

        # nightly archive
        n_assets = [a for a in self.assets(NIGHTLY_REPO, nightly_tag) if a.name.lower().endswith((".7z", ".zip"))]
        if len(n_assets) != 1:
            raise UpstreamError(f"expected one OptiScaler archive in {nightly_tag}, found {[a.name for a in n_assets]}")
        na = n_assets[0]
        nsha = na.sha256 or self.tofu_hash(NIGHTLY_REPO, nightly_tag, na.name)
        nightly = FileSpec("nightly", NIGHTLY_REPO, nightly_tag, na.name, nsha, na.source if na.sha256 else "tofu")

        # cyboman
        cy_tag = self.latest_tag(CYBOMAN_REPO, prefix="dll-")
        cy_sums = parse_sha256sums(self.release_file(CYBOMAN_REPO, cy_tag, "SHA256SUMS"))
        fams = cyboman_families(cy_sums)
        if not fams:
            raise UpstreamError(f"cyboman {cy_tag}: SHA256SUMS lists no archives/DLLs")
        log(f"cyboman FSR4 DLLs: {cy_tag} ({', '.join(fams)})")

        return XSet(
            id=mfgu_tag, mfgu_version=mfgu_tag, created=dt.date.today().isoformat(),
            files={"nightly": nightly, "optiscaler": opti, "mfgu": mfgu},
            cyboman_repo=CYBOMAN_REPO, cyboman_tag=cy_tag, families=fams,
            notes=f"MFG Unlocker {mfgu_tag} + {OPTI_REPO}@{opti_tag} + {nightly_tag}; cyboman {cy_tag}",
        )


def merge_into(manifest: Manifest, new: XSet) -> tuple[str, bool]:
    """Add `new` to the manifest under a unique id. Returns (set id, changed)."""
    for sid, s in manifest.sets.items():
        if s.same_content(new):
            changed = manifest.current != sid
            manifest.current = sid
            return sid, changed
    sid = new.id
    if sid in manifest.sets:  # same MFG Unlocker, different cyboman/nightly
        sid = f"{new.mfgu_version}+{new.cyboman_tag}"
        n = 2
        while sid in manifest.sets:
            sid = f"{new.mfgu_version}+{new.cyboman_tag}.{n}"
            n += 1
    new.id = sid
    manifest.sets[sid] = new
    manifest.current = sid
    return sid, True
