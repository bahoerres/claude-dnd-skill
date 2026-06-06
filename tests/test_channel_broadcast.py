"""Tagged client registry + audience-aware _broadcast + _visible_to."""
from __future__ import annotations

import importlib.util
import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"


def _load_app():
    spec = importlib.util.spec_from_file_location("app_bcast_under_test", str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app_bcast_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeReq:
    def __init__(self, headers=None, args=None, remote_addr="10.0.0.5"):
        self.headers = headers or {}
        self.args = args or {}
        self.remote_addr = remote_addr


class RegistryShapeTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app()
        # Force a clean, controlled registry.
        with self.mod._clients_lock:
            self.mod._clients.clear()

    def _add(self, user, role, chars):
        q = queue.Queue(maxsize=16)
        rec = {"q": q, "user": user, "role": role, "chars": set(chars)}
        with self.mod._clients_lock:
            self.mod._clients.append(rec)
        return q

    def test_clients_hold_records_not_bare_queues(self):
        q = self._add("alice", "dm", [])
        rec = self.mod._clients[0]
        self.assertIs(rec["q"], q)
        self.assertEqual(rec["role"], "dm")

    def test_broadcast_all_reaches_everyone(self):
        # mp active or inactive, "all" reaches everyone
        self.mod._mp_active = lambda: True
        qa = self._add("alice", "dm", [])
        qb = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast({"text": "hi"})
        self.assertEqual(qa.get_nowait(), {"text": "hi"})
        self.assertEqual(qb.get_nowait(), {"text": "hi"})

    def test_full_queue_is_purged(self):
        """A client whose queue is full is dropped from the registry; healthy
        clients still receive the payload and no exception escapes."""
        self.mod._mp_active = lambda: False
        # Healthy queue.
        q_ok = queue.Queue(maxsize=16)
        # Full queue (maxsize 1, pre-filled) -> put_nowait raises queue.Full.
        q_full = queue.Queue(maxsize=1)
        q_full.put_nowait({"stale": True})
        with self.mod._clients_lock:
            self.mod._clients.append({"q": q_ok, "user": "ok", "role": "player", "chars": set()})
            self.mod._clients.append({"q": q_full, "user": "x", "role": "player", "chars": set()})
        self.mod._broadcast({"text": "hi"})
        self.assertEqual(q_ok.get_nowait(), {"text": "hi"})
        users = {c["user"] for c in self.mod._clients}
        self.assertEqual(users, {"ok"})           # full client purged
        self.assertEqual(len(self.mod._clients), 1)


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app()
        with self.mod._clients_lock:
            self.mod._clients.clear()

    def _add(self, user, role, chars):
        q = queue.Queue(maxsize=16)
        rec = {"q": q, "user": user, "role": role, "chars": set(chars)}
        with self.mod._clients_lock:
            self.mod._clients.append(rec)
        return q

    def test_inactive_mp_everything_public(self):
        self.mod._mp_active = lambda: False
        qb = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast({"secret": 1}, audience="dm")  # would be DM-only if active
        self.assertEqual(qb.get_nowait(), {"secret": 1})

    def test_dm_audience_reaches_only_dm(self):
        self.mod._mp_active = lambda: True
        qdm = self._add("alice", "dm", [])
        qpl = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast({"gm": "screen"}, audience="dm")
        self.assertEqual(qdm.get_nowait(), {"gm": "screen"})
        self.assertTrue(qpl.empty())

    def test_dm_sees_everything(self):
        self.mod._mp_active = lambda: True
        qdm = self._add("alice", "dm", [])
        self.mod._broadcast({"whisper": "to-bob"}, audience={"bob"})
        self.assertEqual(qdm.get_nowait(), {"whisper": "to-bob"})

    def test_character_audience_matches_owner(self):
        self.mod._mp_active = lambda: True
        qbob = self._add("bob", "player", ["Aldric"])
        qcar = self._add("carol", "player", ["Mira"])
        self.mod._broadcast({"note": "for Aldric"}, audience={"Aldric"})
        self.assertEqual(qbob.get_nowait(), {"note": "for Aldric"})
        self.assertTrue(qcar.empty())

    def test_user_audience_matches_username(self):
        self.mod._mp_active = lambda: True
        qbob = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast({"note": "for bob"}, audience={"bob"})
        self.assertEqual(qbob.get_nowait(), {"note": "for bob"})

    def test_bare_string_audience_treated_as_single_name(self):
        """A bare-string audience (a contract violation) must behave as a
        single-name audience, not explode into characters: 'Mira' reaches Mira's
        owner, and a user named 'a' (a letter of "Mira") does NOT receive it."""
        self.mod._mp_active = lambda: True
        q_owner = self._add("carol", "player", ["Mira"])
        q_letter = self._add("a", "player", [])         # username collides with a letter
        self.mod._broadcast({"note": "for Mira"}, audience="Mira")  # bare string
        self.assertEqual(q_owner.get_nowait(), {"note": "for Mira"})
        self.assertTrue(q_letter.empty())

    def test_none_audience_reaches_no_player(self):
        self.mod._mp_active = lambda: True
        qpl = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast({"x": 1}, audience=None)
        self.assertTrue(qpl.empty())


class StreamSeamTest(unittest.TestCase):
    """End-to-end: real _resolve_identity + a real state.md drive _broadcast
    filtering, with NO monkeypatching of _mp_active. Proves identity + tagging +
    filter compose the way /stream wires them."""

    def setUp(self):
        self.mod = _load_app()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "campaigns"
        (root / "camp").mkdir(parents=True)
        self.camp = root / "camp"
        camp_file = Path(self.tmp.name) / ".campaign"
        camp_file.write_text("camp")
        self.mod.CAMP_FILE = str(camp_file)
        self.mod._find_campaign = lambda name: root / name
        (self.camp / "ownership.json").write_text(json.dumps({
            "devices": {"dev-alice": "alice", "dev-bob": "bob", "dev-carol": "carol"},
            "characters": {"alice": ["Mira"], "bob": ["Aldric"], "carol": ["Lyra"]},
        }))
        (self.camp / "state.md").write_text("## Session Flags\n- dm: alice\n\n## DM Notes\n")
        with self.mod._clients_lock:
            self.mod._clients.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _connect(self, req):
        # Mirror stream(): resolve identity, build the SAME record shape, register.
        ident = self.mod._resolve_identity(req)
        q = queue.Queue(maxsize=16)
        rec = {"q": q, "user": ident["user"], "role": ident["role"], "chars": ident["chars"]}
        with self.mod._clients_lock:
            self.mod._clients.append(rec)
        return q

    def test_record_shape_built_by_production_resolve(self):
        self._connect(FakeReq(args={"device": "dev-bob"}))
        rec = self.mod._clients[-1]
        self.assertEqual(set(rec.keys()), {"q", "user", "role", "chars"})
        self.assertEqual(rec["user"], "bob")
        self.assertEqual(rec["role"], "player")
        self.assertEqual(rec["chars"], {"Aldric"})

    def test_character_whisper_filters_end_to_end(self):
        qdm = self._connect(FakeReq(args={"device": "dev-alice"}))    # DM
        qbob = self._connect(FakeReq(args={"device": "dev-bob"}))     # owns Aldric
        qcar = self._connect(FakeReq(args={"device": "dev-carol"}))   # owns Lyra
        self.mod._broadcast({"note": "for Aldric"}, audience={"Aldric"})
        self.assertEqual(qbob.get_nowait(), {"note": "for Aldric"})   # owner
        self.assertEqual(qdm.get_nowait(), {"note": "for Aldric"})    # DM sees all
        self.assertTrue(qcar.empty())                                 # unrelated player

    def test_dm_whisper_filters_end_to_end(self):
        qdm = self._connect(FakeReq(args={"device": "dev-alice"}))
        qbob = self._connect(FakeReq(args={"device": "dev-bob"}))
        self.mod._broadcast({"gm": "screen"}, audience="dm")
        self.assertEqual(qdm.get_nowait(), {"gm": "screen"})
        self.assertTrue(qbob.empty())

    def test_no_dm_assigned_everything_public_end_to_end(self):
        # Clear the dm flag: filtering must disengage entirely (backward-compat).
        (self.camp / "state.md").write_text("## Session Flags\n- autorun_interval: 60\n")
        qbob = self._connect(FakeReq(args={"device": "dev-bob"}))
        self.mod._broadcast({"secret": 1}, audience="dm")  # would be DM-only if active
        self.assertEqual(qbob.get_nowait(), {"secret": 1})


if __name__ == "__main__":
    unittest.main()
