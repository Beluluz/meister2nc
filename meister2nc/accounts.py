"""Account/token plumbing shared by the extract and probe commands."""

from __future__ import annotations

from .config import Config
from .mcp_client import MCPClient
from .mt_rest import MTRest
from .oauth import MeisterOAuth, OAuthError, TokenStore


def token_store(cfg: Config) -> TokenStore:
    return TokenStore(cfg.tokens_file)


def oauth(cfg: Config, store: TokenStore | None = None) -> MeisterOAuth:
    return MeisterOAuth(cfg.meister, store or token_store(cfg))


def mcp_client(cfg: Config, label: str) -> MCPClient:
    o = oauth(cfg)
    return MCPClient(cfg.meister["mcp_url"], o.token_provider(label), delay=float(cfg.meister.get("request_delay", 0.15)))


def note_file_auth(cfg: Config, label: str):
    """Headers for downloading note files from
    ``<note_media_base>/note_file/<token>/<key>`` (www.meisternote.com/app/api/media).

    Verified 2026-10-21: the MCP OAuth bearer token is accepted there.  The
    ``/media/…`` path quoted inside note Markdown is a shortened form that
    always answers 401."""
    store = token_store(cfg)
    if not store.get(label):
        raise OAuthError(f"unknown account label {label!r}")
    provider = MeisterOAuth(cfg.meister, store).token_provider(label)

    def headers(force_refresh: bool = False) -> dict:
        return {"Authorization": f"Bearer {provider(force_refresh)}"}
    return headers


def rest_client(cfg: Config, label: str) -> MTRest:
    """REST client for ``label``.  Uses a stored personal access token
    (``auth set-pat``) when present, otherwise the OAuth access token."""
    store = token_store(cfg)
    acct = store.get(label)
    if acct and acct.get("rest_pat"):
        pat = acct["rest_pat"]
        provider = lambda force=False: pat  # noqa: E731
    else:
        if not acct:
            raise OAuthError(f"unknown account label {label!r}")
        provider = MeisterOAuth(cfg.meister, store).token_provider(label)
    return MTRest(cfg.meister["rest_base"], provider, delay=float(cfg.meister.get("request_delay", 0.15)))
