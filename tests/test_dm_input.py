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
        # _resolve_identity reads .args.get / .headers.get — plain dict works fine
        r = _Req()
        r.args = {}
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


class DmInputRouteTest(DmInputTestBase):
    def _post(self, text, device=None, env=None):
        headers = {"X-DND-Device": device} if device else {}
        return self.client.post(
            "/dm-input", json={"text": text}, headers=headers,
            environ_base=env or {"REMOTE_ADDR": "10.0.0.9"})

    def test_player_gets_403(self):
        self._set_dm("alice")
        r = self._post("the door creaks open", device="dev-bob")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.qfile.exists())

    def test_no_dm_assigned_gets_403(self):
        self._no_dm()
        # Even the would-be DM device: route is meaningless outside multiplayer.
        r = self._post("hello", device="dev-alice")
        self.assertEqual(r.status_code, 403)

    def test_dm_post_writes_tagged_queue_line(self):
        self._set_dm("alice")
        r = self._post("everyone roll perception, DC 13", device="dev-alice")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(
            self.qfile.read_text(),
            "[DM]: everyone roll perception, DC 13")

    def test_dm_post_appends_below_existing_queue(self):
        self._set_dm("alice")
        self.qfile.write_text("[Mira]: I check the lock")
        self._post("she finds scratch marks", device="dev-alice")
        self.assertEqual(
            self.qfile.read_text(),
            "[Mira]: I check the lock\n[DM]: she finds scratch marks")

    def test_echo_broadcast_is_dm_audience_only(self):
        self._set_dm("alice")
        self._post("send 3", device="dev-alice")
        echoes = [(p, a) for p, a in self.broadcasts if "dm_input" in p]
        self.assertEqual(len(echoes), 1)
        payload, audience = echoes[0]
        self.assertEqual(audience, "dm")
        self.assertEqual(payload["dm_input"]["text"], "send 3")

    def test_staged_player_inputs_untouched(self):
        self._set_dm("alice")
        with self.mod._staged_lock:
            self.mod._staged["Mira"] = {
                "text": "half-typed action", "ready": False, "timestamp": 0}
        self._post("a dm aside", device="dev-alice")
        with self.mod._staged_lock:
            self.assertIn("Mira", self.mod._staged)
            self.assertEqual(self.mod._staged["Mira"]["text"], "half-typed action")

    def test_empty_text_gets_400(self):
        self._set_dm("alice")
        r = self._post("   ", device="dev-alice")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.qfile.exists())


if __name__ == "__main__":
    unittest.main()
