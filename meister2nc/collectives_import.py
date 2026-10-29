"""Stage 3b – one Collectives collective per MeisterNote root note.

The root note becomes the collective's landing page, its children top-level
pages, deeper notes subpages.  Pages are created through the OCS API and
their content written via WebDAV, so re-runs update in place (page ids are
kept in ``state/notes_mapping.json``).

Members of the collective's Team: per-root config (``collectives.roots``),
command-line ``--group/--member/--admin``, plus derived ones: the root's
author becomes Team admin and every account whose extraction sees the root
becomes a member (Meister's MCP exposes no sharing information, so this
union is the only deterministic source).

Per note:
  1. ``POST /collectives/{cid}/pages/{parentId}`` {title}  (once; later runs
     re-use the page id and fix parent/title with ``PUT``)
  2. ``POST …/pages/{id}/attachments`` for each referenced file not yet present
  3. ``PUT /remote.php/dav/files/<user>/<collectivePath>/<filePath>/<fileName>``
     with the rendered Markdown (``X-OC-MTime`` = note updatedAt)
  4. ``PUT …/pages/{parent}/subpageOrder`` with the sibling order
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from .config import Config
from .deck_import import load_mapping as load_deck_mapping
from .extract_notes import bfs_order
from .nc_client import NCClient, NCError
from .notes_build import FILE_PLACEHOLDER, footer, render_discussions, transform
from .usermap import UserMap
from .util import iso_to_epoch, log, now_epoch, read_json, safe_filename, truncate, write_json

TASKTOKEN_RE = re.compile(r"\[\[mt:tasktoken:([A-Za-z0-9_-]+)\]\]")


def mapping_path(cfg: Config):
    return cfg.state_dir / "notes_mapping.json"


def sync_members(nc: NCClient, collective: dict, groups: list[str] | None, users: list[str] | None,
                 admins: list[str] | None, usermap: UserMap | None = None, prune_covered: bool = False) -> dict:
    """Make groups/users members of the collective's Team (idempotent).
    ``users``/``admins`` are Nextcloud user ids; admins get Team level admin.
    A plain member who is already in one of the collective's groups is not
    added individually (admins always are); with ``prune_covered`` such
    individual memberships from earlier runs are removed again."""
    circle_id = collective.get("circleId")
    result = {"groups": [], "users": [], "admins": [], "covered": [], "pruned": [], "errors": []}
    if not circle_id:
        result["errors"].append("collective has no circleId")
        return result
    group_set = set(dict.fromkeys(groups or []))
    for gid in group_set:
        try:
            nc.team_add_member(circle_id, gid, nc.TEAM_TYPE_GROUP)
            result["groups"].append(gid)
        except NCError as exc:
            result["errors"].append(f"group {gid}: {exc}")

    def covered(uid: str) -> bool:
        return bool(usermap and group_set and (usermap.groups_of(uid) & group_set))

    admin_set = set(admins or [])
    for uid in dict.fromkeys(list(users or []) + list(admins or [])):
        if uid == nc.user:
            continue
        if uid not in admin_set and covered(uid):
            result["covered"].append(uid)
            continue
        try:
            member = nc.team_add_member(circle_id, uid, nc.TEAM_TYPE_USER)
            result["users"].append(uid)
            if uid in admin_set and member.get("id") and int(member.get("level", 0)) < nc.TEAM_LEVEL_ADMIN:
                nc.team_set_level(circle_id, member["id"], nc.TEAM_LEVEL_ADMIN)
                result["admins"].append(uid)
        except NCError as exc:
            result["errors"].append(f"user {uid}: {exc}")
    if prune_covered and group_set and usermap:
        for m in nc.team_members(circle_id):
            uid = m.get("userId")
            if (not str((m.get("basedOn") or {}).get("name", "")).startswith("user:") or uid == nc.user
                    or int(m.get("level", 0)) >= nc.TEAM_LEVEL_ADMIN or not covered(uid)):
                continue
            try:
                nc.team_remove_member(circle_id, m["id"])
                result["pruned"].append(uid)
            except NCError as exc:
                result["errors"].append(f"prune {uid}: {exc}")
    log.info("members of %r: groups %s, users %s, admins %s, covered by group %s%s%s", collective.get("name"),
             result["groups"], result["users"], result["admins"], result["covered"],
             f", pruned {result['pruned']}" if result["pruned"] else "",
             f", errors {result['errors']}" if result["errors"] else "")
    return result


def accounts_seeing(cfg: Config, token: str) -> list[int]:
    """Meister person ids of all extracted accounts whose note tree contains ``token``."""
    out = []
    root = cfg.raw_dir / "accounts"
    if not root.exists():
        return out
    for acct in root.iterdir():
        tree = read_json(acct / "notes" / "tree.json") or []
        if any(n.get("token") == token for n in tree):
            me = read_json(acct / "me.json") or {}
            if me.get("id"):
                out.append(int(me["id"]))
    return out


def run(cfg: Config, nc: NCClient, label: str, collective_id: int | None = None, collective_name: str | None = None,
        limit: int | None = None, test: bool = False, dry_run: bool = False, parent_page_id: int | None = None,
        root_tokens: list[str] | None = None, groups: list[str] | None = None, users: list[str] | None = None,
        admins: list[str] | None = None, prune_covered: bool = False, overwrite_edited: bool = False) -> dict:
    ndir = cfg.account_dir(label) / "notes"
    manifest = read_json(ndir / "manifest.json")
    if not manifest:
        raise SystemExit(f"no notes extracted for {label}")
    tree = {int(r["id"]): r for r in (read_json(ndir / "tree.json") or []) if not r.get("_stub")}
    roots = [n for n in tree.values() if n.get("_root")]
    if root_tokens:
        roots = [tree_node for tree_node in tree.values() if tree_node["token"] in root_tokens]
    if not roots:
        raise SystemExit("no root notes to import")
    if (collective_id or collective_name) and len(roots) > 1:
        raise SystemExit("--collective-id/--collective-name apply to exactly one root; use --root")
    order_all = bfs_order(tree, descending=bool(cfg.collectives.get("order_descending")))
    usermap = UserMap(cfg)
    usermap.extra_names.update(read_json(cfg.state_dir / "persons_names.json", {}) or {})
    deck_map = load_deck_mapping(cfg)
    token_to_card = {info["token"]: info["card_id"] for entry in deck_map.get("projects", {}).values()
                     for info in entry.get("cards", {}).values() if info.get("token")}
    mapping = read_json(mapping_path(cfg), {}) or {}
    mapping.setdefault("collectives", {})
    root_cfgs = cfg.collectives.get("roots") or {}
    emojis = load_emojis(cfg)

    totals = {"collectives": [], "created": 0, "updated": 0, "attachments": 0, "errors": 0}
    processed = 0
    for root in roots:
        subtree = _subtree_ids(tree, [root["token"]])
        if is_empty_root(read_json(ndir / f"{root['token']}.json"), len(subtree)):
            log.info("root %r (%s) has no content and no sub notes – skipped", root.get("name"), root["token"])
            continue
        nodes = [n for n in order_all if int(n["id"]) in subtree]
        nodes.sort(key=lambda n: 0 if n["token"] == root["token"] else 1)  # root first
        if limit is not None:
            nodes = nodes[:limit]  # test mode: first N notes of every root
        processed += len(nodes)
        rcfg = root_cfgs.get(root["token"], {})
        name = collective_name or rcfg.get("name") or (root.get("name") or "Unbenannt").strip() or "Unbenannt"
        if len(name) < 3:  # Teams (circles) reject names shorter than 3 characters
            name = f"{name} (Notizen)"
        if test and not collective_id:
            name = truncate(name, 60) + cfg.collectives["test_suffix"]
        # members: config + command line + derived
        owner_uid = usermap.uid_active(root.get("personId"))
        seen_uids = [usermap.uid_active(pid) for pid in accounts_seeing(cfg, root["token"])]
        m_groups = list(groups or []) + list(rcfg.get("groups") or [])
        m_admins = list(admins or []) + list(rcfg.get("admins") or []) + ([owner_uid] if owner_uid else [])
        m_users = list(users or []) + list(rcfg.get("members") or []) + [u for u in seen_uids if u]
        if dry_run:
            log.info("[dry-run] root %r → collective %r: %d notes, groups %s, admins %s, members %s",
                     root.get("name"), name, len(nodes), m_groups, m_admins, m_users)
            continue
        collective = _ensure_collective(nc, collective_id, name, mapping, root["token"])
        sync_members(nc, collective, m_groups, m_users, m_admins, usermap, prune_covered)
        if emojis.get(root["token"]) and collective.get("emoji") != emojis[root["token"]] and parent_page_id is None:
            try:
                nc.collective_set_emoji(int(collective["id"]), emojis[root["token"]])
            except NCError as exc:
                log.warning("collective emoji: %s", exc)
        stats = _import_subtree(cfg, nc, collective, root, nodes, tree, ndir, mapping, usermap, token_to_card, parent_page_id,
                                emojis, overwrite_edited)
        totals["collectives"].append({"root": root.get("name"), "collective": collective["id"], "name": collective["name"], **stats})
        for k in ("created", "updated", "attachments"):
            totals[k] += stats[k]
        totals["errors"] += len(stats["errors"])
    if not dry_run:
        write_json(mapping_path(cfg), mapping)
    log.info("collectives import done: %s", totals)
    return totals


def edited_in_nextcloud(page: dict, note_updated_at: str | None, migration_user: str) -> bool:
    """True when somebody other than the migration user saved the page after the
    note's own timestamp (the import writes pages with mtime = note updatedAt),
    i.e. the page has been worked on in Nextcloud and must not be overwritten."""
    editor = page.get("lastUserId")
    if not editor or editor == migration_user:
        return False
    return int(page.get("timestamp") or 0) > (iso_to_epoch(note_updated_at) or 0)


def is_empty_root(note: dict | None, subtree_size: int) -> bool:
    """A root without sub notes whose dump has neither content nor discussions
    (typically an untouched default "Neue Notiz") gets no collective of its own.
    Unknown state (not extracted, fetch error) is never treated as empty."""
    if subtree_size > 1 or not note or "_error" in note or "markdownContent" not in note:
        return False
    return not (note.get("markdownContent") or "").strip() and not note.get("_discussions")


def load_emojis(cfg: Config) -> dict[str, str]:
    """``data/page_emojis.json``: note token → emoji (one grapheme), reviewed by hand."""
    data = read_json(cfg.data_dir / "page_emojis.json", {}) or {}
    out = {}
    for token, emoji in data.items():
        if isinstance(emoji, str) and emoji.strip() and is_single_emoji(emoji.strip()):
            out[token] = emoji.strip()
    return out


def is_single_emoji(value: str) -> bool:
    """One emoji (possibly with variation selector / skin tone / ZWJ sequence), no text."""
    if not value or len(value) > 16:
        return False
    import unicodedata
    bases = 0
    for ch in value:
        cat = unicodedata.category(ch)
        if ch in "‍️︎" or 0x1F3FB <= ord(ch) <= 0x1F3FF or 0xE0020 <= ord(ch) <= 0xE007F:
            continue
        if cat in ("So", "Sk", "Mn", "Me") or ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF or 0x2190 <= ord(ch) <= 0x21FF:
            bases += 1
            continue
        return False  # letters, digits, punctuation, spaces
    # one symbol, a flag (two regional indicators) or a ZWJ sequence – not a row of emojis
    return 1 <= bases <= (4 if "‍" in value else 2)


def _import_subtree(cfg: Config, nc: NCClient, collective: dict, root: dict, nodes: list[dict], tree: dict, ndir: Path,
                    mapping: dict, usermap: UserMap, token_to_card: dict, parent_page_id: int | None,
                    emojis: dict[str, str] | None = None, overwrite_edited: bool = False) -> dict:
    emojis = emojis or {}
    cid = int(collective["id"])
    pages = nc.pages(cid)
    landing = next((p for p in pages if p.get("parentId") == 0), None)
    if landing is None:
        raise SystemExit(f"collective {cid} has no landing page")
    cmap = mapping["collectives"].setdefault(str(cid), {"name": collective.get("name"), "pages": {}})
    page_of: dict[str, dict] = cmap["pages"]
    existing_by_id = {int(p["id"]): p for p in pages}
    stats = {"created": 0, "updated": 0, "attachments": 0, "attachments_failed": 0, "links_notes": 0,
             "links_tasks": 0, "errors": []}
    root_token = root["token"]
    # pages of this subtree that an earlier run put into another collective → trash there
    for other_cid, other in list(mapping["collectives"].items()):
        if int(other_cid) == cid:
            continue
        for node in nodes:
            stale = (other.get("pages") or {}).pop(node["token"], None)
            if stale and not stale.get("landing"):
                try:
                    nc.page_trash(int(other_cid), int(stale["page_id"]))
                    log.info("page %r removed from collective %s (now belongs to %r)", stale.get("title"), other_cid,
                             collective.get("name"))
                except NCError as exc:
                    stats["errors"].append({"token": node["token"], "step": "trash-moved", "error": str(exc)})
    # the root note *is* the landing page (unless the subtree goes below an existing page)
    previous_root = page_of.get(root_token)
    if parent_page_id is None:
        page_of[root_token] = {"page_id": int(landing["id"]), "title": collective.get("name"), "note_id": root.get("id"),
                               "landing": True}
        if previous_root and int(previous_root["page_id"]) != int(landing["id"]):
            log.info("root %r was a sub page before → children move to the top level, old page trashed",
                     root.get("name"))
    root_parent = parent_page_id or int(landing["id"])

    def parent_page_for(node: dict) -> int:
        parent_token = _parent_token(tree, node, root_token)
        if parent_token and parent_token in page_of:
            return int(page_of[parent_token]["page_id"])
        return root_parent

    # phase 1: pages exist with correct parent/title --------------------------
    titles_used: dict[int, set] = {}
    for node in nodes:
        token = node["token"]
        if token == root_token and parent_page_id is None:
            continue
        parent_page = parent_page_for(node)
        base_title = (node.get("name") or "Unbenannt").strip() or "Unbenannt"
        if node.get("type") == "TEMPLATE":
            base_title = "[Vorlage] " + base_title
        base_title = safe_filename(base_title, limit=120)
        used = titles_used.setdefault(parent_page, set())
        title, n = base_title, 2
        while title.lower() in used:
            title = f"{base_title} ({n})"
            n += 1
        used.add(title.lower())
        entry = page_of.get(token)
        page = None
        if entry and not entry.get("landing"):
            page = existing_by_id.get(int(entry["page_id"])) or nc.page(cid, int(entry["page_id"]))
        if page is None:
            try:
                page = nc.page_create(cid, parent_page, title)
            except NCError as exc:
                stats["errors"].append({"token": token, "step": "create", "error": str(exc)})
                log.error("create page %r failed: %s", title, exc)
                continue
            stats["created"] += 1
            existing_by_id[int(page["id"])] = page
        else:
            changes = {}
            if int(page.get("parentId") or 0) != parent_page:
                changes["parent_id"] = parent_page
            if page.get("title") != title:
                changes["title"] = title
            if changes:
                try:
                    page = nc.page_move(cid, int(page["id"]), **changes) or page
                    existing_by_id[int(page["id"])] = page
                except NCError as exc:
                    stats["errors"].append({"token": token, "step": "move", "error": str(exc)})
        page_of[token] = {"page_id": int(page["id"]), "title": title, "note_id": node.get("id")}
        wanted = emojis.get(token)
        if wanted and page.get("emoji") != wanted:
            try:
                page = nc.page_set_emoji(cid, int(page["id"]), wanted) or page
                existing_by_id[int(page["id"])] = page
                stats["emojis"] = stats.get("emojis", 0) + 1
            except NCError as exc:
                stats["errors"].append({"token": token, "step": "emoji", "error": str(exc)})
    if previous_root and parent_page_id is None and int(previous_root["page_id"]) != int(landing["id"]):
        try:
            nc.page_trash(cid, int(previous_root["page_id"]))
        except NCError as exc:
            stats["errors"].append({"token": root_token, "step": "trash-old-root", "error": str(exc)})
    write_json(mapping_path(cfg), mapping)
    log.info("%r phase 1 done: %d pages created", collective.get("name"), stats["created"])

    # phase 2: attachments + content ----------------------------------------
    for node in nodes:
        token = node["token"]
        entry = page_of.get(token)
        if not entry:
            continue
        note = read_json(ndir / f"{token}.json") or {}
        if "_error" in note or "markdownContent" not in note:
            stats["errors"].append({"token": token, "step": "content", "error": "note not fetched yet (extraction error)"})
            continue
        files = read_json(ndir / f"{token}.files.json", {}) or {}
        page = nc.page(cid, int(entry["page_id"]))
        if page is None:
            stats["errors"].append({"token": token, "step": "page-get", "error": "page vanished"})
            continue
        if not overwrite_edited and edited_in_nextcloud(page, node.get("updatedAt"), nc.user):
            stats.setdefault("edited_in_nextcloud", []).append(entry["title"])
            log.warning("page %r was edited in Nextcloud by %s after the migration – not overwritten",
                        entry["title"], page.get("lastUserDisplayName") or page.get("lastUserId"))
            continue
        text, refs, _ = transform(note.get("markdownContent") or "", note, files, cfg.collectives)
        try:
            present = {a.get("name"): a for a in nc.page_attachments(cid, int(page["id"]))}
        except NCError:
            present = {}
        for ref in refs:
            placeholder = FILE_PLACEHOLDER % ref["key"]
            local = ref.get("local")
            link = None
            if local and Path(local).exists():
                fname = safe_filename(ref["name"])
                att = present.get(fname)
                if att is None:
                    mimetype = ref.get("content_type") or mimetypes.guess_type(fname)[0] or "application/octet-stream"
                    try:
                        att = nc.page_upload_attachment(cid, int(page["id"]), fname, Path(local).read_bytes(), mimetype)
                        stats["attachments"] += 1
                        if att:
                            present[att.get("name")] = att
                    except NCError as exc:
                        stats["attachments_failed"] += 1
                        stats["errors"].append({"token": token, "step": "attachment", "file": fname, "error": str(exc)})
                        att = None
                if att:
                    link = f".attachments.{page['id']}/{att.get('name', fname)}"
            if link:
                # Nextcloud Text inserts *every* attachment as an image node
                # (![name](.attachments.<id>/file)) and renders non-images as a file
                # card; a plain link would be resolved against the page URL instead
                alt = re.sub(r"[\[\]]", "", ref["name"])
                md = f"![{alt}]({_enc(link)})"
            else:
                md = f"_[Datei aus MeisterNote nicht übernommen: {ref['name']}]_"
            text = text.replace(placeholder, md)

        def note_repl(m: re.Match) -> str:
            tok = m.group(1)
            target = _find_page(mapping, tok)
            if target:
                stats["links_notes"] += 1
                tcid, tpid = target
                tcoll = collective if tcid == cid else next((c for c in nc.collectives() if int(c["id"]) == tcid), None)
                tpage = (existing_by_id.get(tpid) if tcid == cid else None) or nc.page(tcid, tpid)
                if tcoll and tpage:
                    return nc.page_link(tcoll, tpage)
            return f"https://www.meisternote.com/app/note/{tok}"
        text = re.sub(r"\{\{m2nc-note:([A-Za-z0-9_-]+)\}\}", note_repl, text)

        def task_repl(m: re.Match) -> str:
            tok = m.group(1)
            card = token_to_card.get(tok)
            if card:
                stats["links_tasks"] += 1
                return nc.deck_card_url(card)
            return f"https://www.meistertask.com/app/task/{tok}"
        text = TASKTOKEN_RE.sub(task_repl, text)

        if cfg.collectives.get("include_discussions", True) and note.get("_discussions"):
            text += render_discussions(note["_discussions"], usermap.name)
        if cfg.collectives.get("page_footer", True):
            text += footer(note)
        # no "# Title" line is added: Collectives shows the page title above the content
        coll_path = page.get("collectivePath") or landing.get("collectivePath")
        if not coll_path:
            stats["errors"].append({"token": token, "step": "content", "error": "page info has no collectivePath"})
            continue
        segments = [coll_path] + ([page["filePath"]] if page.get("filePath") else []) + [page["fileName"]]
        try:
            nc.dav_put(segments, text.encode("utf-8"), mtime=iso_to_epoch(node.get("updatedAt")))
            stats["updated"] += 1
        except NCError as exc:
            if "→ 423" in str(exc):  # someone has the page open in the editor; next run retries
                stats.setdefault("locked", []).append(entry["title"])
                log.warning("page %r is locked (open in an editor) – skipped, retried on the next run", entry["title"])
            else:
                stats["errors"].append({"token": token, "step": "content", "error": str(exc)})
                log.error("write page %s failed: %s", entry["title"], exc)
    log.info("%r phase 2 done: %d pages written, %d attachments uploaded", collective.get("name"),
             stats["updated"], stats["attachments"])

    # phase 3: sibling order --------------------------------------------------
    children: dict[int, list[int]] = {}
    for node in nodes:
        if node["token"] == root_token and parent_page_id is None:
            continue
        entry = page_of.get(node["token"])
        if entry:
            children.setdefault(parent_page_for(node), []).append(int(entry["page_id"]))
    for parent_page, ids in children.items():
        if len(ids) < 2:
            continue
        try:
            nc.page_set_subpage_order(cid, parent_page, ids)
        except NCError as exc:
            stats["errors"].append({"page": parent_page, "step": "order", "error": str(exc)})
    cmap["last_run"] = now_epoch()
    cmap["root_token"] = root_token
    cmap["stats"] = {k: v for k, v in stats.items() if k != "errors"} | {"errors": len(stats["errors"])}
    write_json(mapping_path(cfg), mapping)
    if stats["errors"]:
        log.warning("%d errors for %r: %s", len(stats["errors"]), collective.get("name"), stats["errors"][:3])
    return stats


def _find_page(mapping: dict, token: str) -> tuple[int, int] | None:
    for cid, cmap in (mapping.get("collectives") or {}).items():
        entry = (cmap.get("pages") or {}).get(token)
        if entry:
            return int(cid), int(entry["page_id"])
    return None


def _enc(path: str) -> str:
    from urllib.parse import quote
    return "/".join(quote(p) for p in path.split("/"))


def _parent_token(tree: dict, node: dict, root_token: str) -> str | None:
    if node.get("token") == root_token:
        return None
    parent = tree.get(node.get("parentId"))
    return parent["token"] if parent else None


def _subtree_ids(tree: dict, root_tokens: list[str]) -> set:
    roots = {r["id"] for r in tree.values() if r["token"] in root_tokens}
    keep = set(roots)
    changed = True
    while changed:
        changed = False
        for r in tree.values():
            if r["id"] not in keep and r.get("parentId") in keep:
                keep.add(r["id"])
                changed = True
    return keep


def collective_name_key(name: str) -> str:
    """Nextcloud strips double quotes from Team names, turns "/" into "-" and
    compares loosely, so match on the same normalised form (no quotes, no
    slashes, collapsed spaces, lower case)."""
    return re.sub(r"\s+", " ", (name or "").replace('"', "").replace("/", "-")).strip().lower()


def _ensure_collective(nc: NCClient, collective_id: int | None, name: str, mapping: dict | None = None,
                       root_token: str | None = None) -> dict:
    """The collective for a root: an explicit id, else the one the mapping
    recorded for this root (names may have been changed in Nextcloud), else a
    name match, else a new one."""
    existing = nc.collectives()
    if collective_id:
        for c in existing:
            if int(c["id"]) == int(collective_id):
                return c
        raise SystemExit(f"collective {collective_id} not accessible for this user")
    for cid, entry in ((mapping or {}).get("collectives") or {}).items():
        if root_token and entry.get("root_token") == root_token:
            for c in existing:
                if int(c["id"]) == int(cid):
                    return c
    for c in existing:
        if collective_name_key(c.get("name")) == collective_name_key(name):
            return c
    name = re.sub(r"\s+", " ", name.replace('"', "")).strip()
    log.info("creating collective %r", name)
    created = nc.collective_create(name)
    if not created:
        raise SystemExit(f"could not create collective {name!r}")
    return created
