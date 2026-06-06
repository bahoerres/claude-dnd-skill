# Multiplayer Channel Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the D&D display companion from a single all-to-all broadcast into server-side, per-connection audience filtering, so a remote/personal browser receives only what its owner is authorized to see — and a human DM can hold a private "GM screen" channel.

**Architecture:** Insert an identity + routing layer between sender and SSE display. Identity is resolved once per `/stream` connection (device-UUID in v1, forward-auth header as the upgrade). The single `_broadcast` chokepoint gains an `audience` filter; the on-connect catch-up and the replay log become channel-tagged and scoped. **Filtering is opt-in: it engages only when a DM is assigned for the session** (`dm:` field in `state.md`). With no DM assigned, behavior is byte-for-byte the current couch-coop broadcast — this is the backward-compatibility contract and it makes every phase safe to ship.

**Tech Stack:** Python 3.10+, Flask + flask-cors (SSE via `Response` generator), stdlib `queue`/`threading`/`json`/`re`. Tests: `unittest` + `_load_module` importlib pattern already used in `tests/test_display_robustness.py`. Frontend: vanilla JS `EventSource` in `display/templates/index.html`.

---

## Key architectural decisions (read before coding)

1. **Opt-in via DM-assignment.** `_mp_active()` returns `True` only when `_session_dm()` is non-`None`. Every filter (`_visible_to`, the on-connect scoping) short-circuits to "everything visible" when `_mp_active()` is `False`. This is the "mode flag gated on DM-assignment" from `multiplayer-design.md` — no human DM → current behavior.

2. **Default `audience="all"` keeps every existing call valid.** There are ~30 `_broadcast(...)` call sites (verified). None change. Only new private sends pass an audience.

3. **Backward-compat for the host screen.** A connection from `127.0.0.1`/`::1` with no ownership mapping resolves to `role="dm"`, so the local host machine running the server keeps seeing everything. An explicit device→user mapping overrides this (mapping wins).

4. **Reuse `_find_campaign`.** The app already imports `from paths import find_campaign as _find_campaign` (line ~50). The campaign directory resolver is `_find_campaign(name)` — used for `ownership.json` and `state.md`. Do **not** reinvent path logic. There is no `pathlib.Path` import in the app; use `os.path`.

5. **Line numbers drift.** All line numbers below are from the 2026-06-05 checkout. Anchor on symbol names (`def _broadcast`, `@app.route("/stream")`), not numbers.

---

## File structure

| File | Responsibility | Change |
|------|----------------|--------|
| `display/dnd-display-app.py` | SSE server, the one fan-out, all routes | Add channel-layer section (identity + ownership + visibility); modify `_broadcast`, `_clients` registry, `/stream`, `/chunk` |
| `display/send.py` | CLI → POST `/chunk` | Add `--audience` flag → `payload["audience"]` |
| `display/templates/index.html` | Browser SSE client | One line: append `?device=` to the `EventSource` URL |
| `tests/test_channel_identity.py` | Identity + ownership + DM-assignment readers | **Create** |
| `tests/test_channel_broadcast.py` | Tagged registry + audience-aware `_broadcast` + `_visible_to` | **Create** |
| `tests/test_channel_stream_scope.py` | `_entry_visible`, `_scope_stats`, channel tagging in `/chunk` | **Create** |
| `tests/test_send_audience.py` | `send.py --audience` → payload | **Create** |
| `SKILL-commands.md` | `/dnd` command procedures | Add `/dnd dm` (assign/clear/status) + `/dnd own` (ownership map) procedures |
| `display/README.md` / root `README.md` | Docs | Document private channels + DM assignment (after backbone runs) |

**New runtime artifact:** `~/.claude/dnd/campaigns/<name>/ownership.json`:

```json
{
  "devices":    { "8b1c…uuid": "alice", "3f9a…uuid": "bob" },
  "characters": { "alice": ["Mira"], "bob": ["Aldric"] }
}
```

**New session flag:** `dm: <user>` written into `state.md` under `## Session Flags` (next to `autorun_interval`).

---

## Phase 1 — The Spine (identity, tagged registry, audience-aware broadcast)

After Phase 1 you can send a private payload to one role/user and prove only the right queues receive it. Existing flows are untouched because `audience` defaults to `"all"` and `_mp_active()` is `False` until a DM is assigned. **One PR.**

### Task 1: Identity + ownership + DM-assignment readers

**Files:**
- Modify: `display/dnd-display-app.py` — add a new section immediately after the device-approval block (after `_device_ok`, ~line 245)
- Test: `tests/test_channel_identity.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_channel_identity.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_channel_identity.py -v`
Expected: FAIL — `AttributeError: module 'app_under_test' has no attribute '_session_dm'`.

- [ ] **Step 3: Write minimal implementation**

In `display/dnd-display-app.py`, immediately after the `_device_ok` function (after ~line 245), add:

```python
# ─── Channel / audience layer: identity + ownership ──────────────────────────
# Private filtering is OPT-IN. It engages only when a DM is assigned for the
# session (the `dm:` flag in state.md). With no DM assigned, _mp_active() is
# False and the server behaves exactly like the single-broadcast original.

def _campaign_dir():
    """Resolve the current campaign directory via the canonical paths helper."""
    try:
        name = open(CAMP_FILE).read().strip()
    except Exception:
        return None
    if not name:
        return None
    try:
        return _find_campaign(name)
    except Exception:
        return None


def _ownership() -> dict:
    """Per-campaign device→user and user→characters map. Missing file = empty."""
    cdir = _campaign_dir()
    if cdir is None:
        return {"devices": {}, "characters": {}}
    try:
        with open(os.path.join(str(cdir), "ownership.json")) as fh:
            data = json.load(fh)
    except Exception:
        return {"devices": {}, "characters": {}}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("devices", {})
    data.setdefault("characters", {})
    return data


def _device_user_map() -> dict:
    return _ownership()["devices"]


def _owned_characters(user) -> set:
    if not user:
        return set()
    return set(_ownership()["characters"].get(user, []))


def _session_dm():
    """Read the `dm:` assignment from state.md Session Flags. None = no DM."""
    cdir = _campaign_dir()
    if cdir is None:
        return None
    try:
        text = open(os.path.join(str(cdir), "state.md")).read()
    except Exception:
        return None
    m = re.search(r"(?mi)^\s*[-*]?\s*dm:\s*(.+?)\s*$", text)
    if not m:
        return None
    return m.group(1).strip() or None


def _mp_active() -> bool:
    """Private filtering engages only when a DM is assigned for the session."""
    return _session_dm() is not None


def _resolve_identity(req) -> dict:
    """Resolve the identity of an SSE connection.

    Upgrade path: a trusted X-Remote-User header injected by a forward-auth
    proxy (the app is not the sole ingress on its own, so this is only trusted
    when such a proxy is deployed). v1 path: the device UUID the browser stores
    in localStorage, passed on the EventSource URL as ?device=.
    """
    user = req.headers.get("X-Remote-User")
    device_id = req.args.get("device") or ""
    if not user:
        user = _device_user_map().get(device_id)
    dm = _session_dm()
    role = "dm" if (user and user == dm) else "player"
    # Backward-compat: the local host machine running the server is the trusted
    # operator. When nothing maps it and it connects from localhost, treat it as
    # DM so the host screen keeps seeing everything. Explicit mapping overrides.
    remote = req.remote_addr or ""
    if user is None and remote in ("127.0.0.1", "::1"):
        role = "dm"
    return {"user": user, "device": device_id, "role": role,
            "chars": _owned_characters(user)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_channel_identity.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_channel_identity.py display/dnd-display-app.py
git commit -m "feat(display): identity + ownership + DM-assignment readers for channel layer"
```

---

### Task 2: Tag the client registry

`_clients` goes from `list[queue.Queue]` to a list of small records `{"q", "user", "role", "chars"}`. Every site that iterates or mutates `_clients` updates. Verified sites: globals (763), `_broadcast` (1001–1010), `/health` count (1098–1099), `/stream` append (1945–1946), `/stream` cleanup (2003–2007).

**Files:**
- Modify: `display/dnd-display-app.py` (registry shape + all `_clients` sites)
- Test: `tests/test_channel_broadcast.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_channel_broadcast.py`:

```python
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
        # mp inactive OR active, "all" reaches everyone
        self.mod._mp_active = lambda: True
        qa = self._add("alice", "dm", [])
        qb = self._add("bob", "player", ["Aldric"])
        self.mod._broadcast({"text": "hi"})
        self.assertEqual(qa.get_nowait(), {"text": "hi"})
        self.assertEqual(qb.get_nowait(), {"text": "hi"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_channel_broadcast.py::RegistryShapeTest -v`
Expected: FAIL — `_broadcast` still does `q.put_nowait` on records (`AttributeError: 'dict' object has no attribute 'put_nowait'`) or the append in `/stream` is still a bare queue.

- [ ] **Step 3: Write minimal implementation**

Change the global annotation (line ~763):

```python
_clients: list[dict] = []   # records: {"q": Queue, "user", "role", "chars": set}
```

Update `_broadcast` (lines ~1001–1010) to iterate records — **(this is replaced fully in Task 3; for now the minimal change keeps it working):**

```python
def _broadcast(payload: dict) -> None:
    with _clients_lock:
        dead = []
        for c in _clients:
            try:
                c["q"].put_nowait(payload)
            except queue.Full:
                dead.append(c)
        for c in dead:
            _clients.remove(c)
```

Update `/health` count (lines ~1098–1099) — no change needed (`len(_clients)` still valid).

Update `/stream` append (lines ~1945–1946):

```python
    q: queue.Queue = queue.Queue(maxsize=256)
    ident = _resolve_identity(request)
    client = {"q": q, "user": ident["user"], "role": ident["role"], "chars": ident["chars"]}
    with _clients_lock:
        _clients.append(client)
```

Update `/stream` cleanup (lines ~2003–2007):

```python
        except GeneratorExit:
            with _clients_lock:
                try:
                    _clients.remove(client)
                except ValueError:
                    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_channel_broadcast.py::RegistryShapeTest -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_channel_broadcast.py display/dnd-display-app.py
git commit -m "feat(display): tag SSE client registry with identity records"
```

---

### Task 3: Audience-aware `_broadcast` + `_visible_to`

**Files:**
- Modify: `display/dnd-display-app.py` (`_broadcast` signature + new `_visible_to`)
- Test: `tests/test_channel_broadcast.py` (add `VisibilityTest`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_channel_broadcast.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_channel_broadcast.py::VisibilityTest -v`
Expected: FAIL — `_broadcast() got an unexpected keyword argument 'audience'`.

- [ ] **Step 3: Write minimal implementation**

Replace `_broadcast` (the version from Task 2) and add `_visible_to` directly below it:

```python
def _broadcast(payload: dict, audience="all") -> None:
    # audience: "all" | "dm" | set of usernames/character names
    with _clients_lock:
        dead = []
        for c in _clients:
            if not _visible_to(c, audience):
                continue
            try:
                c["q"].put_nowait(payload)
            except queue.Full:
                dead.append(c)
        for c in dead:
            _clients.remove(c)


def _visible_to(client, audience) -> bool:
    # When private filtering is off, everything is public (current behavior).
    if not _mp_active():
        return True
    if audience == "all":
        return True
    if client["role"] == "dm":
        return True                      # DM sees everything
    if audience == "dm":
        return False                     # DM-only, this client is a player
    # audience is a set of usernames / character names
    return bool(client["chars"] & set(audience)) or client["user"] in set(audience)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_channel_broadcast.py -v`
Expected: PASS (all RegistryShapeTest + VisibilityTest).

- [ ] **Step 5: Full regression + commit**

```bash
python3 -m pytest tests/ -v
git add tests/test_channel_broadcast.py display/dnd-display-app.py
git commit -m "feat(display): audience-aware _broadcast with per-connection visibility filter"
```

**Phase 1 checkpoint:** `_broadcast(payload, audience="dm")` reaches only DM connections when a DM is assigned, and behaves identically to before when none is. Existing call sites untouched.

---

## Phase 2 — Leak closure (channel-tag the log, scope the on-connect catch-up)

Until this lands, "private" is cosmetic: the `/stream` on-connect block still floods the full history, full stats, everyone's staged inputs, and **DM-only device-approval requests** to every browser. This is the security-meaningful phase.

### Task 4: Channel-tag the replay log + `_entry_visible`

The `/chunk` handler builds `log_entry` dicts appended to `_text_log` (4 sites: ~1146, 1166, 1194, 1252). Add a `channel` field, default `"public"`, sourced from an optional `audience` field on the POST body. Add `_entry_visible(entry, ident)`.

**Files:**
- Modify: `display/dnd-display-app.py` (`/chunk` handler + new `_entry_visible`)
- Test: `tests/test_channel_stream_scope.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_channel_stream_scope.py`:

```python
"""Channel tagging, _entry_visible, and on-connect scoping for /stream."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load_app():
    spec = importlib.util.spec_from_file_location("app_scope_under_test", str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app_scope_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class EntryVisibleTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app()
        self.mod._mp_active = lambda: True

    def _ident(self, user, role, chars):
        return {"user": user, "role": role, "chars": set(chars), "device": ""}

    def test_public_entry_visible_to_all(self):
        e = {"text": "hi", "channel": "public"}
        self.assertTrue(self.mod._entry_visible(e, self._ident("bob", "player", ["Aldric"])))

    def test_untagged_entry_treated_public(self):
        e = {"text": "legacy entry, no channel"}
        self.assertTrue(self.mod._entry_visible(e, self._ident("bob", "player", [])))

    def test_dm_entry_hidden_from_player(self):
        e = {"text": "secret", "channel": "dm"}
        self.assertFalse(self.mod._entry_visible(e, self._ident("bob", "player", ["Aldric"])))
        self.assertTrue(self.mod._entry_visible(e, self._ident("alice", "dm", [])))

    def test_character_channel_visible_to_owner(self):
        e = {"text": "for Aldric", "channel": "Aldric"}
        self.assertTrue(self.mod._entry_visible(e, self._ident("bob", "player", ["Aldric"])))
        self.assertFalse(self.mod._entry_visible(e, self._ident("carol", "player", ["Mira"])))

    def test_inactive_mp_everything_visible(self):
        self.mod._mp_active = lambda: False
        e = {"text": "secret", "channel": "dm"}
        self.assertTrue(self.mod._entry_visible(e, self._ident("bob", "player", [])))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_channel_stream_scope.py::EntryVisibleTest -v`
Expected: FAIL — `module has no attribute '_entry_visible'`.

- [ ] **Step 3: Write minimal implementation**

Add `_entry_visible` next to `_visible_to`:

```python
def _entry_visible(entry, ident) -> bool:
    if not _mp_active():
        return True
    ch = entry.get("channel", "public")
    if ch == "public" or ident["role"] == "dm":
        return True
    return ch == ident["user"] or ch in ident["chars"]
```

In `/chunk`, stamp the channel on the log entry. The cleanest single insertion point is the main narration path, right where the campaign stamp is added (~line 1244, just before `with _text_log_lock:`). Add channel derivation near the top of `chunk()` (after `data = request.get_json(...)`, ~line 1115):

```python
    audience_tag = str(data.get("audience") or "public").strip() or "public"
```

Then, in **each** of the 4 `log_entry` construction blocks (milestone ~1141, inspiration ~1161, xp ~1192, main narration ~1232/1244), add before the corresponding `_text_log.append`:

```python
    log_entry["channel"] = audience_tag
```

For the main narration path, place it alongside the existing `_camp` stamp block (~line 1248):

```python
    log_entry["channel"] = audience_tag
    # Stamp campaign on tail entries to prevent bleed when switching campaigns
    try:
        _camp_stamp = open(CAMP_FILE).read().strip()
        if _camp_stamp:
            log_entry["_camp"] = _camp_stamp
    except Exception:
        pass
```

(The audience also routes the live broadcast — wired in Task 6. Here we only tag the stored entry so replay can filter.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_channel_stream_scope.py::EntryVisibleTest -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_channel_stream_scope.py display/dnd-display-app.py
git commit -m "feat(display): channel-tag replay log entries + _entry_visible filter"
```

---

### Task 5: Scope the `/stream` on-connect catch-up + frontend `?device=`

Replace each global flush in `/stream` (~1948–1992) with an identity-scoped one. `ident` is already resolved in Task 2. Add `_scope_stats`.

**Policy note (product decision, documented default):** `_scope_stats` returns the full blob to the DM. To a player it returns their owned characters in full plus a public summary (name, race, class, level, current/max HP) of everyone else — enough for a shared party panel without leaking another player's full sheet, conditions, slots, or inventory. Adjust the public-field set to taste; the seam is the `_PUBLIC_STAT_FIELDS` constant.

**Files:**
- Modify: `display/dnd-display-app.py` (`/stream` on-connect block + new `_scope_stats`)
- Modify: `display/templates/index.html` (line ~4635 EventSource URL)
- Test: `tests/test_channel_stream_scope.py` (add `ScopeStatsTest`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_channel_stream_scope.py`:

```python
class ScopeStatsTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_app()
        self.mod._mp_active = lambda: True
        self.stats = {"players": [
            {"name": "Aldric", "race": "Human", "class": "Fighter", "level": 2,
             "hp": {"current": 10, "max": 18}, "conditions": ["Poisoned"],
             "sheet": {"inventory": ["Longsword"]}},
            {"name": "Mira", "race": "Elf", "class": "Rogue", "level": 2,
             "hp": {"current": 14, "max": 14}, "conditions": [],
             "sheet": {"inventory": ["Dagger"]}},
        ]}

    def _ident(self, user, role, chars):
        return {"user": user, "role": role, "chars": set(chars), "device": ""}

    def test_dm_gets_full_blob(self):
        out = self.mod._scope_stats(self.stats, self._ident("alice", "dm", []))
        self.assertEqual(out, self.stats)

    def test_player_gets_own_full_others_public(self):
        out = self.mod._scope_stats(self.stats, self._ident("bob", "player", ["Aldric"]))
        by_name = {p["name"]: p for p in out["players"]}
        # Own character: full sheet retained
        self.assertIn("sheet", by_name["Aldric"])
        self.assertEqual(by_name["Aldric"]["conditions"], ["Poisoned"])
        # Other character: public summary only, no sheet/conditions
        self.assertNotIn("sheet", by_name["Mira"])
        self.assertNotIn("conditions", by_name["Mira"])
        self.assertEqual(by_name["Mira"]["hp"], {"current": 14, "max": 14})
        self.assertEqual(by_name["Mira"]["class"], "Rogue")

    def test_inactive_mp_returns_full(self):
        self.mod._mp_active = lambda: False
        out = self.mod._scope_stats(self.stats, self._ident("bob", "player", ["Aldric"]))
        self.assertEqual(out, self.stats)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_channel_stream_scope.py::ScopeStatsTest -v`
Expected: FAIL — `module has no attribute '_scope_stats'`.

- [ ] **Step 3: Write minimal implementation**

Add near `_entry_visible`:

```python
_PUBLIC_STAT_FIELDS = ("name", "race", "class", "level", "hp")


def _scope_stats(stats, ident) -> dict:
    """DM: full blob. Player: own characters in full + public summary of others."""
    if not _mp_active() or ident["role"] == "dm":
        return stats
    players = stats.get("players", [])
    scoped = []
    for p in players:
        if p.get("name") in ident["chars"]:
            scoped.append(p)                       # own character, full
        else:
            scoped.append({k: p[k] for k in _PUBLIC_STAT_FIELDS if k in p})
    out = dict(stats)
    out["players"] = scoped
    return out
```

Now rewrite the `/stream` on-connect block. Replace the device-approval flush (~1989–1992) so it is DM-only:

```python
    # Send any pending device approval requests — DM only.
    if ident["role"] == "dm":
        with _devices_lock:
            for dev in list(_pending_devices.values()):
                q.put_nowait({"device_request": {"id": dev["id"], "ip": dev["ip"]}})
```

Replace the replay flush (~1955–1958):

```python
    with _text_log_lock:
        recent = [e for e in _text_log if _entry_visible(e, ident)]
    if recent:
        q.put_nowait({"replay_batch": recent})
```

Replace the stats flush (~1961–1963):

```python
    with _stats_lock:
        if _current_stats:
            q.put_nowait({"stats": _scope_stats(dict(_current_stats), ident)})
```

Replace the pending-input / staged / queue flushes (~1966–1978) so a player only sees their own and the DM sees all. Staged/pending/queue scoping keys off the `char`/owner field already present in those structures; when not identifiable, default to DM-only (private):

```python
    # Pending input + staged inputs + queue status: DM sees all; a player sees
    # only entries for characters they own. (When mp inactive, send everything.)
    def _mine(entry):
        if not _mp_active() or ident["role"] == "dm":
            return True
        owner = (entry or {}).get("char") or (entry or {}).get("name")
        return owner in ident["chars"]

    with _input_lock:
        if _input_queue:
            pend = [e for e in _input_queue if _mine(e)] if _mp_active() else list(_input_queue)
            if pend:
                q.put_nowait({"pending_input": pend})

    with _staged_lock:
        if _staged:
            snap = _staged_snapshot()
            if _mp_active() and ident["role"] != "dm":
                snap = [s for s in snap if _mine(s)]
            if snap:
                q.put_nowait({"staged_inputs": snap})

    with _queue_status_lock:
        if _queue_status:
            q.put_nowait({"queue_status": list(_queue_status)})
```

> **Verify before applying:** confirm the element shape of `_input_queue`, `_staged_snapshot()`, and `_queue_status` (grep their producers). If `_staged_snapshot()` returns a dict keyed by character rather than a list, adapt `_mine`/filtering accordingly. This is the one spot where the real structures must be checked against the snippet.

Scene, autorun_cycle, autorun_threshold flushes are public — leave unchanged.

Frontend, `display/templates/index.html` line ~4635:

```javascript
  evtSource = new EventSource('/stream?device=' + encodeURIComponent(_deviceId || ''));
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_channel_stream_scope.py -v`
Expected: PASS (EntryVisibleTest + ScopeStatsTest).

- [ ] **Step 5: Manual smoke test**

Run the display, open two browsers (one localhost = DM, one with a `?device=` mapped to a player via `ownership.json`), assign a DM in `state.md`, and confirm: player browser does not receive `device_request`, sees only its own staged input, and a `dm`-channel narration does not appear in its replay on reconnect.

Run: `bash display/start-display.sh` then exercise via `curl`/two browser tabs.
Expected: player connection's network log shows no `device_request` events; DM connection does.

- [ ] **Step 6: Full regression + commit**

```bash
python3 -m pytest tests/ -v
git add tests/test_channel_stream_scope.py display/dnd-display-app.py display/templates/index.html
git commit -m "feat(display): scope /stream on-connect catch-up by identity; close the broadcast leak"
```

**Phase 2 checkpoint:** the on-connect flood is closed. With a DM assigned, a player browser receives only public + own data; with none assigned, the catch-up is byte-for-byte the original.

---

## Phase 3 — Ergonomics + assistant-mode wiring

### Task 6: `send.py --audience` flag → live broadcast routing

So Claude can whisper to the DM (`--audience dm`) or to one character/user vs. broadcast to the table. `send.py` adds the flag; `/chunk` routes the live `_broadcast` by it (the stored entry was already tagged in Task 4).

**Files:**
- Modify: `display/send.py` (argparse + payload)
- Modify: `display/dnd-display-app.py` (`/chunk` — route `_broadcast` by audience)
- Test: `tests/test_send_audience.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_send_audience.py`:

```python
"""send.py --audience flows into the chunk payload; /chunk routes the broadcast."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class AudienceFlagTest(unittest.TestCase):
    def setUp(self):
        self.send = _load(DISPLAY / "send.py", "send_aud_under_test")

    def test_parser_accepts_audience(self):
        parser = self.send._build_parser()
        args = parser.parse_args(["--audience", "dm"])
        self.assertEqual(args.audience, "dm")

    def test_audience_default_is_none(self):
        parser = self.send._build_parser()
        args = parser.parse_args([])
        self.assertIsNone(args.audience)


class ChunkAudienceRouteTest(unittest.TestCase):
    def test_audience_to_set_helper(self):
        app = _load(DISPLAY / "dnd-display-app.py", "app_aud_under_test")
        self.assertEqual(app._audience_arg("all"), "all")
        self.assertEqual(app._audience_arg("dm"), "dm")
        self.assertEqual(app._audience_arg("Aldric"), {"Aldric"})
        self.assertEqual(app._audience_arg(None), "all")
        self.assertEqual(app._audience_arg("public"), "all")


if __name__ == "__main__":
    unittest.main()
```

> This task assumes `send.py`'s argparse setup is extracted into a `_build_parser()` function. If `main()` still builds the parser inline (it does today, ~line 365), the first step is a pure refactor: move the `argparse.ArgumentParser(...)` + all `add_argument` calls into `def _build_parser() -> argparse.ArgumentParser:` returning the parser, and have `main()` call `args = _build_parser().parse_args()`. Do that refactor, run `python3 -m pytest tests/test_display_robustness.py -v` to confirm nothing broke, commit it, then proceed.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_send_audience.py -v`
Expected: FAIL — `_build_parser` missing and/or `_audience_arg` missing.

- [ ] **Step 3: Write minimal implementation**

In `send.py`, inside `_build_parser()`, add alongside the other flags:

```python
    parser.add_argument("--audience", metavar="all|dm|<name>", default=None,
                        help="Route this send: 'all' (default broadcast), 'dm' "
                             "(private DM channel), or a character/user name.")
```

Where the chunk `payload` is assembled in `main()` (after content flags, ~line 545), add:

```python
            if args.audience:
                payload["audience"] = args.audience
```

In `dnd-display-app.py`, add the converter near `_visible_to`:

```python
def _audience_arg(value):
    """Map a send-side audience string to a _broadcast audience value."""
    if not value or value in ("all", "public"):
        return "all"
    if value == "dm":
        return "dm"
    return {str(value)}
```

In `/chunk`, route the live broadcast. The main narration path ends with `_broadcast(payload)` (~line 1259). Change it to:

```python
    _broadcast(payload, audience=_audience_arg(audience_tag))
    return "", 204
```

(`audience_tag` was computed at the top of `chunk()` in Task 4.) Apply the same `audience=_audience_arg(audience_tag)` to the milestone/inspiration/xp `_broadcast(payload)` calls (~1151, 1171, 1199) so awards can also be whispered if ever needed; default `"public"` → `"all"` keeps them broadcast.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_send_audience.py -v`
Expected: PASS.

- [ ] **Step 5: Full regression + commit**

```bash
python3 -m pytest tests/ -v
git add tests/test_send_audience.py display/send.py display/dnd-display-app.py
git commit -m "feat(display): send.py --audience routes narration to dm/character/all channels"
```

**Phase 3a checkpoint:** Claude can now `send.py --audience dm "<gm note>"` and only DM connections receive it, live and on replay.

---

### Task 7a: DM assignment command + `state.md` field

The server already reads `dm:` (`_session_dm`, Task 1). This task adds the write path and a status view. These are command procedures (file writes), not server code.

**Files:**
- Modify: `SKILL-commands.md` — add `/dnd dm` procedures
- Modify: `templates/state.md` — document the `dm:` Session Flag (optional, defaults absent)

- [ ] **Step 1: Document the field**

In `templates/state.md`, under `## Session Flags`, add a commented example so new campaigns show the seam:

```markdown
## Session Flags
- autorun_interval: 60
# - dm: <username>        # uncomment + set to hand creative authority to a human DM
```

- [ ] **Step 2: Add `/dnd dm` procedures to `SKILL-commands.md`**

Add a section specifying three procedures (no code — these are agent procedures Claude follows):

- `/dnd dm assign <user>` — write/replace the `dm:` line under `## Session Flags` in the loaded campaign's `state.md`. Confirm the user exists in `ownership.json` characters (warn if not). Announce: private channels are now active; Claude demotes to assistant (see 7b).
- `/dnd dm clear` — remove the `dm:` line. Announce: private filtering off; Claude resumes autonomous DM (current default behavior).
- `/dnd dm status` — read and report the current `dm:` value (or "no DM assigned — Claude is DM").

- [ ] **Step 3: Add `/dnd own` procedure**

Add a procedure to manage `ownership.json`:

- `/dnd own <user> <Character>` — create/append the user→character mapping in the campaign's `ownership.json`.
- `/dnd own device <device-id> <user>` — map an approved device UUID to a user. (Device UUIDs surface in the DM's device-approval cards.)

- [ ] **Step 4: Commit**

```bash
git add SKILL-commands.md templates/state.md
git commit -m "feat(dnd): /dnd dm + /dnd own commands for DM assignment and ownership map"
```

**Phase 3a checkpoint:** a human DM can be assigned/cleared at the table, flipping private filtering on and off — and the whole channel layer is exercised end to end.

---

### Task 7b: Assistant-mode prompt regime — GATED, do not build yet

This is the "real second prompt regime" (`multiplayer-design.md` → *Assistant mode*): Claude demotes from autonomous DM to compute-and-propose, streaming rulings/math/narration options into the private `dm` channel for the human to curate, and acting as combat coordinator. It is a `SKILL.md` change, not server plumbing — the server already supports it once Tasks 1–6 exist.

**It is deliberately deferred.** The design's own open items must be resolved by *watching a session run on the backbone first*:

1. **Player-facing combat UX** — what each player sees/taps on their turn is unspeced; Blake hasn't run combat live in his campaign yet.
2. **Autorun in assistant mode** — the hand-a-fight-back-to-Claude toggle is unspeced.
3. Whether this targets a remote group or an in-person table with a rotating human DM determines whether a DM authoring surface feeds the shared narration channel.

**Action:** after Phase 1–3a ship and one session has run with a DM assigned, hold a short brainstorming pass (superpowers:brainstorming) to resolve items 1–3 against observed behavior, then write a separate plan: `docs/superpowers/plans/<date>-assistant-mode-prompt-regime.md`. Do not write speculative `SKILL.md` authority logic before that session happens — it would encode guesses the design explicitly flagged as see-it-to-understand-it.

---

## Sequencing summary

| PR | Tasks | Ships | Risk |
|----|-------|-------|------|
| 1 | 1–3 | Identity + tagged registry + audience-aware broadcast | Low — defaults preserve behavior |
| 2 | 4–5 | Leak closure (the security-meaningful part) | Medium — touches `/stream` flood + `/chunk`; verify staged/queue shapes |
| 3 | 6, 7a | `--audience` routing + DM-assignment commands | Low |
| (later) | 7b | Assistant-mode prompt regime | Gated on a real session |

---

## Self-review

**Spec coverage (against both `/tmp` design docs):**
- Identity on connect (device + forward-auth) → Task 1 ✓
- Per-campaign ownership map (`user → character(s)`) → Task 1 (`ownership.json`) ✓
- DM capability vs. per-session assignment → Task 1 (`_session_dm`) + Task 7a (`/dnd dm`) ✓ (capability = membership in `ownership.json`; assignment = `dm:` flag)
- Tagged client registry → Task 2 ✓
- Audience-aware `_broadcast`, default `"all"` → Task 3 ✓
- On-connect catch-up scoping (replay, stats, device_request, staged/pending/queue) → Task 5 ✓
- Channel-tagged replay log → Task 4 ✓
- Send-side audience flag → Task 6 ✓
- Compute-then-commit gate / assistant mode → Task 7b (gated, with rationale) ✓ — server primitives exist after Task 6; the prompt regime is correctly deferred per the design's open items
- VTT canvas → explicitly out of scope (design doc defers it) ✓

**Placeholder scan:** No "TBD/handle appropriately" left. The one place requiring live verification (staged/queue element shape in Task 5) is called out explicitly with a grep instruction rather than guessed.

**Type/name consistency:** `_mp_active`, `_session_dm`, `_resolve_identity`, `_visible_to`, `_entry_visible`, `_scope_stats`, `_audience_arg`, client record keys `{"q","user","role","chars"}`, identity dict keys `{"user","device","role","chars"}`, `audience` values `"all"|"dm"|set`, channel tag string `"public"|"dm"|<name>` — used consistently across all tasks and tests.

**Known soft spots to verify during execution:**
- `_staged_snapshot()` / `_input_queue` / `_queue_status` element shapes (Task 5) — grep producers before applying the `_mine` filter.
- `send.py` parser refactor to `_build_parser()` (Task 6) is a prerequisite refactor; commit it separately first.
- `_find_campaign` return type (Path vs str) — `_scope_stats`/ownership use `os.path.join(str(cdir), ...)` to be agnostic.

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-05-multiplayer-channel-layer.md`.**
