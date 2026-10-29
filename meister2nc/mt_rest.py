"""MeisterTask REST API client (https://developers.meistertask.com).

Base URL ``https://www.meistertask.com/api``; bearer token; pagination with
``items`` (default 50, max 500) and ``page`` (1-based) until an empty page.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests

from .util import log


class MTError(RuntimeError):
    pass


class MTRest:
    def __init__(self, base: str, token_provider: Callable[[bool], str], delay: float = 0.15):
        self.base = base.rstrip("/")
        self.token_provider = token_provider
        self.delay = delay
        self.http = requests.Session()
        self._last = 0.0
        self._lock = threading.Lock()

    def _wait(self) -> None:
        with self._lock:
            gap = self.delay - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

    def request(self, method: str, path: str, params: dict | None = None, ok404: bool = False,
                ok403: bool = False, _retry: int = 0) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        self._wait()
        token = self.token_provider(False)
        try:
            resp = self.http.request(method, url, params=params, timeout=90,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except requests.RequestException as exc:  # timeouts, resets: retry with backoff
            if _retry < 4:
                log.warning("%s %s: %s – retrying", method, path, type(exc).__name__)
                time.sleep(5 * (_retry + 1))
                return self.request(method, path, params, ok404, ok403, _retry=_retry + 1)
            raise MTError(f"{method} {url} failed after retries: {exc}") from exc
        self._last = time.monotonic()
        if resp.status_code == 401 and _retry == 0:
            self.token_provider(True)
            return self.request(method, path, params, ok404, ok403, _retry=1)
        if resp.status_code == 429 and _retry < 6:
            delay = float(resp.headers.get("Retry-After") or 20)
            log.warning("MeisterTask rate limit, sleeping %.0fs", delay)
            time.sleep(delay)
            return self.request(method, path, params, ok404, ok403, _retry=_retry + 1)
        if resp.status_code >= 500 and _retry < 3:
            time.sleep(5 * (_retry + 1))
            return self.request(method, path, params, ok404, ok403, _retry=_retry + 1)
        if resp.status_code == 404 and ok404:
            return None
        if resp.status_code == 403 and ok403:
            return None
        if resp.status_code >= 400:
            raise MTError(f"{method} {url} → {resp.status_code}: {resp.text[:400]}")
        if not resp.content:
            return None
        return resp.json()

    def get(self, path: str, params: dict | None = None, **kw) -> Any:
        return self.request("GET", path, params, **kw)

    def get_all(self, path: str, params: dict | None = None, items: int = 500, **kw) -> list:
        """Follow ``page`` until an empty page or a short page."""
        out: list = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"items": items, "page": page})
            batch = self.get(path, query, **kw)
            if batch is None:
                return out if out else []
            if not isinstance(batch, list):
                return batch
            out.extend(batch)
            if len(batch) < items:
                break
            page += 1
        return out

    # --- downloads --------------------------------------------------------
    def download(self, url: str, dest, with_auth: bool = True, max_bytes: int | None = None) -> dict:
        """Download a file.  The first hop carries the bearer header; redirects
        to other hosts (e.g. signed storage URLs) are followed without it.
        Network errors are retried three times.
        Returns {"status": int|str, "bytes": int, "content_type": str, "final_url": str}."""
        last_exc = None
        for attempt in range(4):
            try:
                return self._download_once(url, dest, with_auth, max_bytes)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("download %s: %s – retrying", url.split("?")[0][-60:], type(exc).__name__)
                time.sleep(5 * (attempt + 1))
        return {"status": f"error: {type(last_exc).__name__}", "bytes": 0, "content_type": "", "final_url": url}

    def _download_once(self, url: str, dest, with_auth: bool, max_bytes: int | None) -> dict:
        self._wait()
        headers = {}
        if with_auth:
            headers["Authorization"] = f"Bearer {self.token_provider(False)}"
        resp = self.http.get(url, headers=headers, allow_redirects=False, stream=True, timeout=120)
        self._last = time.monotonic()
        hops = 0
        while resp.status_code in (301, 302, 303, 307, 308) and hops < 5:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                break
            if location.startswith("/"):
                from urllib.parse import urljoin
                location = urljoin(url, location)
            hops += 1
            same_host = location.split("/")[2] == url.split("/")[2] if "://" in location else True
            resp = self.http.get(location, headers=headers if same_host else {}, allow_redirects=False,
                                 stream=True, timeout=120)
        info = {"status": resp.status_code, "bytes": 0, "content_type": resp.headers.get("Content-Type", ""),
                "final_url": resp.url}
        if resp.status_code != 200:
            resp.close()
            return info
        total = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(1 << 16):
                if not chunk:
                    continue
                total += len(chunk)
                if max_bytes and total > max_bytes:
                    resp.close()
                    info["status"] = 413
                    return info
                fh.write(chunk)
        info["bytes"] = total
        return info
