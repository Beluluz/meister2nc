"""Connectivity and capability probes – run these before any extraction.

Meister side (needs one authorized account label):
  * MCP initialize, tool count, mt_whoami
  * REST /persons/me with the *same* OAuth token (one consent for both?)
  * download of a task attachment through the REST ``url``
  * download of a note file (``/media/note_file/…``) with the bearer token
Nextcloud side (needs user + app password):
  * status, logged-in user, Deck import systems, Collectives list,
    user listing permission (admin?)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import requests

from .accounts import mcp_client, note_file_auth, rest_client, token_store
from .config import Config
from .extract_notes import note_files
from .mcp_client import MCPError
from .mt_rest import MTError
from .nc_client import NCClient, NCError
from .util import log, parse_tool_payload


def probe_meister(cfg: Config, label: str, deep: bool = True) -> dict:
    out: dict = {"label": label}
    store = token_store(cfg)
    if not store.get(label):
        out["error"] = f"no token for label {label!r}; run `auth login --label {label}` first"
        return out
    tok = store.get(label)
    out["token"] = {"has_refresh_token": bool(tok.get("refresh_token")), "scope": tok.get("scope"),
                    "expires_at": tok.get("expires_at")}

    # MCP
    try:
        mcp = mcp_client(cfg, label)
        init = mcp.initialize()
        tools = mcp.list_tools()
        who = parse_tool_payload(mcp.call_tool("mt_whoami"))
        out["mcp"] = {"ok": True, "server": init.get("serverInfo"), "protocol": init.get("protocolVersion"),
                      "tools": len(tools), "whoami": who}
        log.info("MCP ok: %d tools, user %s %s (team %s)", len(tools), (who or {}).get("firstname"),
                 (who or {}).get("lastname"), ((who or {}).get("team") or {}).get("name"))
    except (MCPError, Exception) as exc:  # noqa: BLE001
        out["mcp"] = {"ok": False, "error": str(exc)}
        log.error("MCP failed: %s", exc)
        mcp = None

    # REST with the OAuth token
    rest = rest_client(cfg, label)
    try:
        me = rest.get("/persons/me")
        projects = rest.get("/projects", {"status": "all", "items": 500})
        out["rest"] = {"ok": True, "me": {k: me.get(k) for k in ("id", "firstname", "lastname", "email")},
                       "projects_visible": len(projects or [])}
        log.info("REST ok with this token: %s <%s>, %d projects visible", me.get("firstname"), me.get("email"),
                 len(projects or []))
    except (MTError, Exception) as exc:  # noqa: BLE001
        out["rest"] = {"ok": False, "error": str(exc)}
        log.error("REST with the OAuth token failed: %s", exc)
        projects = []

    if not deep:
        return out

    # task attachment download
    out["attachment_download"] = {"tested": False}
    if out.get("rest", {}).get("ok"):
        try:
            found = None
            for project in (projects or [])[:12]:
                tasks = rest.get(f"/projects/{project['id']}/tasks", {"items": 15, "page": 1}) or []
                for task in tasks:
                    atts = rest.get(f"/tasks/{task['id']}/attachments") or []
                    atts = [a for a in atts if a.get("source") == 3 and (a.get("url") or a.get("download_url"))]
                    if atts:
                        found = (task, atts[0])
                        break
                if found:
                    break
            if found:
                task, att = found
                url = att.get("url") or att.get("download_url")
                with tempfile.TemporaryDirectory() as tmp:
                    info = rest.download(url, Path(tmp) / "probe.bin", with_auth=True)
                out["attachment_download"] = {"tested": True, "task": task["id"], "attachment": att["id"],
                                              "name": att.get("name"), "fields": sorted(att.keys()), **info}
                log.info("attachment download: HTTP %s, %s bytes (%s)", info["status"], info["bytes"], att.get("name"))
            else:
                out["attachment_download"] = {"tested": False, "reason": "no MeisterTask-hosted attachment found in sample"}
        except Exception as exc:  # noqa: BLE001
            out["attachment_download"] = {"tested": True, "error": str(exc)}

    # note file download
    out["note_file_download"] = {"tested": False}
    if mcp is not None:
        try:
            roots = parse_tool_payload(mcp.call_tool("mt_notes_list", {"status": "ACTIVE"})) or []
            if isinstance(roots, dict):
                roots = roots.get("rows", [])
            candidates = []
            for root in roots[:3]:
                payload = parse_tool_payload(mcp.call_tool("mt_notes_children_list", {"token": root["token"]}))
                rows = payload["rows"] if isinstance(payload, dict) else (payload or [])
                candidates.extend([root] + rows[:60])
            tested = False
            for node in candidates:
                note = parse_tool_payload(mcp.call_tool("mt_notes_get", {"token": node["token"]})) or {}
                files = note_files(note.get("markdownContent", ""), node["token"])
                if not files:
                    continue
                f = files[0]
                url = f"{cfg.meister['note_media_base'].rstrip('/')}/note_file/{f['token']}/{f['key']}"
                resp = requests.get(url, headers=note_file_auth(cfg, label)(False), timeout=60)
                out["note_file_download"] = {"tested": True, "note": node["token"], "file": f["name"], "url": url,
                                             "status": resp.status_code, "bytes": len(resp.content),
                                             "content_type": resp.headers.get("Content-Type")}
                log.info("note file download: HTTP %s, %s bytes (%s)", resp.status_code, len(resp.content), f["name"])
                tested = True
                break
            if not tested:
                out["note_file_download"] = {"tested": False, "reason": "no note with files among the first candidates"}
        except Exception as exc:  # noqa: BLE001
            out["note_file_download"] = {"tested": True, "error": str(exc)}
    return out


def probe_nextcloud(cfg: Config, admin: bool = False) -> dict:
    out: dict = {"account": "admin" if admin else "migration"}
    try:
        if admin:
            user, password = cfg.nc_admin()
        else:
            user, password = cfg.nc_user(), cfg.nc_password()
        nc = NCClient(cfg.nextcloud["url"], user, password, cfg.nextcloud.get("deck_api_version", "1.1"))
    except (SystemExit, TypeError) as exc:
        return {"error": str(exc)}
    try:
        out["status"] = nc.status()
        me = nc.user_me()
        out["user"] = {"id": me.get("id"), "displayname": me.get("displayname"), "groups": me.get("groups")}
        log.info("Nextcloud %s, user %s (groups %s)", out["status"].get("versionstring"), me.get("id"), me.get("groups"))
    except NCError as exc:
        out["error"] = str(exc)
        log.error("Nextcloud login failed: %s", exc)
        return out
    try:
        systems = nc.deck_import_systems()
        out["deck_import_systems"] = systems
        log.info("Deck import API reachable: %s", systems)
    except NCError as exc:
        out["deck_import_systems"] = {"error": str(exc)}
        log.error("Deck import API not reachable: %s", exc)
    try:
        out["deck_boards"] = len(nc.deck_boards())
    except NCError as exc:
        out["deck_boards"] = {"error": str(exc)}
    try:
        colls = nc.collectives()
        out["collectives"] = [{"id": c.get("id"), "name": c.get("name"), "slug": c.get("slug")} for c in colls]
        log.info("Collectives reachable: %d collectives", len(colls))
    except NCError as exc:
        out["collectives"] = {"error": str(exc)}
        log.error("Collectives API not reachable: %s", exc)
    try:
        users = nc.users_details()
        out["users_details"] = {"ok": users is not None, "count": len(users or {})}
        log.info("user listing: %s", "ok (%d users)" % len(users) if users else "not permitted (no admin) → usermap.overrides.json or an admin account needed")
    except NCError as exc:
        out["users_details"] = {"ok": False, "error": str(exc)}
    try:
        groups = nc.groups()
        out["groups"] = groups
        if groups:
            log.info("groups (ids for deck.team_group / group_map): %s", groups)
    except NCError as exc:
        out["groups"] = {"error": str(exc)}
    return out
