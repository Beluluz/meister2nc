"""Configuration and data-directory layout.

All state lives below one data directory (default ``./data``, override with
``--data`` or the ``M2NC_DATA`` environment variable):

    data/
      config.json          non-secret settings, merged over DEFAULTS
      secrets.json         optional, mode 0600: {"nc_password": "..."}
      tokens.json          OAuth tokens per Meister account label (mode 0600)
      raw/accounts/<label> verbatim API dumps per Meister account
      raw/files/           downloaded attachments and note files
      build/               generated Deck JSON / Markdown, reports
      state/               id mappings (what was imported where)
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "meister": {
        "mcp_url": "https://mcp.meistertask.com/mcp",
        "rest_base": "https://www.meistertask.com/api",
        # Public OAuth client id published by Meister for local MCP clients
        # (support article "MeisterTask MCP Server", 2026-10-18).
        "client_id": "XOmw6CwwLZ7-SbcknuAK4-5HXoC3mvea229Z3WFB1oE",
        "callback_port": 18920,
        "scope": "meistertask",
        "resource": "https://mcp.meistertask.com",
        # frontend CONFIG.meisternote.mediaEndpoint; files are <base>/note_file/<token>/<key>
        "note_media_base": "https://www.meisternote.com/app/api/media",
        "request_delay": 0.15,
        "include_trashed_tasks": False,
        "max_attachment_mb": 50,
    },
    "nextcloud": {
        "url": "https://cloud.example.org",
        "user": "",
        "password_env": "M2NC_NC_PASSWORD",
        # optional admin account, used only by `usermap` (listing users needs admin)
        "admin_user": "",
        "admin_password_env": "M2NC_NC_ADMIN_PASSWORD",
        "deck_api_version": "1.1",
        "request_delay": 0.05,
    },
    "deck": {
        "board_color": "0082c9",
        "test_suffix": " [TEST]",
        # Nextcloud group id that stands for the whole Meister team ("Team" project
        # memberships) → board ACL of type group; null = add members individually
        "team_group": None,
        # Meister Business groups → Nextcloud group ids, e.g. {"123456": "marketing"}
        "group_map": {},
        "card_footer": True,
        "comment_max_chars": 950,
        # also archive cards of tasks completed more than N days ago (keeps boards
        # small; Deck loads archived cards only on demand); null = only tasks that
        # are archived in MeisterTask
        "archive_completed_after_days": None,
    },
    "collectives": {
        # one collective per MeisterNote root note; per-root overrides by token:
        # {"<token>": {"name": "...", "groups": ["gid"], "admins": ["uid"], "members": ["uid"]}}
        # Members are otherwise derived: the root's author becomes Team admin and every
        # account whose extraction sees the root becomes a member.
        "roots": {},
        "test_suffix": " [TEST]",
        "include_discussions": True,
        "order_descending": False,
        "unescape_html_entities": True,
        "page_footer": True,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, data_dir: str | os.PathLike | None = None):
        root = data_dir or os.environ.get("M2NC_DATA") or "data"
        self.data_dir = Path(root).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = self.data_dir / "config.json"
        user_cfg = {}
        if cfg_file.exists():
            user_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.values = _merge(DEFAULTS, user_cfg)
        self._secrets: dict | None = None

    # --- sections -------------------------------------------------------
    @property
    def meister(self) -> dict:
        return self.values["meister"]

    @property
    def nextcloud(self) -> dict:
        return self.values["nextcloud"]

    @property
    def deck(self) -> dict:
        return self.values["deck"]

    @property
    def collectives(self) -> dict:
        return self.values["collectives"]

    # --- paths ----------------------------------------------------------
    @property
    def tokens_file(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    def account_dir(self, label: str) -> Path:
        return self.raw_dir / "accounts" / label

    @property
    def files_dir(self) -> Path:
        return self.raw_dir / "files"

    @property
    def build_dir(self) -> Path:
        return self.data_dir / "build"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    # --- secrets --------------------------------------------------------
    def secrets(self) -> dict:
        if self._secrets is None:
            f = self.data_dir / "secrets.json"
            self._secrets = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        return self._secrets

    def nc_password(self) -> str:
        env_name = self.nextcloud.get("password_env") or "M2NC_NC_PASSWORD"
        value = os.environ.get(env_name) or self.secrets().get("nc_password")
        if not value:
            raise SystemExit(
                f"Nextcloud app password missing: set ${env_name} or put "
                f'{{"nc_password": "..."}} into {self.data_dir / "secrets.json"}'
            )
        return value

    def nc_user(self) -> str:
        user = self.nextcloud.get("user") or os.environ.get("M2NC_NC_USER")
        if not user:
            raise SystemExit("Nextcloud user missing: set nextcloud.user in config.json or $M2NC_NC_USER")
        return user

    def nc_admin(self) -> tuple[str, str] | None:
        """(user, app password) of an optional admin account for the user mapping."""
        user = self.nextcloud.get("admin_user") or os.environ.get("M2NC_NC_ADMIN_USER")
        if not user:
            return None
        env_name = self.nextcloud.get("admin_password_env") or "M2NC_NC_ADMIN_PASSWORD"
        password = os.environ.get(env_name) or self.secrets().get("nc_admin_password")
        if not password:
            raise SystemExit(f"admin app password missing: set ${env_name} or nc_admin_password in secrets.json")
        return user, password
