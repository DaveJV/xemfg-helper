"""HTTP access. Everything network-bound goes through a Fetcher so tests can fake it."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol

Progress = Callable[[int, int | None], None]


class FetchError(RuntimeError):
    pass


class Fetcher(Protocol):
    def get_text(self, url: str, accept: str | None = None) -> str: ...
    def get_json(self, url: str): ...
    def final_url(self, url: str) -> str: ...
    def download(self, url: str, dest: Path, progress: Progress | None = None) -> None: ...


class HttpFetcher:
    """httpx-based fetcher with retries. Uses `gh auth token` when available (higher API limits)."""

    def __init__(self, retries: int = 3, timeout: float = 60.0):
        import httpx

        self.retries = retries
        headers = {"User-Agent": "xemfg"}
        token = _gh_token()
        self._api_headers = dict(headers, Authorization=f"Bearer {token}") if token else headers
        self.client = httpx.Client(follow_redirects=True, timeout=timeout, headers=headers)

    def _get(self, url: str, **kw):
        import httpx

        last = None
        for attempt in range(self.retries):
            try:
                headers = kw.pop("headers", {})
                if url.startswith("https://api.github.com/"):
                    headers = {**self._api_headers, **headers}
                r = self.client.get(url, headers=headers, **kw)
                if r.status_code == 404:
                    raise FetchError(f"404 Not Found: {url}")
                if r.status_code in (403, 429) and url.startswith("https://api.github.com/"):
                    raise FetchError(f"{r.status_code} from GitHub API (rate limited?): {url}")
                r.raise_for_status()
                return r
            except FetchError:
                raise
            except httpx.HTTPError as e:
                last = e
                time.sleep(1 + attempt)
        raise FetchError(f"{url}: {last}")

    def get_text(self, url: str, accept: str | None = None) -> str:
        return self._get(url, headers={"Accept": accept} if accept else {}).text

    def get_json(self, url: str):
        return self._get(url, headers={"Accept": "application/vnd.github+json"}).json()

    def final_url(self, url: str) -> str:
        return str(self._get(url).url)

    def download(self, url: str, dest: Path, progress: Progress | None = None) -> None:
        import httpx

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        last = None
        for attempt in range(self.retries):
            try:
                with self.client.stream("GET", url) as r:
                    if r.status_code == 404:
                        raise FetchError(f"404 Not Found: {url}")
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0) or None
                    done = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, total)
                tmp.replace(dest)
                return
            except FetchError:
                tmp.unlink(missing_ok=True)
                raise
            except httpx.HTTPError as e:
                last = e
                tmp.unlink(missing_ok=True)
                time.sleep(1 + attempt)
        raise FetchError(f"download failed {url}: {last}")


def _gh_token() -> str | None:
    import shutil
    import subprocess

    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
