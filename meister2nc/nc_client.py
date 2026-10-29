"""Nextcloud client: OCS, Deck REST (+ import API), Collectives OCS API, WebDAV.

Verified against:
* Deck ``docs/API.md`` and ``appinfo/routes.php`` (main, 2026-10)
* Deck ``BoardImportApiController``: ``POST /index.php/apps/deck/api/v{ver}/boards/import``
  with ``{system, config, data}``; owner is forced to the authenticated user
* Collectives ``openapi.json`` (OCS v2, ``/ocs/v2.php/apps/collectives/api/v1.0``)
* Provisioning API ``GET /ocs/v2.php/cloud/users/details`` (admin/sub-admin only)
* WebDAV ``/remote.php/dav/files/<user>/…`` with ``X-OC-MTime``
"""

from __future__ import annotations

import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import requests

from .util import log


class NCError(RuntimeError):
    pass


class NCClient:
    def __init__(self, url: str, user: str, password: str, deck_api_version: str = "1.1", delay: float = 0.05):
        self.url = url.rstrip("/")
        self.user = user
        self.http = requests.Session()
        self.http.auth = (user, password)
        self.http.headers.update({"OCS-APIRequest": "true", "Accept": "application/json"})
        self.deck_base = f"{self.url}/index.php/apps/deck/api/v{deck_api_version}"
        self.deck_ocs = f"{self.url}/ocs/v2.php/apps/deck/api/v1.0"
        self.coll_base = f"{self.url}/ocs/v2.php/apps/collectives/api/v1.0"
        self.dav_base = f"{self.url}/remote.php/dav/files/{urllib.parse.quote(user)}"
        self.delay = delay
        self._last = 0.0

    def _wait(self):
        gap = self.delay - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)

    def _req(self, method: str, url: str, *, json_body: Any = None, data: Any = None, files: Any = None,
             headers: dict | None = None, params: dict | None = None, ok: tuple = (), _retry: int = 0) -> requests.Response:
        self._wait()
        hdrs = dict(headers or {})
        if json_body is not None:
            hdrs["Content-Type"] = "application/json"
            data = json.dumps(json_body)
        try:
            resp = self.http.request(method, url, data=data, files=files, headers=hdrs, params=params, timeout=300)
        except requests.RequestException as exc:  # network blip: retry a few times with backoff
            if _retry < 4:
                log.warning("%s %s: %s – retrying in %ds", method, url.split("?")[0][-70:], type(exc).__name__, 10 * (_retry + 1))
                time.sleep(10 * (_retry + 1))
                return self._req(method, url, json_body=json_body, data=data, files=files, headers=headers,
                                 params=params, ok=ok, _retry=_retry + 1)
            raise NCError(f"{method} {url} failed after retries: {exc}") from exc
        self._last = time.monotonic()
        if resp.status_code == 429 and _retry < 5:
            time.sleep(float(resp.headers.get("Retry-After") or 10))
            return self._req(method, url, json_body=json_body, data=data, files=files, headers=headers,
                             params=params, ok=ok, _retry=_retry + 1)
        if resp.status_code in (502, 503, 504) and _retry < 3:
            time.sleep(5 * (_retry + 1))
            return self._req(method, url, json_body=json_body, data=data, files=files, headers=headers,
                             params=params, ok=ok, _retry=_retry + 1)
        if resp.status_code >= 400 and resp.status_code not in ok:
            raise NCError(f"{method} {url} → {resp.status_code}: {resp.text[:600]}")
        return resp

    @staticmethod
    def _unwrap(resp: requests.Response) -> Any:
        if not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError:
            raise NCError(f"non-JSON response from {resp.url}: {resp.text[:300]}")
        if isinstance(body, dict) and "ocs" in body:
            meta = body["ocs"].get("meta", {})
            if meta.get("statuscode") not in (100, 200, None):
                raise NCError(f"OCS error {meta.get('statuscode')}: {meta.get('message')}")
            return body["ocs"].get("data")
        return body

    # --- generic ----------------------------------------------------------
    def status(self) -> dict:
        return self._req("GET", f"{self.url}/status.php").json()

    def capabilities(self) -> dict:
        return self._unwrap(self._req("GET", f"{self.url}/ocs/v2.php/cloud/capabilities", params={"format": "json"}))

    def users_details(self, limit: int = 200, include_disabled: bool = True) -> dict | None:
        """All users with details (admin or sub-admin), enabled ones from
        ``/cloud/users/details`` and, optionally, disabled ones from
        ``/cloud/users/disabled`` (each entry carries ``enabled``).  Returns
        ``None`` when the account may not list users (empty result for a non-admin)."""
        users: dict = {}
        endpoints = ["/ocs/v2.php/cloud/users/details"] + (["/ocs/v2.php/cloud/users/disabled"] if include_disabled else [])
        for i, endpoint in enumerate(endpoints):
            offset = 0
            while True:
                data = self._unwrap(self._req("GET", f"{self.url}{endpoint}",
                                              params={"limit": limit, "offset": offset, "format": "json"}, ok=(403,)))
                batch = (data or {}).get("users") or {}
                if isinstance(batch, list):  # empty PHP array encodes as []
                    batch = {}
                for uid, info in batch.items():
                    if isinstance(info, dict):
                        info.setdefault("enabled", i == 0)
                        if i == 1:
                            info["enabled"] = False
                    users[uid] = info
                if len(batch) < limit:
                    break
                offset += limit
        return users or None

    def groups(self) -> list:
        """Group ids (admin or sub-admin); Deck ACL entries of type 1 use these ids."""
        data = self._unwrap(self._req("GET", f"{self.url}/ocs/v2.php/cloud/groups", params={"format": "json"}, ok=(403,)))
        return list((data or {}).get("groups") or [])

    def user_me(self) -> dict:
        return self._unwrap(self._req("GET", f"{self.url}/ocs/v2.php/cloud/user", params={"format": "json"}))

    # --- Deck -------------------------------------------------------------
    def deck_get(self, path: str) -> Any:
        return self._unwrap(self._req("GET", f"{self.deck_base}{path}", params={"format": "json"}))

    def deck_put(self, path: str, body: dict) -> Any:
        return self._unwrap(self._req("PUT", f"{self.deck_base}{path}", json_body=body, params={"format": "json"}))

    def deck_post(self, path: str, body: dict) -> Any:
        return self._unwrap(self._req("POST", f"{self.deck_base}{path}", json_body=body, params={"format": "json"}))

    def deck_delete(self, path: str) -> Any:
        return self._unwrap(self._req("DELETE", f"{self.deck_base}{path}", params={"format": "json"}))

    def deck_import_systems(self) -> Any:
        return self.deck_get("/boards/import/getSystems")

    def deck_import(self, board_data: dict, owner_uid: str) -> dict:
        """Import one or more boards in Deck JSON format.  ``board_data`` is
        ``{"boards": [...]}``.  The server forces the owner to the logged-in user."""
        payload = {
            "system": "DeckJson",
            "config": {"owner": owner_uid, "uidRelation": {"meister2nc": owner_uid}},
            "data": board_data,
        }
        resp = self._req("POST", f"{self.deck_base}/boards/import", json_body=payload, params={"format": "json"})
        return self._unwrap(resp)

    def deck_boards(self) -> list:
        return self.deck_get("/boards") or []

    def deck_board(self, board_id: int) -> dict:
        return self.deck_get(f"/boards/{board_id}")

    def deck_stacks(self, board_id: int, archived: bool = False) -> list:
        return self.deck_get(f"/boards/{board_id}/stacks" + ("/archived" if archived else "")) or []

    def deck_card(self, board_id: int, stack_id: int, card_id: int) -> dict:
        return self.deck_get(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}")

    def deck_update_card(self, board_id: int, stack_id: int, card_id: int, card: dict) -> dict:
        body = {
            "title": card["title"],
            "description": card.get("description") or "",
            "type": card.get("type") or "plain",
            "order": card.get("order", 0),
            "duedate": card.get("duedate"),
            "archived": bool(card.get("archived")),
            "done": card.get("done"),
        }
        if card.get("owner"):
            owner = card["owner"]
            body["owner"] = owner.get("uid") if isinstance(owner, dict) else owner
        return self.deck_put(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}", body)

    def deck_card_archive(self, board_id: int, stack_id: int, card_id: int, archive: bool = True) -> Any:
        action = "archive" if archive else "unarchive"
        return self.deck_put(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/{action}", {})

    def deck_board_archive(self, board: dict, archived: bool) -> Any:
        return self.deck_put(f"/boards/{board['id']}", {"title": board["title"], "color": board.get("color") or "0082c9",
                                                         "archived": archived})

    def deck_add_dependency(self, board_id: int, stack_id: int, card_id: int, dependent_card_id: int) -> Any:
        """Deck ≥ 1.18: card ``card_id`` lists ``dependent_card_id`` as dependent card
        (route card_api#assignDependentCard; idempotent server-side)."""
        return self.deck_post(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/dependentCards/{dependent_card_id}", {})

    def deck_create_stack(self, board_id: int, title: str, order: int) -> dict:
        return self.deck_post(f"/boards/{board_id}/stacks", {"title": title, "order": order})

    def deck_reorder_card(self, board_id: int, stack_id: int, card_id: int, target_stack_id: int, order: int) -> Any:
        """Move a card to a stack (also on another board – CardService::reorder has
        no same-board check, verified in stable34)."""
        return self.deck_put(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/reorder",
                             {"stackId": target_stack_id, "order": order})

    def deck_assign_label(self, board_id: int, stack_id: int, card_id: int, label_id: int, assign: bool = True) -> Any:
        action = "assignLabel" if assign else "removeLabel"
        return self.deck_put(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/{action}", {"labelId": label_id})

    def deck_assign_user(self, board_id: int, stack_id: int, card_id: int, uid: str, assign: bool = True) -> Any:
        action = "assignUser" if assign else "unassignUser"
        return self.deck_put(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/{action}", {"userId": uid})

    def deck_delete_card(self, board_id: int, stack_id: int, card_id: int) -> Any:
        return self.deck_delete(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}")

    def deck_upload_attachment(self, board_id: int, stack_id: int, card_id: int, name: str, content: bytes,
                               mimetype: str = "application/octet-stream") -> Any:
        """Upload a file attachment.  The REST route answers 400 for multipart
        uploads (verified 2026-10-22); the OCS route ``attachment_ocs#create``
        accepts them."""
        resp = self._req("POST", f"{self.deck_ocs}/cards/{card_id}/attachment",
                         data={"type": "file"}, files={"file": (name, content, mimetype)}, params={"format": "json"})
        return self._unwrap(resp)

    def deck_remove_dependency(self, board_id: int, stack_id: int, card_id: int, dependent_card_id: int) -> Any:
        return self.deck_delete(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/dependentCards/{dependent_card_id}")

    def deck_card_attachments(self, board_id: int, stack_id: int, card_id: int) -> list:
        return self.deck_get(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments") or []

    def deck_delete_attachment(self, board_id: int, stack_id: int, card_id: int, att_type: str, attachment_id: int) -> Any:
        return self.deck_delete(f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments/{att_type}/{attachment_id}")

    def deck_delete_board(self, board_id: int) -> bool:
        """Soft-delete a board.  Deck answers 403 *after* a successful delete of an
        archived board (the response can no longer be read), so the result is
        verified against the board list.

        Callers must remove file attachments first (``deck_purge_attachments``):
        a soft-deleted board that still owns ``file`` attachments breaks Deck's
        share provider for the owner – WebDAV listings answer 500 and other
        boards' attachments 403 until the board is restored or purged
        (observed on Nextcloud 34 / Deck, 2026-10-22)."""
        try:
            self.deck_delete(f"/boards/{board_id}")
        except NCError as exc:
            if "403" not in str(exc):
                raise
        for b in self.deck_boards():
            if int(b.get("id", 0)) == int(board_id):
                return bool(b.get("deletedAt"))
        return True

    def deck_purge_attachments(self, board_id: int) -> int:
        """Delete every file attachment of a board (unarchiving cards as needed)."""
        removed = 0
        for archived in (False, True):
            for stack in self.deck_stacks(board_id, archived=archived):
                for card in stack.get("cards") or []:
                    if not card.get("attachmentCount") and not card.get("attachments"):
                        continue
                    atts = self.deck_card_attachments(board_id, stack["id"], card["id"])
                    if not atts:
                        continue
                    if card.get("archived"):
                        self.deck_card_archive(board_id, stack["id"], card["id"], archive=False)
                    for att in atts:
                        try:
                            self.deck_delete_attachment(board_id, stack["id"], card["id"], att.get("type") or "file", att["id"])
                            removed += 1
                        except NCError as exc:
                            log.warning("could not delete attachment %s of card %s: %s", att.get("id"), card["id"], exc)
        return removed

    def deck_card_url(self, card_id: int) -> str:
        return f"{self.url}/index.php/apps/deck/card/{card_id}"

    # --- Collectives ------------------------------------------------------
    def coll(self, method: str, path: str, body: dict | None = None, files=None, ok: tuple = ()) -> Any:
        resp = self._req(method, f"{self.coll_base}{path}", json_body=body if files is None else None,
                         files=files, data=None if files is None else (body or {}),
                         params={"format": "json"}, ok=ok)
        if resp.status_code >= 400:
            return None
        return self._unwrap(resp)

    def collectives(self) -> list:
        return (self.coll("GET", "/collectives") or {}).get("collectives", [])

    def collective_create(self, name: str, emoji: str | None = None) -> dict:
        body = {"name": name}
        if emoji:
            body["emoji"] = emoji
        return (self.coll("POST", "/collectives", body) or {}).get("collective")

    def pages(self, collective_id: int) -> list:
        return (self.coll("GET", f"/collectives/{collective_id}/pages") or {}).get("pages", [])

    def page(self, collective_id: int, page_id: int) -> dict | None:
        data = self.coll("GET", f"/collectives/{collective_id}/pages/{page_id}", ok=(404,))
        return (data or {}).get("page") if data else None

    def page_create(self, collective_id: int, parent_id: int, title: str) -> dict:
        return (self.coll("POST", f"/collectives/{collective_id}/pages/{parent_id}", {"title": title}) or {}).get("page")

    def page_move(self, collective_id: int, page_id: int, parent_id: int | None = None, title: str | None = None,
                  index: int | None = None) -> dict:
        body: dict = {}
        if parent_id is not None:
            body["parentId"] = parent_id
        if title is not None:
            body["title"] = title
        if index is not None:
            body["index"] = index
        return (self.coll("PUT", f"/collectives/{collective_id}/pages/{page_id}", body) or {}).get("page")

    def page_set_subpage_order(self, collective_id: int, page_id: int, order: list[int]) -> dict:
        return (self.coll("PUT", f"/collectives/{collective_id}/pages/{page_id}/subpageOrder",
                          {"subpageOrder": json.dumps(order)}) or {}).get("page")

    def page_set_emoji(self, collective_id: int, page_id: int, emoji: str | None) -> dict:
        return (self.coll("PUT", f"/collectives/{collective_id}/pages/{page_id}/emoji", {"emoji": emoji}) or {}).get("page")

    def collective_set_emoji(self, collective_id: int, emoji: str | None) -> dict:
        return (self.coll("PUT", f"/collectives/{collective_id}", {"emoji": emoji}) or {}).get("collective")

    def page_attachments(self, collective_id: int, page_id: int) -> list:
        return (self.coll("GET", f"/collectives/{collective_id}/pages/{page_id}/attachments") or {}).get("attachments", [])

    def page_upload_attachment(self, collective_id: int, page_id: int, name: str, content: bytes, mimetype: str) -> dict:
        data = self.coll("POST", f"/collectives/{collective_id}/pages/{page_id}/attachments",
                         files={"file": (name, content, mimetype or "application/octet-stream")})
        return (data or {}).get("attachment")

    def page_trash(self, collective_id: int, page_id: int) -> Any:
        return self.coll("DELETE", f"/collectives/{collective_id}/pages/{page_id}", ok=(404,))

    # --- Teams (circles app) – a collective's members are its Team's members ---
    TEAM_TYPE_USER = 1
    TEAM_TYPE_GROUP = 2
    TEAM_LEVEL_MEMBER = 1
    TEAM_LEVEL_MODERATOR = 4
    TEAM_LEVEL_ADMIN = 8
    TEAM_LEVEL_OWNER = 9

    def team_members(self, circle_id: str) -> list:
        data = self._unwrap(self._req("GET", f"{self.url}/ocs/v2.php/apps/circles/circles/{circle_id}/members",
                                      params={"format": "json"}))
        return list(data or [])

    def team_add_member(self, circle_id: str, member_id: str, member_type: int) -> dict:
        """circles ``Local#memberAdd``: POST /circles/{circleId}/members {userId, type};
        caller must be at least moderator of the Team.  Idempotent for our purpose:
        an existing member is returned, not duplicated."""
        prefix = "group:" if member_type == self.TEAM_TYPE_GROUP else "user:"
        for m in self.team_members(circle_id):
            # groups come back with userType 16 (represented as a sub-team), so match on
            # the id and the "group:"/"user:" prefix of basedOn.name instead
            if m.get("userId") == member_id and str((m.get("basedOn") or {}).get("name", "")).startswith(prefix):
                return m
        data = self._unwrap(self._req("POST", f"{self.url}/ocs/v2.php/apps/circles/circles/{circle_id}/members",
                                      json_body={"userId": member_id, "type": member_type}, params={"format": "json"}))
        return data or {}

    def team_remove_member(self, circle_id: str, member_id: str) -> Any:
        return self._unwrap(self._req("DELETE", f"{self.url}/ocs/v2.php/apps/circles/circles/{circle_id}/members/{member_id}",
                                      params={"format": "json"}))

    def team_set_level(self, circle_id: str, member_id: str, level: int) -> dict:
        data = self._unwrap(self._req("PUT", f"{self.url}/ocs/v2.php/apps/circles/circles/{circle_id}/members/{member_id}/level",
                                      json_body={"level": level}, params={"format": "json"}))
        return data or {}

    def collective_link(self, collective: dict) -> str:
        url_path = f"{collective['slug']}-{collective['id']}" if collective.get("slug") else collective["name"]
        return f"{self.url}/index.php/apps/collectives/{urllib.parse.quote(url_path)}"

    def page_link(self, collective: dict, page: dict) -> str:
        base = self.collective_link(collective)
        if page.get("slug"):
            return f"{base}/{urllib.parse.quote(page['slug'] + '-' + str(page['id']))}"
        path = "/".join(urllib.parse.quote(p) for p in (page.get("filePath") or "").split("/") if p)
        title = "" if page.get("fileName") == "Readme.md" else urllib.parse.quote(page.get("title", ""))
        parts = [p for p in (path, title) if p]
        link = base + ("/" + "/".join(parts) if parts else "")
        if page.get("parentId"):
            link += f"?fileId={page['id']}"
        return link

    # --- WebDAV -----------------------------------------------------------
    def dav_path(self, *segments: str) -> str:
        parts = []
        for seg in segments:
            for piece in str(seg).split("/"):
                if piece:
                    parts.append(urllib.parse.quote(piece))
        return self.dav_base + "/" + "/".join(parts)

    def dav_put(self, path_segments: list[str], content: bytes, mtime: int | None = None,
                content_type: str = "text/markdown; charset=utf-8") -> int:
        headers = {"Content-Type": content_type}
        if mtime:
            headers["X-OC-MTime"] = str(int(mtime))
        resp = self._req("PUT", self.dav_path(*path_segments), data=content, headers=headers)
        return resp.status_code

    def dav_mkcol(self, path_segments: list[str]) -> int:
        resp = self._req("MKCOL", self.dav_path(*path_segments), ok=(405,))
        return resp.status_code

    def dav_propfind(self, path_segments: list[str], depth: int = 0) -> list[dict]:
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                '<d:prop><oc:fileid/><d:getlastmodified/><d:getcontentlength/><d:resourcetype/></d:prop></d:propfind>')
        resp = self._req("PROPFIND", self.dav_path(*path_segments), data=body.encode(),
                         headers={"Depth": str(depth), "Content-Type": "application/xml"}, ok=(404,))
        if resp.status_code == 404:
            return []
        root = ET.fromstring(resp.content)
        ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
        out = []
        for item in root.findall("d:response", ns):
            href = item.findtext("d:href", default="", namespaces=ns)
            fileid = item.findtext(".//oc:fileid", default=None, namespaces=ns)
            is_dir = item.find(".//d:resourcetype/d:collection", ns) is not None
            out.append({"href": urllib.parse.unquote(href), "fileid": int(fileid) if fileid else None, "dir": is_dir})
        return out

    def dav_get(self, path_segments: list[str]) -> bytes | None:
        resp = self._req("GET", self.dav_path(*path_segments), ok=(404,))
        if resp.status_code == 404:
            return None
        return resp.content
