"""Stage 1a – dump everything the MeisterTask REST API exposes for one
account into ``raw/accounts/<label>/`` (verbatim JSON, resumable).

Endpoints (developers.meistertask.com, verified 2026-10-21):
  GET /persons/me, /persons
  GET /projects?status=all                     (active + archived)
  GET /projects/{id}/sections?status=all
  GET /projects/{id}/labels
  GET /projects/{id}/members?include_persons=true   (rights, memberships, persons)
  GET /projects/{id}/persons
  GET /projects/{id}/custom_field_types        (Business plan)
  GET /projects/{id}/tasks?status=open|completed|completed_archived|trashed
  GET /tasks/{id}/task_labels, /checklists, /checklist_items, /comments,
      /attachments, /task_relationships, /subtasks, /task_subscriptions,
      /custom_fields, /work_intervals
Pagination: items (max 500) + page (1-based) until an empty/short page.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .accounts import rest_client
from .config import Config
from .util import log, now_epoch, read_json, safe_filename, write_json

TASK_STATUSES = ["open", "completed", "completed_archived"]


def _section_rank(sections: list[dict]) -> dict[int, int]:
    ordered = sorted(sections, key=lambda s: (s.get("sequence") if s.get("sequence") is not None else 0, s["id"]))
    return {s["id"]: i for i, s in enumerate(ordered)}


def projects_extracted_elsewhere(cfg: Config, label: str) -> dict[int, str]:
    """project id → other account label that already holds a complete dump of it."""
    out: dict[int, str] = {}
    root = cfg.raw_dir / "accounts"
    if not root.exists():
        return out
    for acct in sorted(root.iterdir()):
        if acct.name == label:
            continue
        man = read_json(acct / "manifest.json") or {}
        for pid, p in (man.get("projects") or {}).items():
            if not p.get("failed") and not p.get("limit", man.get("limit")):
                out.setdefault(int(pid), acct.name)
    return out


def run(cfg: Config, label: str, project_ids: list[int] | None = None, limit: int | None = None,
        refresh: bool = False, include_trashed: bool | None = None, with_files: bool = True,
        workers: int = 4, all_projects: bool = False) -> dict:
    rest = rest_client(cfg, label)
    acct = cfg.account_dir(label)
    acct.mkdir(parents=True, exist_ok=True)
    elsewhere = {} if all_projects else projects_extracted_elsewhere(cfg, label)
    if include_trashed is None:
        include_trashed = bool(cfg.meister.get("include_trashed_tasks"))
    statuses = TASK_STATUSES + (["trashed"] if include_trashed else [])
    max_bytes = int(float(cfg.meister.get("max_attachment_mb", 50)) * 1024 * 1024)

    me = rest.get("/persons/me")
    write_json(acct / "me.json", me)
    log.info("account %s = %s %s <%s> (id %s)", label, me.get("firstname"), me.get("lastname"), me.get("email"), me.get("id"))

    persons = rest.get_all("/persons")
    write_json(acct / "persons.json", persons)
    log.info("persons visible: %d", len(persons))

    projects = rest.get_all("/projects", {"status": "all"})
    write_json(acct / "projects.json", projects)
    log.info("projects visible: %d (%d archived)", len(projects), sum(1 for p in projects if p.get("status") == 5))

    # the manifest is merged across runs: a run limited to some projects must not
    # forget the others (they decide what other accounts may skip)
    manifest = read_json(acct / "manifest.json", {}) or {}
    manifest.update({"label": label, "extracted_at": now_epoch()})
    manifest.setdefault("projects", {})
    manifest.pop("limit", None)
    known_persons = {int(p["id"]) for p in persons if p.get("id")} | {int(me["id"])}
    persons_extra: dict = read_json(acct / "persons_extra.json", {}) or {}
    manifest["projects_seen"] = [int(p["id"]) for p in projects]  # visibility, incl. skipped ones
    for project in projects:
        pid = int(project["id"])
        if project_ids and pid not in project_ids:
            continue
        if pid in elsewhere:
            log.info("project %s %r already extracted by account %s – skipped (use --all-projects to force)",
                     pid, project.get("name"), elsewhere[pid])
            continue
        pdir = acct / "projects" / str(pid)
        pdir.mkdir(parents=True, exist_ok=True)
        write_json(pdir / "project.json", project)
        sections = rest.get_all(f"/projects/{pid}/sections", {"status": "all"})
        write_json(pdir / "sections.json", sections)
        labels = rest.get(f"/projects/{pid}/labels") or []
        write_json(pdir / "labels.json", labels)
        members = rest.get(f"/projects/{pid}/members", {"include_persons": "true"}, ok404=True, ok403=True)
        write_json(pdir / "members.json", members)
        p_persons = rest.get(f"/projects/{pid}/persons", ok403=True, ok404=True) or []
        write_json(pdir / "persons.json", p_persons)
        cf_types = rest.get(f"/projects/{pid}/custom_field_types", ok403=True, ok404=True) or []
        write_json(pdir / "custom_field_types.json", cf_types)

        tasks: dict[int, dict] = {}
        for status in statuses:
            for task in rest.get_all(f"/projects/{pid}/tasks", {"status": status}):
                tasks[int(task["id"])] = task
        rank = _section_rank(sections)
        ordered = sorted(tasks.values(), key=lambda t: (rank.get(t.get("section_id"), 9999),
                                                         t.get("sequence") if t.get("sequence") is not None else 0,
                                                         t["id"]))
        if limit:
            ordered = ordered[:limit]
        write_json(pdir / "tasks.json", ordered)
        log.info("project %s %r: %d tasks%s", pid, project.get("name"), len(ordered),
                 f" (limited to {limit})" if limit else "")

        for p in p_persons + ((members or {}).get("persons") or [] if isinstance(members, dict) else []):
            if p.get("id"):
                known_persons.add(int(p["id"]))

        referenced: set[int] = set()
        todo: list[dict] = []
        for task in ordered:
            tid = int(task["id"])
            tfile = pdir / "task" / f"{tid}.json"
            if tfile.exists() and not refresh:
                existing = read_json(tfile)
                if existing and existing.get("task", {}).get("updated_at") == task.get("updated_at") and existing.get("_complete"):
                    referenced |= referenced_person_ids(existing)
                    continue
            todo.append(task)
        # the REST API answers in ~0.4 s per call and a task needs ~12 calls → fetch
        # task details concurrently (GET only; the session is shared)
        done = 0
        failed: list[dict] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_extract_task, rest, cfg, task, with_files, max_bytes): task for task in todo}
            for fut in as_completed(futures):
                task = futures[fut]
                try:
                    detail = fut.result()
                except Exception as exc:  # noqa: BLE001 – one task must not abort the run
                    log.error("  task %s failed: %s", task["id"], exc)
                    failed.append({"task": task["id"], "error": str(exc)[:300]})
                    continue
                write_json(pdir / "task" / f"{int(task['id'])}.json", detail)
                referenced |= referenced_person_ids(detail)
                done += 1
                if done % 25 == 0:
                    log.info("  %d/%d tasks", done, len(todo))
        if failed:
            log.warning("  %d tasks failed in project %s – they are retried on the next run", len(failed), pid)
        # people removed from the team still appear on comments etc. → resolve one by one
        for person_id in sorted(referenced - known_persons):
            if str(person_id) in persons_extra:
                continue
            person = rest.get(f"/persons/{person_id}", ok404=True, ok403=True)
            persons_extra[str(person_id)] = person or {"id": person_id, "_unresolved": True}
            log.info("  resolved person %s → %s", person_id, "not available" if not person else person.get("email"))
        known_persons |= referenced
        write_json(acct / "persons_extra.json", persons_extra)
        manifest["projects"][str(pid)] = {"name": project.get("name"), "status": project.get("status"),
                                          "tasks": len(ordered), "sections": len(sections), "labels": len(labels),
                                          "failed": failed, "limit": limit, "extracted_at": now_epoch()}
        write_json(acct / "manifest.json", manifest)
    write_json(acct / "manifest.json", manifest)  # also when every project was skipped
    resolve_unresolved_persons(cfg, label, acct, persons_extra)
    log.info("extraction for %s complete", label)
    return manifest


def resolve_unresolved_persons(cfg: Config, label: str, acct: Path, persons_extra: dict) -> None:
    """People removed from the Meister team are gone from /persons and
    /persons/{id}, but the MCP activity feed of a task they commented on still
    carries their name (verified 2026-10-22).  Manual names go into
    ``data/persons.overrides.json`` as ``{"<id>": "First Last"}``."""
    overrides = read_json(cfg.data_dir / "persons.overrides.json", {}) or {}
    wanted = {int(k) for k, v in persons_extra.items() if v.get("_unresolved")}
    for pid in list(wanted):
        name = overrides.get(str(pid))
        if name:
            first, _, last = name.strip().partition(" ")
            persons_extra[str(pid)] = {"id": pid, "firstname": first, "lastname": last, "_source": "override"}
            wanted.discard(pid)
    if not wanted:
        write_json(acct / "persons_extra.json", persons_extra)
        return
    # tasks where each wanted person left a trace
    tasks_for: dict[int, list[int]] = {}
    for tfile in (acct / "projects").glob("*/task/*.json"):
        detail = read_json(tfile) or {}
        for pid in referenced_person_ids(detail) & wanted:
            tasks_for.setdefault(pid, []).append(int(detail["task"]["id"]))
    try:
        from .accounts import mcp_client
        mcp = mcp_client(cfg, label)
        mcp.initialize()
    except Exception as exc:  # noqa: BLE001
        log.warning("MCP unavailable for person resolution: %s", exc)
        return
    from .util import parse_tool_payload
    for pid, task_ids in tasks_for.items():
        found = None
        for task_id in task_ids[:3]:
            cursor = None
            for _ in range(10):
                args = {"taskId": task_id, "first": 50}
                if cursor:
                    args["after"] = cursor
                try:
                    payload = parse_tool_payload(mcp.call_tool("mt_tasks_activities_list", args)) or {}
                except Exception as exc:  # noqa: BLE001
                    log.debug("activities %s: %s", task_id, exc)
                    break
                for node in payload.get("nodes") or []:
                    person = node.get("person") or {}
                    if person.get("id") == pid and (person.get("firstname") or person.get("lastname")):
                        found = person
                        break
                if found or not (payload.get("pageInfo") or {}).get("hasNextPage"):
                    break
                cursor = (payload.get("pageInfo") or {}).get("endCursor")
            if found:
                break
        if found:
            persons_extra[str(pid)] = {"id": pid, "firstname": found.get("firstname"), "lastname": found.get("lastname"),
                                       "_source": "activities"}
            log.info("  resolved removed person %s via activity feed → %s %s", pid, found.get("firstname"), found.get("lastname"))
        else:
            log.info("  person %s stays unresolved – add it to data/persons.overrides.json", pid)
    write_json(acct / "persons_extra.json", persons_extra)


def referenced_person_ids(detail: dict) -> set[int]:
    ids: set[int] = set()
    task = detail.get("task") or {}
    if task.get("assigned_to_id"):
        ids.add(int(task["assigned_to_id"]))
    for key in ("comments", "attachments", "subscriptions", "work_intervals"):
        for item in detail.get(key) or []:
            if isinstance(item, dict) and item.get("person_id"):
                ids.add(int(item["person_id"]))
    for st in detail.get("subtasks") or []:
        if st.get("assigned_to_id"):
            ids.add(int(st["assigned_to_id"]))
    return ids


def _extract_task(rest, cfg: Config, task: dict, with_files: bool, max_bytes: int) -> dict:
    tid = int(task["id"])
    detail = {
        "task": task,
        "labels": rest.get(f"/tasks/{tid}/task_labels") or [],
        "checklists": rest.get_all(f"/tasks/{tid}/checklists"),
        "checklist_items": rest.get_all(f"/tasks/{tid}/checklist_items"),
        "comments": rest.get_all(f"/tasks/{tid}/comments"),
        "attachments": rest.get(f"/tasks/{tid}/attachments") or [],
        "relationships": rest.get(f"/tasks/{tid}/task_relationships") or [],
        "subtasks": rest.get_all(f"/tasks/{tid}/subtasks", ok404=True) or [],
        "subscriptions": rest.get(f"/tasks/{tid}/task_subscriptions") or [],
        "custom_fields": rest.get(f"/tasks/{tid}/custom_fields", ok403=True, ok404=True) or [],
        "work_intervals": rest.get(f"/tasks/{tid}/work_intervals", ok403=True, ok404=True) or [],
    }
    if with_files:
        for att in detail["attachments"]:
            _download_attachment(rest, cfg, att, max_bytes)
    detail["_complete"] = True
    detail["_extracted_at"] = now_epoch()
    return detail


def _download_attachment(rest, cfg: Config, att: dict, max_bytes: int) -> None:
    url = att.get("url") or att.get("download_url")
    if not url:
        att["_download"] = {"status": "no-url"}
        return
    if url.startswith("/"):
        url = cfg.meister["rest_base"].split("/api")[0] + url
    name = safe_filename(att.get("name") or f"attachment-{att['id']}")
    dest_dir = cfg.files_dir / "attachments" / str(att["id"])
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        att["_local"] = str(dest)
        att["_download"] = {"status": 200, "bytes": dest.stat().st_size, "cached": True}
        return
    if att.get("size") and int(att["size"]) > max_bytes:
        att["_download"] = {"status": "too-large", "bytes": int(att["size"])}
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    info = rest.download(url, dest, with_auth=True, max_bytes=max_bytes)
    att["_download"] = info
    if info["status"] == 200:
        att["_local"] = str(dest)
    else:
        log.warning("attachment %s (%s) download failed: %s", att["id"], name, info["status"])
        if dest.exists():
            dest.unlink()


def collect_raw_projects(cfg: Config) -> dict[int, dict]:
    """Merge all account dumps: for each project id gather sections/labels/
    members from the first account that has them and the union of tasks."""
    merged: dict[int, dict] = {}
    accounts_root = cfg.raw_dir / "accounts"
    if not accounts_root.exists():
        return merged
    for acct in sorted(accounts_root.iterdir()):
        pdirs = acct / "projects"
        if not pdirs.exists():
            continue
        for pdir in sorted(pdirs.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
            if not pdir.name.isdigit():
                continue
            pid = int(pdir.name)
            project = read_json(pdir / "project.json")
            if not project:
                continue
            entry = merged.setdefault(pid, {"project": project, "sections": [], "labels": [], "members": None,
                                            "persons": [], "custom_field_types": [], "tasks": {}, "accounts": []})
            entry["accounts"].append(acct.name)
            for key in ("sections", "labels", "persons", "custom_field_types"):
                data = read_json(pdir / f"{key}.json") or []
                if data and not entry[key]:
                    entry[key] = data
            members = read_json(pdir / "members.json")
            if members and not entry["members"]:
                entry["members"] = members
            for task in read_json(pdir / "tasks.json") or []:
                tid = int(task["id"])
                detail = read_json(pdir / "task" / f"{tid}.json")
                if detail and tid not in entry["tasks"]:
                    entry["tasks"][tid] = detail
    return merged


def collect_persons(cfg: Config) -> dict[int, dict]:
    persons: dict[int, dict] = {}
    accounts_root = cfg.raw_dir / "accounts"
    if not accounts_root.exists():
        return persons
    for acct in accounts_root.iterdir():
        for src in [acct / "persons.json", acct / "me.json"] + list((acct / "projects").glob("*/persons.json")):
            data = read_json(src)
            if not data:
                continue
            items = data if isinstance(data, list) else [data]
            for p in items:
                if isinstance(p, dict) and p.get("id"):
                    persons.setdefault(int(p["id"]), p)
        for p in (read_json(acct / "persons_extra.json", {}) or {}).values():
            if isinstance(p, dict) and p.get("id") and not p.get("_unresolved"):
                persons.setdefault(int(p["id"]), p)
        for members in (acct / "projects").glob("*/members.json"):
            data = read_json(members) or {}
            for p in (data.get("persons") or []) if isinstance(data, dict) else []:
                if p.get("id"):
                    persons.setdefault(int(p["id"]), p)
    return persons
