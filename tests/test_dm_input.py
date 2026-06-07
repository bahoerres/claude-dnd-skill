"""Task 7b: /dm-input lane + `you` identity event (assistant-mode plumbing)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_app(name="app_dminput_under_test"):
    spec = importlib.util.spec_from_file_location(name, str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class DmInputTestBase(unittest.TestCase):
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
        # Redirect the queue file into the sandbox
        self.qfile = Path(self.tmp.name) / ".input_queue"
        self.mod.QUEUE_FILE = str(self.qfile)
        self.mod._token_ok = lambda: True
        self.mod._rate_ok = lambda ip: True
        self.broadcasts = []
        self.mod._broadcast = (
            lambda payload, audience="all": self.broadcasts.append((payload, audience))
        )
        self.client = self.mod.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _set_dm(self, who):
        self.camp.joinpath("state.md").write_text(
            f"## Session Flags\n- dm: {who}\n\n## DM Notes\n")

    def _no_dm(self):
        self.camp.joinpath("state.md").write_text(
            "## Session Flags\n- autorun_interval: 60\n")


class YouEventTest(DmInputTestBase):
    def _ident(self, device):
        class _Req:
            args = {}
            headers = {"X-DND-Device": device}
            remote_addr = "10.0.0.9"   # not localhost — no host convenience
        # _resolve_identity reads .args.get / .headers.get
        class _Args(dict):
            def get(self, k, d=None): return dict.get(self, k, d)
        r = _Req()
        r.args = _Args()
        return self.mod._resolve_identity(r)

    def test_first_catchup_payload_is_you_with_dm_role(self):
        self._set_dm("alice")
        payloads = self.mod._catchup_payloads(self._ident("dev-alice"))
        self.assertEqual(
            payloads[0], {"you": {"role": "dm", "user": "alice"}})

    def test_first_catchup_payload_is_you_with_player_role(self):
        self._set_dm("alice")
        payloads = self.mod._catchup_payloads(self._ident("dev-bob"))
        self.assertEqual(
            payloads[0], {"you": {"role": "player", "user": "bob"}})


if __name__ == "__main__":
    unittest.main()
