"""PR3 Task 6: send.py --audience flag, _audience_arg, and live /chunk routing."""
from __future__ import annotations

import importlib.util
import json
import queue
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── send.py CLI ─────────────────────────────────────────────────────────────

class AudienceFlagTest(unittest.TestCase):
    def setUp(self):
        self.send = _load(DISPLAY / "send.py", "send_aud_under_test")

    def test_parser_accepts_audience(self):
        args = self.send._build_parser().parse_args(["--audience", "dm"])
        self.assertEqual(args.audience, "dm")

    def test_audience_default_is_none(self):
        args = self.send._build_parser().parse_args([])
        self.assertIsNone(args.audience)


# ─── _audience_arg mapping ───────────────────────────────────────────────────

class AudienceArgTest(unittest.TestCase):
    def setUp(self):
        self.app = _load(DISPLAY / "dnd-display-app.py", "app_aud_arg_under_test")

    def test_all_and_public_and_empty_map_to_all(self):
        self.assertEqual(self.app._audience_arg("all"), "all")
        self.assertEqual(self.app._audience_arg("public"), "all")
        self.assertEqual(self.app._audience_arg(""), "all")
        self.assertEqual(self.app._audience_arg(None), "all")

    def test_dm_maps_to_dm(self):
        self.assertEqual(self.app._audience_arg("dm"), "dm")

    def test_name_maps_to_singleton_set(self):
        self.assertEqual(self.app._audience_arg("Aldric"), {"Aldric"})

    def test_non_string_maps_to_all(self):
        self.assertEqual(self.app._audience_arg({"x": 1}), "all")

    def test_reserved_tokens_case_insensitive(self):
        self.assertEqual(self.app._audience_arg("DM"), "dm")
        self.assertEqual(self.app._audience_arg("Public"), "all")
        self.assertEqual(self.app._audience_arg("ALL"), "all")


# ─── /chunk live broadcast is routed by audience ─────────────────────────────

class ChunkLiveRoutingTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load(DISPLAY / "dnd-display-app.py", "app_chunk_route_under_test")
        self.mod._mp_active = lambda: True
        self.mod._token_ok = lambda: True
        self.mod._persist_log = lambda: None
        self.mod._persist_tail = lambda: None
        with self.mod._clients_lock:
            self.mod._clients.clear()
        self.client = self.mod.app.test_client()

    def _add(self, user, role, chars):
        q = queue.Queue(maxsize=64)
        with self.mod._clients_lock:
            self.mod._clients.append({"q": q, "user": user, "role": role, "chars": set(chars)})
        return q

    def test_dm_whisper_not_delivered_live_to_player(self):
        qdm = self._add("alice", "dm", [])
        qpl = self._add("bob", "player", ["Aldric"])
        self.client.post("/chunk", json={"text": "hidden HP 12/30", "audience": "dm"})
        got_dm = [qdm.get_nowait() for _ in range(qdm.qsize())]
        self.assertTrue(any(p.get("text") == "hidden HP 12/30" for p in got_dm))
        self.assertTrue(qpl.empty())

    def test_default_narration_broadcasts_to_all(self):
        qdm = self._add("alice", "dm", [])
        qpl = self._add("bob", "player", ["Aldric"])
        self.client.post("/chunk", json={"text": "the tavern is warm"})
        self.assertTrue(any(p.get("text") == "the tavern is warm" for p in [qpl.get_nowait() for _ in range(qpl.qsize())]))

    def test_character_whisper_reaches_owner_not_others(self):
        qbob = self._add("bob", "player", ["Aldric"])
        qcar = self._add("carol", "player", ["Mira"])
        self.client.post("/chunk", json={"text": "you alone spot it", "audience": "Aldric"})
        self.assertTrue(any(p.get("text") == "you alone spot it" for p in [qbob.get_nowait() for _ in range(qbob.qsize())]))
        self.assertTrue(qcar.empty())

    def test_award_blocks_routed_by_audience(self):
        """All four /chunk paths route by audience — a dm-whispered award block
        does not reach a player (the inspiration/xp/milestone branches)."""
        award_keys = {"inspiration_award", "xp_award", "milestone_award"}
        for body in (
            {"inspiration_award": "Aldric", "text": "Aldric", "audience": "dm"},
            {"xp_award": {"summary": "+100"}, "text": "+100", "audience": "dm"},
            {"milestone_award": "Hero", "label": "Deed", "text": "Hero", "audience": "dm"},
        ):
            with self.mod._clients_lock:
                self.mod._clients.clear()
            qdm = self._add("alice", "dm", [])
            qpl = self._add("bob", "player", ["Aldric"])
            self.client.post("/chunk", json=body)
            dm_payloads = [qdm.get_nowait() for _ in range(qdm.qsize())]
            pl_payloads = [qpl.get_nowait() for _ in range(qpl.qsize())]
            self.assertTrue(any(award_keys & set(p) for p in dm_payloads), f"DM missed award: {body}")
            self.assertFalse(any(award_keys & set(p) for p in pl_payloads), f"award leaked to player: {body}")


class SendAwardAudienceTest(unittest.TestCase):
    """send.py must attach --audience to the award bodies, not just narration."""

    def setUp(self):
        self.send = _load(DISPLAY / "send.py", "send_award_under_test")
        self.posts = []
        self.send._read_token = lambda: "tok"

        def _capture(url, data, token):
            self.posts.append((url, json.loads(data.decode())))
            return True
        self.send._post = _capture

    def _chunk_bodies(self):
        return [b for (u, b) in self.posts if u.endswith("/chunk")]

    def test_inspiration_award_carries_audience(self):
        self.send.main(["--audience", "dm", "--inspiration-award", "Aldric"])
        insp = [b for b in self._chunk_bodies() if "inspiration_award" in b]
        self.assertTrue(insp and insp[0].get("audience") == "dm")

    def test_xp_award_carries_audience(self):
        self.send.main(["--audience", "dm", "--xp-award", '{"summary":"+100"}'])
        xp = [b for b in self._chunk_bodies() if "xp_award" in b]
        self.assertTrue(xp and xp[0].get("audience") == "dm")

    def test_milestone_award_carries_audience(self):
        self.send.main(["--audience", "dm", "--milestone-award", "Hero", "--milestone-label", "Deed"])
        ms = [b for b in self._chunk_bodies() if "milestone_award" in b]
        self.assertTrue(ms and ms[0].get("audience") == "dm")

    def test_no_audience_means_no_key(self):
        self.send.main(["--inspiration-award", "Aldric"])
        insp = [b for b in self._chunk_bodies() if "inspiration_award" in b]
        self.assertTrue(insp and "audience" not in insp[0])


if __name__ == "__main__":
    unittest.main()
