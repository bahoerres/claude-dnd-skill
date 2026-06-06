"""PR3: device approve/deny write-path authz (resolve identity from X-DND-Device)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_app(name="app_devauthz_under_test"):
    spec = importlib.util.spec_from_file_location(name, str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class DeviceAuthzTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "campaigns"
        (root / "camp").mkdir(parents=True)
        self.camp = root / "camp"
        camp_file = Path(self.tmp.name) / ".campaign"
        camp_file.write_text("camp")
        self.mod.CAMP_FILE = str(camp_file)
        self.mod._find_campaign = lambda n: root / n
        (self.camp / "ownership.json").write_text(json.dumps({
            "devices": {"dev-alice": "alice", "dev-bob": "bob"},
            "characters": {"alice": ["Mira"], "bob": ["Aldric"]},
        }))
        self.mod._token_ok = lambda: True
        self.mod._persist_approved_devices = lambda: None
        self.mod._persist_pending_devices = lambda: None
        self.mod._broadcast = lambda *a, **k: None
        self.client = self.mod.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _set_dm(self, who):
        self.camp.joinpath("state.md").write_text(f"## Session Flags\n- dm: {who}\n\n## DM Notes\n")

    def _no_dm(self):
        self.camp.joinpath("state.md").write_text("## Session Flags\n- autorun_interval: 60\n")

    def test_player_cannot_approve_when_dm_assigned(self):
        self._set_dm("alice")
        r = self.client.post("/device/approve", json={"id": "x"}, headers={"X-DND-Device": "dev-bob"})
        self.assertEqual(r.status_code, 403)

    def test_player_cannot_deny_when_dm_assigned(self):
        self._set_dm("alice")
        r = self.client.post("/device/deny", json={"id": "x"}, headers={"X-DND-Device": "dev-bob"})
        self.assertEqual(r.status_code, 403)

    def test_dm_can_approve(self):
        self._set_dm("alice")
        r = self.client.post("/device/approve", json={"id": "x"}, headers={"X-DND-Device": "dev-alice"})
        self.assertEqual(r.status_code, 204)

    def test_anyone_can_approve_when_no_dm(self):
        # Backward-compat: with no DM assigned, the shared-token gate is unchanged.
        self._no_dm()
        r = self.client.post("/device/approve", json={"id": "x"}, headers={"X-DND-Device": "dev-bob"})
        self.assertEqual(r.status_code, 204)


class ResolveIdentityHeaderTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app("app_devauthz_ident_under_test")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "campaigns"
        (root / "camp").mkdir(parents=True)
        self.camp = root / "camp"
        camp_file = Path(self.tmp.name) / ".campaign"
        camp_file.write_text("camp")
        self.mod.CAMP_FILE = str(camp_file)
        self.mod._find_campaign = lambda n: root / n
        (self.camp / "ownership.json").write_text(json.dumps({
            "devices": {"dev-bob": "bob"}, "characters": {"bob": ["Aldric"]},
        }))
        self.camp.joinpath("state.md").write_text("## Session Flags\n- dm: alice\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_device_resolved_from_header_when_no_query(self):
        class Req:
            headers = {"X-DND-Device": "dev-bob"}
            args = {}
            remote_addr = "10.0.0.5"
        ident = self.mod._resolve_identity(Req())
        self.assertEqual(ident["user"], "bob")
        self.assertEqual(ident["chars"], {"Aldric"})

    def test_query_param_takes_precedence_over_header(self):
        class Req:
            headers = {"X-DND-Device": "dev-bob"}
            args = {"device": "dev-unknown"}
            remote_addr = "10.0.0.5"
        ident = self.mod._resolve_identity(Req())
        self.assertIsNone(ident["user"])  # query device wins, and it's unmapped


if __name__ == "__main__":
    unittest.main()
