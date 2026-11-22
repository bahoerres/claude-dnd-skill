# Assistant-Mode Prompt Regime (Task 7b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a human DM is assigned, Claude demotes to assistant (mechanics commit, story drafts to the GM screen) and the browser-only DM gets a private input lane that drives Claude's turns.

**Architecture:** One new Flask route (`/dm-input`) appends `[DM]:`-tagged lines to the existing `.input_queue` (autorun polls it at 0.3s — immediate fire, no ready-gate). A `you` identity event on SSE connect lets the frontend show a DM-only input bar. The behavioral regime is SKILL.md prose: two output lanes, numbered drafts via `send.py --audience dm`, conversational approval.

**Tech Stack:** Flask (display/dnd-display-app.py), vanilla JS in a single Jinja template, Python `unittest` (existing 183-test suite pattern), no new dependencies.

**Spec:** `docs/superpowers/plans/2026-06-06-assistant-mode-prompt-regime.md`. One refinement vs spec §4: the queue file (not a separate trigger mechanism) is the immediate path, because `autorun-wait.sh` polls `.input_queue` every 0.3s. The spec's intent ("existing pipe, immediate, ready-gate untouched") holds exactly.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `display/dnd-display-app.py` | Flask server | Add `you` catch-up event; add `/dm-input` route |
| `display/templates/index.html` | Browser UI | `you` handler, DM input bar (CSS+HTML+JS), `dm_input` echo status |
| `SKILL.md` | Claude's behavior | New `## Assistant Mode` section; autorun `[DM]:` line handling |
| `SKILL-commands.md` | Command docs | Replace the "does not yet auto-demote" note (line ~629) |
| `tests/test_dm_input.py` | New test module | All server tests for this feature |
| `CHANGELOG.md` | Release notes | Entry under Unreleased |

Background you need (verified against current code):

- `_resolve_identity(request)` → `{"user", "device", "role", "chars"}`; role is `"dm"` iff the mapped user equals the `dm:` Session Flag (plus a localhost-host convenience). Device id arrives as `X-DND-Device` header on POSTs. (`display/dnd-display-app.py:341`)
- `_mp_active()` → True iff a DM is assigned. (`display/dnd-display-app.py:336`)
- `QUEUE_FILE` (`.input_queue`) line format: `[CharName]: action text`, one per line, **no trailing newline** (see `_check_auto_trigger`, `display/dnd-display-app.py:417-448`). In autorun, `autorun-wait.sh` polls this file every 0.3s, consumes it, and returns the content as Claude's next turn input. In manual mode `wrapper.py` injects it on the host's next Enter.
- `_broadcast(payload, audience="all")` — audience `"dm"` reaches only DM-role SSE clients. (`display/dnd-display-app.py:1126`)
- `_catchup_payloads(ident)` builds the on-connect SSE replay. (`display/dnd-display-app.py:2190`)
- Test harness pattern: `tests/test_device_authz.py` — loads the app module by path, fakes a campaign dir + `ownership.json`, stubs `_token_ok`/`_broadcast`, uses Flask's test client.
- Run the suite with: `python3 -m unittest discover -s tests` from the repo root.

---

### Task 1: `you` identity event on SSE connect

The browser currently never learns its own role (filtering is server-side). The DM input bar needs it.

**Files:**
- Test: `tests/test_dm_input.py` (create)
- Modify: `display/dnd-display-app.py:2202-2206` (`_catchup_payloads`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_dm_input.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_dm_input -v`
Expected: both `YouEventTest` tests FAIL — `payloads[0]` is the `scene` payload, not `you`.

- [ ] **Step 3: Implement**

In `display/dnd-display-app.py`, `_catchup_payloads` — after `out: list = []` (line ~2203) and **before** the scene append, insert:

```python
    # Who-am-I — first, so the browser can gate DM-only UI (dm-input bar)
    # before any other render. Harmless extra key for old clients.
    out.append({"you": {"role": ident["role"], "user": ident["user"]}})
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_dm_input -v`
Expected: 2 tests PASS.

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python3 -m unittest discover -s tests`
Expected: all pass (183 + 2).

- [ ] **Step 6: Commit**

```bash
git add tests/test_dm_input.py display/dnd-display-app.py
git commit -m "feat(display): send 'you' identity event on SSE connect"
```

---

### Task 2: `POST /dm-input` route

**Files:**
- Test: `tests/test_dm_input.py` (extend)
- Modify: `display/dnd-display-app.py` (insert route after `/player-input` handler, line ~1944)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dm_input.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_dm_input -v`
Expected: all `DmInputRouteTest` tests FAIL with 404 (route doesn't exist).

- [ ] **Step 3: Implement the route**

In `display/dnd-display-app.py`, insert after the `/player-input` handler (after line ~1944, before `/device/approve`):

```python
@app.route("/dm-input", methods=["POST"])
def dm_input():
    """Private DM→Claude input lane (assistant mode — channel-layer Task 7b).

    Body: {"text": "..."}. Only the assigned DM may post (same write-path
    authz pattern as /device/approve). The line is appended to .input_queue
    tagged `[DM]:` — in autorun, autorun-wait.sh polls that file every 0.3s,
    so DM input fires Claude's turn immediately WITHOUT touching the staged
    ready-gate (_staged is never read or swept here). Echo goes to the dm
    channel only; players see nothing.
    """
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(request.remote_addr):
        return "Too Many Requests", 429
    # Meaningless outside multiplayer; role check covers the localhost-host
    # convenience (a direct localhost browser with no mapping is the trusted
    # operator and may post once a DM is assigned).
    if not _mp_active() or _resolve_identity(request)["role"] != "dm":
        return "Forbidden", 403

    data = request.get_json(force=True, silent=True) or {}
    text = _sanitize_input(str(data.get("text", "")))
    if not text:
        return "Bad Request", 400

    line = f"[DM]: {text}"
    try:
        existing = ""
        if os.path.exists(QUEUE_FILE):
            existing = open(QUEUE_FILE).read().rstrip("\n")
        with open(QUEUE_FILE, "w") as f:
            f.write(f"{existing}\n{line}" if existing else line)
    except Exception:
        return "Error", 500

    _broadcast({"dm_input": {"text": text, "timestamp": _time.time()}},
               audience="dm")
    return "", 204
```

(`_time`, `os`, `_sanitize_input`, `_rate_ok` are already imported/defined at module level — verify, don't re-import.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_dm_input -v`
Expected: all 9 tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest discover -s tests`
Expected: all pass (183 + 9).

- [ ] **Step 6: Commit**

```bash
git add tests/test_dm_input.py display/dnd-display-app.py
git commit -m "feat(display): /dm-input — private DM→Claude lane, immediate-fire, dm-scoped echo"
```

---

### Task 3: DM input bar in the browser

No JS test harness exists in this repo — this task is implemented, then verified manually (Step 4).

**Files:**
- Modify: `display/templates/index.html` (CSS ~line 1477, HTML ~line 1696, JS ~lines 4053, 4642+)

- [ ] **Step 1: Add CSS**

In the `<style>` block, immediately after the `#dm-help-btn` rules (after line ~1477):

```css
  /* ── DM input bar (assistant mode — visible only to the dm role) ── */
  #dm-input-bar {
    display: none;            /* shown via JS when you.role === 'dm' */
    position: fixed;
    bottom: 12px;
    left: 50%;
    transform: translateX(-50%);
    width: min(680px, 80vw);
    gap: 8px;
    align-items: center;
    z-index: 60;
    background: rgba(20, 14, 6, 0.92);
    border: 1px solid rgba(180, 140, 60, 0.45);
    border-radius: 8px;
    padding: 8px 10px;
  }
  #dm-input-text {
    flex: 1;
    background: transparent;
    border: none;
    outline: none;
    color: rgba(230, 215, 180, 0.95);
    font: inherit;
  }
  #dm-input-send {
    color: rgba(220, 185, 100, 1);
    background: none;
    border: 1px solid rgba(210, 170, 80, 0.6);
    border-radius: 6px;
    padding: 4px 12px;
    cursor: pointer;
    font: inherit;
  }
  #dm-input-status {
    font-size: 0.8em;
    color: rgba(180, 140, 60, 0.8);
    white-space: nowrap;
  }
```

- [ ] **Step 2: Add HTML**

Immediately after the DM Help button (line ~1696, `<button id="dm-help-btn" ...>`):

```html
<div id="dm-input-bar">
  <input id="dm-input-text" type="text" autocomplete="off"
         placeholder="DM → Claude (players never see this)…" />
  <span id="dm-input-status"></span>
  <button id="dm-input-send" title="Send privately to Claude">Send</button>
</div>
```

- [ ] **Step 3: Add JS**

(a) Near the device-id setup (after line ~4056, `localStorage.setItem('dnd_device_id', _deviceId);`):

```javascript
// ── DM input bar (assistant mode) ──────────────────────────────────────────
let _myRole = 'player';
const _dmBar    = document.getElementById('dm-input-bar');
const _dmText   = document.getElementById('dm-input-text');
const _dmSend   = document.getElementById('dm-input-send');
const _dmStatus = document.getElementById('dm-input-status');

async function _sendDmInput() {
  const text = _dmText.value.trim();
  if (!text) return;
  _dmSend.disabled = true;
  _dmStatus.textContent = '…';
  try {
    const res = await fetch('/dm-input', {
      method: 'POST',
      headers: _authHeaders(),
      body: JSON.stringify({ text }),
    });
    if (res.ok) {
      _dmText.value = '';
      _dmStatus.textContent = '✓ delivered';
    } else {
      _dmStatus.textContent = `✗ ${res.status}`;
    }
  } catch (_) {
    _dmStatus.textContent = '✗ offline';
  }
  _dmSend.disabled = false;
  _dmText.focus();
}
_dmSend.addEventListener('click', _sendDmInput);
_dmText.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); _sendDmInput(); }
});
```

Note: `_authHeaders()` (defined ~line 4060) already sends `X-DND-Token` + `X-DND-Device`. If `_authHeaders` is defined *below* the insertion point, place this block after it — the listeners only fire post-load either way.

(b) In `evtSource.onmessage` (the `if (payload.X)` chain starting line ~4646), add alongside the other handlers:

```javascript
    if (payload.you) {
      _myRole = payload.you.role;
      _dmBar.style.display = (_myRole === 'dm') ? 'flex' : 'none';
    }
    if (payload.dm_input) {
      // Echo from the dm channel (covers a second DM-role screen, e.g. host)
      _dmStatus.textContent = '✓ delivered';
    }
```

- [ ] **Step 4: Manual verification**

```bash
# Terminal 1 — start the display server
python3 display/dnd-display-app.py
```

1. Ensure the loaded campaign's `state.md` has a `## Session Flags` section with `- dm: <user>` (or use a scratch campaign; `scripts/mp_admin.py dm-assign <user>` does it).
2. Open `http://localhost:5001` — localhost with no device mapping resolves to DM: **the bar must be visible**.
3. Type "test whisper" + Enter — expect `✓ delivered`, and `~/.claude/skills/dnd/display/.input_queue` containing `[DM]: test whisper`.
4. Remove the `dm:` line, reload. **Expected: the bar is still visible on localhost, but a send returns `✗ 403`.** (The localhost convenience in `_resolve_identity` reports role "dm" for the host screen even with no DM assigned, so the bar shows — but `/dm-input` checks `_mp_active()` and rejects. Acceptable asymmetry: localhost is the trusted host screen; LAN players never see the bar.)
5. Open from a phone/LAN browser mapped to a non-DM user — bar hidden.

- [ ] **Step 5: Run the full suite (template change can't break it, but cheap)**

Run: `python3 -m unittest discover -s tests`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add display/templates/index.html
git commit -m "feat(display): DM input bar — dm-role-gated private lane UI"
```

---

### Task 4: The regime itself — SKILL.md + SKILL-commands.md

**Files:**
- Modify: `SKILL.md` (insert new section between the Autorun section and `## XP Awards`, line ~334)
- Modify: `SKILL-commands.md:629` (the "does not yet auto-demote" note)

- [ ] **Step 1: Add the Assistant Mode section to SKILL.md**

Insert before `## XP Awards` (line ~335):

```markdown
## Assistant Mode (human DM assigned)

Check `state.md → ## Session Flags` for a `dm:` line on every `/dnd load` and at each turn boundary. If present, a human DM holds creative authority and **you are the assistant, not the DM**. If absent (or after `/dnd dm clear`), run as autonomous DM exactly as before — everything in this section applies ONLY while `dm:` is set.

**The one rule:** *if it's deterministic, commit it; if it's words, draft it.*

| Lane | Examples | Where it goes |
|------|----------|---------------|
| **Mechanics** (deterministic) | dice results, HP/slot/condition deltas, stat updates, initiative math | Commit directly: `send.py` / `push_stats.py` to the shared display, as normal |
| **Story** (words) | narration, scene text, NPC dialogue, check reveals, options | Numbered draft to the GM screen ONLY: `send.py --audience dm`. NEVER to the shared display without DM approval |

**Draft protocol.** Number drafts sequentially per session (#1, #2, …). Every draft ends with the affordance hint so the DM never has to remember syntax:

```bash
python3 ~/.claude/skills/dnd/display/send.py --audience dm \
  "**#3 · reveal → Vesper** — *Fresh scratch marks ring the lock — someone forced this door, recently and badly.*
_reply to send it, change it, or drop it_"
```

**DM input is authoritative.** Lines tagged `[DM]:` in the input queue (from autorun-wait.sh or check_input.py) are the human DM speaking privately to you. Their narrative statements are canon — update state and respond (with drafts/mechanics) accordingly, on the dm channel. Treat approval language conversationally: "send 3", "the scratch marks one", "3 but creepier", "drop it", "send 3 to everyone" all work. **If you are not sure which draft or what action the DM meant, ask on the dm channel — never guess-and-commit.** On approval, send the (possibly edited) text via `send.py` to the draft's target audience. Drafts are conversational state: when the scene has clearly moved on, retire or re-offer stale drafts rather than blindly sending them.

**Skill checks.** The DC stays on the dm channel — players never see it. Roll outcomes may commit publicly as ✓/✗ per character (bare numbers only if the DM says so). Write the reveal *per passer* as separate drafts (different passers can get different slices). On approval, whisper each with `send.py --audience <character>`.

**Take the wheel.** If the DM says to take over ("take the wheel", "run this room, then hand back"), run as autonomous DM — story lane commits directly to the shared display — until the DM says they're back or the named boundary arrives. The `dm:` flag does not change; this is a posture, not a mode switch.

**Autorun integration.** Autorun works normally in assistant mode. `[DM]:` lines in the AUTORUN output are the DM lane (handle per above); `[CharName]:` lines remain player actions. A mixed batch = resolve player mechanics, then address the DM's input. Player staged input still respects the ready-gate; DM input arrives immediately.
```

- [ ] **Step 2: Update the SKILL-commands.md note**

At `SKILL-commands.md:629`, replace:

```markdown
> **Note:** assigning a human DM is intended to also shift Claude from autonomous DM to assistant/combat-coordinator. That prompt regime is a separate, in-progress piece (see the channel-layer plan, Task 7b). Today, assigning a DM gives you the private-channel plumbing — whispers, scoped stats, the GM screen, DM-only approvals — but Claude does not yet auto-demote.
```

with:

```markdown
> **Note:** assigning a human DM also shifts Claude from autonomous DM to assistant: mechanics commit, story goes to the GM screen as numbered drafts for DM approval, and the DM drives Claude through the private dm-input bar. See `SKILL.md → ## Assistant Mode`. Combat coordination remains a follow-on spec.
```

- [ ] **Step 3: Re-read both edits in place**

Run: `grep -n "Assistant Mode" SKILL.md SKILL-commands.md`
Expected: the new section heading in SKILL.md and the pointer in SKILL-commands.md.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md SKILL-commands.md
git commit -m "feat(dnd): assistant-mode prompt regime — claude computes, the DM commits"
```

---

### Task 5: Close out

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/plans/2026-06-05-multiplayer-channel-layer.md` (Task 7b status)

- [ ] **Step 1: Full suite, one last time**

Run: `python3 -m unittest discover -s tests`
Expected: all pass (183 + 9 new).

- [ ] **Step 2: CHANGELOG entry**

Add under the Unreleased/top section of `CHANGELOG.md` (match the file's existing format):

```markdown
- Assistant mode (Task 7b): with a human DM assigned, Claude demotes to
  compute-and-propose — mechanics commit, story drafts to the GM screen for
  conversational approval. New `/dm-input` route + DM-only input bar give a
  browser-only DM a private lane that fires Claude's turn immediately.
```

- [ ] **Step 3: Mark 7b shipped in the master plan**

In `docs/superpowers/plans/2026-06-05-multiplayer-channel-layer.md`, in the sequencing table (line ~1037), change the 7b row's status note from `Gated on a real session` to `Shipped — see 2026-06-06-assistant-mode-implementation.md`. Leave the Task 7b body text intact (it documents why it was gated; history is useful).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md docs/superpowers/plans/2026-06-05-multiplayer-channel-layer.md
git commit -m "docs: changelog + master-plan status for assistant mode (7b)"
```

- [ ] **Step 5: Live-session validation (not part of this branch's done-criteria)**

The regime's behavioral half is validated at the table, per the spec §9 checklist: mechanics commit unprompted; no story reaches players un-approved; drafts numbered with hint; ambiguity → clarify; DC private; take-the-wheel engages/disengages; `/dnd dm clear` restores autonomy. Failures become SKILL.md fixes, not server changes.

---

## Out of scope (per spec §8)

Combat coordinator UX (own spec) · player↔player PMs · Approach B button-approval queue (trigger: approve-fatigue) · v2 `dice_request` DC leak (prerequisite when this regime ports to v2).
