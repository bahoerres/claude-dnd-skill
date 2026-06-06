"""Channel tagging (/chunk), _entry_visible, and /stream on-connect scoping."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_app(name="app_scope_under_test"):
    spec = importlib.util.spec_from_file_location(name, str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ident(user, role, chars):
    return {"user": user, "role": role, "chars": set(chars), "device": ""}


# ─── _entry_visible ──────────────────────────────────────────────────────────

class EntryVisibleTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app()
        self.mod._mp_active = lambda: True

    def test_public_entry_visible_to_all(self):
        e = {"text": "hi", "channel": "public"}
        self.assertTrue(self.mod._entry_visible(e, _ident("bob", "player", ["Aldric"])))

    def test_untagged_entry_treated_public(self):
        e = {"text": "legacy entry, no channel"}
        self.assertTrue(self.mod._entry_visible(e, _ident("bob", "player", [])))

    def test_dm_entry_hidden_from_player(self):
        e = {"text": "secret", "channel": "dm"}
        self.assertFalse(self.mod._entry_visible(e, _ident("bob", "player", ["Aldric"])))
        self.assertTrue(self.mod._entry_visible(e, _ident("alice", "dm", [])))

    def test_character_channel_visible_to_owner(self):
        e = {"text": "for Aldric", "channel": "Aldric"}
        self.assertTrue(self.mod._entry_visible(e, _ident("bob", "player", ["Aldric"])))
        self.assertFalse(self.mod._entry_visible(e, _ident("carol", "player", ["Mira"])))

    def test_user_channel_visible_to_named_user(self):
        e = {"text": "for bob", "channel": "bob"}
        self.assertTrue(self.mod._entry_visible(e, _ident("bob", "player", [])))

    def test_inactive_mp_everything_visible(self):
        self.mod._mp_active = lambda: False
        e = {"text": "secret", "channel": "dm"}
        self.assertTrue(self.mod._entry_visible(e, _ident("bob", "player", [])))


# ─── channel tagging in /chunk ───────────────────────────────────────────────

class ChunkChannelTagTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app("app_chunk_under_test")
        # Neutralise side effects: no token gate, no disk writes, no broadcast.
        self.mod._token_ok = lambda: True
        self.mod._persist_log = lambda: None
        self.mod._persist_tail = lambda: None
        self.mod._broadcast = lambda *a, **k: None
        self.mod._text_log.clear()
        self.client = self.mod.app.test_client()

    def _last(self):
        return self.mod._text_log[-1]

    def test_default_channel_is_public(self):
        self.client.post("/chunk", json={"text": "the tavern is warm"})
        self.assertEqual(self._last().get("channel"), "public")

    def test_audience_dm_tags_channel_dm(self):
        self.client.post("/chunk", json={"text": "hidden HP 12/30", "audience": "dm"})
        self.assertEqual(self._last().get("channel"), "dm")

    def test_audience_character_tags_channel(self):
        self.client.post("/chunk", json={"text": "you alone notice it", "audience": "Aldric"})
        self.assertEqual(self._last().get("channel"), "Aldric")

    def test_player_chunk_default_public(self):
        self.client.post("/chunk", json={"text": "I attack", "player": "Aldric"})
        self.assertEqual(self._last().get("channel"), "public")

    def test_xp_award_entry_tagged(self):
        # xp awards carry a text summary (matches send.py); the entry is tagged public.
        self.client.post("/chunk", json={"xp_award": {"summary": "+50 XP"}, "text": "+50 XP"})
        self.assertEqual(self._last().get("channel"), "public")


if __name__ == "__main__":
    unittest.main()
