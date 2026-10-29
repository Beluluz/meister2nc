"""Unit tests for the pure transformation code (no network)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meister2nc import deck_build, notes_build, util  # noqa: E402
from meister2nc.extract_notes import bfs_order, note_files  # noqa: E402


class CompactTable(unittest.TestCase):
    TEXT = ('[3]{__typename,contentType,editable,id,insertedAt,name,parentId,personId,sequence,status,token,type,updatedAt,url}:\n'
            '  Note,yjs,true,100001,"2023-12-07T08:41:10.362488Z",External contacts,100000,20000001,-16384,ACTIVE,Ab1Cd2Ef3Gh4,TEMPLATE,"2024-06-06T13:47:24.519705Z","https://www.meisternote.com/app/note/Ab1Cd2Ef3Gh4"\n'
            '  Note,yjs,true,100002,"2023-12-07T08:45:14.344316Z","Name, with comma",100000,20000001,-32768,ACTIVE,Ij5Kl6Mn_7Op,NOTE,"2024-06-06T13:47:29.027690Z","https://www.meisternote.com/app/note/Ij5Kl6Mn_7Op"\n'
            '  Note,yjs,true,100003,"2026-04-13T08:06:05.956636Z","13.04.2026 Meeting: „Which questions does the customer ask?\\"",100000,20000002,-32768,ACTIVE,Qr8St9Uv-0Wx,NOTE,"2026-04-13T08:06:38.779145Z","https://www.meisternote.com/app/note/Qr8St9Uv-0Wx"\n'
            '  Note,yjs,true,100004,"2025-07-01T09:53:10.950474Z",Truncated ro')

    def test_parse(self):
        claimed, rows = util.parse_compact_table(self.TEXT)
        self.assertEqual(claimed, 3)
        self.assertEqual(len(rows), 3)  # truncated fourth row dropped
        self.assertEqual(rows[2]["name"], '13.04.2026 Meeting: „Which questions does the customer ask?"')
        self.assertEqual(rows[2]["token"], "Qr8St9Uv-0Wx")
        self.assertEqual(rows[0]["id"], 100001)
        self.assertEqual(rows[0]["parentId"], 100000)
        self.assertEqual(rows[0]["editable"], True)
        self.assertEqual(rows[1]["name"], "Name, with comma")
        self.assertEqual(rows[1]["token"], "Ij5Kl6Mn_7Op")

    def test_payload_dispatch(self):
        result = {"content": [{"type": "text", "text": "Found 3 child note(s)"}, {"type": "text", "text": self.TEXT}]}
        payload = util.parse_tool_payload(result)
        self.assertEqual(payload["__claimed__"], 3)
        self.assertEqual(len(payload["rows"]), 3)
        result = {"content": [{"type": "text", "text": "Note content loaded."}, {"type": "text", "text": '{"id": 1, "x": [1,2]}'}]}
        self.assertEqual(util.parse_tool_payload(result)["id"], 1)


class Times(unittest.TestCase):
    def test_atom(self):
        self.assertEqual(util.iso_to_atom("2019-05-09T14:49:18.303930Z"), "2019-05-09T14:49:18+00:00")
        self.assertEqual(util.iso_to_epoch("1970-01-01T00:00:10Z"), 10)
        self.assertIsNone(util.iso_to_atom(None))

    def test_chunks(self):
        text = "Absatz eins.\n\n" + ("wort " * 300) + "\n\nEnde."
        chunks = util.chunk_text(text, 400)
        self.assertTrue(all(len(c) <= 400 for c in chunks))
        self.assertEqual(" ".join(" ".join(c.split()) for c in chunks), " ".join(text.split()))


class NotesTransform(unittest.TestCase):
    MD = ('<!-- meisternote:lop {"content":"","text":[],"type":"lop"} -->\n[List of pages]\n\n'
          '<!-- meisternote:toc {"content":"","text":[],"type":"toc"} -->\n[Table of contents]\n\n# Titel\n\nText -&gt; mehr\n\n'
          '<!-- meisternote:file {"content":"","fileContentType":"image/jpeg","fileKey":"KEY1","fileName":"Shot 1.jpg","text":[],"type":"file"} -->\n'
          '![Shot 1.jpg](/media/note_file/TOK/KEY1)\n\n'
          '<!-- meisternote:file {"content":"","fileContentType":"application/pdf","fileKey":"KEY2","fileName":"Doc.pdf","text":[],"type":"file"} -->\n'
          '[Attached file]\n\nSiehe https://www.meisternote.com/app/note/abcDEF123/slug und https://www.meistertask.com/app/task/tok123/name\n')

    def test_transform(self):
        files = {"KEY1": {"local": "/tmp/x.jpg", "status": 200}, "KEY2": {"local": None, "status": 401}}
        text, refs, stats = notes_build.transform(self.MD, {}, files, {"unescape_html_entities": True})
        self.assertNotIn("meisternote:", text)
        self.assertNotIn("[List of pages]", text)
        self.assertNotIn("[Table of contents]", text)
        self.assertTrue(text.startswith("# Titel"))                 # so the import adds no second heading
        self.assertIn("Text -> mehr", text)
        self.assertIn("{{m2nc-file:KEY1}}", text)
        self.assertIn("{{m2nc-file:KEY2}}", text)
        self.assertNotIn("[Attached file]", text)
        self.assertIn("{{m2nc-note:abcDEF123}}", text)
        self.assertIn("[[mt:tasktoken:tok123]]", text)
        self.assertEqual(stats["markers"], {"lop": 1, "toc": 1, "file": 2})
        by_key = {r["key"]: r for r in refs}
        self.assertTrue(by_key["KEY1"]["is_image"])
        self.assertFalse(by_key["KEY2"]["is_image"])
        self.assertEqual(by_key["KEY2"]["name"], "Doc.pdf")

    def test_discussion_rendering(self):
        disc = [{"commentCount": 2, "_status": "ACTIVE", "comments": [
            {"content": {"comment_text": [{"insert": {"mention": {"person_id": 1, "type": "person"}}}, {"insert": " bitte "},
                                          {"insert": {"emoticon": {"colons": ":smile:", "skin": 1}}}]},
             "personId": 2, "insertedAt": "2024-04-24T13:28:57.282756Z"},
            {"content": {"comment_text": [{"insert": "Zwei\nZeilen"}]}, "personId": 1, "insertedAt": "2024-04-24T13:40:56Z"}]}]
        out = notes_build.render_discussions(disc, lambda pid: {1: "Alice", 2: "Bob"}.get(pid, "?"))
        self.assertIn("## Kommentare (MeisterNote)", out)
        self.assertIn("- Bob (2024-04-24 13:28): **@Alice** bitte :smile:", out)
        self.assertIn("- Alice (2024-04-24 13:40): Zwei\n  Zeilen", out)
        self.assertIn("(offen, 2 Kommentare)", out)

    def test_note_files(self):
        files = note_files(self.MD, "TOK")
        self.assertEqual({f["key"] for f in files}, {"KEY1", "KEY2"})

    def test_bfs(self):
        tree = {1: {"id": 1, "token": "a", "parentId": None, "sequence": 0, "_root": True},
                2: {"id": 2, "token": "b", "parentId": 1, "sequence": 5},
                3: {"id": 3, "token": "c", "parentId": 1, "sequence": -5},
                4: {"id": 4, "token": "d", "parentId": 3, "sequence": 0}}
        self.assertEqual([n["token"] for n in bfs_order(tree)], ["a", "c", "b", "d"])


class DeckBuild(unittest.TestCase):
    def _ctx(self):
        class UM:
            extra_names = {}
            def uid(self, pid):
                return {"1": "alice", "3": "carol"}.get(str(pid))
            def enabled(self, pid):
                return str(pid) == "1"          # carol (3) exists but is disabled
            def uid_active(self, pid):
                return self.uid(pid) if self.enabled(pid) else None
            def name(self, pid):
                return {"1": "Alice", "2": "Bob", "3": "Carol"}.get(str(pid), f"Person {pid}")
        class Cfg:
            deck = {"card_footer": True, "comment_max_chars": 950, "test_suffix": " [TEST]", "board_color": "0082c9"}
            meister = {"max_attachment_mb": 50}
        return deck_build.BuildContext(Cfg(), UM(), "migration", False)

    def test_description_and_comments(self):
        ctx = self._ctx()
        detail = {
            "task": {"id": 10, "token": "tok10", "name": "Aufgabe", "notes": "Beschreibung", "status": 2,
                     "status_updated_at": "2024-01-02T03:04:05.123456Z", "created_at": "2024-01-01T00:00:00Z",
                     "updated_at": "2024-01-03T00:00:00Z", "due": "2024-02-01T10:00:00Z", "assigned_to_id": 1,
                     "section_name": "Done", "tracked_time": 3600},
            "labels": [{"label_id": 5}], "checklists": [{"id": 1, "name": "CL", "sequence": 0}],
            "checklist_items": [{"id": 1, "checklist_id": 1, "name": "A", "status": 5, "sequence": 0},
                                {"id": 2, "checklist_id": 1, "name": "B", "status": 1, "sequence": 1}],
            "comments": [{"id": 7, "person_id": 2, "text": "Hallo <person_id>1</person_id> " + "x" * 2000,
                          "created_at": "2024-01-02T03:04:05.123456Z"}],
            "attachments": [{"id": 3, "name": "link", "source": 6, "url": "https://example.org"}],
            "relationships": [{"owner_id": 10, "target_id": 11, "type": "blocks"}],
            "subtasks": [], "subscriptions": [{"person_id": 1}], "custom_fields": [], "work_intervals": [],
        }
        detail["checklists"].append({"id": 2, "name": "Leer", "sequence": 1})   # empty checklist → not rendered
        card = deck_build.build_card(ctx, 99, detail, 0, {}, {5}, 1700000000)
        self.assertEqual(card["labels"], [{"id": 5, "boardId": 99}])
        self.assertEqual(card["assignedUsers"][0]["participant"]["uid"], "alice")
        self.assertNotIn("Leer", card["description"])
        self.assertIn("### CL\n- [x] A", card["description"])          # single checklist → its name is the heading
        self.assertNotIn("### Checklisten", card["description"])
        self.assertNotIn("Weitere Angaben", card["description"])
        self.assertNotIn("Beobachter", card["description"])
        self.assertIn("### Verknüpfte Karten", card["description"])
        self.assertIn("Erfasste Zeit: 1h 00m", card["description"])
        self.assertEqual(card["done"], "2024-01-02T03:04:05+00:00")
        self.assertEqual(card["duedate"], "2024-02-01T10:00:00+00:00")
        self.assertIn("- [x] A", card["description"])
        self.assertIn("[[mt:depof:11]]", card["description"])          # "blocks" → other card depends on this
        self.assertIn("MeisterTask-Task `tok10`", card["description"])
        self.assertIn("[link](https://example.org)", card["description"])
        self.assertTrue(len(card["comments"]) >= 3)
        self.assertTrue(all(len(c["message"]) <= 1000 for c in card["comments"]))
        self.assertTrue(card["comments"][0]["message"].startswith("(1/"))
        self.assertIn('@"alice"', card["comments"][0]["message"])          # mapped → real mention
        self.assertNotIn("@Alice", card["comments"][0]["message"])
        self.assertEqual(card["comments"][0]["actorId"], "migration")  # Bob unmapped → attributed in text
        self.assertIn("Bob", card["comments"][0]["message"])
        self.assertEqual(card["comments"][0]["creationDateTime"], "2024-01-02T03:04:05+00:00")
        self.assertTrue(deck_build.FOOTER_RE.search(card["description"]))

    def test_related_and_multiline_items(self):
        ctx = self._ctx()
        detail = {"task": {"id": 40, "token": "tok40", "name": "X", "notes": "", "status": 1,
                           "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"},
                  "labels": [], "checklists": [{"id": 1, "name": "Checkliste", "sequence": 0}],
                  "checklist_items": [{"id": 1, "checklist_id": 1, "name": "Erstes:\n- Unterpunkt a\n- Unterpunkt b", "status": 1, "sequence": 0},
                                      {"id": 2, "checklist_id": 1, "name": "Zweites", "status": 5, "sequence": 1}],
                  "comments": [], "attachments": [], "relationships": [{"owner_id": 40, "target_id": 41, "type": "related_to"},
                                                                       {"owner_id": 42, "target_id": 40, "type": "duplicate_of"}],
                  "subtasks": [], "subscriptions": [], "custom_fields": [], "work_intervals": []}
        card = deck_build.build_card(ctx, 99, detail, 0, {}, set(), 1700000000)
        d = card["description"]
        self.assertIn("- [ ] Erstes:\n  - Unterpunkt a\n  - Unterpunkt b\n- [x] Zweites", d)
        self.assertIn("[[mt:depboth:41]]", d)
        self.assertIn("[[mt:depboth:42]]", d)
        from meister2nc.deck_import import dependencies_of
        pairs = dependencies_of({"stacks": [{"cards": [card]}]})
        self.assertEqual(sorted(pairs), sorted([["40", "41"], ["41", "40"], ["40", "42"], ["42", "40"]]))
        # subtasks: parent lists child and child lists parent
        detail["subtasks"] = [{"id": 43, "name": "Teil", "status": 1, "sequence": 0}]
        detail["relationships"] = []
        card = deck_build.build_card(ctx, 99, detail, 0, {}, set(), 1700000000)
        self.assertIn("- [ ] Teil [[mt:depboth:43]]", card["description"])
        self.assertEqual(sorted(dependencies_of({"stacks": [{"cards": [card]}]})), [["40", "43"], ["43", "40"]])

    def test_leading_rule_dropped(self):
        ctx = self._ctx()
        detail = {"task": {"id": 30, "token": "tok30", "name": "X", "notes": "---\n\n# Offboarding\n- Laptop", "status": 1,
                           "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"},
                  "labels": [], "checklists": [], "checklist_items": [], "comments": [], "attachments": [],
                  "relationships": [], "subtasks": [], "subscriptions": [], "custom_fields": [], "work_intervals": []}
        card = deck_build.build_card(ctx, 99, detail, 0, {}, set(), 1700000000)
        self.assertTrue(card["description"].startswith("# Offboarding"))

    def test_disabled_account(self):
        ctx = self._ctx()
        detail = {"task": {"id": 20, "token": "tok20", "name": "X", "notes": "", "status": 1, "assigned_to_id": 3,
                           "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z"},
                  "labels": [], "checklists": [], "checklist_items": [],
                  "comments": [{"id": 8, "person_id": 3, "text": "von Carol", "created_at": "2024-01-02T03:04:05Z"}],
                  "attachments": [], "relationships": [], "subtasks": [], "subscriptions": [], "custom_fields": [],
                  "work_intervals": []}
        card = deck_build.build_card(ctx, 99, detail, 0, {}, set(), 1700000000)
        self.assertFalse(card["description"].startswith("---"))          # would render as front matter
        self.assertEqual(card["assignedUsers"], [])                       # disabled → not assigned
        self.assertIn("ohne aktives Nextcloud-Konto: Carol", card["description"])
        self.assertEqual(card["comments"][0]["actorId"], "carol")          # but still the comment author
        self.assertEqual(card["comments"][0]["message"], "von Carol")
        self.assertEqual(ctx.report["unmapped_persons"], {})


class ArchiveOldCompleted(unittest.TestCase):
    def test_rule(self):
        ctx = DeckBuild._ctx(self)
        ctx.cfg.deck["archive_completed_after_days"] = 90
        ctx = deck_build.BuildContext(ctx.cfg, ctx.usermap, "migration", False)
        self.assertIsNotNone(ctx.archive_before)

        def card(status, status_updated_at):
            detail = {"task": {"id": 1, "token": "t", "name": "T", "status": status, "status_updated_at": status_updated_at,
                               "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-03T00:00:00Z"},
                      "labels": [], "checklists": [], "checklist_items": [], "comments": [], "attachments": [],
                      "relationships": [], "subtasks": [], "subscriptions": [], "custom_fields": [], "work_intervals": []}
            return deck_build.build_card(ctx, 99, detail, 0, {}, set(), 1700000000)
        self.assertTrue(card(2, "2024-01-02T00:00:00Z")["archived"])      # completed long ago
        self.assertFalse(card(2, "2099-01-02T00:00:00Z")["archived"])     # completed recently
        self.assertFalse(card(2, None)["archived"])                        # unknown date → keep
        self.assertFalse(card(1, "2024-01-02T00:00:00Z")["archived"])     # open
        self.assertTrue(card(18, "2099-01-02T00:00:00Z")["archived"])     # archived in MeisterTask
        ctx.cfg.deck.pop("archive_completed_after_days")
        ctx = deck_build.BuildContext(ctx.cfg, ctx.usermap, "migration", False)
        self.assertFalse(card(2, "2024-01-02T00:00:00Z")["archived"])     # option off


class Mentions(unittest.TestCase):
    def test_resolve(self):
        class UM:
            def uid(self, pid):
                return {"1": "0f9a1d2c-3b4e-4f60-8a71-9b2c3d4e5f60"}.get(str(pid))
            def name(self, pid):
                return {"1": "Alice A", "2": "Bob Builder"}.get(str(pid), f"Person {pid}")
        text = "Hi <person_id>1</person_id> und <person_id>2</person_id>, <all/> bitte lesen, <here/> auch &amp; so"
        out = deck_build.resolve_mentions(text, UM())
        self.assertEqual(out, 'Hi @"0f9a1d2c-3b4e-4f60-8a71-9b2c3d4e5f60" und **Bob Builder**, **(an alle)** bitte lesen, '
                              '**(an alle Beteiligten)** auch & so')


class Emoji(unittest.TestCase):
    def test_single_emoji(self):
        from meister2nc.collectives_import import is_single_emoji
        for ok in ("📚", "🛠️", "👩‍💻", "🇩🇪", "✅", "➡️"):
            self.assertTrue(is_single_emoji(ok), ok)
        for bad in ("", "abc", "📚 Bücher", "📚📚📚📚📚📚📚📚📚", "x"):
            self.assertFalse(is_single_emoji(bad), repr(bad))


class EmailMatching(unittest.TestCase):
    def test_variants(self):
        from meister2nc.usermap import email_variants, meister_closed
        self.assertEqual(email_variants("Jane.Doe+closed@Example.org"), ["jane.doe+closed@example.org", "jane.doe@example.org"])
        self.assertEqual(email_variants("jane@example.org"), ["jane@example.org"])
        self.assertEqual(email_variants(None), [])
        self.assertTrue(meister_closed("jane.doe+closed@example.org"))
        self.assertFalse(meister_closed("jane.doe+other@example.org"))


class ReferencedPersons(unittest.TestCase):
    def test_collect(self):
        from meister2nc.extract_tasks import referenced_person_ids
        detail = {"task": {"assigned_to_id": 1}, "comments": [{"person_id": 2}], "attachments": [{"person_id": 3}],
                  "subscriptions": [{"person_id": 4}], "work_intervals": [{"person_id": 5}], "subtasks": [{"assigned_to_id": 6}]}
        self.assertEqual(referenced_person_ids(detail), {1, 2, 3, 4, 5, 6})


class CollectiveNames(unittest.TestCase):
    def test_key_matches_nextcloud_normalisation(self):
        from meister2nc.collectives_import import collective_name_key as key
        self.assertEqual(key("Leasing / Rental"), key("Leasing - Rental"))
        self.assertEqual(key("Workflows/useful info"), key("Workflows-useful info"))
        self.assertEqual(key('Project "The way to success"'), key("Project The way to success"))
        self.assertNotEqual(key("HR"), key("HR (Notes)"))


class EditedInNextcloud(unittest.TestCase):
    def test_predicate(self):
        from meister2nc.collectives_import import edited_in_nextcloud as edited
        note_ts = "2026-10-19T12:25:09Z"                                   # epoch 1792000000-ish
        later, earlier = 1793261347, 1700000000
        self.assertTrue(edited({"lastUserId": "bob", "timestamp": later}, note_ts, "sync"))
        self.assertFalse(edited({"lastUserId": "sync", "timestamp": later}, note_ts, "sync"))    # our own write
        self.assertFalse(edited({"lastUserId": "bob", "timestamp": earlier}, note_ts, "sync"))  # note is newer
        self.assertFalse(edited({"lastUserId": None, "timestamp": later}, note_ts, "sync"))
        self.assertTrue(edited({"lastUserId": "bob", "timestamp": later}, None, "sync"))       # unknown note time


class EmptyRoot(unittest.TestCase):
    def test_predicate(self):
        from meister2nc.collectives_import import is_empty_root
        self.assertTrue(is_empty_root({"markdownContent": " \n"}, 1))
        self.assertTrue(is_empty_root({"markdownContent": "", "_discussions": []}, 1))
        self.assertFalse(is_empty_root({"markdownContent": "text"}, 1))
        self.assertFalse(is_empty_root({"markdownContent": ""}, 2))  # has sub notes
        self.assertFalse(is_empty_root({"markdownContent": "", "_discussions": [{"id": 1}]}, 1))
        self.assertFalse(is_empty_root({"_error": "HTTP 504"}, 1))  # unknown → keep
        self.assertFalse(is_empty_root(None, 1))


if __name__ == "__main__":
    unittest.main()
