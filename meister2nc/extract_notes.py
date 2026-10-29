"""Stage 1b – dump MeisterNote content through the Meister MCP server.

Observed server behaviour (2026-10-21):
* ``mt_notes_list``          → root notes only (JSON array)
* ``mt_notes_children_list`` → the *whole subtree* of a note, flattened, in
  tree-walk order (all direct children first); large results come as a
  compact ``[N]{cols}:`` CSV table and are cut at ~100 000 characters with
  a "Response truncated" notice.  The walker therefore recurses into every
  direct child whenever a listing is incomplete and compares the union with
  the claimed count.
* ``mt_notes_get``           → JSON with ``markdownContent`` (Markdown plus
  ``<!-- meisternote:<type> {json} -->`` block markers), ``revisionId``,
  ``taskLinks``, ``parentId``, ``rootId``.
* note files are referenced as ``/media/note_file/<token>/<fileKey>`` inside
  the Markdown; the downloadable form is
  ``https://www.meisternote.com/app/api/media/note_file/<token>/<fileKey>``
  with the OAuth bearer token (verified 2026-10-21).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from .accounts import mcp_client, note_file_auth
from .config import Config
from .mcp_client import MCPError
from .util import log, now_epoch, parse_tool_payload, read_json, safe_filename, tool_texts, write_json

FILE_MARKER = re.compile(r"<!--\s*meisternote:file\s+(\{.*?\})\s*-->", re.S)
MEDIA_URL = re.compile(r"/media/note_file/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)")
FOUND_RE = re.compile(r"Found (\d+) (?:child )?note", re.I)


class NoteWalker:
    def __init__(self, mcp, status: str = "ACTIVE"):
        self.mcp = mcp
        self.status = status
        self.nodes: dict[int, dict] = {}
        self.calls = 0
        self.gaps: list[dict] = []

    def _children(self, token: str) -> tuple[int | None, list[dict]]:
        result = self.mcp.call_tool("mt_notes_children_list", {"token": token, "status": self.status})
        self.calls += 1
        payload = parse_tool_payload(result)
        claimed = None
        for text in tool_texts(result):
            m = FOUND_RE.search(text)
            if m:
                claimed = int(m.group(1))
                break
        rows: list[dict] = []
        if isinstance(payload, dict) and "rows" in payload:
            rows = payload["rows"]
            if claimed is None:
                claimed = payload.get("__claimed__")
        elif isinstance(payload, list):
            rows = payload
            if claimed is None:
                claimed = len(rows)
        rows = [r for r in rows if isinstance(r, dict) and r.get("id") and r.get("token")]
        return claimed, rows

    def walk(self, root_token: str, root_id: int) -> None:
        claimed, rows = self._children(root_token)
        for row in rows:
            self.nodes[int(row["id"])] = row
        complete = claimed is not None and len(rows) >= claimed
        log.info("subtree of %s: %s claimed, %d received%s", root_token, claimed, len(rows),
                 "" if complete else " → recursing per direct child")
        if complete:
            return
        direct = [r for r in rows if r.get("parentId") == root_id]
        if not direct and rows:
            self.gaps.append({"token": root_token, "claimed": claimed, "received": len(rows),
                              "reason": "truncated listing without direct children"})
            return
        for child in direct:
            self.walk(child["token"], int(child["id"]))
        got = sum(1 for n in self.nodes.values() if self._is_descendant(int(n["id"]), root_id))
        if claimed is not None and got < claimed:
            self.gaps.append({"token": root_token, "claimed": claimed, "received": got,
                              "reason": "descendants unreachable through direct children"})

    def _is_descendant(self, node_id: int, ancestor: int) -> bool:
        seen = set()
        cur = self.nodes.get(node_id)
        while cur and cur.get("parentId") is not None:
            pid = cur["parentId"]
            if pid == ancestor:
                return True
            if pid in seen:
                return False
            seen.add(pid)
            cur = self.nodes.get(pid)
        return False


def run(cfg: Config, label: str, root_tokens: list[str] | None = None, limit: int | None = None,
        refresh: bool = False, with_files: bool = True, statuses: tuple[str, ...] = ("ACTIVE",),
        with_discussions: bool = True, all_roots: bool = False) -> dict:
    mcp = mcp_client(cfg, label)
    mcp.initialize()
    ndir = cfg.account_dir(label) / "notes"
    ndir.mkdir(parents=True, exist_ok=True)
    manifest = read_json(ndir / "manifest.json", {}) or {}
    manifest.update({"label": label, "extracted_at": now_epoch(), "limit": limit, "gaps": [], "roots": []})

    roots: list[dict] = []
    for status in statuses:
        payload = parse_tool_payload(mcp.call_tool("mt_notes_list", {"status": status}))
        rows = payload["rows"] if isinstance(payload, dict) and "rows" in payload else (payload or [])
        for row in rows:
            if isinstance(row, dict) and row.get("token"):
                row["_status_filter"] = status
                roots.append(row)
    if root_tokens:
        roots = [r for r in roots if r["token"] in root_tokens]
        known = {r["token"] for r in roots}
        for token in root_tokens:
            if token not in known:  # a sub-note used as root: fetch metadata
                meta = parse_tool_payload(mcp.call_tool("mt_notes_get", {"token": token}))
                roots.append({"id": meta["id"], "token": token, "name": meta.get("name"), "parentId": meta.get("parentId"),
                              "type": "NOTE", "status": "ACTIVE", "_status_filter": "ACTIVE", "sequence": 0})
    log.info("roots: %s", [r.get("name") for r in roots])
    # roots another account already extracted: keep only a stub (for membership
    # derivation), do not walk or download them again
    elsewhere: dict[str, str] = {}
    if not all_roots:
        for acct_dir in sorted((cfg.raw_dir / "accounts").iterdir()):
            if acct_dir.name == label:
                continue
            for n in read_json(acct_dir / "notes" / "tree.json") or []:
                if n.get("_root") and not n.get("_stub") and n.get("token"):
                    elsewhere.setdefault(n["token"], acct_dir.name)
    stubs = [r for r in roots if r["token"] in elsewhere and not (root_tokens and r["token"] in root_tokens)]
    roots = [r for r in roots if r not in stubs]
    for r in stubs:
        log.info("root %r already extracted by account %s – recorded as stub only", r.get("name"), elsewhere[r["token"]])

    # person names (MCP has no e-mails, but names for note authors / discussions)
    try:
        people = parse_tool_payload(mcp.call_tool("mt_persons_list")) or []
        if isinstance(people, dict):
            people = people.get("rows") or people.get("persons") or []
        names = read_json(cfg.state_dir / "persons_names.json", {}) or {}
        for p in people:
            if isinstance(p, dict) and p.get("id"):
                names[str(p["id"])] = " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x) or str(p["id"])
        write_json(cfg.state_dir / "persons_names.json", names)
    except MCPError as exc:
        log.debug("mt_persons_list failed: %s", exc)

    tree: dict[int, dict] = {}
    for r in stubs:
        tree[int(r["id"])] = dict(r, _root=True, _stub=True)
    for root in roots:
        tree[int(root["id"])] = dict(root, _root=True)
        for status in statuses:
            walker = NoteWalker(mcp, status)
            walker.walk(root["token"], int(root["id"]))
            tree.update(walker.nodes)
            manifest["gaps"].extend(walker.gaps)
            manifest["roots"].append({"token": root["token"], "name": root.get("name"), "status": status,
                                      "nodes": len(walker.nodes), "calls": walker.calls})
    # merge with the tree from earlier runs so that `--root` runs never shrink it
    tree_file = ndir / "tree.json"
    previous = {int(r["id"]): r for r in (read_json(tree_file) or []) if r.get("id")} if root_tokens else {}
    full_tree = dict(previous)
    full_tree.update(tree)
    for node in full_tree.values():
        node["_root"] = node.get("parentId") is None or node.get("parentId") not in full_tree
    write_json(tree_file, list(full_tree.values()))
    log.info("tree: %d notes (%d in this run)", len(full_tree), len(tree))
    if manifest["gaps"]:
        log.warning("tree gaps: %s", manifest["gaps"])

    order_full = [n for n in bfs_order(full_tree) if not n.get("_stub")]
    this_run = {int(n["id"]) for n in tree.values() if not n.get("_stub")}
    order = [n for n in order_full if int(n["id"]) in this_run]
    if limit:
        order = order[:limit]
    files_ok: bool | None = None  # re-tested on every run; a rejection stops downloads for this run only
    media_base = cfg.meister["note_media_base"].rstrip("/")
    auth_headers = note_file_auth(cfg, label)
    done = 0
    counts = {"notes": 0, "skipped": 0, "files": 0, "files_failed": 0, "discussions": 0}
    for node in order:
        token = node["token"]
        nfile = ndir / f"{token}.json"
        existing = read_json(nfile)
        # The listing's updatedAt is NOT bumped by content edits (verified 2026-10-23:
        # revision 4 → 5 with unchanged updatedAt), so every note is fetched once and
        # its revisionId compared; discussions are only re-read when it changed.
        try:
            payload = parse_tool_payload(mcp.call_tool("mt_notes_get", {"token": token})) or {}
        except MCPError as exc:
            log.warning("mt_notes_get %s failed: %s – kept previous dump, retried next run", token, str(exc)[:120])
            counts["failed"] = counts.get("failed", 0) + 1
            if not existing:  # remember the failure so the import skips the note
                write_json(nfile, {"_error": str(exc)[:300], "_tree": node, "name": node.get("name"), "token": token})
            continue
        unchanged = (existing and not refresh and payload.get("revisionId") is not None
                     and payload.get("revisionId") == existing.get("revisionId") and "_extracted_at" in existing)
        if unchanged:
            if existing.get("_tree") != node:  # moved / renamed / reordered
                existing["_tree"] = node
                existing["_tree_updatedAt"] = node.get("updatedAt")
                write_json(nfile, existing)
            counts["skipped"] += 1
        else:
            payload["_tree_updatedAt"] = node.get("updatedAt")
            payload["_tree"] = node
            payload["_extracted_at"] = now_epoch()
            if with_discussions and payload.get("id"):
                payload["_discussions"] = _discussions(mcp, int(payload["id"]))
                counts["discussions"] += len(payload["_discussions"])
            write_json(nfile, payload)
            existing = payload
            counts["notes"] += 1
        if with_files and existing and existing.get("markdownContent") and files_ok is not False:
            ok, n_ok, n_fail = _download_note_files(cfg, token, existing, media_base, auth_headers, ndir)
            counts["files"] += n_ok
            counts["files_failed"] += n_fail
            if ok is not None:
                files_ok = ok
                manifest["files_download_ok"] = ok
        done += 1
        if done % 25 == 0:
            log.info("  %d/%d notes", done, len(order))
            write_json(ndir / "manifest.json", manifest)
    manifest["counts"] = counts
    manifest["order"] = [n["token"] for n in order_full]
    manifest["processed"] = [n["token"] for n in order]
    write_json(ndir / "manifest.json", manifest)
    log.info("notes extraction complete: %s (file download ok: %s)", counts, files_ok)
    return manifest


def _discussions(mcp, note_id: int) -> list:
    out = []
    for status in ("ACTIVE", "RESOLVED"):
        try:
            payload = parse_tool_payload(mcp.call_tool("mt_notes_discussions_list", {"noteId": note_id, "status": status}))
        except MCPError as exc:
            log.debug("discussions %s %s: %s", note_id, status, exc)
            continue
        rows = payload["rows"] if isinstance(payload, dict) and "rows" in payload else payload
        if isinstance(rows, dict):
            rows = rows.get("discussions") or rows.get("items") or []
        for row in rows or []:
            if isinstance(row, dict):
                row["_status"] = status
                out.append(row)
    return out


def note_files(markdown: str, token: str) -> list[dict]:
    """All files referenced by a note: marker JSON plus bare media URLs."""
    files: dict[str, dict] = {}
    for m in FILE_MARKER.finditer(markdown or ""):
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        key = meta.get("fileKey")
        if key:
            files[key] = {"key": key, "name": meta.get("fileName") or key, "content_type": meta.get("fileContentType") or "",
                          "token": token}
    for m in MEDIA_URL.finditer(markdown or ""):
        tok, key = m.group(1), m.group(2)
        files.setdefault(key, {"key": key, "name": key, "content_type": "", "token": tok})
    return list(files.values())


def _download_note_files(cfg: Config, token: str, note: dict, media_base: str, auth_headers, ndir: Path):
    files = note_files(note.get("markdownContent", ""), token)
    if not files:
        return None, 0, 0
    ffile = ndir / f"{token}.files.json"
    state = read_json(ffile, {}) or {}
    n_ok = n_fail = 0
    verdict: bool | None = None
    for f in files:
        key = f["key"]
        entry = state.get(key) or {}
        dest_dir = cfg.files_dir / "note_files" / f["token"] / key
        dest = dest_dir / safe_filename(f["name"])
        if entry.get("status") == 200 and dest.exists():
            n_ok += 1
            verdict = verdict if verdict is not None else True
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        url = f"{media_base}/note_file/{f['token']}/{key}"
        try:
            resp = requests.get(url, headers=auth_headers(False), timeout=120, stream=True)
            if resp.status_code == 401:
                resp.close()  # token may have expired → one refresh, then retry
                resp = requests.get(url, headers=auth_headers(True), timeout=120, stream=True)
            status = resp.status_code
            size = 0
            if status == 200:
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(1 << 16):
                        fh.write(chunk)
                        size += len(chunk)
                verdict = True
                n_ok += 1
            else:
                n_fail += 1
                if status in (401, 403):
                    verdict = False
            ctype = resp.headers.get("Content-Type", "")
            resp.close()
        except requests.RequestException as exc:
            status, size, ctype = "error", 0, str(exc)
            n_fail += 1
        state[key] = {"name": f["name"], "content_type": f.get("content_type") or ctype, "status": status,
                      "bytes": size, "local": str(dest) if status == 200 else None}
        if verdict is False:
            log.warning("note file download rejected (%s) for %s – stopping file downloads; see manifest", status, url)
            write_json(ffile, state)
            return False, n_ok, n_fail
    write_json(ffile, state)
    return verdict, n_ok, n_fail


def bfs_order(tree: dict[int, dict], descending: bool = False) -> list[dict]:
    """Roots first, then level by level, siblings by ``sequence``."""
    children: dict[int | None, list[dict]] = {}
    ids = set(tree.keys())
    for node in tree.values():
        parent = node.get("parentId")
        key = parent if (parent in ids and not node.get("_root")) else None
        children.setdefault(key, []).append(node)
    def seq(n: dict) -> float:
        try:
            return float(n.get("sequence") or 0)
        except (TypeError, ValueError):
            return 0.0
    for lst in children.values():
        lst.sort(key=lambda n: (seq(n) * (-1 if descending else 1), str(n.get("insertedAt") or ""), int(n["id"])))
    order: list[dict] = []
    queue = list(children.get(None, []))
    seen = set()
    while queue:
        node = queue.pop(0)
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        order.append(node)
        queue.extend(children.get(node["id"], []))
    return order
