"""Tagged client registry + audience-aware _broadcast + _visible_to."""
from __future__ import annotations

import importlib.util
import queue
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_app():
    spec = importlib.util.spec_from_file_location("app_bcast_under_test", str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app_bcast_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


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


if __name__ == "__main__":
    unittest.main()
