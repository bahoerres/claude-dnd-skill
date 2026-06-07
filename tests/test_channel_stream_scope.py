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

    def test_dm_channel_not_leaked_to_player_named_or_owning_dm(self):
        """Reserved 'dm' token must never match a player by name/char (parity
        with _visible_to). A non-DM account literally named 'dm', or owning a
        character named 'dm', must not read dm-channel replay."""
        e = {"text": "secret", "channel": "dm"}
        self.assertFalse(self.mod._entry_visible(e, _ident("dm", "player", [])))
        self.assertFalse(self.mod._entry_visible(e, _ident("x", "player", ["dm"])))


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

    def test_non_string_audience_defaults_public(self):
        self.client.post("/chunk", json={"text": "narration", "audience": {"x": 1}})
        self.assertEqual(self._last().get("channel"), "public")

    def test_whitespace_audience_defaults_public(self):
        self.client.post("/chunk", json={"text": "narration", "audience": "   "})
        self.assertEqual(self._last().get("channel"), "public")

    def test_audience_all_normalized_to_public_channel(self):
        # "all" means public; the stored channel must be "public" so replay
        # treats it as visible-to-all (not as a private channel named "all").
        self.client.post("/chunk", json={"text": "the gate opens", "audience": "all"})
        self.assertEqual(self._last().get("channel"), "public")

    def test_reserved_audience_case_insensitive(self):
        self.client.post("/chunk", json={"text": "hidden roll", "audience": "DM"})
        self.assertEqual(self._last().get("channel"), "dm")
        self.client.post("/chunk", json={"text": "open scene", "audience": "Public"})
        self.assertEqual(self._last().get("channel"), "public")

    def test_typed_narration_branches_tagged(self):
        for flag, val in (("npc", "Osk"), ("dice", True), ("action", "kick door"), ("tutor", True)):
            self.mod._text_log.clear()
            self.client.post("/chunk", json={"text": "x", flag: val, "audience": "dm"})
            self.assertEqual(self._last().get("channel"), "dm", flag)

    def test_milestone_and_inspiration_tagged(self):
        self.mod._text_log.clear()
        self.client.post("/chunk", json={"milestone_award": "Boss", "text": "Boss", "audience": "dm"})
        self.assertEqual(self._last().get("channel"), "dm")
        self.mod._text_log.clear()
        self.client.post("/chunk", json={"inspiration_award": "Aldric", "text": "Aldric", "audience": "Aldric"})
        self.assertEqual(self._last().get("channel"), "Aldric")


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
        kinds = self._kinds(out)
        # `you` identity event now leads; scene is second
        self.assertEqual(kinds[0], "you")
        self.assertEqual(kinds[1], "scene")

    def test_catchup_order_matches_inline_contract(self):
        """Refactor parity: every surface populated -> kinds appear in the same
        order the old inline /stream block emitted them."""
        self.mod._current_stats = {"players": [{"name": "Aldric", "hp": {"current": 1, "max": 1}}]}
        self.mod._input_queue = [{"character": "Aldric", "text": "hi"}]
        self.mod._staged = {"Aldric": {"text": "x", "ready": True}}
        self.mod._queue_status = ["Aldric"]
        self.mod._autorun_cycle = {"elapsed": 1}
        self.mod._autorun_threshold = 2
        out = self.mod._catchup_payloads(_ident("alice", "dm", []))
        self.assertEqual(self._kinds(out), [
            "you", "scene", "replay_batch", "stats", "pending_input", "staged_inputs",
            "queue_status", "autorun_cycle", "autorun_threshold", "device_request",
        ])

    def test_deferred_surfaces_pass_through_full(self):
        """staged/pending/queue are intentionally NOT scoped in PR2 — pin it so
        the deferral can't be silently broken (or silently 'fixed')."""
        self.mod._input_queue = [{"character": "Mira", "text": "secret move"}]
        self.mod._staged = {"Mira": {"text": "x", "ready": False}}
        self.mod._queue_status = ["Mira"]
        out = self.mod._catchup_payloads(_ident("bob", "player", ["Aldric"]))  # owns Aldric, not Mira
        by = {next(iter(p.keys())): p for p in out}
        self.assertEqual(by["pending_input"]["pending_input"], [{"character": "Mira", "text": "secret move"}])
        self.assertEqual(by["staged_inputs"]["staged_inputs"], {"Mira": {"text": "x", "ready": False}})
        self.assertEqual(by["queue_status"]["queue_status"], ["Mira"])

    def test_stats_catchup_scoped_for_player(self):
        """stats catch-up IS scoped (PR2 fix): a player does not get another
        player's full sheet on connect."""
        self.mod._current_stats = {"players": [
            {"name": "Aldric", "class": "Fighter", "level": 2, "hp": {"current": 1, "max": 1}, "sheet": {"inventory": ["A"]}},
            {"name": "Mira", "class": "Rogue", "level": 2, "hp": {"current": 2, "max": 2}, "sheet": {"inventory": ["Secret"]}},
        ]}
        out = self.mod._catchup_payloads(_ident("bob", "player", ["Aldric"]))
        stats = next(p["stats"] for p in out if "stats" in p)
        by = {p["name"]: p for p in stats["players"]}
        self.assertIn("sheet", by["Aldric"])       # own char full
        self.assertNotIn("sheet", by["Mira"])       # other: summary only


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

    def test_device_request_reaches_all_when_no_dm(self):
        self.mod._mp_active = lambda: False
        qa = self._add("alice", "dm", [])
        qb = self._add("bob", "player", ["Aldric"])
        self.mod._device_ok("new-dev2", "10.0.0.51")
        self.assertEqual(qa.get_nowait(), {"device_request": {"id": "new-dev2", "ip": "10.0.0.51"}})
        self.assertEqual(qb.get_nowait(), {"device_request": {"id": "new-dev2", "ip": "10.0.0.51"}})


class ScopeStatsTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app("app_scopestats_under_test")
        self.mod._mp_active = lambda: True
        self.stats = {"players": [
            {"name": "Aldric", "race": "Human", "class": "Fighter", "level": 2,
             "hp": {"current": 10, "max": 18}, "conditions": ["Poisoned"],
             "spell_slots": {"1": 2}, "sheet": {"inventory": ["Longsword", "Secret Letter"]}},
            {"name": "Mira", "race": "Elf", "class": "Rogue", "level": 2,
             "hp": {"current": 14, "max": 14}, "conditions": [],
             "sheet": {"inventory": ["Dagger"]}},
        ], "turn_order": {"order": ["Aldric", "Mira"], "current": "Aldric"}}

    def test_dm_gets_full(self):
        self.assertEqual(self.mod._scope_stats(self.stats, _ident("alice", "dm", [])), self.stats)

    def test_player_own_full_others_public_summary(self):
        out = self.mod._scope_stats(self.stats, _ident("bob", "player", ["Aldric"]))
        by = {p["name"]: p for p in out["players"]}
        self.assertIn("sheet", by["Aldric"])              # own character: full
        self.assertEqual(by["Aldric"]["conditions"], ["Poisoned"])
        self.assertEqual(set(by["Mira"].keys()), {"name", "race", "class", "level", "hp"})
        self.assertEqual(by["Mira"]["hp"], {"current": 14, "max": 14})
        self.assertEqual(out["turn_order"], self.stats["turn_order"])  # non-player keys pass through

    def test_inactive_returns_full(self):
        self.mod._mp_active = lambda: False
        self.assertEqual(
            self.mod._scope_stats(self.stats, _ident("bob", "player", ["Aldric"])), self.stats)


class LiveStatsScopeTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app("app_livestats_under_test")
        self.mod._mp_active = lambda: True
        with self.mod._clients_lock:
            self.mod._clients.clear()
        self.stats = {"players": [
            {"name": "Aldric", "class": "Fighter", "level": 2, "hp": {"current": 10, "max": 18}, "sheet": {"inventory": ["Secret"]}},
            {"name": "Mira", "class": "Rogue", "level": 2, "hp": {"current": 14, "max": 14}, "sheet": {"inventory": ["Dagger"]}},
        ]}

    def _add(self, user, role, chars):
        q = queue.Queue(maxsize=16)
        with self.mod._clients_lock:
            self.mod._clients.append({"q": q, "user": user, "role": role, "chars": set(chars)})
        return q

    def test_live_stats_scoped_per_connection(self):
        qdm = self._add("alice", "dm", [])
        qbob = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast_stats(self.stats)
        dm_by = {p["name"]: p for p in qdm.get_nowait()["stats"]["players"]}
        bob_by = {p["name"]: p for p in qbob.get_nowait()["stats"]["players"]}
        self.assertIn("sheet", dm_by["Mira"])             # DM sees all
        self.assertNotIn("sheet", bob_by["Mira"])         # other player's sheet hidden
        self.assertIn("sheet", bob_by["Aldric"])          # own character full

    def test_inactive_live_stats_full_to_all(self):
        self.mod._mp_active = lambda: False
        qbob = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast_stats(self.stats)
        self.assertEqual(qbob.get_nowait()["stats"], self.stats)


if __name__ == "__main__":
    unittest.main()
