"""OAuth 2.1 (authorization code + PKCE, public client) against Meister's
authorization server, as advertised by https://mcp.meistertask.com/.well-known/
oauth-authorization-server:

    authorization_endpoint  https://accounts.meister.co/oauth/authorize
    token_endpoint          https://accounts.meister.co/oauth/token
    grant types             authorization_code, refresh_token
    PKCE                    S256
    client auth             none (public client)
    dynamic registration    not offered → the published client id is used

Two ways to obtain a user's consent:

* ``login``  – local browser flow; a tiny HTTP server on the callback port
               receives the redirect.
* ``link`` + ``complete`` – for colleagues on other machines: generate the
               authorize URL for them, they sign in, their browser lands on
               ``http://localhost:<port>/callback?code=...`` (a connection
               error page on their machine), they send that URL back and the
               code is exchanged here.  The PKCE verifier never leaves this
               machine.

Tokens (incl. refresh tokens) are stored per account label in
``data/tokens.json`` (mode 0600) and refreshed on demand.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from .util import log, read_json, write_json


class OAuthError(RuntimeError):
    pass


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = read_json(self.path, {"accounts": {}, "pending": {}})
        self.data.setdefault("accounts", {})
        self.data.setdefault("pending", {})

    def save(self) -> None:
        write_json(self.path, self.data, mode=0o600)

    def labels(self) -> list[str]:
        return sorted(self.data["accounts"].keys())

    def get(self, label: str) -> dict | None:
        return self.data["accounts"].get(label)

    def put(self, label: str, token: dict) -> None:
        self.data["accounts"][label] = token
        self.save()

    def remove(self, label: str) -> None:
        self.data["accounts"].pop(label, None)
        self.data["pending"].pop(label, None)
        self.save()


class MeisterOAuth:
    def __init__(self, cfg_meister: dict, store: TokenStore, session: requests.Session | None = None):
        self.cfg = cfg_meister
        self.store = store
        self.http = session or requests.Session()
        self._metadata: dict | None = None

    # --- discovery --------------------------------------------------------
    def metadata(self) -> dict:
        if self._metadata is None:
            base = self.cfg["resource"].rstrip("/")
            resp = self.http.get(f"{base}/.well-known/oauth-authorization-server", timeout=30)
            resp.raise_for_status()
            self._metadata = resp.json()
        return self._metadata

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{int(self.cfg['callback_port'])}/callback"

    # --- PKCE -------------------------------------------------------------
    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    # --- step 1: authorize URL ------------------------------------------
    def build_authorize_url(self, label: str, scope: str | None = None) -> str:
        verifier, challenge = self._pkce_pair()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.cfg["client_id"],
            "redirect_uri": self.redirect_uri,
            "scope": scope or self.cfg.get("scope") or "meistertask",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": self.cfg["resource"],
        }
        self.store.data["pending"][label] = {
            "state": state,
            "verifier": verifier,
            "scope": params["scope"],
            "created_at": int(time.time()),
        }
        self.store.save()
        return self.metadata()["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)

    # --- step 2: exchange -------------------------------------------------
    def complete(self, label: str, callback_url_or_code: str) -> dict:
        pending = self.store.data["pending"].get(label)
        if not pending:
            raise OAuthError(f"no pending authorization for label {label!r}; run `auth link` or `auth login` first")
        code, state = self._parse_callback(callback_url_or_code)
        if state is not None and state != pending["state"]:
            raise OAuthError("state mismatch: the callback URL does not belong to this pending authorization")
        token = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.cfg["client_id"],
            "code_verifier": pending["verifier"],
            "resource": self.cfg["resource"],
        })
        token["label"] = label
        token["scope_requested"] = pending["scope"]
        self.store.put(label, token)
        self.store.data["pending"].pop(label, None)
        self.store.save()
        return token

    @staticmethod
    def _parse_callback(value: str) -> tuple[str, str | None]:
        value = value.strip()
        if "://" in value or value.startswith("/callback"):
            query = urllib.parse.urlparse(value).query
            qs = urllib.parse.parse_qs(query)
            if "error" in qs:
                raise OAuthError(f"authorization failed: {qs.get('error')} {qs.get('error_description', '')}")
            if "code" not in qs:
                raise OAuthError("callback URL contains no code parameter")
            return qs["code"][0], (qs.get("state") or [None])[0]
        return value, None

    def _token_request(self, form: dict) -> dict:
        resp = self.http.post(
            self.metadata()["token_endpoint"],
            data=form,
            headers={"Accept": "application/json"},
            timeout=60,
        )
        if resp.status_code != 200:
            raise OAuthError(f"token endpoint returned {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(f"token response without access_token: {body}")
        body["obtained_at"] = int(time.time())
        if "expires_in" in body:
            body["expires_at"] = body["obtained_at"] + int(body["expires_in"])
        return body

    # --- refresh ----------------------------------------------------------
    def refresh(self, label: str) -> dict:
        token = self.store.get(label)
        if not token:
            raise OAuthError(f"unknown account label {label!r}")
        if not token.get("refresh_token"):
            raise OAuthError(f"account {label!r} has no refresh token; re-authorize with `auth login`/`auth link`")
        new = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": self.cfg["client_id"],
            "resource": self.cfg["resource"],
        })
        merged = dict(token)
        merged.update(new)
        if "refresh_token" not in new:  # server did not rotate → keep old
            merged["refresh_token"] = token["refresh_token"]
        self.store.put(label, merged)
        log.info("refreshed token for %s", label)
        return merged

    def access_token(self, label: str, min_validity: int = 90) -> str:
        token = self.store.get(label)
        if not token:
            raise OAuthError(f"unknown account label {label!r}; run `auth login --label {label}`")
        expires_at = token.get("expires_at")
        if expires_at and expires_at - int(time.time()) < min_validity and token.get("refresh_token"):
            token = self.refresh(label)
        return token["access_token"]

    def token_provider(self, label: str):
        """Return a callable (force_refresh: bool) -> access token."""
        def provider(force_refresh: bool = False) -> str:
            if force_refresh:
                return self.refresh(label)["access_token"]
            return self.access_token(label)
        return provider

    # --- local browser flow ---------------------------------------------
    def login_local(self, label: str, scope: str | None = None, open_browser: bool = True, timeout: int = 600) -> dict:
        url = self.build_authorize_url(label, scope)
        port = int(self.cfg["callback_port"])
        received: dict = {}
        done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                received["url"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h2>meister2nc: Anmeldung erhalten.</h2>"
                    "<p>Dieses Fenster kann geschlossen werden.</p></body></html>".encode()
                )
                done.set()

            def log_message(self, *args):  # silence
                pass

        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print("\nOpen this URL in the browser of the Meister account to migrate:\n", flush=True)
            print(url + "\n", flush=True)
            if open_browser:
                try:
                    webbrowser.open(url)
                except Exception:  # pragma: no cover - environment specific
                    pass
            print(f"Waiting up to {timeout}s for the callback on http://localhost:{port}/callback …", flush=True)
            print("(If the browser shows a connection error instead, paste its full URL with: "
                  f"python3 -m meister2nc auth complete --label {label} '<url>')", flush=True)
            if not done.wait(timeout):
                raise OAuthError("timed out waiting for the OAuth callback")
        finally:
            server.shutdown()
            server.server_close()
        return self.complete(label, received["url"])
