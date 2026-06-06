"""PR3 Task 7a: scripts/mp_admin.py — ownership + DM-assignment write path."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
DISPLAY = REPO / "display"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


STATE_TEMPLATE = (
    "# State\n\n## Session Flags\n- autorun_interval: 60\n\n"
    "## DM Notes (hidden from players)\n- dm: this prose must be ignored\n"
)


class MpAdminTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load(SCRIPTS / "mp_admin.py", "mp_admin_under_test")
        self.tmp = tempfile.TemporaryDirectory()
        self.camp = Path(self.tmp.name) / "camp"
        self.camp.mkdir()
        self.mod.find_campaign = lambda n: self.camp
        (self.camp / "state.md").write_text(STATE_TEMPLATE)

    def tearDown(self):
        self.tmp.cleanup()

    # ── DM flag ──────────────────────────────────────────────────────────────

    def test_assign_then_get(self):
        self.mod.set_dm("camp", "alice")
        self.assertEqual(self.mod.get_dm("camp"), "alice")

    def test_assign_writes_into_session_flags_not_dm_notes(self):
        self.mod.set_dm("camp", "alice")
        text = (self.camp / "state.md").read_text()
        flags = text.split("## DM Notes")[0]
        self.assertIn("- dm: alice", flags)

    def test_reassign_replaces_no_duplicate(self):
        self.mod.set_dm("camp", "alice")
        self.mod.set_dm("camp", "bob")
        text = (self.camp / "state.md").read_text()
        self.assertEqual(text.count("dm: bob"), 1)
        self.assertNotIn("dm: alice", text.split("## DM Notes")[0])
        self.assertEqual(self.mod.get_dm("camp"), "bob")

    def test_clear_removes_flag(self):
        self.mod.set_dm("camp", "alice")
        self.mod.clear_dm("camp")
        self.assertIsNone(self.mod.get_dm("camp"))

    def test_get_dm_ignores_dm_notes_prose(self):
        # No dm: in Session Flags, but DM Notes has 'dm: ...' prose.
        self.assertIsNone(self.mod.get_dm("camp"))

    # ── ownership.json ───────────────────────────────────────────────────────

    def test_own_device_and_char(self):
        self.mod.own_device("camp", "dev-1", "alice")
        self.mod.own_char("camp", "alice", "Mira")
        data = json.loads((self.camp / "ownership.json").read_text())
        self.assertEqual(data["devices"], {"dev-1": "alice"})
        self.assertEqual(data["characters"], {"alice": ["Mira"]})

    def test_own_char_no_duplicate(self):
        self.mod.own_char("camp", "alice", "Mira")
        self.mod.own_char("camp", "alice", "Mira")
        data = json.loads((self.camp / "ownership.json").read_text())
        self.assertEqual(data["characters"]["alice"], ["Mira"])

    def test_collision_warning(self):
        # user 'Mira' exists; alice owns a character also named 'Mira'.
        self.mod.own_char("camp", "alice", "Mira")
        self.mod.own_char("camp", "Mira", "Lyra")
        self.assertIsNotNone(self.mod._collision_warning(self.mod.load_ownership("camp")))

    # ── Defensive / edge cases (PR3 review) ──────────────────────────────────

    def test_malformed_ownership_subtypes_coerced(self):
        (self.camp / "ownership.json").write_text('{"devices": "notadict", "characters": null}')
        data = self.mod.load_ownership("camp")
        self.assertEqual(data["devices"], {})
        self.assertEqual(data["characters"], {})
        # and the writers don't crash on the recovered structure
        self.mod.own_device("camp", "dev-1", "alice")
        self.mod.own_char("camp", "alice", "Mira")

    def test_hash_in_dm_name_rejected(self):
        with self.assertRaises(SystemExit):
            self.mod.set_dm("camp", "bob#1234")

    def test_hash_in_owner_name_rejected(self):
        with self.assertRaises(SystemExit):
            self.mod.own_char("camp", "bob#1", "Mira")
        with self.assertRaises(SystemExit):
            self.mod.own_device("camp", "dev-1", "bob#1")

    def test_empty_device_id_rejected(self):
        with self.assertRaises(SystemExit):
            self.mod.own_device("camp", "", "alice")

    def test_set_dm_removes_stale_duplicates(self):
        # Two pre-existing dm: lines; set_dm must leave exactly one.
        self.camp.joinpath("state.md").write_text(
            "## Session Flags\n- dm: alice\n- dm: stale\n\n## DM Notes\n")
        self.mod.set_dm("camp", "carol")
        flags = (self.camp / "state.md").read_text().split("## DM Notes")[0]
        self.assertEqual(flags.count("dm:"), 1)
        self.assertEqual(self.mod.get_dm("camp"), "carol")


class WritePathMatchesServerReadTest(unittest.TestCase):
    """Round-trip: mp_admin writes dm:, the server's _session_dm reads it back —
    proving the write path and the read path agree on format."""

    def test_roundtrip(self):
        admin = _load(SCRIPTS / "mp_admin.py", "mp_admin_rt")
        app = _load(DISPLAY / "dnd-display-app.py", "app_rt_under_test")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        camp = Path(tmp.name) / "camp"
        camp.mkdir()
        (camp / "state.md").write_text(STATE_TEMPLATE)
        camp_file = Path(tmp.name) / ".campaign"
        camp_file.write_text("camp")

        admin.find_campaign = lambda n: camp
        app.CAMP_FILE = str(camp_file)
        app._find_campaign = lambda n: camp

        self.assertIsNone(app._session_dm())          # nothing assigned yet
        admin.set_dm("camp", "alice")
        self.assertEqual(app._session_dm(), "alice")  # server reads what admin wrote
        self.assertTrue(app._mp_active())
        admin.clear_dm("camp")
        self.assertIsNone(app._session_dm())


if __name__ == "__main__":
    unittest.main()
