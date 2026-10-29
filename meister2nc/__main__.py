"""Command line interface: ``python3 -m meister2nc <command> …``"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import Config
from .util import log, setup_logging, write_json


def _ids(values: list[str] | None) -> list[int] | None:
    if not values:
        return None
    out = []
    for v in values:
        out.extend(int(x) for x in v.split(",") if x.strip())
    return out


def _labels(cfg: Config, label: str) -> list[str]:
    """``--label all`` runs a command for every authorized Meister account."""
    if label != "all":
        return [label]
    from .accounts import token_store
    labels = token_store(cfg).labels()
    if not labels:
        raise SystemExit("no authorized accounts; run `auth login` / `auth link` first")
    return labels


def _nc(cfg: Config, admin: bool = False):
    """Nextcloud client as the migration user, or (``admin=True``) as the
    optional admin account when one is configured."""
    from .nc_client import NCClient
    user, password = cfg.nc_user(), None
    if admin and cfg.nc_admin():
        user, password = cfg.nc_admin()
        log.info("using admin account %s for this step", user)
    else:
        password = cfg.nc_password()
    return NCClient(cfg.nextcloud["url"], user, password,
                    cfg.nextcloud.get("deck_api_version", "1.1"), delay=float(cfg.nextcloud.get("request_delay", 0.05)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="meister2nc", description="Deterministic MeisterTask/MeisterNote → Nextcloud migration")
    p.add_argument("--data", help="data directory (default ./data or $M2NC_DATA)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    # auth
    a = sub.add_parser("auth", help="Meister OAuth accounts").add_subparsers(dest="auth_cmd", required=True)
    x = a.add_parser("login", help="local browser login (callback on localhost)")
    x.add_argument("--label", default="me")
    x.add_argument("--scope")
    x.add_argument("--no-browser", action="store_true")
    x.add_argument("--timeout", type=int, default=900, help="seconds to wait for the browser callback")
    x = a.add_parser("link", help="print an authorize URL for a colleague")
    x.add_argument("--label", required=True)
    x.add_argument("--scope")
    x = a.add_parser("complete", help="finish a `link` authorization with the callback URL the colleague sent back")
    x.add_argument("--label", required=True)
    x.add_argument("callback_url")
    x = a.add_parser("refresh")
    x.add_argument("--label", required=True)
    x = a.add_parser("set-pat", help="store a MeisterTask personal access token for REST (fallback)")
    x.add_argument("--label", required=True)
    x.add_argument("--from-env", default="M2NC_MT_PAT")
    a.add_parser("list")
    x = a.add_parser("remove")
    x.add_argument("--label", required=True)

    # probe
    x = sub.add_parser("probe", help="check Meister and Nextcloud access")
    x.add_argument("--label", default="me")
    x.add_argument("--skip-meister", action="store_true")
    x.add_argument("--skip-nextcloud", action="store_true")
    x.add_argument("--quick", action="store_true", help="skip download tests")

    # extract
    e = sub.add_parser("extract", help="dump Meister data").add_subparsers(dest="extract_cmd", required=True)
    x = e.add_parser("tasks")
    x.add_argument("--label", default="me")
    x.add_argument("--project", action="append", help="project id(s), comma separated or repeated")
    x.add_argument("--limit", type=int, help="test mode: first N tasks per project")
    x.add_argument("--refresh", action="store_true", help="re-fetch tasks already dumped")
    x.add_argument("--no-files", action="store_true")
    x.add_argument("--include-trashed", action="store_true")
    x.add_argument("--workers", type=int, default=4, help="parallel task-detail requests (default 4)")
    x.add_argument("--all-projects", action="store_true", help="also re-extract projects another account already holds")
    x = e.add_parser("notes")
    x.add_argument("--label", default="me")
    x.add_argument("--root", action="append", help="root note token(s)")
    x.add_argument("--limit", type=int, help="test mode: first N notes (BFS order)")
    x.add_argument("--refresh", action="store_true")
    x.add_argument("--no-files", action="store_true")
    x.add_argument("--no-discussions", action="store_true")
    x.add_argument("--archived", action="store_true", help="also walk ARCHIVE status")
    x.add_argument("--all-roots", action="store_true", help="also re-extract roots another account already holds")

    # usermap
    x = sub.add_parser("usermap", help="map MeisterTask persons to Nextcloud users by e-mail")
    x.add_argument("--no-nextcloud", action="store_true", help="only use usermap.overrides.json")

    # build
    b = sub.add_parser("build", help="transform raw dumps").add_subparsers(dest="build_cmd", required=True)
    x = b.add_parser("deck")
    x.add_argument("--project", action="append")
    x.add_argument("--limit", type=int, help="test mode: first N cards per board")
    x.add_argument("--test", action="store_true", help="append the test suffix to board titles")
    x = b.add_parser("notes")
    x.add_argument("--label", default="me")
    x.add_argument("--limit", type=int)
    x = b.add_parser("emoji-input", help="write title/path/excerpt per note for a one-off icon suggestion")
    x.add_argument("--label", default="me")
    x = b.add_parser("emoji-check", help="validate data/page_emojis.json")
    x.add_argument("--label", default="me")

    # import
    i = sub.add_parser("import", help="write to Nextcloud").add_subparsers(dest="import_cmd", required=True)
    x = i.add_parser("deck")
    x.add_argument("--project", action="append")
    x.add_argument("--replace", action="store_true", help="delete the board imported earlier and import again")
    x.add_argument("--update", action="store_true", help="only migrate what changed since the last import")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--no-link-pass", action="store_true")
    x = i.add_parser("deck-links", help="only run the relationship link pass")
    x.add_argument("--project", action="append")
    x.add_argument("--force", action="store_true", help="re-check every card that has references, not only new ones")
    x = i.add_parser("deck-delete", help="delete a Deck board safely (attachments first)")
    x.add_argument("--board-id", type=int, required=True)
    x = i.add_parser("notes")
    x.add_argument("--label", default="me")
    x.add_argument("--collective-id", type=int)
    x.add_argument("--collective-name")
    x.add_argument("--parent-page-id", type=int, help="import below this page instead of the landing page")
    x.add_argument("--root", action="append", help="only this note subtree")
    x.add_argument("--limit", type=int, help="test mode: first N notes")
    x.add_argument("--test", action="store_true", help="use a separate test collective")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--group", action="append", help="Nextcloud group id to add as Team member (repeatable)")
    x.add_argument("--member", action="append", help="Nextcloud user id to add as Team member (repeatable)")
    x.add_argument("--admin", action="append", help="Nextcloud user id to add as Team admin (repeatable)")
    x.add_argument("--prune-covered", action="store_true",
                   help="remove individual Team members that a configured group already covers")
    x.add_argument("--overwrite-edited", action="store_true",
                   help="also rewrite pages that were edited in Nextcloud after the migration (their current "
                        "content stays available under the page's file versions)")

    sub.add_parser("report", help="print a status summary")

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    cfg = Config(args.data)

    if args.cmd == "auth":
        return cmd_auth(cfg, args)
    if args.cmd == "probe":
        from .probe import probe_meister, probe_nextcloud
        result = {}
        if not args.skip_meister:
            result["meister"] = probe_meister(cfg, args.label, deep=not args.quick)
        if not args.skip_nextcloud:
            result["nextcloud"] = probe_nextcloud(cfg)
            if cfg.nc_admin():
                result["nextcloud_admin"] = probe_nextcloud(cfg, admin=True)
        write_json(cfg.state_dir / "probe.json", result)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    if args.cmd == "extract":
        for label in _labels(cfg, args.label):
            if args.extract_cmd == "tasks":
                from .extract_tasks import run
                run(cfg, label, _ids(args.project), args.limit, args.refresh,
                    True if args.include_trashed else None, with_files=not args.no_files, workers=args.workers,
                    all_projects=args.all_projects)
            else:
                from .extract_notes import run
                statuses = ("ACTIVE", "ARCHIVE") if args.archived else ("ACTIVE",)
                run(cfg, label, args.root, args.limit, args.refresh, with_files=not args.no_files,
                    statuses=statuses, with_discussions=not args.no_discussions, all_roots=args.all_roots)
        return 0
    if args.cmd == "usermap":
        from .usermap import build
        build(cfg, None if args.no_nextcloud else _nc(cfg, admin=True))
        return 0
    if args.cmd == "build":
        if args.build_cmd == "deck":
            from .deck_build import build_all
            build_all(cfg, _ids(args.project), args.limit, args.test)
        elif args.build_cmd == "emoji-input":
            from .notes_build import emoji_input
            emoji_input(cfg, args.label)
        elif args.build_cmd == "emoji-check":
            from .notes_build import emoji_check
            emoji_check(cfg, args.label)
        else:
            from .notes_build import build_all
            for label in _labels(cfg, args.label):
                build_all(cfg, label, args.limit)
        return 0
    if args.cmd == "import":
        nc = _nc(cfg)
        if args.import_cmd == "deck":
            from .deck_import import run
            run(cfg, nc, _ids(args.project), args.replace, args.dry_run, link_pass=not args.no_link_pass,
                update=args.update)
        elif args.import_cmd == "deck-links":
            from .deck_import import load_mapping, run_link_pass
            run_link_pass(cfg, nc, load_mapping(cfg), _ids(args.project), force=args.force)
        elif args.import_cmd == "deck-delete":
            from .deck_import import delete_board
            delete_board(nc, args.board_id)
        else:
            from .collectives_import import run
            for label in _labels(cfg, args.label):
                run(cfg, nc, label, args.collective_id, args.collective_name, args.limit, args.test,
                    args.dry_run, args.parent_page_id, args.root, groups=args.group, users=args.member, admins=args.admin,
                    prune_covered=args.prune_covered, overwrite_edited=args.overwrite_edited)
        return 0
    if args.cmd == "report":
        from .report import render
        print(render(cfg))
        return 0
    return 1


def cmd_auth(cfg: Config, args) -> int:
    import os
    from .accounts import oauth, token_store
    store = token_store(cfg)
    o = oauth(cfg, store)
    if args.auth_cmd == "login":
        token = o.login_local(args.label, args.scope, open_browser=not args.no_browser, timeout=args.timeout)
        print(f"stored token for {args.label!r} (scope={token.get('scope')}, refresh={'yes' if token.get('refresh_token') else 'no'})")
    elif args.auth_cmd == "link":
        url = o.build_authorize_url(args.label, args.scope)
        print("Send this URL to the colleague. After signing in, the browser lands on a page at "
              "http://localhost:18920/callback?code=… that cannot load on their machine – they copy that full URL "
              "from the address bar and send it back. Then run:\n"
              f"  python3 -m meister2nc auth complete --label {args.label} '<pasted url>'\n\n{url}")
    elif args.auth_cmd == "complete":
        token = o.complete(args.label, args.callback_url)
        print(f"stored token for {args.label!r} (refresh={'yes' if token.get('refresh_token') else 'no'})")
    elif args.auth_cmd == "refresh":
        o.refresh(args.label)
        print("refreshed")
    elif args.auth_cmd == "set-pat":
        value = os.environ.get(args.from_env)
        if not value:
            raise SystemExit(f"environment variable {args.from_env} is empty")
        acct = store.get(args.label) or {}
        acct["rest_pat"] = value
        store.put(args.label, acct)
        print(f"stored REST personal access token for {args.label!r}")
    elif args.auth_cmd == "list":
        for label in store.labels():
            t = store.get(label)
            print(f"{label}: refresh={'yes' if t.get('refresh_token') else 'no'} pat={'yes' if t.get('rest_pat') else 'no'} "
                  f"expires_at={t.get('expires_at')}")
        for label, pend in store.data.get("pending", {}).items():
            print(f"{label}: pending authorization since {pend.get('created_at')}")
    elif args.auth_cmd == "remove":
        store.remove(args.label)
        print("removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
