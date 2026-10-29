# meister2nc

Deterministic migration of MeisterTask projects to Nextcloud Deck and of
MeisterNote pages to Nextcloud Collectives. Pure Python 3.11+, only
`requests` as dependency, no language model anywhere in the data path.

```
Meister ──extract──▶ data/raw (verbatim JSON + files)
                      │
                      ├─ build deck ──▶ data/build/deck/<project>.json  (Deck JSON import format)
                      │                  └─ import deck ──▶ Deck import API (authors + timestamps kept)
                      └─ build notes ─▶ data/build/notes/… (Markdown preview)
                                         └─ import notes ─▶ Collectives API + WebDAV (pages updated in place)
```

Every stage reads and writes plain files below `data/`, so a run can be
inspected, repeated and diffed. Nothing needs shell or `occ` access on the
Nextcloud server; everything goes through the public HTTP APIs of Deck,
Collectives, Teams (circles) and WebDAV.

Tested with Nextcloud 34, Deck 1.18.5 (dependent cards need Deck ≥ 1.18),
Collectives 4.7.1 and Text 8.0.0, for a complete multi-account migration.

This project is not affiliated with or endorsed by Meister. It uses the OAuth
client ID that Meister publishes for local MCP clients and two undocumented
endpoints (the OAuth token on the REST API, the note-file download); Meister
may restrict either at any time. Each person authorises access to their own
account only.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json data/config.json      # edit nextcloud.url and nextcloud.user
export M2NC_NC_PASSWORD='<app password of the migration user>'
python3 -m meister2nc auth login --label me   # browser consent with your Meister account
python3 -m meister2nc probe --label me        # MCP, REST, downloads, Nextcloud
```

`auth login` runs the OAuth code flow with the public client id Meister
publishes for local MCP clients (callback `http://localhost:18920/callback`).
Tokens (with refresh tokens) are stored in `data/tokens.json` (mode 0600).

The OAuth token is accepted by Meister's MCP server, by the MeisterTask REST
API and by the note-file endpoint
`https://www.meisternote.com/app/api/media/note_file/<token>/<key>` (the
`/media/…` path quoted inside note Markdown is a shortened form that always
answers 401). One consent per user therefore covers tasks, notes and files;
`probe` checks all three.

### Nextcloud accounts, groups

The migration user needs no admin rights (board import, Collectives, WebDAV).
Only `usermap` lists all users with e-mail addresses, which is an admin API.
Either make the migration user admin, or set `nextcloud.admin_user` plus
`$M2NC_NC_ADMIN_PASSWORD` (or `nc_admin_password` in `data/secrets.json`, mode
0600); then `usermap` uses the admin account and everything else the migration
user. The migration password can also live in `secrets.json` as `nc_password`.

Meister "team projects" (project `share_mode` 2, visible to the whole
organisation; also explicit "Team" memberships on the Business plan) map to
one Nextcloud group: put its group **id** into `deck.team_group` (`probe` lists
the ids when run with admin rights). Business "Groups" map through
`deck.group_map` (`{"<meister group id>": "<nextcloud gid>"}`). Projects covered
by a group get the group with edit rights plus the project admins as individual
ACL entries with manage rights; invite-only projects (`share_mode` 0) get every
member individually. Team projects without a configured group, and unmapped
Group memberships, are listed in the build report.

## Collecting access for several accounts

A Meister token only sees the projects and notes its owner can see. Private
projects and personal notes therefore need a consent from each colleague.
Access tokens expire, refresh tokens do not (as long as they are used), so
consents can be collected over days:

```bash
python3 -m meister2nc auth link --label anna     # prints an authorize URL → send to Anna
# Anna signs in; her browser ends on http://localhost:18920/callback?code=… (an error page on her PC);
# she sends that URL back.
python3 -m meister2nc auth complete --label anna 'http://localhost:18920/callback?code=…&state=…'
python3 -m meister2nc auth list
```

The PKCE verifier never leaves this machine. If Meister ever rejects the OAuth
token on the REST API, a personal access token can be stored per label with
`auth set-pat --label anna` (read from `$M2NC_MT_PAT`); MCP still uses OAuth.

Projects seen by several accounts are extracted once (merged by id across the
per-account manifests); `--label all` runs a command for every stored account.

## Test run (10 cards / 10 notes, safe to repeat)

```bash
python3 -m meister2nc extract tasks --label me --project 123456 --limit 10   # ids: data/raw/accounts/me/manifest.json
python3 -m meister2nc usermap
python3 -m meister2nc build deck --project 123456 --limit 10 --test
python3 -m meister2nc import deck --project 123456 --dry-run
python3 -m meister2nc import deck --project 123456            # creates "<name> [TEST]"
python3 -m meister2nc import deck --project 123456 --replace  # second run: replaces, never duplicates

python3 -m meister2nc extract notes --label me --limit 10
python3 -m meister2nc build notes --label me --limit 10
python3 -m meister2nc import notes --label me --limit 10 --test   # collective "<root name> [TEST]"
python3 -m meister2nc import notes --label me --limit 10 --test   # second run: updates pages in place
python3 -m meister2nc report
```

Deck's import API only creates boards. A re-run with `--replace` deletes the
board and imports again (card ids change). `--update` migrates only the
difference since the last import, per card:

- unchanged cards are left alone (fingerprint stored at import time);
- changed fields (title, description, due, done/archived, labels, assignees,
  column) are updated in place through the card API;
- cards with new comments or attachments, and new tasks, are imported through
  a temporary board (keeps comment authors and timestamps), moved into the
  real board, given their labels and attachments, and the old card is deleted;
- tasks that vanished from MeisterTask are reported, not deleted.

Links and dependencies are always the last step and also rewrite card URLs
that pointed to replaced cards. `--update --dry-run` shows the plan.
Collectives pages are files, so a notes re-run is a true in-place update
(page ids kept in `data/state/notes_mapping.json`).

## Full run

```bash
python3 -m meister2nc extract tasks --label all
python3 -m meister2nc extract notes --label all
python3 -m meister2nc usermap            # check data/state/usermap.json, add usermap.overrides.json
python3 -m meister2nc build deck
python3 -m meister2nc import deck        # per project; link pass runs afterwards
python3 -m meister2nc build notes --label me
python3 -m meister2nc import notes --label me                                       # one collective per root note
python3 -m meister2nc import notes --label anna --collective-name "Private – Anna"   # private notes
python3 -m meister2nc report
```

Private projects appear only in their owner's dump and are imported like any
other (owner of all boards is the migration user because the import API forces
it; the real members get ACL entries, admins with manage rights).

`scripts/push-all.sh` pushes everything that is not yet in Nextcloud for all
stored accounts (usermap → build → per board import or `--update` → link pass →
notes per account) and can be stopped between boards; `scripts/full-refresh.sh`
re-fetches everything with `--refresh` first. Both log to `data/`.

A daily sync is the same sequence: `extract … --label all`, `build deck`,
`import deck --update` per board, `import deck-links`, `import notes --label
all --prune-covered`. Without `--refresh` only tasks and notes whose `updated_at`
changed are fetched again.

## What is preserved

| MeisterTask | Deck |
|---|---|
| project (active/archived), name | board (archived flag) |
| members + roles | ACL (edit / share / manage) |
| sections, order | stacks, order |
| labels | labels (+ assignment) |
| task name, notes (Markdown), order, due, assignee, created/updated, completed | card title, description, order, duedate, assigned user, createdAt/lastModified, done/archived |
| completed more than `deck.archive_completed_after_days` days ago (config, default off) | archived card (keeps boards small; `--update` archives cards as they age) |
| subtasks, "blocked by" / "blocks" relations | Deck dependent cards (Deck ≥ 1.18), created in the post-import pass; counterparts that were not migrated stay as text |
| "related to" / "duplicate of" relations | dependent cards in both directions |
| watchers | additional Deck assignees |
| checklists (non-empty), custom fields, link attachments, time tracking | Markdown sections in the description |
| comments with author + time (mentions resolved) | comments with the same author and time; >1000 chars split into parts |
| file attachments | card attachments (type `file`) |

| MeisterNote | Collectives |
|---|---|
| root note | one collective per root (root = landing page); Team members: per-root config `collectives.roots`, `--group/--member/--admin`, the root's author as admin, and every account whose extraction sees the root |
| empty root (no content, no sub notes, no discussions) | skipped, no collective (same-named roots of different accounts would otherwise share one collective) |
| page edited in Nextcloud after the migration (last editor ≠ migration user, file newer than the note) | left untouched, listed under `edited_in_nextcloud` in the summary |
| note tree, order | page tree, subpage order |
| Markdown content | Markdown (block markers removed) |
| images / files | page attachments (`.attachments.<id>/`) |
| links to notes / tasks | links to pages / Deck cards |
| discussions | "Kommentare" section |
| templates | pages prefixed `[Vorlage]` |

Authorship of pages cannot be set through the API; the migration user becomes
last editor. The note's `updatedAt` is written as file mtime.

Per-root collective settings go into `collectives.roots` in `data/config.json`:

```json
"roots": {
  "<root note token>": {"name": "Team Wiki", "groups": ["staff"], "admins": ["uid"], "members": ["uid"]}
}
```

The generated headings and labels (relation names, "Kommentare", "[Vorlage]",
"Erfasste Zeit", …) are German; they are plain string constants in
`meister2nc/deck_build.py`, `meister2nc/notes_build.py` and
`meister2nc/collectives_import.py`.

## Deleting imported boards

Never delete an imported board in the Deck UI while it still has file
attachments. Deck keeps the attachment shares of a soft-deleted board, and its
share provider then throws for every *member* of that board, which breaks
their whole Files/WebDAV listing (observed on Deck 1.18.5). Use
`python3 -m meister2nc import deck-delete --board-id <id>` instead, which
removes the attachments first. If it already happened: restore the board in
Deck ("deleted boards"), then run the same command.

## Locked and edited pages

A page that is open in the Text editor answers `423 Locked` to WebDAV; the import
skips it and the next run retries. If a lock survives for days although nobody
has the page open, it is a stale Text session. The migration user can release it:

```
DELETE /ocs/v2.php/apps/files_lock/lock/<pageId>      (page id = file id)
```

Pages that someone edited in Nextcloud after the migration (last editor is not
the migration user and the file is newer than the note) are never overwritten;
`import notes --overwrite-edited` forces it, the previous content stays
available in the page's file versions.

## Page icons (optional, one-off)

MeisterNote's note icons are not exposed by any API. Instead:

```bash
python3 -m meister2nc build emoji-input --label me     # title/path/excerpt per note → build/notes/me/emoji_input.json
# one-off suggestion step (a person or a model picks one emoji per token, never touches content) → data/page_emojis.json
python3 -m meister2nc build emoji-check --label me     # validates {token: emoji}
python3 -m meister2nc import notes --label me          # applies emojis to pages / collectives, idempotent
```

Edit `data/page_emojis.json` by hand at any time; the import only changes an
icon that differs from the file.

## Known limits

* Deck comment limit 1000 chars → long comments become `(n/m)` parts.
* Deck has no section colours, timeline, watchers → text only.
* The Meister MCP server cuts subtree listings at ~100 kB; the walker recurses
  per direct child and reports any remaining gap in the notes manifest.
* Note files are downloaded with the OAuth bearer token; `probe` tells whether
  Meister accepts that. If not, the pages are still migrated with a note where
  each file was.
* Users without a Nextcloud account: assignees are dropped, comment authors are
  kept as text (`**Name (MeisterTask):**`), listed in `report`.
* Meister marks closed accounts as `first.last+closed@domain`; the mapping
  falls back to the address without the `+tag`, keeps comment authorship on
  that Nextcloud account, and treats the person as inactive for assignments
  and ACL (same as a disabled Nextcloud account).
* Nextcloud turns `/` into `-` and drops double quotes in collective and Team
  names, and Team names need at least three characters; the importer matches
  existing collectives by root token first and by normalised name second.
* The MeisterTask REST API answers about 0.4 s per call; task details are
  fetched with `--workers` parallel requests (default 4).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

Copyright (C) 2026 Benjamin Lungwitz

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version (SPDX: `AGPL-3.0-or-later`, the same license as Nextcloud Deck
and Collectives). See [LICENSE](LICENSE) for the full text.
