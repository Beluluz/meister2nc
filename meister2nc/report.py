"""Human-readable summary of what was extracted, built and imported."""

from __future__ import annotations

from .config import Config
from .util import epoch_to_display, read_json


def render(cfg: Config) -> str:
    lines = ["# meister2nc – Status", ""]
    accounts_root = cfg.raw_dir / "accounts"
    if accounts_root.exists():
        lines.append("## Extraktion")
        for acct in sorted(accounts_root.iterdir()):
            man = read_json(acct / "manifest.json") or {}
            total = sum(p.get("tasks", 0) for p in man.get("projects", {}).values())
            lines.append(f"- Konto `{acct.name}`: {len(man.get('projects', {}))} Projekte, {total} Tasks"
                         + (f" (Limit {man['limit']})" if man.get("limit") else "")
                         + (f", Stand {epoch_to_display(man.get('extracted_at'))}" if man.get("extracted_at") else ""))
            nman = read_json(acct / "notes" / "manifest.json") or {}
            if nman:
                counts = nman.get("counts", {})
                lines.append(f"  - Notes: {len(nman.get('order', []))} in Reihenfolge, {counts.get('notes', 0)} neu geladen, "
                             f"{counts.get('files', 0)} Dateien, Datei-Download ok: {nman.get('files_download_ok')}")
                for gap in nman.get("gaps", []):
                    lines.append(f"  - LÜCKE im Baum: {gap}")
        lines.append("")
    um = read_json(cfg.state_dir / "usermap.json")
    if um:
        lines.append("## Benutzerzuordnung")
        lines.append(f"- {len(um.get('persons', {}))} Personen, {len(um.get('unmapped', []))} ohne Nextcloud-Konto")
        for u in um.get("unmapped", []):
            lines.append(f"  - {u['name']} <{u.get('email')}> (MT-ID {u['id']})")
        lines.append("")
    rep = read_json(cfg.build_dir / "deck" / "report.json")
    if rep:
        lines.append("## Deck-Build")
        for pid, p in rep.get("projects", {}).items():
            lines.append(f"- {pid} {p['title']}: {p['cards']} Karten, {p['stacks']} Spalten, {p['labels']} Labels, {p['acl']} Freigaben")
        lines.append(f"- geteilte Kommentare (>1000 Zeichen): {rep.get('comments_split', 0)}")
        lines.append(f"- eingebettete Anhänge: {rep.get('attachments_embedded', 0)}, übersprungen: {len(rep.get('attachments_skipped', []))}, "
                     f"Link-Anhänge: {rep.get('link_attachments', 0)}")
        for pid_, info in rep.get("unmapped_persons", {}).items():
            lines.append(f"  - nicht zugeordnet: {info['name']} (MT-ID {pid_}, {info['count']}× bei {info['where']})")
        lines.append("")
    dm = read_json(cfg.state_dir / "deck_mapping.json")
    if dm:
        lines.append("## Deck-Import")
        for pid, e in dm.get("projects", {}).items():
            lines.append(f"- Projekt {pid} → Board {e.get('board_id')} ({e.get('title')}), {len(e.get('cards', {}))} Karten zugeordnet, "
                         f"{epoch_to_display(e.get('imported_at'))}")
        lines.append("")
    nm = read_json(cfg.state_dir / "notes_mapping.json")
    if nm:
        lines.append("## Collectives-Import")
        for cid, c in nm.get("collectives", {}).items():
            st = c.get("stats", {})
            lines.append(f"- Collective {cid} ({c.get('name')}): {len(c.get('pages', {}))} Seiten, "
                         f"{st.get('attachments', 0)} Anhänge, {len(st.get('errors', []))} Fehler, {epoch_to_display(c.get('last_run'))}")
        lines.append("")
    return "\n".join(lines)
