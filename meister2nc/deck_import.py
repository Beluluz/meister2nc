"""Stage 3a – Deck import, update and the final link/dependency pass.

Full import: ``POST /index.php/apps/deck/api/v1.1/boards/import`` with
``{"system": "DeckJson", "config": {...}, "data": {"boards": [...]}}``.  The
importer only creates boards, so ``--replace`` deletes the board created
earlier (file attachments first, see NCClient.deck_delete_board) and imports
again.

Update (``--update``): compares every freshly built card with the fingerprint
stored at the last import:
  * unchanged                      → nothing
  * fields changed, same comments/attachments → card API update in place
    (title, description, order, due, done, archived, labels, assignees, stack)
  * new comments/attachments, or a new task → the card is imported through a
    temporary board (keeps comment authors and timestamps), moved into the real
    board with ``reorder`` (CardService has no same-board check), given its
    labels and attachments through the API, and the old card is deleted
  * tasks that vanished from MeisterTask are only reported
Links and dependencies are always the last step (``run_link_pass``), which also
rewrites card URLs that point to replaced cards.

The card footer ``MeisterTask-Task `<token>``` is how created card ids are
recovered after any import.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re

from .config import Config
from .deck_build import FOOTER_RE
from .nc_client import NCClient, NCError
from .util import log, now_epoch, read_json, write_json

PLACEHOLDER_RE = re.compile(r"\[\[mt:(task|dep|depof|depboth):(\d+)\]\]")
LINE_WITH_PLACEHOLDER_RE = re.compile(r"^[ \t]*-[^\n]*\[\[mt:(dep|depof|depboth):(\d+)\]\][^\n]*\n?", re.M)
# a reference written by an earlier pass for a task that was not migrated *yet*
# (other project imported later); becomes a link or disappears once it exists
UNRESOLVED_RE = re.compile(r"\(nicht migriert, MeisterTask-ID (\d+)\)")
UNRESOLVED_LINE_RE = re.compile(r"^[ \t]*-[^\n]*\(nicht migriert, MeisterTask-ID (\d+)\)[^\n]*\n?", re.M)
EMPTY_SECTION_RE = re.compile(r"^### [^\n]+\n(?=\s*(?:###|_Migriert|---|$))", re.M)
CARD_URL_RE = re.compile(r"(/apps/deck/card/)(\d+)")


def mapping_path(cfg: Config):
    return cfg.state_dir / "deck_mapping.json"


def load_mapping(cfg: Config) -> dict:
    data = read_json(mapping_path(cfg), {}) or {}
    data.setdefault("projects", {})
    data.setdefault("replaced", {})
    return data


# --- fingerprints -----------------------------------------------------------

def fingerprint(card: dict) -> dict:
    """Edited comments and replaced attachments count as changes too (message /
    content hashes, not just ids and names)."""
    fields = {k: card.get(k) for k in ("title", "description", "order", "archived", "duedate", "done", "labels",
                                       "assignedUsers", "lastModified")}
    return {
        "fields": hashlib.sha1(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "comments": sorted(f"{c.get('id')}:{hashlib.sha1((c.get('message') or '').encode()).hexdigest()[:12]}"
                           for c in card.get("comments") or []),
        "attachments": sorted(f"{a.get('data')}:{a.get('size')}" for a in card.get("attachments") or []),
    }


def strip_attachments(board: dict) -> tuple[dict, dict[str, list]]:
    """Import payload without attachments + {task id: attachments} for the upload pass."""
    payload = json.loads(json.dumps(board))
    files: dict[str, list] = {}
    for stack in payload["stacks"]:
        for card in stack["cards"]:
            if card.get("attachments"):
                files[str(card["id"])] = card["attachments"]
            card["attachments"] = []
    return payload, files


def _base_name(name: str) -> str:
    """``IMG_7665 (2).JPG`` → ``IMG_7665.JPG``"""
    return re.sub(r" \(\d+\)(\.[^.]+)?$", r"\1", name)


def upload_attachments(nc: NCClient, writable: "Writable", board_id: int, cards: dict[str, dict],
                       files: dict[str, list]) -> dict:
    """Upload the files of every card through the OCS attachment endpoint
    (archived cards and boards are unarchived around it).  Files already
    present on the card (same name) are skipped, so the pass is idempotent."""
    stats = {"uploaded": 0, "skipped": 0, "failed": 0}
    for tid, atts in files.items():
        info = cards.get(tid)
        if not info:
            stats["failed"] += len(atts)
            continue

        def do(current, info=info, atts=atts):
            # Deck stores all files of the owner in one folder and renames duplicates
            # "name (2).ext", so compare on the name without that suffix plus the size
            present = {(_base_name(a.get("data") or ""), int((a.get("extendedData") or {}).get("filesize") or 0))
                       for a in nc.deck_card_attachments(board_id, info["stack_id"], info["card_id"])}
            for att in atts:
                if (_base_name(att["data"]), int(att.get("size") or 0)) in present:
                    stats["skipped"] += 1
                    continue
                path = att.get("localPath")
                if not path or not os.path.exists(path):
                    stats["failed"] += 1
                    continue
                with open(path, "rb") as fh:
                    content = fh.read()
                try:
                    nc.deck_upload_attachment(board_id, info["stack_id"], info["card_id"], att["data"], content,
                                              mimetypes.guess_type(att["data"])[0] or "application/octet-stream")
                    stats["uploaded"] += 1
                except NCError as exc:
                    stats["failed"] += 1
                    log.warning("attachment %s on card %s: %s", att["data"], info["card_id"], exc)
        if not writable.card(board_id, info["stack_id"], info["card_id"], do):
            stats["failed"] += len(atts)
    return stats


def dependencies_of(board: dict) -> list[list[str]]:
    """(owner task id, dependent task id) pairs encoded in the built descriptions."""
    pairs = []
    for stack in board["stacks"]:
        for card in stack["cards"]:
            tid = str(card["id"])
            for kind, other in PLACEHOLDER_RE.findall(card.get("description") or ""):
                if kind in ("dep", "depboth"):
                    pairs.append([tid, other])
                if kind in ("depof", "depboth"):
                    pairs.append([other, tid])
    return pairs


def cards_of(board: dict) -> dict[str, dict]:
    return {str(c["id"]): dict(c, _stack_id=s["id"]) for s in board["stacks"] for c in s["cards"]}


# --- full import ------------------------------------------------------------

def run(cfg: Config, nc: NCClient, project_ids: list[int] | None = None, replace: bool = False,
        dry_run: bool = False, link_pass: bool = True, update: bool = False) -> dict:
    build_dir = cfg.build_dir / "deck"
    files = sorted(build_dir.glob("*.json"), key=lambda p: p.name)
    mapping = load_mapping(cfg)
    owner = cfg.nc_user()
    summary = {"imported": [], "skipped": [], "replaced": [], "updated": [], "failed": []}
    for f in files:
        if f.name.endswith(".meta.json") or f.name == "report.json":
            continue
        pid = int(f.stem)
        if project_ids and pid not in project_ids:
            continue
        doc = read_json(f)
        board = doc["boards"][0]
        meta = read_json(build_dir / f"{pid}.meta.json", {}) or {}
        validate_board(board)
        existing = mapping["projects"].get(str(pid))
        if existing and update:
            if dry_run:
                plan = plan_update(nc, existing, board)
                log.info("[dry-run] %s: %d unchanged, %d field updates, %d re-creations, %d new, %d vanished",
                         board["title"], *[len(plan[k]) for k in ("unchanged", "fields", "recreate", "new", "vanished")])
                continue
            try:
                result = run_update(cfg, nc, mapping, pid, existing, board, meta, owner)
                summary["updated"].append({"project": pid, **result})
            except NCError as exc:
                log.error("update of project %s failed: %s", pid, exc)
                summary["failed"].append({"project": pid, "error": str(exc)})
            continue
        if existing and not replace:
            log.info("project %s already imported as board %s – skipping (use --replace or --update)", pid, existing["board_id"])
            summary["skipped"].append(pid)
            continue
        if dry_run:
            log.info("[dry-run] would import %r (%d cards, %d comments, %d attachments)", board["title"],
                     sum(len(s["cards"]) for s in board["stacks"]),
                     sum(len(c["comments"]) for s in board["stacks"] for c in s["cards"]),
                     sum(len(c["attachments"]) for s in board["stacks"] for c in s["cards"]))
            continue
        if existing and replace:
            delete_board(nc, int(existing["board_id"]))
            gone = {int(i["card_id"]) for i in existing.get("cards", {}).values()}
            mapping["replaced"] = {k: v for k, v in mapping["replaced"].items() if int(v) not in gone and int(k) not in gone}
            summary["replaced"].append(pid)
        payload, files = strip_attachments(board)
        try:
            result = nc.deck_import({"boards": [payload]}, owner)
        except NCError as exc:
            log.error("import of project %s failed: %s", pid, exc)
            summary["failed"].append({"project": pid, "error": str(exc)})
            continue
        board_id = _board_id(result, nc, board["title"])
        entry = {"board_id": board_id, "title": board["title"], "imported_at": now_epoch(),
                 "cards": {}, "stacks": {}, "dependencies": dependencies_of(board), "archived": board.get("archived")}
        if board_id:
            entry.update(recover_ids(nc, board_id, meta))
        built = cards_of(board)
        for tid, info in entry["cards"].items():
            if tid in built:
                info["fp"] = fingerprint(built[tid])
        mapping["projects"][str(pid)] = entry
        write_json(mapping_path(cfg), mapping)
        att_stats = {}
        if board_id and files:
            writable = Writable(nc)
            try:
                att_stats = upload_attachments(nc, writable, board_id, entry["cards"], files)
            finally:
                writable.restore()
        summary["imported"].append({"project": pid, "board_id": board_id, "cards_mapped": len(entry["cards"]),
                                    "attachments": att_stats})
        log.info("imported project %s → board %s (%d cards mapped, attachments %s)", pid, board_id, len(entry["cards"]),
                 att_stats or "none")
    if link_pass and not dry_run:
        summary["links"] = run_link_pass(cfg, nc, mapping, project_ids)
    write_json(cfg.state_dir / "deck_import_summary.json", summary)
    return summary


def delete_board(nc: NCClient, board_id: int) -> bool:
    """Remove file attachments first, then soft-delete (see NCClient.deck_delete_board)."""
    try:
        removed = nc.deck_purge_attachments(board_id)
        ok = nc.deck_delete_board(board_id)
        log.info("deleted board %s (%d attachments removed first): %s", board_id, removed, "ok" if ok else "NOT deleted")
        return ok
    except NCError as exc:
        log.warning("could not delete board %s: %s", board_id, exc)
        return False


def validate_board(board: dict) -> None:
    problems = []
    if not board.get("title"):
        problems.append("board without title")
    for stack in board.get("stacks", []):
        if not stack.get("title"):
            problems.append(f"stack {stack.get('id')} without title (importer would skip it and crash on its cards)")
        for card in stack.get("cards", []):
            for key in ("id", "title", "description", "order", "archived", "createdAt", "lastModified", "lastEditor",
                        "duedate", "labels", "assignedUsers", "comments", "attachments"):
                if key not in card:
                    problems.append(f"card {card.get('id')} misses {key}")
            for c in card.get("comments", []):
                if len(c.get("message", "")) > 1000:
                    problems.append(f"comment {c.get('id')} longer than 1000 chars")
                if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", c.get("creationDateTime") or ""):
                    problems.append(f"comment {c.get('id')} has bad creationDateTime {c.get('creationDateTime')}")
    if problems:
        raise SystemExit("Deck JSON validation failed:\n  " + "\n  ".join(problems[:30]))


def _board_id(result, nc: NCClient, title: str) -> int | None:
    if isinstance(result, dict):
        if result.get("id"):
            return int(result["id"])
        inner = result.get("data") if isinstance(result.get("data"), dict) else None
        if inner and inner.get("id"):
            return int(inner["id"])
    boards = [b for b in nc.deck_boards() if b.get("title") == title and not b.get("deletedAt")]
    if boards:
        return int(max(boards, key=lambda b: b.get("id", 0))["id"])
    return None


def recover_ids(nc: NCClient, board_id: int, meta: dict) -> dict:
    """Map MeisterTask task ids → Deck card ids via the footer token."""
    token_to_task = {v["token"]: tid for tid, v in (meta.get("tasks") or {}).items() if v.get("token")}
    cards: dict[str, dict] = {}
    stacks: dict[str, int] = {}
    title_to_section = {v: k for k, v in (meta.get("sections") or {}).items()}
    for archived in (False, True):
        try:
            stack_list = nc.deck_stacks(board_id, archived=archived)
        except NCError as exc:
            log.warning("could not list stacks (archived=%s): %s", archived, exc)
            continue
        for stack in stack_list:
            sec = title_to_section.get(stack.get("title"))
            if sec:
                stacks[sec] = stack["id"]
            for card in stack.get("cards") or []:
                m = FOOTER_RE.search(card.get("description") or "")
                if not m:
                    continue
                tid = token_to_task.get(m.group(1))
                if tid:
                    cards[str(tid)] = {"card_id": card["id"], "stack_id": stack["id"], "token": m.group(1)}
    return {"cards": cards, "stacks": stacks}


# --- writable helpers -------------------------------------------------------

class Writable:
    """Temporarily unarchives boards and cards so that Deck accepts changes;
    everything is archived again in ``restore()``."""

    def __init__(self, nc: NCClient):
        self.nc = nc
        self.boards: dict[int, dict] = {}

    def board(self, board_id: int) -> None:
        if board_id in self.boards:
            return
        board = self.nc.deck_board(board_id)
        self.boards[board_id] = board
        if board.get("archived"):
            self.nc.deck_board_archive(board, False)

    def card(self, board_id: int, stack_id: int, card_id: int, action) -> bool:
        self.board(board_id)
        card = self.nc.deck_card(board_id, stack_id, card_id)
        archived = bool(card.get("archived"))
        ok = False
        try:
            if archived:
                self.nc.deck_card_archive(board_id, stack_id, card_id, archive=False)
            action(card)
            ok = True
        except NCError as exc:
            log.warning("card %s: %s", card_id, exc)
        finally:
            if archived:
                try:
                    self.nc.deck_card_archive(board_id, stack_id, card_id, archive=True)
                except NCError as exc:
                    log.warning("re-archiving card %s failed: %s", card_id, exc)
        return ok

    def restore(self) -> None:
        for board_id, board in self.boards.items():
            if board.get("archived"):
                try:
                    self.nc.deck_board_archive(board, True)
                except NCError as exc:
                    log.warning("re-archiving board %s failed: %s", board_id, exc)
        self.boards = {}


# --- update -----------------------------------------------------------------

def plan_update(nc: NCClient, entry: dict, board: dict) -> dict:
    built = cards_of(board)
    plan = {"unchanged": [], "fields": [], "recreate": [], "new": [], "vanished": []}
    for tid, card in built.items():
        info = entry["cards"].get(tid)
        if not info:
            plan["new"].append(tid)
            continue
        fp_new, fp_old = fingerprint(card), info.get("fp") or {}
        if fp_new == fp_old:
            plan["unchanged"].append(tid)
        elif fp_new["comments"] == fp_old.get("comments") and fp_new["attachments"] == fp_old.get("attachments"):
            plan["fields"].append(tid)
        else:
            plan["recreate"].append(tid)
    for tid in entry["cards"]:
        if tid not in built:
            plan["vanished"].append(tid)
    return plan


def run_update(cfg: Config, nc: NCClient, mapping: dict, pid: int, entry: dict, board: dict, meta: dict, owner: str) -> dict:
    board_id = int(entry["board_id"])
    plan = plan_update(nc, entry, board)
    log.info("update %r: %d unchanged, %d field updates, %d re-creations, %d new, %d vanished", board["title"],
             *[len(plan[k]) for k in ("unchanged", "fields", "recreate", "new", "vanished")])
    built = cards_of(board)
    real_board = nc.deck_board(board_id)
    label_ids_by_title = {l["title"]: l["id"] for l in real_board.get("labels") or []}
    built_label_title = {l["id"]: l["title"] for l in board["labels"]}
    writable = Writable(nc)
    result = {"fields": 0, "recreated": 0, "new": 0, "vanished": plan["vanished"], "failed": []}
    try:
        writable.board(board_id)
        # board title / archived state
        if real_board.get("title") != board["title"] or bool(real_board.get("archived")) != bool(board.get("archived")):
            nc.deck_put(f"/boards/{board_id}", {"title": board["title"], "color": real_board.get("color") or board.get("color"),
                                               "archived": False})
            writable.boards[board_id]["archived"] = bool(board.get("archived"))  # re-archived in restore()
            writable.boards[board_id]["title"] = board["title"]
            entry["title"] = board["title"]
            log.info("  board title/archived updated")
        # labels that appeared after the first import
        for lab in board["labels"]:
            if lab["title"] not in label_ids_by_title:
                created = nc.deck_post(f"/boards/{board_id}/labels", {"title": lab["title"], "color": lab["color"]})
                label_ids_by_title[lab["title"]] = created["id"]
                log.info("  created label %r", lab["title"])
        # stacks: new sections, renamed/reordered sections
        section_titles = meta.get("sections") or {}
        real_stacks = {s["id"]: s for a in (False, True) for s in nc.deck_stacks(board_id, archived=a)}
        for stack in board["stacks"]:
            sec = str(stack["id"])
            if sec not in entry["stacks"] and sec in section_titles:
                created = nc.deck_create_stack(board_id, stack["title"], stack["order"])
                entry["stacks"][sec] = created["id"]
                log.info("  created stack %r", stack["title"])
                continue
            real = real_stacks.get(entry["stacks"].get(sec))
            if real and (real.get("title") != stack["title"] or real.get("order") != stack["order"]):
                nc.deck_put(f"/boards/{board_id}/stacks/{real['id']}", {"title": stack["title"], "order": stack["order"]})
                log.info("  stack %r updated", stack["title"])

        def stack_for(card: dict) -> int:
            return int(entry["stacks"].get(str(card["_stack_id"])) or next(iter(entry["stacks"].values())))

        # 1) field updates in place
        for tid in plan["fields"]:
            card, info = built[tid], entry["cards"][tid]
            was: dict = {}

            def apply(current, card=card, info=info, was=was):
                was["archived"] = bool(current.get("archived"))
                _apply_fields(nc, board_id, info, current, card, label_ids_by_title, built_label_title, stack_for(card))
            if writable.card(board_id, info["stack_id"], info["card_id"], apply):
                info["stack_id"] = stack_for(card)
                if was.get("archived") != bool(card.get("archived")):  # Writable restored the old state
                    try:
                        nc.deck_card_archive(board_id, info["stack_id"], info["card_id"], archive=bool(card.get("archived")))
                        result["archived" if card.get("archived") else "unarchived"] = \
                            result.get("archived" if card.get("archived") else "unarchived", 0) + 1
                    except NCError as exc:
                        log.warning("  archive state of card %s: %s", info["card_id"], exc)
                        result["failed"].append(tid)
                        continue
                info["fp"] = fingerprint(card)
                info.pop("links", None)  # description is fresh (placeholders) → link pass must revisit
                result["fields"] += 1
            else:
                result["failed"].append(tid)

        # 2) re-creations and new cards through a temporary board
        todo = plan["recreate"] + plan["new"]
        if todo:
            tmp_cards = []
            for tid in todo:
                c = dict(built[tid])
                c.update({"labels": [], "attachments": [], "archived": False})
                c.pop("_stack_id", None)
                tmp_cards.append(c)
            tmp_board = {"id": -pid, "title": f"{board['title']} [update {now_epoch()}]", "owner": owner,
                         "color": board.get("color", "0082c9"), "archived": False, "deletedAt": 0,
                         "lastModified": now_epoch(), "labels": [], "acl": [],
                         "stacks": [{"id": 1, "title": "tmp", "order": 0, "lastModified": now_epoch(), "cards": tmp_cards}]}
            tmp_id = _board_id(nc.deck_import({"boards": [tmp_board]}, owner), nc, tmp_board["title"])
            if not tmp_id:
                raise NCError("temporary board import returned no id")
            recovered = recover_ids(nc, tmp_id, meta)["cards"]
            for tid in todo:
                new = recovered.get(tid)
                if not new:
                    result["failed"].append(tid)
                    log.warning("  card for task %s not found on the temporary board", tid)
                    continue
                card = built[tid]
                target_stack = stack_for(card)
                try:
                    nc.deck_reorder_card(tmp_id, new["stack_id"], new["card_id"], target_stack, card["order"])
                    new_info = {"card_id": new["card_id"], "stack_id": target_stack, "token": new["token"]}

                    def finish(current, card=card, new_info=new_info):
                        _apply_fields(nc, board_id, new_info, current, card, label_ids_by_title, built_label_title, target_stack)
                    writable.card(board_id, target_stack, new_info["card_id"], finish)
                    if card.get("attachments"):
                        upload_attachments(nc, writable, board_id, {tid: new_info}, {tid: card["attachments"]})
                    if card.get("archived"):
                        nc.deck_card_archive(board_id, target_stack, new_info["card_id"], archive=True)
                except NCError as exc:
                    log.warning("  moving task %s failed: %s", tid, exc)
                    result["failed"].append(tid)
                    continue
                old = entry["cards"].get(tid)
                mapping.setdefault("pending_rewrite", []).append(tid)
                if old:
                    # soft-deleted cards would linger in other cards' dependency lists
                    for owner_tid, dep_tid in entry.get("dependencies") or []:
                        owner = entry["cards"].get(owner_tid) if dep_tid == tid else None
                        if owner and owner["card_id"] != old["card_id"]:
                            writable.card(board_id, owner["stack_id"], owner["card_id"],
                                          lambda cur, o=owner, d=old: nc.deck_remove_dependency(board_id, o["stack_id"], o["card_id"], d["card_id"]))
                    _delete_card(nc, writable, board_id, old)
                    mapping["replaced"][str(old["card_id"])] = new_info["card_id"]
                    result["recreated"] += 1
                else:
                    result["new"] += 1
                new_info["fp"] = fingerprint(card)
                entry["cards"][tid] = new_info
            delete_board(nc, tmp_id)
        # dependencies that disappeared in MeisterTask
        new_deps = dependencies_of(board)
        for owner_tid, dep_tid in [p for p in (entry.get("dependencies") or []) if p not in new_deps]:
            owner, dependent = entry["cards"].get(owner_tid), entry["cards"].get(dep_tid)
            if owner and dependent:
                writable.card(board_id, owner["stack_id"], owner["card_id"],
                              lambda cur, o=owner, d=dependent: nc.deck_remove_dependency(board_id, o["stack_id"], o["card_id"], d["card_id"]))
                log.info("  dependency %s → %s removed", owner_tid, dep_tid)
        entry["dependencies"] = new_deps
        entry["updated_at"] = now_epoch()
        mapping["projects"][str(pid)] = entry
        write_json(mapping_path(cfg), mapping)
    finally:
        writable.restore()
    for tid in plan["vanished"]:
        log.info("  task %s no longer in MeisterTask – card %s kept", tid, entry["cards"][tid]["card_id"])
    log.info("update of %r done: %s", board["title"], {k: v for k, v in result.items() if k != "vanished"})
    return result


def _apply_fields(nc: NCClient, board_id: int, info: dict, current: dict, card: dict, label_ids_by_title: dict,
                  built_label_title: dict, target_stack: int) -> None:
    stack_id = info["stack_id"]
    if current.get("stackId") and int(current["stackId"]) != target_stack:
        nc.deck_reorder_card(board_id, stack_id, info["card_id"], target_stack, card["order"])
        stack_id = info["stack_id"] = target_stack
    body = dict(current)
    body.update({"title": card["title"], "description": card["description"], "order": card["order"],
                 "duedate": card.get("duedate"), "done": card.get("done"), "archived": False})
    nc.deck_update_card(board_id, stack_id, info["card_id"], body)
    # labels
    want = {label_ids_by_title.get(built_label_title.get(l["id"])) for l in card.get("labels") or []} - {None}
    have = {l["id"] for l in current.get("labels") or []}
    for lid in want - have:
        nc.deck_assign_label(board_id, stack_id, info["card_id"], lid, True)
    for lid in have - want:
        nc.deck_assign_label(board_id, stack_id, info["card_id"], lid, False)
    # assignees
    want_u = {a["participant"]["uid"] for a in card.get("assignedUsers") or []}
    have_u = {(a.get("participant") or {}).get("uid") for a in current.get("assignedUsers") or []} - {None}
    for uid in want_u - have_u:
        nc.deck_assign_user(board_id, stack_id, info["card_id"], uid, True)
    for uid in have_u - want_u:
        nc.deck_assign_user(board_id, stack_id, info["card_id"], uid, False)


def _delete_card(nc: NCClient, writable: Writable, board_id: int, info: dict) -> None:
    """Remove the attachments, then the card (no re-archiving afterwards: Deck
    answers 403 "Card is deleted" for that)."""
    writable.board(board_id)
    b, s, c = board_id, info["stack_id"], info["card_id"]
    try:
        current = nc.deck_card(b, s, c)
        if current.get("archived"):
            nc.deck_card_archive(b, s, c, archive=False)
        for att in nc.deck_card_attachments(b, s, c):
            nc.deck_delete_attachment(b, s, c, att.get("type") or "file", att["id"])
        nc.deck_delete_card(b, s, c)
    except NCError as exc:
        log.warning("deleting old card %s failed: %s", c, exc)


# --- final pass: dependencies, links, stale URLs -------------------------------

def run_link_pass(cfg: Config, nc: NCClient, mapping: dict, project_ids: list[int] | None = None,
                  force: bool = False) -> dict:
    """Last step after any import or update:

    1. (re)apply every recorded dependency whose both cards exist (idempotent)
    2. ``[[mt:dep/depof:ID]]`` lines with a migrated counterpart are removed from
       the description, other placeholders become card links or references
    3. card URLs pointing to cards replaced by an update are rewritten
    Deck refuses changes on archived cards/boards → unarchived around the change."""
    task_to_card: dict[str, dict] = {}
    for pid, entry in mapping["projects"].items():
        for tid, info in entry.get("cards", {}).items():
            task_to_card[tid] = dict(info, board_id=entry.get("board_id"))
    names: dict[str, str] = {}
    build_dir = cfg.build_dir / "deck"
    for pid in mapping["projects"]:
        meta = read_json(build_dir / f"{pid}.meta.json", {}) or {}
        names.update({tid: v.get("name") for tid, v in (meta.get("tasks") or {}).items()})
    replaced = {str(k): int(v) for k, v in (mapping.get("replaced") or {}).items()}
    for old in list(replaced):  # follow chains: 338 → 347 → 349
        seen, cur = set(), replaced[old]
        while str(cur) in replaced and cur not in seen:
            seen.add(cur)
            cur = replaced[str(cur)]
        replaced[old] = cur
    # tasks whose card got a new id since the last pass → cards linking to them need a URL rewrite
    pending_rewrite = set(mapping.get("pending_rewrite") or [])
    stats = {"cards_updated": 0, "cards_checked": 0, "links_resolved": 0, "links_unresolved": 0, "dependencies": 0,
             "dependencies_failed": 0, "urls_rewritten": 0}
    writable = Writable(nc)
    try:
        for pid, entry in mapping["projects"].items():
            if project_ids and int(pid) not in project_ids:
                continue
            board_id = entry.get("board_id")
            if not board_id:
                continue
            built_doc = read_json(build_dir / f"{pid}.json")
            built = cards_of(built_doc["boards"][0]) if built_doc else {}
            # 1) dependencies – the current build decides which pairs exist (so a
            #    changed mapping rule propagates without re-importing); only pairs
            #    not yet applied for the current card ids are sent
            if built_doc:
                entry["dependencies"] = dependencies_of(built_doc["boards"][0])
            applied = entry.setdefault("dependencies_applied", {})
            for owner_tid, dep_tid in entry.get("dependencies") or []:
                owner, dependent = task_to_card.get(owner_tid), task_to_card.get(dep_tid)
                if not owner or not dependent:
                    continue
                key = f"{owner_tid}|{dep_tid}"
                ids = [owner["card_id"], dependent["card_id"]]
                if applied.get(key) == ids:
                    continue

                def add(current, owner=owner, dependent=dependent):
                    if dependent["card_id"] in (current.get("dependentCards") or []):
                        return
                    nc.deck_add_dependency(owner["board_id"], owner["stack_id"], owner["card_id"], dependent["card_id"])
                if writable.card(owner["board_id"], owner["stack_id"], owner["card_id"], add):
                    stats["dependencies"] += 1
                    applied[key] = ids
                else:
                    stats["dependencies_failed"] += 1
            # 2) + 3) descriptions – only cards that can have something to rewrite:
            #    not processed yet, or an earlier "nicht migriert" target exists now,
            #    or they link to a card whose id changed
            for tid, info in entry.get("cards", {}).items():
                refs = {other for _, other in PLACEHOLDER_RE.findall((built.get(tid) or {}).get("description") or "")}
                state = info.get("links") or {}
                needs = (force or not state.get("done")
                         or any(t in task_to_card for t in state.get("unresolved") or [])
                         or bool(refs & pending_rewrite))
                if not needs:
                    continue
                if not refs:  # nothing to resolve in this description, no request needed
                    info["links"] = {"done": True, "unresolved": []}
                    continue
                stats["cards_checked"] += 1
                try:
                    card = nc.deck_card(board_id, info["stack_id"], info["card_id"])
                except NCError as exc:
                    log.warning("card %s: %s", info["card_id"], exc)
                    continue
                info["links"] = {"done": True, "unresolved": sorted(t for t in refs if t not in task_to_card)}
                desc = card.get("description") or ""
                new_desc = desc
                for m in LINE_WITH_PLACEHOLDER_RE.finditer(desc):
                    if task_to_card.get(m.group(2)):
                        new_desc = new_desc.replace(m.group(0), "", 1)
                # references from earlier passes whose target exists now
                dep_pairs = {(o, d) for o, d in entry.get("dependencies") or []}
                for m in UNRESOLVED_LINE_RE.finditer(desc):
                    other = m.group(1)
                    if task_to_card.get(other) and ((tid, other) in dep_pairs or (other, tid) in dep_pairs):
                        new_desc = new_desc.replace(m.group(0), "", 1)  # now a Deck dependency

                def unresolved_repl(m: re.Match) -> str:
                    other = m.group(1)
                    target = task_to_card.get(other)
                    if target:
                        stats["links_resolved"] += 1
                        return f"→ [{names.get(other) or 'Karte'}]({nc.deck_card_url(target['card_id'])})"
                    return m.group(0)
                new_desc = UNRESOLVED_RE.sub(unresolved_repl, new_desc)

                def repl(m: re.Match) -> str:
                    other = m.group(2)
                    target = task_to_card.get(other)
                    if target:
                        stats["links_resolved"] += 1
                        return f"[{names.get(other) or 'Karte'}]({nc.deck_card_url(target['card_id'])})"
                    stats["links_unresolved"] += 1
                    return (f"{names[other]} " if names.get(other) and m.group(1) != "dep" else "") + \
                        f"(nicht migriert, MeisterTask-ID {other})"
                new_desc = PLACEHOLDER_RE.sub(repl, new_desc)
                if replaced:
                    def url_repl(m: re.Match) -> str:
                        new_id = replaced.get(m.group(2))
                        if new_id:
                            stats["urls_rewritten"] += 1
                            return f"{m.group(1)}{new_id}"
                        return m.group(0)
                    new_desc = CARD_URL_RE.sub(url_repl, new_desc)
                new_desc = EMPTY_SECTION_RE.sub("", new_desc)
                new_desc = re.sub(r"\n---\n\s*(?=_Migriert)", "\n", new_desc)
                new_desc = re.sub(r"\n{3,}", "\n\n", new_desc)
                if new_desc != desc:
                    def update(current, new_desc=new_desc):
                        current["description"] = new_desc
                        nc.deck_update_card(board_id, info["stack_id"], info["card_id"], current)
                    if writable.card(board_id, info["stack_id"], info["card_id"], update):
                        stats["cards_updated"] += 1
        mapping["pending_rewrite"] = []
        write_json(mapping_path(cfg), mapping)
    finally:
        writable.restore()
    log.info("link pass: %s", stats)
    return stats
