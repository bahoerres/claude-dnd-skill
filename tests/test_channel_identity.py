"""Identity + ownership + DM-assignment readers for the channel layer."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_app():
    spec = importlib.util.spec_from_file_location("app_under_test", str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeReq:
    def __init__(self, headers=None, args=None, remote_addr="10.0.0.5"):
        self.headers = headers or {}
        self.args = args or {}
        self.remote_addr = remote_addr


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "campaigns"
        (root / "camp").mkdir(parents=True)
        self.camp = root / "camp"
        # Point the app at our temp tree.
        self.camp_file = Path(self.tmp.name) / ".campaign"
        self.camp_file.write_text("camp")
        self.mod.CAMP_FILE = str(self.camp_file)
        self.mod._find_campaign = lambda name: root / name
        # ownership.json
        (self.camp / "ownership.json").write_text(json.dumps({
            "devices": {"dev-alice": "alice", "dev-bob": "bob"},
            "characters": {"alice": ["Mira"], "bob": ["Aldric"]},
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def _set_dm(self, who):
        self.camp.joinpath("state.md").write_text(
            f"# State\n\n## Session Flags\n- autorun_interval: 60\n- dm: {who}\n"
        )

    def test_no_state_file_means_no_dm(self):
        self.assertIsNone(self.mod._session_dm())
        self.assertFalse(self.mod._mp_active())

    def test_session_dm_reads_flag(self):
        self._set_dm("alice")
        self.assertEqual(self.mod._session_dm(), "alice")
        self.assertTrue(self.mod._mp_active())

    def test_owned_characters(self):
        self.assertEqual(self.mod._owned_characters("alice"), {"Mira"})
        self.assertEqual(self.mod._owned_characters("nobody"), set())
        self.assertEqual(self.mod._owned_characters(None), set())

    def test_resolve_identity_device_to_player(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(FakeReq(args={"device": "dev-bob"}))
        self.assertEqual(ident["user"], "bob")
        self.assertEqual(ident["role"], "player")
        self.assertEqual(ident["chars"], {"Aldric"})

    def test_resolve_identity_dm_role(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(FakeReq(args={"device": "dev-alice"}))
        self.assertEqual(ident["role"], "dm")

    def test_resolve_identity_forward_auth_header_wins(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(
            FakeReq(headers={"X-Remote-User": "alice"}, args={"device": "dev-bob"})
        )
        self.assertEqual(ident["user"], "alice")
        self.assertEqual(ident["role"], "dm")

    def test_localhost_unmapped_defaults_to_dm(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(FakeReq(args={}, remote_addr="127.0.0.1"))
        self.assertEqual(ident["role"], "dm")

    def test_localhost_mapped_player_is_not_dm(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(
            FakeReq(args={"device": "dev-bob"}, remote_addr="127.0.0.1")
        )
        self.assertEqual(ident["role"], "player")


if __name__ == "__main__":
    unittest.main()
