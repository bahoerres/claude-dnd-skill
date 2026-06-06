"""Channel tagging (/chunk), _entry_visible, and /stream on-connect scoping."""
from __future__ import annotations

import importlib.util
import queue
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


# ─── /stream on-connect catch-up scoping ─────────────────────────────────────

class CatchupPayloadsTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app("app_catchup_under_test")
        self.mod._mp_active = lambda: True
        # Isolate: empty the optional surfaces so we test replay + device_request.
        self.mod._current_stats = {}
        self.mod._input_queue = []
        self.mod._staged = {}
        self.mod._queue_status = []
        self.mod._autorun_cycle = {}
        self.mod._autorun_threshold = None
        self.mod._text_log.clear()
        self.mod._text_log.append({"text": "public line", "channel": "public"})
        self.mod._text_log.append({"text": "gm secret", "channel": "dm"})
        self.mod._pending_devices = {"d1": {"id": "d1", "ip": "10.0.0.7"}}

    def _kinds(self, payloads):
        return [next(iter(p.keys())) for p in payloads]

    def _replay(self, payloads):
        for p in payloads:
            if "replay_batch" in p:
                return p["replay_batch"]
        return None

    def test_dm_gets_device_request_and_full_replay(self):
        out = self.mod._catchup_payloads(_ident("alice", "dm", []))
        self.assertIn("device_request", self._kinds(out))
        self.assertEqual([e["text"] for e in self._replay(out)], ["public line", "gm secret"])

    def test_player_no_device_request_filtered_replay(self):
        out = self.mod._catchup_payloads(_ident("bob", "player", ["Aldric"]))
        self.assertNotIn("device_request", self._kinds(out))
        self.assertEqual([e["text"] for e in self._replay(out)], ["public line"])

    def test_mp_inactive_everyone_gets_device_request_and_full_replay(self):
        self.mod._mp_active = lambda: False
        out = self.mod._catchup_payloads(_ident("bob", "player", ["Aldric"]))
        self.assertIn("device_request", self._kinds(out))   # backward-compat: all see it
        self.assertEqual([e["text"] for e in self._replay(out)], ["public line", "gm secret"])

    def test_scene_is_first_payload(self):
        out = self.mod._catchup_payloads(_ident("bob", "player", []))
        self.assertEqual(self._kinds(out)[0], "scene")


class DeviceRequestLiveScopeTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app("app_devreq_under_test")
        self.mod._mp_active = lambda: True
        self.mod._persist_pending_devices = lambda: None
        with self.mod._devices_lock:
            self.mod._pending_devices.clear()
            self.mod._approved_devices.clear()
            self.mod._denied_devices.clear()
        with self.mod._clients_lock:
            self.mod._clients.clear()

    def _add(self, user, role, chars):
        q = queue.Queue(maxsize=16)
        with self.mod._clients_lock:
            self.mod._clients.append({"q": q, "user": user, "role": role, "chars": set(chars)})
        return q

    def test_new_lan_device_request_reaches_only_dm(self):
        qdm = self._add("alice", "dm", [])
        qpl = self._add("bob", "player", ["Aldric"])
        status = self.mod._device_ok("new-dev", "10.0.0.50")  # LAN, unseen -> pending + broadcast
        self.assertEqual(status, "pending")
        self.assertEqual(qdm.get_nowait(), {"device_request": {"id": "new-dev", "ip": "10.0.0.50"}})
        self.assertTrue(qpl.empty())


if __name__ == "__main__":
    unittest.main()
