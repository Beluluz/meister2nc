"""Stage 2b – transform MeisterNote Markdown into Collectives Markdown.

Pure functions; the Collectives importer substitutes the placeholders once it
knows page ids and attachment names:

    {{m2nc-file:<fileKey>}}   → ![name](.attachments.<pageId>/<name>) or [name](…)
    {{m2nc-note:<token>}}     → Collectives page link (or original URL)
    [[mt:task:<id>]]          → Deck card link (or MeisterTask URL)
"""

from __future__ import annotations

import html
import json
import re

from .config import Config
from .util import epoch_to_display, iso_to_epoch, log, read_json, safe_filename, write_json

MARKER_RE = re.compile(r"<!--\s*meisternote:(\w+)\s+(\{.*?\})\s*-->[ \t]*\n?", re.S)
MEDIA_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(/media/note_file/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)\)")
LOP_LINE_RE = re.compile(r"^\[List of pages\][ \t]*\n?", re.M)
# visible placeholder lines of MeisterNote blocks that have no Collectives equivalent
# (the Text outline is a viewer toggle, not part of the file)
PLACEHOLDER_LINES_RE = re.compile(r"^\[(Table of contents|Inhaltsverzeichnis)\][ \t]*\n?", re.M)
ATTACHED_LINE_RE = re.compile(r"^\[Attached file\][ \t]*\n?", re.M)
NOTE_URL_RE = re.compile(r"https?://(?:www\.)?meisternote\.com/app/note/([A-Za-z0-9_-]+)(?:/[^\s)\]]*)?")
TASK_URL_RE = re.compile(r"https?://(?:www\.)?meistertask\.com/app/task/([A-Za-z0-9_-]+)(?:/[^\s)\]]*)?")
FILE_PLACEHOLDER = "{{m2nc-file:%s}}"
NOTE_PLACEHOLDER = "{{m2nc-note:%s}}"
IMAGE_TYPES = ("image/",)


def transform(markdown: str, note: dict, files: dict, cfg_coll: dict) -> tuple[str, list[dict], dict]:
    """Return (markdown with placeholders, referenced files, stats)."""
    text = markdown or ""
    stats = {"markers": {}, "files": 0, "note_links": 0, "task_links": 0}
    refs: dict[str, dict] = {}

    def marker_repl(m: re.Match) -> str:
        kind = m.group(1)
        stats["markers"][kind] = stats["markers"].get(kind, 0) + 1
        if kind == "file":
            try:
                meta = json.loads(m.group(2))
            except json.JSONDecodeError:
                return ""
            key = meta.get("fileKey")
            if key:
                refs[key] = {"key": key, "name": meta.get("fileName") or key, "content_type": meta.get("fileContentType") or ""}
        return ""
    text = MARKER_RE.sub(marker_repl, text)
    text = LOP_LINE_RE.sub("", text)
    text = PLACEHOLDER_LINES_RE.sub("", text)

    def media_repl(m: re.Match) -> str:
        alt, token, key = m.group(1), m.group(2), m.group(3)
        refs.setdefault(key, {"key": key, "name": alt or key, "content_type": ""})
        stats["files"] += 1
        return FILE_PLACEHOLDER % key
    text = MEDIA_LINK_RE.sub(media_repl, text)
    # "[Attached file]" lines follow file markers for non-images: replace by placeholder of the last marker
    pending = [k for k in refs if FILE_PLACEHOLDER % k not in text]
    def attached_repl(m: re.Match) -> str:
        if pending:
            key = pending.pop(0)
            stats["files"] += 1
            return FILE_PLACEHOLDER % key + "\n"
        return ""
    text = ATTACHED_LINE_RE.sub(attached_repl, text)
    for key in pending:  # markers without any visible link → append
        text += "\n" + FILE_PLACEHOLDER % key + "\n"
        stats["files"] += 1

    def note_repl(m: re.Match) -> str:
        stats["note_links"] += 1
        return NOTE_PLACEHOLDER % m.group(1)
    text = NOTE_URL_RE.sub(note_repl, text)

    def task_repl(m: re.Match) -> str:
        stats["task_links"] += 1
        return "[[mt:tasktoken:%s]]" % m.group(1)
    text = TASK_URL_RE.sub(task_repl, text)

    if cfg_coll.get("unescape_html_entities", True):
        text = html.unescape(text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip() + "\n"

    for ref in refs.values():
        info = (files or {}).get(ref["key"]) or {}
        ref["local"] = info.get("local")
        ref["status"] = info.get("status")
        if not ref.get("content_type") and info.get("content_type"):
            ref["content_type"] = info["content_type"]
        ref["is_image"] = str(ref.get("content_type") or "").startswith(IMAGE_TYPES)
    return text, list(refs.values()), stats


def comment_text(content, name_of) -> str:
    """MeisterNote discussion comments are Quill-style deltas:
    ``{"comment_text": [{"insert": "text"}, {"insert": {"mention": {"person_id": 1}}},
    {"insert": {"emoticon": {"colons": ":smile:"}}}]}``.  Plain strings pass through."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        ops = content.get("comment_text") or content.get("ops") or []
    elif isinstance(content, list):
        ops = content
    else:
        return str(content)
    out = []
    for op in ops:
        ins = op.get("insert") if isinstance(op, dict) else op
        if isinstance(ins, str):
            out.append(ins)
        elif isinstance(ins, dict):
            if "mention" in ins:
                m = ins["mention"] or {}
                out.append(f"**@{name_of(m.get('person_id'))}**" if m.get("person_id") else "**@?**")
            elif "emoticon" in ins:
                out.append((ins["emoticon"] or {}).get("colons") or "")
            else:
                out.append(json.dumps(ins, ensure_ascii=False))
    return "".join(out).strip()


def render_discussions(discussions: list[dict], name_of) -> str:
    if not discussions:
        return ""
    lines = ["", "---", "## Kommentare (MeisterNote)", ""]
    for d in discussions:
        status = "aufgelöst" if (d.get("_status") == "RESOLVED" or d.get("resolved") or d.get("status") == "RESOLVED") else "offen"
        quote = d.get("quote") or d.get("blockText") or ""
        head = f"**Diskussion** ({status}, {d.get('commentCount', '')} Kommentare)".replace(",  Kommentare", "")
        if quote:
            head += f" zu: _{str(quote)[:200]}_"
        lines.append(head)
        comments = d.get("comments") or d.get("items") or []
        if isinstance(comments, dict):
            comments = comments.get("items") or []
        for c in comments:
            who = name_of(c.get("personId") or c.get("person_id"))
            when = c.get("insertedAt") or c.get("createdAt") or c.get("created_at") or ""
            body = comment_text(c.get("content") if c.get("content") is not None else (c.get("text") or c.get("markdownContent")), name_of)
            body = body.replace("\n", "\n  ")
            lines.append(f"- {who} ({when[:16].replace('T', ' ')}): {body}")
        lines.append("")
    return "\n".join(lines)


def footer(note: dict) -> str:
    updated = note.get("_tree", {}).get("updatedAt") or ""
    return f"\n_Migriert aus MeisterNote · {note.get('url', '')} · Stand {updated[:16].replace('T', ' ')}_\n"


def emoji_input(cfg: Config, label: str) -> dict:
    """Write ``build/notes/<label>/emoji_input.json``: for every note its title,
    path, type and a short text excerpt – the material a one-off icon suggestion
    (a separate, model-based step that never touches page content) works from.
    Suggestions go into ``data/page_emojis.json`` as {token: emoji} and are
    validated with ``build emoji-check`` before the import applies them."""
    ndir = cfg.account_dir(label) / "notes"
    manifest = read_json(ndir / "manifest.json")
    if not manifest:
        raise SystemExit(f"no notes extracted for {label}")
    tree = {int(r["id"]): r for r in (read_json(ndir / "tree.json") or [])}
    out: dict[str, dict] = {}
    for token in manifest.get("order") or []:
        note = read_json(ndir / f"{token}.json") or {}
        node = note.get("_tree") or next((n for n in tree.values() if n["token"] == token), None)
        if not node:
            continue
        path, cur, guard = [], tree.get(node.get("parentId")), 0
        while cur and guard < 20:
            path.insert(0, cur.get("name") or "")
            cur, guard = tree.get(cur.get("parentId")), guard + 1
        text = MARKER_RE.sub("", note.get("markdownContent") or "")
        text = LOP_LINE_RE.sub("", text)
        text = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", text)
        excerpt = " ".join(text.split())[:300]
        out[token] = {"title": node.get("name"), "path": " / ".join(p for p in path if p), "type": node.get("type"),
                      "excerpt": excerpt, "children": sum(1 for n in tree.values() if n.get("parentId") == node.get("id"))}
    dest = cfg.build_dir / "notes" / label / "emoji_input.json"
    write_json(dest, out)
    log.info("emoji input for %d notes written to %s", len(out), dest)
    return out


def emoji_check(cfg: Config, label: str) -> dict:
    from .collectives_import import is_single_emoji
    data = read_json(cfg.data_dir / "page_emojis.json", {}) or {}
    ndir = cfg.account_dir(label) / "notes"
    known = set((read_json(ndir / "manifest.json") or {}).get("order") or [])
    bad = {t: e for t, e in data.items() if not (isinstance(e, str) and is_single_emoji(e.strip()))}
    unknown = [t for t in data if t not in known]
    log.info("page_emojis.json: %d entries, %d invalid, %d unknown tokens, %d notes without emoji",
             len(data), len(bad), len(unknown), len(known - set(data)))
    for t, e in list(bad.items())[:10]:
        log.info("  invalid: %s → %r", t, e)
    return {"entries": len(data), "invalid": bad, "unknown": unknown, "missing": len(known - set(data))}


def build_all(cfg: Config, label: str, limit: int | None = None) -> dict:
    """Render every extracted note to ``build/notes/<label>/<token>.md`` for
    inspection and write a manifest with page titles and file references."""
    ndir = cfg.account_dir(label) / "notes"
    manifest = read_json(ndir / "manifest.json")
    if not manifest:
        raise SystemExit(f"no notes extracted for {label}; run `extract notes --label {label}` first")
    out_dir = cfg.build_dir / "notes" / label
    out_dir.mkdir(parents=True, exist_ok=True)
    order = manifest.get("order") or []
    if limit:
        order = order[:limit]
    built = {"label": label, "pages": {}, "marker_types": {}, "files_missing": 0}
    for token in order:
        note = read_json(ndir / f"{token}.json")
        if not note or not note.get("_tree"):
            continue
        files = read_json(ndir / f"{token}.files.json", {}) or {}
        text, refs, stats = transform(note.get("markdownContent") or "", note, files, cfg.collectives)
        for k, v in stats["markers"].items():
            built["marker_types"][k] = built["marker_types"].get(k, 0) + v
        built["files_missing"] += sum(1 for r in refs if not r.get("local"))
        (out_dir / f"{safe_filename(token)}.md").write_text(text, encoding="utf-8")
        tree = note["_tree"]
        built["pages"][token] = {"title": tree.get("name"), "parentId": tree.get("parentId"), "id": tree.get("id"),
                                 "type": tree.get("type"), "files": len(refs), "stats": stats}
    write_json(out_dir / "manifest.json", built)
    log.info("rendered %d notes; marker types seen: %s; files without local copy: %d",
             len(built["pages"]), built["marker_types"], built["files_missing"])
    return built
