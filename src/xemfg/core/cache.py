"""Download cache, one folder per component version: download, verify SHA-256, extract.

    ~/.cache/xemfg/<component>/<tag>_<sha8>/<file>   (+ x/ when extracted)
    ~/.cache/xemfg/cyboman/<tag>/<archive>            (+ <family>/ when extracted)

A file is only ever used after its hash matched; a mismatch deletes it and aborts. Files in the
per-set folders of xemfg 0.1 and xemfg-linux.sh (~/.cache/xemfg/<set>/) are reused when their hash matches.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .fsutil import sha256_or_none
from .manifest import FileSpec, ManifestError, XSet
from .net import Fetcher, Progress

# names xemfg-linux.sh used for the same files, reused when their hash matches
LEGACY_NAMES = {".7z": ["cyboman.7z"], ".zip": ["cyboman.zip"]}


class HashMismatch(RuntimeError):
    pass


class ExtractError(RuntimeError):
    pass


def extract(archive: Path, dest: Path) -> None:
    """Extract with bsdtar, falling back to 7z. Extracts to a temp dir, then moves into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}."))
    errors = []
    try:
        for cmd in (["bsdtar", "-xf", str(archive), "-C", str(tmp)],
                    ["7z", "x", "-y", f"-o{tmp}", str(archive)]):
            if not shutil.which(cmd[0]):
                errors.append(f"{cmd[0]} not installed")
                continue
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                _check_tree(tmp)
                if dest.exists():
                    shutil.rmtree(dest)
                tmp.rename(dest)
                return
            errors.append(f"{cmd[0]}: {r.stderr.strip()[-300:]}")
            shutil.rmtree(tmp)
            tmp.mkdir()
        raise ExtractError(f"could not extract {archive.name}: {'; '.join(errors)}")
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _check_tree(root: Path) -> None:
    """Refuse archives containing symlinks (nothing we install is a link)."""
    for p in root.rglob("*"):
        if p.is_symlink():
            raise ExtractError(f"archive contains a symlink: {p.relative_to(root)}")


def component_dir(root: Path, spec: FileSpec) -> Path:
    return root / spec.key / f"{spec.tag}_{spec.sha256[:8]}"


class SetCache:
    """The verified files of one combination of component versions (`xset`)."""

    def __init__(self, cache_dir: Path, xset: XSet, fetcher: Fetcher,
                 log: Callable[[str], None] = lambda s: None, progress: Progress | None = None, store=None):
        self.root = cache_dir
        self.store = store  # core.store.Store: DLLs used before, tried before downloading
        self.xset = xset
        self.fetcher = fetcher
        self.log = log
        self.progress = progress

    @property
    def dir(self) -> Path:  # where new downloads land, for messages
        return self.root

    def _legacy(self, names: list[str], sha256: str) -> Path | None:
        """A file with the right hash in an old per-set folder (xemfg 0.1, xemfg-linux.sh)."""
        if not self.root.is_dir():
            return None
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name in ("nightly", "optiscaler", "mfgu", "cyboman"):
                continue
            for n in names:
                cand = d / n
                if cand.is_file() and sha256_or_none(cand) == sha256:
                    return cand
        return None

    def _fetch_verified(self, url: str, dest_dir: Path, name: str, sha256: str) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / name
        if path.is_file():
            if sha256_or_none(path) == sha256:
                return path
            self.log(f"cached {name} has the wrong hash; deleting")
            path.unlink()
        old = self._legacy([name, *LEGACY_NAMES.get(path.suffix, [])], sha256)
        if old is not None:
            try:
                os.link(old, path)
            except OSError:
                shutil.copy2(old, path)
            if sha256_or_none(path) == sha256:
                return path
            path.unlink(missing_ok=True)
        kept = self.store.find(sha256) if self.store else None
        if kept is not None:
            shutil.copy2(kept, path)
            if sha256_or_none(path) == sha256:
                return path
            path.unlink(missing_ok=True)
        self.log(f"downloading {url}")
        self.fetcher.download(url, path, self.progress)
        got = sha256_or_none(path)
        if got != sha256:
            path.unlink(missing_ok=True)
            raise HashMismatch(f"{name}: expected {sha256}, got {got}; deleted the download")
        return path

    def file(self, spec: FileSpec) -> Path:
        return self._fetch_verified(spec.url, component_dir(self.root, spec), spec.filename, spec.sha256)

    def _extracted(self, spec: FileSpec) -> Path:
        archive = self.file(spec)
        dest = component_dir(self.root, spec) / "x"
        marker = dest / ".xemfg-extracted"
        if marker.is_file() and marker.read_text().strip() == spec.sha256:
            return dest
        self.log(f"extracting {archive.name}")
        extract(archive, dest)
        marker.write_text(spec.sha256)
        return dest

    def nightly_dir(self) -> Path:
        d = self._extracted(self.xset.nightly)
        if not (d / "OptiScaler").is_dir() or not (d / "OptiScaler.ini").is_file():
            raise ExtractError(f"{self.xset.nightly.asset} has no OptiScaler folder / OptiScaler.ini")
        return d

    def optiscaler_dll(self) -> Path:
        return self.file(self.xset.optiscaler)

    def mfgu_dll(self) -> Path:
        d = self._extracted(self.xset.mfgu)
        dll = next((p for p in d.rglob("*") if p.name.lower() == "mfgunlocker.dll"), None)
        if dll is None:
            raise ExtractError("MFGUnlocker.dll not found in the MFG Unlocker zip")
        want = self.xset.mfgu.inner_sha256.get("MFGUnlocker.dll")
        if want and sha256_or_none(dll) != want:
            shutil.rmtree(d)
            raise HashMismatch("MFGUnlocker.dll inside the zip has the wrong hash; deleted the extraction")
        return dll

    def cyboman_dir(self) -> Path:
        return self.root / "cyboman" / self.xset.cyboman_tag

    def cyboman_dll(self, family: str, choice: str) -> Path | None:
        if choice == "none":
            return None
        from .fsr4choice import variant_dir

        fam = self.xset.family(family)
        vdir = variant_dir(fam, choice)
        want = fam.dlls[vdir]
        dest = self.cyboman_dir() / family
        target = dest / vdir / "amd_fidelityfx_upscaler_dx12.dll"
        if sha256_or_none(target) == want:
            return target
        kept = self.store.find(want) if self.store else None
        if kept is not None:  # used before: no need for the archive
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kept, target)
            if sha256_or_none(target) == want:
                return target
        errors = []
        for asset, sha in fam.archives:  # .7z first; the .zip is the fallback, as in the script
            try:
                archive = self._fetch_verified(self.xset.cyboman_url(asset), self.cyboman_dir(), asset, sha)
                extract(archive, dest)
            except ExtractError as e:
                errors.append(str(e))
                self.log(f"{asset}: extraction failed, trying the next archive")
                continue
            found = target if target.exists() else next(
                (p for p in dest.rglob("amd_fidelityfx_upscaler_dx12.dll") if p.parent.name == vdir), None)
            if found and sha256_or_none(found) == want:
                return found
            errors.append(f"{asset}: {vdir} DLL missing or wrong hash")
        raise HashMismatch(f"cyboman {family}/{vdir}: " + "; ".join(errors))

    def official_dll(self, d) -> Path:
        """An official upscaler DLL (fsr4.OfficialDll) from AMD's FidelityFX SDK repo, verified by its SHA-256."""
        from .fsr4 import UPSCALER

        dest = self.root / "fsr4_official" / f"{d.version}_{d.sha256[:8]}"
        return self._fetch_verified(d.url, dest, UPSCALER[d.api], d.sha256)

    def official_ready(self, d) -> bool:
        from .fsr4 import UPSCALER

        p = self.root / "fsr4_official" / f"{d.version}_{d.sha256[:8]}" / UPSCALER[d.api]
        return sha256_or_none(p) == d.sha256 or (self.store is not None and self.store.find(d.sha256) is not None)

    def ready(self, fsr4: str, family: str) -> bool:
        """Everything for this combination is already downloaded, verified and extracted."""
        xs = self.xset

        def extracted(spec):
            m = component_dir(self.root, spec) / "x/.xemfg-extracted"
            return m.is_file() and m.read_text().strip() == spec.sha256

        ok = extracted(xs.nightly) and extracted(xs.mfgu) and \
            sha256_or_none(component_dir(self.root, xs.optiscaler) / xs.optiscaler.filename) == xs.optiscaler.sha256
        if ok and fsr4.startswith("cyboman:"):
            from .fsr4choice import variant_dir

            fam = xs.family(family)
            vdir = variant_dir(fam, fsr4.split(":", 1)[1])
            ok = sha256_or_none(self.cyboman_dir() / family / vdir / "amd_fidelityfx_upscaler_dx12.dll") == fam.dlls[vdir]
        return ok

    def prefetch(self, family: str, choice: str) -> None:
        self.nightly_dir()
        self.optiscaler_dll()
        self.mfgu_dll()
        self.cyboman_dll(family, choice)


def validate_choice(xset: XSet, family: str, choice: str) -> None:
    if choice != "none":
        xset.family(family).variant_dir(choice)
    elif choice not in ("none",):
        raise ManifestError(choice)
