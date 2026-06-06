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
            f"# State\n\n## Session Flags\n- autorun_interval: 60\n- dm: {who}\n\n"
            f"## DM Notes (hidden from players)\n"
        )

    def _write_state(self, body):
        self.camp.joinpath("state.md").write_text(body)

    def _write_ownership(self, raw):
        self.camp.joinpath("ownership.json").write_text(raw)

    # ── DM assignment reader ─────────────────────────────────────────────────

    def test_no_state_file_means_no_dm(self):
        self.assertIsNone(self.mod._session_dm())
        self.assertFalse(self.mod._mp_active())

    def test_session_dm_reads_flag(self):
        self._set_dm("alice")
        self.assertEqual(self.mod._session_dm(), "alice")
        self.assertTrue(self.mod._mp_active())

    def test_dm_prose_in_dm_notes_does_not_activate(self):
        """A `dm:`-prefixed line in free-form DM Notes must NOT flip multiplayer
        on. Only the Session Flags section is authoritative."""
        self._write_state(
            "# State\n\n## Session Flags\n- autorun_interval: 60\n\n"
            "## DM Notes (hidden from players)\n"
            "- dm: remember to foreshadow the betrayal\n"
        )
        self.assertIsNone(self.mod._session_dm())
        self.assertFalse(self.mod._mp_active())

    def test_dm_inline_comment_is_stripped(self):
        self._write_state(
            "## Session Flags\n- dm: alice  # the host is DM tonight\n\n## DM Notes\n"
        )
        self.assertEqual(self.mod._session_dm(), "alice")

    def test_dm_commented_line_ignored(self):
        self._write_state("## Session Flags\n- # dm: alice\n\n## DM Notes\n")
        self.assertIsNone(self.mod._session_dm())

    def test_dm_empty_value_is_none(self):
        self._write_state("## Session Flags\n- dm:   \n\n## DM Notes\n")
        self.assertIsNone(self.mod._session_dm())

    def test_dm_first_flag_wins_on_duplicate(self):
        self._write_state("## Session Flags\n- dm: alice\n- dm: bob\n\n## DM Notes\n")
        self.assertEqual(self.mod._session_dm(), "alice")

    # ── Ownership reader ─────────────────────────────────────────────────────

    def test_owned_characters(self):
        self.assertEqual(self.mod._owned_characters("alice"), {"Mira"})
        self.assertEqual(self.mod._owned_characters("nobody"), set())
        self.assertEqual(self.mod._owned_characters(None), set())

    def test_malformed_ownership_json_is_empty(self):
        self._write_ownership("{not json")
        self.assertEqual(self.mod._ownership(), {"devices": {}, "characters": {}})
        self.assertEqual(self.mod._device_user_map(), {})

    def test_ownership_list_not_dict_is_empty(self):
        self._write_ownership("[1, 2, 3]")
        self.assertEqual(self.mod._device_user_map(), {})
        self.assertEqual(self.mod._owned_characters("alice"), set())

    def test_owned_characters_string_value_does_not_explode(self):
        """characters[user] as a bare string must not explode into letters."""
        self._write_ownership(json.dumps({
            "devices": {}, "characters": {"alice": "Mira"},
        }))
        self.assertEqual(self.mod._owned_characters("alice"), set())

    def test_owned_characters_null_value(self):
        self._write_ownership(json.dumps({
            "devices": {}, "characters": {"alice": None},
        }))
        self.assertEqual(self.mod._owned_characters("alice"), set())

    # ── Identity resolution ──────────────────────────────────────────────────

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

    def test_localhost_unmapped_defaults_to_dm(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(FakeReq(args={}, remote_addr="127.0.0.1"))
        self.assertEqual(ident["role"], "dm")

    def test_ipv6_localhost_unmapped_defaults_to_dm(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(FakeReq(args={}, remote_addr="::1"))
        self.assertEqual(ident["role"], "dm")

    def test_localhost_mapped_player_is_not_dm(self):
        self._set_dm("alice")
        ident = self.mod._resolve_identity(
            FakeReq(args={"device": "dev-bob"}, remote_addr="127.0.0.1")
        )
        self.assertEqual(ident["role"], "player")

    def test_mp_inactive_resolve_has_no_error(self):
        # No DM assigned: identity still resolves; a remote unmapped client is a player.
        ident = self.mod._resolve_identity(FakeReq(args={"device": "dev-bob"}))
        self.assertEqual(ident["user"], "bob")
        self.assertEqual(ident["role"], "player")  # bob != (no) dm

    # ── Forward-auth (X-Remote-User) trust boundary ──────────────────────────

    def test_forward_auth_header_ignored_by_default(self):
        """v1 (default): X-Remote-User is NOT trusted; identity comes from device."""
        self._set_dm("alice")
        ident = self.mod._resolve_identity(
            FakeReq(headers={"X-Remote-User": "alice"}, args={"device": "dev-bob"})
        )
        self.assertEqual(ident["user"], "bob")     # device wins, header ignored
        self.assertEqual(ident["role"], "player")

    def test_forward_auth_header_honored_when_enabled_from_trusted_proxy(self):
        self._set_dm("alice")
        self.mod._FORWARD_AUTH = True
        self.mod._TRUSTED_PROXIES = {"10.0.0.9"}
        ident = self.mod._resolve_identity(
            FakeReq(headers={"X-Remote-User": "alice"}, args={"device": "dev-bob"},
                    remote_addr="10.0.0.9")
        )
        self.assertEqual(ident["user"], "alice")
        self.assertEqual(ident["role"], "dm")

    def test_forward_auth_header_ignored_from_untrusted_remote(self):
        self._set_dm("alice")
        self.mod._FORWARD_AUTH = True
        self.mod._TRUSTED_PROXIES = {"10.0.0.9"}
        ident = self.mod._resolve_identity(
            FakeReq(headers={"X-Remote-User": "alice"}, args={"device": "dev-bob"},
                    remote_addr="10.0.0.5")   # not a trusted proxy
        )
        self.assertEqual(ident["user"], "bob")    # header from untrusted host ignored
        self.assertEqual(ident["role"], "player")

    def test_localhost_dm_fallback_disabled_under_forward_auth(self):
        """Under forward-auth, localhost is the proxy, not the host: no auto-DM."""
        self._set_dm("alice")
        self.mod._FORWARD_AUTH = True
        self.mod._TRUSTED_PROXIES = {"127.0.0.1", "::1"}
        ident = self.mod._resolve_identity(FakeReq(args={}, remote_addr="127.0.0.1"))
        self.assertEqual(ident["role"], "player")


if __name__ == "__main__":
    unittest.main()
