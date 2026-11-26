"""Download cache (~/.cache/xemfg/<set>/): download, verify SHA-256, extract.

A file is only ever used after its hash matched; a mismatch deletes it and aborts.
"""
from __future__ import annotations

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


class SetCache:
    def __init__(self, cache_dir: Path, xset: XSet, fetcher: Fetcher,
                 log: Callable[[str], None] = lambda s: None, progress: Progress | None = None):
        self.dir = cache_dir
        self.xset = xset
        self.fetcher = fetcher
        self.log = log
        self.progress = progress

    def _fetch_verified(self, url: str, name: str, sha256: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        for cand in [path] + [self.dir / n for n in LEGACY_NAMES.get(path.suffix, [])]:
            if cand.is_file():
                if sha256_or_none(cand) == sha256:
                    return cand
                if cand == path:
                    self.log(f"cached {name} has the wrong hash; deleting")
                    cand.unlink()
        self.log(f"downloading {url}")
        self.fetcher.download(url, path, self.progress)
        got = sha256_or_none(path)
        if got != sha256:
            path.unlink(missing_ok=True)
            raise HashMismatch(f"{name}: expected {sha256}, got {got}; deleted the download")
        return path

    def file(self, spec: FileSpec) -> Path:
        return self._fetch_verified(spec.url, spec.filename, spec.sha256)

    def _extracted(self, spec: FileSpec, subdir: str) -> Path:
        archive = self.file(spec)
        dest = self.dir / subdir
        marker = dest / ".xemfg-extracted"
        if marker.is_file() and marker.read_text().strip() == spec.sha256:
            return dest
        self.log(f"extracting {archive.name}")
        extract(archive, dest)
        marker.write_text(spec.sha256)
        return dest

    def nightly_dir(self) -> Path:
        d = self._extracted(self.xset.nightly, "nightly")
        if not (d / "OptiScaler").is_dir() or not (d / "OptiScaler.ini").is_file():
            raise ExtractError(f"{self.xset.nightly.asset} has no OptiScaler folder / OptiScaler.ini")
        return d

    def optiscaler_dll(self) -> Path:
        return self.file(self.xset.optiscaler)

    def mfgu_dll(self) -> Path:
        d = self._extracted(self.xset.mfgu, "mfgu")
        dll = next((p for p in d.rglob("*") if p.name.lower() == "mfgunlocker.dll"), None)
        if dll is None:
            raise ExtractError("MFGUnlocker.dll not found in the MFG Unlocker zip")
        want = self.xset.mfgu.inner_sha256.get("MFGUnlocker.dll")
        if want and sha256_or_none(dll) != want:
            shutil.rmtree(d)
            raise HashMismatch("MFGUnlocker.dll inside the zip has the wrong hash; deleted the extraction")
        return dll

    def cyboman_dll(self, family: str, choice: str) -> Path | None:
        if choice == "none":
            return None
        fam = self.xset.family(family)
        vdir = fam.variant_dir(choice)
        want = fam.dlls[vdir]
        dest = self.dir / f"cyboman-{family}"
        target = dest / vdir / "amd_fidelityfx_upscaler_dx12.dll"
        if sha256_or_none(target) == want:
            return target
        errors = []
        for asset, sha in fam.archives:  # .7z first; the .zip is the fallback, as in the script
            try:
                archive = self._fetch_verified(self.xset.cyboman_url(asset), asset, sha)
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
