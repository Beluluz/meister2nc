"""Stage 2a – turn raw MeisterTask dumps into Deck JSON import documents.

The shape follows ``lib/Service/Importer/Systems/DeckJsonService.php``
(nextcloud/deck main, 2026-10): every property read by the importer is
always present.  Timestamps are unix seconds, ``duedate``/``done`` and comment
``creationDateTime`` are ISO-8601 with offset and without microseconds.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from .config import Config
from .extract_tasks import collect_raw_projects
from .usermap import UserMap
from .util import chunk_text, format_duration, iso_to_atom, iso_to_epoch, log, now_epoch, truncate, write_json

MENTION_RE = re.compile(r"<person_id>(\d+)</person_id>|<group_id>(\d+)</group_id>|<here\s*/>|<all\s*/>")
RELATION_LABEL = {"duplicate_of": "Duplikat von", "blocks": "Blockiert", "blocked_by": "Blockiert durch",
                  "related_to": "Verwandt mit", "is_parent_of": "Übergeordnet für", "is_child_of": "Unteraufgabe von"}
STATUS_LABEL = {1: "offen", 2: "erledigt", 8: "Papierkorb", 18: "erledigt & archiviert"}
# Placeholders resolved by the post-import pass (deck_import.run_link_pass):
#   [[mt:task:ID]]   → link to the migrated card (or MeisterTask reference)
#   [[mt:dep:ID]]    → Deck dependency: this card lists card ID as dependent card
#                      (MeisterTask subtasks and "blocked by")
#   [[mt:depof:ID]]  → Deck dependency on the other card: card ID lists this card
#                      (MeisterTask "blocks" / "is child of")
# Deck ≥ 1.18 "dependent cards" are directed and untyped; the importer does not
# read them, hence the second pass.
#   [[mt:depboth:ID]] → dependency in both directions (symmetric MeisterTask
#                      relations "related to" / "duplicate of")
PLACEHOLDER = "[[mt:task:{id}]]"
DEP_PLACEHOLDER = "[[mt:dep:{id}]]"
DEPOF_PLACEHOLDER = "[[mt:depof:{id}]]"
DEPBOTH_PLACEHOLDER = "[[mt:depboth:{id}]]"
# parent/child in both directions so that a subtask also shows its parent (Deck only
# displays outgoing dependencies); blocking keeps its direction
DEPENDENCY_TYPES = {"is_parent_of": DEPBOTH_PLACEHOLDER, "is_child_of": DEPBOTH_PLACEHOLDER,
                    "blocked_by": DEP_PLACEHOLDER, "blocks": DEPOF_PLACEHOLDER,
                    "related_to": DEPBOTH_PLACEHOLDER, "duplicate_of": DEPBOTH_PLACEHOLDER}
FOOTER_RE = re.compile(r"MeisterTask-Task `([A-Za-z0-9_-]+)`")


class BuildContext:
    def __init__(self, cfg: Config, usermap: UserMap, owner_uid: str, test: bool):
        self.cfg = cfg
        self.usermap = usermap
        self.owner_uid = owner_uid
        self.test = test
        days = cfg.deck.get("archive_completed_after_days")
        # tasks completed before this moment are archived in Deck as well
        self.archive_before: int | None = now_epoch() - int(days) * 86400 if days else None
        self.report: dict = {"projects": {}, "unmapped_persons": {}, "comments_split": 0, "attachments_skipped": [],
                             "attachments_embedded": 0, "link_attachments": 0}

    def note_unmapped(self, person_id, where: str) -> None:
        if person_id is None:
            return
        key = str(person_id)
        entry = self.report["unmapped_persons"].setdefault(key, {"name": self.usermap.name(person_id), "count": 0, "where": where})
        entry["count"] += 1


def build_all(cfg: Config, project_ids: list[int] | None = None, limit: int | None = None, test: bool = False) -> dict:
    raw = collect_raw_projects(cfg)
    if not raw:
        raise SystemExit("no raw task dumps found – run `extract tasks` first")
    usermap = UserMap(cfg)
    ctx = BuildContext(cfg, usermap, cfg.nc_user(), test)
    out_dir = cfg.build_dir / "deck"
    out_dir.mkdir(parents=True, exist_ok=True)
    for pid, entry in sorted(raw.items()):
        if project_ids and pid not in project_ids:
            continue
        board, meta = build_board(ctx, pid, entry, limit)
        write_json(out_dir / f"{pid}.json", {"boards": [board]})
        write_json(out_dir / f"{pid}.meta.json", meta)
        ctx.report["projects"][str(pid)] = {"title": board["title"], "stacks": len(board["stacks"]),
                                            "cards": sum(len(s["cards"]) for s in board["stacks"]),
                                            "labels": len(board["labels"]), "acl": len(board["acl"]),
                                            "accounts": entry["accounts"]}
        log.info("built board %r: %d cards", board["title"], ctx.report["projects"][str(pid)]["cards"])
    ctx.report["built_at"] = now_epoch()
    write_json(out_dir / "report.json", ctx.report)
    return ctx.report


def build_board(ctx: BuildContext, pid: int, entry: dict, limit: int | None) -> tuple[dict, dict]:
    cfg = ctx.cfg
    project = entry["project"]
    persons_by_id = {int(p["id"]): p for p in entry.get("persons") or [] if p.get("id")}
    members = entry.get("members") or {}
    if isinstance(members, dict):
        for p in members.get("persons") or []:
            if p.get("id"):
                persons_by_id.setdefault(int(p["id"]), p)
    for p in persons_by_id.values():
        ctx.usermap.extra_names.setdefault(str(p["id"]), " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x))

    title = truncate(project.get("name") or f"Projekt {pid}", 100 - len(cfg.deck["test_suffix"]) if ctx.test else 100)
    if ctx.test:
        title += cfg.deck["test_suffix"]
    board_ts = iso_to_epoch(project.get("updated_at"), now_epoch())

    # labels ------------------------------------------------------------
    labels = []
    for lab in entry.get("labels") or []:
        labels.append({"id": int(lab["id"]), "title": truncate(lab.get("name") or "Label", 100),
                       "color": _color(lab.get("color"), "31cc7c"), "lastModified": board_ts})
    label_ids = {l["id"] for l in labels}

    # acl ---------------------------------------------------------------
    # Meister: project_rights = individual persons (role_id 1 admin, 2 member,
    # 3 read-only, 4 commenter); project_memberships = the whole Team or a
    # Business Group.  Deck: type 0 = user, type 1 = group (participant = gid).
    acl = []
    seen_uids = set()
    rights = (members.get("project_rights") or []) if isinstance(members, dict) else []
    memberships = (members.get("project_memberships") or []) if isinstance(members, dict) else []
    role_by_person = {int(r["person_id"]): r.get("role_id") for r in rights if r.get("person_id")}
    group_acl = []
    # share_mode 2 = "team project": visible to the whole Meister team (verified in the
    # REST project object); explicit Team/Group memberships are the Business-plan form.
    team_wide = project.get("share_mode") == 2 or any(m.get("member_type") == "Team" for m in memberships)
    if team_wide:
        gid = cfg.deck.get("team_group")
        if gid:
            group_acl.append({"type": 1, "participant": gid, "permissionEdit": True,
                              "permissionShare": False, "permissionManage": False})
        else:
            ctx.report.setdefault("team_projects_without_group", []).append({"project": pid, "title": project.get("name")})
    for m in memberships:
        if m.get("member_type") != "Group":
            continue
        gid = (cfg.deck.get("group_map") or {}).get(str(m.get("member_id")))
        if not gid:
            ctx.report.setdefault("memberships_unmapped", []).append(
                {"project": pid, "member_type": m.get("member_type"), "member_id": m.get("member_id")})
            continue
        role = m.get("role_id")
        group_acl.append({"type": 1, "participant": gid, "permissionEdit": role not in (3, 4),
                          "permissionShare": role == 1, "permissionManage": role == 1})
    # individuals: everyone with access when no group covers the project,
    # otherwise only the project admins (manage rights on top of the group)
    if group_acl:
        individuals = {p for p, role in role_by_person.items() if role == 1}
    else:
        individuals = set(persons_by_id) | set(role_by_person)
    for person_id in sorted(individuals):
        uid = ctx.usermap.uid_active(person_id)
        if not uid:
            if not ctx.usermap.uid(person_id):
                ctx.note_unmapped(person_id, "acl")
            continue
        if uid == ctx.owner_uid or uid in seen_uids:
            continue
        seen_uids.add(uid)
        role = role_by_person.get(person_id)
        acl.append({"type": 0, "participant": uid, "permissionEdit": role not in (3, 4),
                    "permissionShare": role in (1, None), "permissionManage": role == 1})
    acl.extend(group_acl)

    # stacks ------------------------------------------------------------
    sections = sorted(entry.get("sections") or [], key=lambda s: (s.get("sequence") or 0, s["id"]))
    stacks: dict[int, dict] = {}
    for order, sec in enumerate(sections):
        if sec.get("status") == 2:
            continue
        stacks[int(sec["id"])] = {"id": int(sec["id"]), "title": truncate(sec.get("name") or "Unbenannt", 100) or "Unbenannt",
                                  "order": order, "lastModified": iso_to_epoch(sec.get("updated_at"), board_ts), "cards": []}
    fallback_stack = {"id": -1, "title": "Sonstige (MeisterTask)", "order": len(stacks), "lastModified": board_ts, "cards": []}

    # cards -------------------------------------------------------------
    tasks = sorted(entry["tasks"].values(),
                   key=lambda d: (stacks.get(d["task"].get("section_id"), fallback_stack)["order"],
                                  d["task"].get("sequence") or 0, d["task"]["id"]))
    if limit:
        tasks = tasks[:limit]
    meta = {"project_id": pid, "tasks": {}, "sections": {str(k): v["title"] for k, v in stacks.items()}}
    cf_types = {int(t["id"]): t for t in entry.get("custom_field_types") or [] if t.get("id")}
    counters: dict[int, int] = {}
    for detail in tasks:
        task = detail["task"]
        stack = stacks.get(task.get("section_id")) or fallback_stack
        order = counters.get(stack["id"], 0)
        counters[stack["id"]] = order + 1
        card = build_card(ctx, pid, detail, order, cf_types, label_ids, board_ts)
        stack["cards"].append(card)
        meta["tasks"][str(task["id"])] = {"token": task.get("token"), "name": task.get("name"),
                                          "section_id": task.get("section_id"), "updated_at": task.get("updated_at")}
    if fallback_stack["cards"]:
        stacks[-1] = fallback_stack

    board = {
        "id": pid,
        "title": title,
        "owner": ctx.owner_uid,
        "color": _color(cfg.deck.get("board_color"), "0082c9"),
        "archived": project.get("status") == 5,
        "deletedAt": 0,
        "lastModified": board_ts,
        "labels": labels,
        "acl": acl,
        "stacks": sorted(stacks.values(), key=lambda s: s["order"]),
    }
    return board, meta


def build_card(ctx: BuildContext, pid: int, detail: dict, order: int, cf_types: dict, label_ids: set, board_ts: int) -> dict:
    cfg = ctx.cfg
    task = detail["task"]
    status = task.get("status")
    created = iso_to_epoch(task.get("created_at"), board_ts)
    modified = iso_to_epoch(task.get("updated_at"), created)
    # Deck allows several assignees: MeisterTask's assignee plus its watchers
    assigned: list[str] = []
    not_assignable: list[str] = []
    candidates = ([task.get("assigned_to_id")] if task.get("assigned_to_id") else []) + \
        [s.get("person_id") for s in detail.get("subscriptions") or [] if s.get("person_id")]
    for person_id in candidates:
        uid = ctx.usermap.uid_active(person_id)
        if uid:
            if uid not in assigned:
                assigned.append(uid)
        else:
            if not ctx.usermap.uid(person_id):
                ctx.note_unmapped(person_id, "assignee/watcher")
            name = ctx.usermap.name(person_id)
            if name not in not_assignable:
                not_assignable.append(name)
    assignee_uid = assigned[0] if assigned else None
    title = truncate((task.get("name") or "").strip() or "(ohne Titel)", 255)
    archived = status == 18
    if status == 2 and ctx.archive_before is not None:  # completed long ago → archived too
        archived = iso_to_epoch(task.get("status_updated_at"), ctx.archive_before) < ctx.archive_before

    card = {
        "id": int(task["id"]),
        "title": title,
        "description": compose_description(ctx, detail, cf_types, title_truncated=(title != (task.get("name") or "")),
                                           not_assignable=not_assignable),
        "order": order,
        "archived": archived,
        "createdAt": created,
        "lastModified": modified,
        "lastEditor": assignee_uid or ctx.owner_uid,
        "duedate": iso_to_atom(task.get("due")),
        "startdate": None,
        "done": iso_to_atom(task.get("status_updated_at")) if status in (2, 18) else None,
        "labels": [{"id": int(l["label_id"]), "boardId": pid} for l in detail.get("labels") or []
                   if l.get("label_id") and int(l["label_id"]) in label_ids],
        "assignedUsers": [{"participant": {"uid": uid, "type": 0}} for uid in assigned],
        "comments": build_comments(ctx, detail),
        "attachments": build_attachments(ctx, detail),
    }
    return card


def compose_description(ctx: BuildContext, detail: dict, cf_types: dict, title_truncated: bool = False,
                        not_assignable: list[str] | None = None) -> str:
    task = detail["task"]
    um = ctx.usermap
    parts: list[str] = []
    if title_truncated:
        parts.append(f"**{task.get('name')}**\n")
    notes = (task.get("notes") or "").strip()
    # a description starting with "---" renders as YAML front matter in Nextcloud Text
    # (grey code block); MeisterTask notes sometimes begin with such a rule → drop it
    notes = re.sub(r"^(?:-{3,}|\*{3,}|_{3,})[ \t]*\n+", "", notes).strip()
    if notes:
        parts.append(notes)

    extras: list[str] = []
    # checklists (only those that have items; MeisterTask keeps empty headings around)
    items_by_list: dict[int, list] = {}
    for item in detail.get("checklist_items") or []:
        items_by_list.setdefault(item.get("checklist_id"), []).append(item)
    checklists = sorted(detail.get("checklists") or [], key=lambda c: (c.get("sequence") or 0, c["id"]))
    if items_by_list:
        filled = [cl for cl in checklists if items_by_list.get(cl["id"])]
        orphan = [cid for cid in items_by_list if cid not in {cl["id"] for cl in filled}]
        lines: list[str] = []

        def item_lines(items: list) -> list[str]:
            out = []
            for it in sorted(items, key=lambda i: (i.get("sequence") or 0, i["id"])):
                out.extend(checklist_item_lines(it))
            return out
        if len(filled) == 1 and not orphan:
            # a single checklist: its own name is the only heading (no "Checklisten" group)
            lines.append(f"### {filled[0].get('name') or 'Checkliste'}")
            lines.extend(item_lines(items_by_list[filled[0]["id"]]))
        else:
            lines.append("### Checklisten")
            for cl in filled:
                lines.append(f"**{cl.get('name') or 'Checkliste'}**")
                lines.extend(item_lines(items_by_list[cl["id"]]))
                lines.append("")
            for cid in orphan:
                lines.extend(item_lines(items_by_list[cid]))
        extras.append("\n".join(lines).rstrip())
    # subtasks
    subtasks = detail.get("subtasks") or []
    if subtasks:
        lines = ["### Unteraufgaben"]
        for st in sorted(subtasks, key=lambda s: (s.get("sequence") or 0, s["id"])):
            mark = "x" if st.get("status") in (2, 18) else " "
            lines.append(f"- [{mark}] {st.get('name') or ''} {DEPBOTH_PLACEHOLDER.format(id=st['id'])}")
        extras.append("\n".join(lines))
    # relationships
    subtask_ids = {int(st["id"]) for st in subtasks if st.get("id")}
    rels = []
    for rel in detail.get("relationships") or []:
        other = rel.get("target_id") if rel.get("owner_id") == task["id"] else rel.get("owner_id")
        if rel.get("type") == "is_parent_of" and other in subtask_ids:
            continue  # already listed under "Unteraufgaben"
        rels.append((rel, other))
    if rels:
        # every relation becomes a Deck dependency after import (symmetric ones in
        # both directions); the lines below only survive for counterparts that
        # were not migrated
        lines = ["### Verknüpfte Karten"]
        for rel, other in rels:
            rtype = rel.get("type")
            placeholder = DEPENDENCY_TYPES.get(rtype, PLACEHOLDER)
            lines.append(f"- {RELATION_LABEL.get(rtype, rtype)}: {placeholder.format(id=other)}")
        extras.append("\n".join(lines))
    # custom fields
    cfs = detail.get("custom_fields") or []
    if cfs:
        lines = ["### Felder", "| Feld | Wert |", "|---|---|"]
        for cf in cfs:
            t = cf_types.get(cf.get("custom_field_type_id")) or {}
            name = t.get("name") or f"Feld {cf.get('custom_field_type_id')}"
            value = str(cf.get("value") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {name} | {value} |")
        extras.append("\n".join(lines))
    # link / cloud attachments
    link_lines = []
    for att in detail.get("attachments") or []:
        if att.get("source") in (1, 2, 4, 6) or not att.get("_local"):
            if att.get("source") in (1, 2, 4, 6):
                url = att.get("url") or ""
                link_lines.append(f"- [{att.get('name') or url}]({url})")
                ctx.report["link_attachments"] += 1
    if link_lines:
        extras.append("### Links\n" + "\n".join(link_lines))
    # what Deck cannot hold natively: people without an (active) Nextcloud account
    # who were assigned or watching, and tracked time
    residual = []
    if not_assignable:
        residual.append("Beteiligt in MeisterTask ohne aktives Nextcloud-Konto: " + ", ".join(not_assignable))
    if task.get("tracked_time"):
        per_person: dict = {}
        for wi in detail.get("work_intervals") or []:
            start, end = iso_to_epoch(wi.get("started_at")), iso_to_epoch(wi.get("finished_at"))
            if start and end and end > start:
                per_person[wi.get("person_id")] = per_person.get(wi.get("person_id"), 0) + (end - start)
        detail_txt = "; ".join(f"{um.name(p)} {format_duration(s)}" for p, s in per_person.items())
        residual.append(f"Erfasste Zeit: {format_duration(task['tracked_time'])}" + (f" ({detail_txt})" if detail_txt else ""))
    if residual:
        extras.append("\n".join(f"_{line}_" for line in residual))

    if extras:
        # a description that *starts* with "---" would be rendered by Nextcloud Text
        # as YAML front matter (grey code-like block) → separator only after notes
        parts.append(("\n---\n" if parts else "") + "\n\n".join(extras))
    if ctx.cfg.deck.get("card_footer", True):
        url = f"https://www.meistertask.com/app/task/{task.get('token')}"
        parts.append(f"\n_Migriert aus MeisterTask · MeisterTask-Task `{task.get('token')}` · {url}_")
    return "\n".join(parts).strip() + "\n"


def checklist_item_lines(item: dict) -> list[str]:
    """One task-list entry; MeisterTask items may span several lines (own
    sub-bullets), which are indented so that they stay inside the entry."""
    text = (item.get("name") or "").strip()
    first, *rest = text.split("\n") if text else [""]
    lines = [f"- [{'x' if item.get('status') == 5 else ' '}] {first.strip()}"]
    for line in rest:
        line = line.rstrip()
        if line.strip():
            lines.append("  " + line.strip())
    return lines


def build_comments(ctx: BuildContext, detail: dict) -> list[dict]:
    um = ctx.usermap
    out = []
    limit = int(ctx.cfg.deck.get("comment_max_chars", 950))
    for c in sorted(detail.get("comments") or [], key=lambda c: (c.get("created_at") or "", c["id"])):
        author_uid = um.uid(c.get("person_id"))
        text = resolve_mentions(c.get("text") or "", um)
        if not author_uid:
            ctx.note_unmapped(c.get("person_id"), "comment")
            text = f"**{um.name(c.get('person_id'))} (MeisterTask):**\n{text}"
            author_uid = ctx.owner_uid
        chunks = chunk_text(text, limit - 12)
        if len(chunks) > 1:
            ctx.report["comments_split"] += 1
        when = iso_to_atom(c.get("created_at")) or iso_to_atom(c.get("updated_at"))
        for i, chunk in enumerate(chunks, 1):
            msg = chunk if len(chunks) == 1 else f"({i}/{len(chunks)}) {chunk}"
            out.append({"id": f"{c['id']}" if len(chunks) == 1 else f"{c['id']}-{i}", "parentId": "0",
                        "actorType": "users", "actorId": author_uid, "message": msg, "creationDateTime": when})
    return out


def build_attachments(ctx: BuildContext, detail: dict) -> list[dict]:
    out = []
    max_bytes = int(float(ctx.cfg.meister.get("max_attachment_mb", 50)) * 1024 * 1024)
    for att in detail.get("attachments") or []:
        local = att.get("_local")
        if not local or att.get("source") in (1, 2, 4, 6):
            if att.get("source") not in (1, 2, 4, 6):
                ctx.report["attachments_skipped"].append({"task": detail["task"]["id"], "id": att.get("id"),
                                                          "name": att.get("name"), "reason": (att.get("_download") or {}).get("status")})
            continue
        path = Path(local)
        if not path.exists() or path.stat().st_size > max_bytes:
            ctx.report["attachments_skipped"].append({"task": detail["task"]["id"], "id": att.get("id"),
                                                      "name": att.get("name"), "reason": "missing or too large"})
            continue
        uid = ctx.usermap.uid(att.get("person_id")) or ctx.owner_uid
        ts = iso_to_epoch(att.get("created_at"), now_epoch())
        # not embedded as base64: a 3,500-task board would become a ~1 GB request.
        # The importer strips attachments from the payload and uploads the files
        # per card afterwards (OCS attachment endpoint).
        out.append({"type": "file", "data": path.name, "localPath": str(path), "size": path.stat().st_size,
                    "createdBy": uid, "createdAt": ts, "lastModified": iso_to_epoch(att.get("updated_at"), ts)})
        ctx.report["attachments_embedded"] += 1
    return out


def resolve_mentions(text: str, um: UserMap) -> str:
    """MeisterTask mention tags → Nextcloud comment mentions.

    Nextcloud parses ``@word`` as a user mention and shows unknown ids as
    "Unknown account", so only persons with a Nextcloud account become
    mentions (quoted id form ``@"uid"``, rendered with the display name);
    everyone else is written as a plain bold name, and ``@all``/``@here``
    become plain text."""
    def repl(m: re.Match) -> str:
        if m.group(1):
            person_id = int(m.group(1))
            uid = um.uid(person_id)
            return f'@"{uid}"' if uid else f"**{um.name(person_id)}**"
        if m.group(2):
            return f"**Gruppe {m.group(2)}**"
        return "**(an alle Beteiligten)**" if "here" in m.group(0) else "**(an alle)**"
    return html.unescape(MENTION_RE.sub(repl, text))


def _color(value: str | None, default: str) -> str:
    v = (value or "").strip().lstrip("#")
    return v if re.fullmatch(r"[0-9a-fA-F]{6}", v) else default
