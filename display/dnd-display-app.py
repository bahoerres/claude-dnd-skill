"""
app.py — DnD DM display server

Receives text chunks from wrapper.py, detects scene context from keywords,
and pushes both to the browser via Server-Sent Events.

Endpoints:
    GET  /                   → serves index.html
    POST /chunk              → receives text chunk from wrapper.py
    POST /stats              → receives character/combat stat updates (merged, persisted)
    GET  /stream             → SSE stream to browser (text + scene + stats events)
    GET  /ping               → health check
    POST /clear              → wipe text log and broadcast clear event
    POST /player-input         → legacy queue endpoint (check_input.py compat)
    POST /player-input/drain   → drain legacy queue (check_input.py compat)
    POST /player-input/stage   → stage an action for review before firing
    POST /player-input/ready   → mark a staged action as ready
    POST /player-input/unstage → remove a staged action
    POST /player-input/skip    → skip a character's turn (stages + readies a skip entry)
    GET  /srd-lookup           → look up a spell/item/feature/condition by name
"""

import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
from collections import deque
from typing import Optional
from flask import Flask, Response, request, render_template, jsonify, send_from_directory
from flask_cors import CORS

LOG_FILE      = os.path.expanduser("~/.claude/skills/dnd/display/text_log.json")
SCRIPTS_DIR   = os.path.expanduser("~/.claude/skills/dnd/scripts")

# SRD lookup module — degrades silently if dataset not built
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
try:
    import lookup as _lookup
    _SRD_AVAILABLE = True
except Exception:
    _lookup = None          # type: ignore
    _SRD_AVAILABLE = False

from paths import find_campaign as _find_campaign

# Audio module — degrades silently if numpy not installed
_AUDIO_DIR = os.path.dirname(os.path.abspath(__file__))
import sys as _sys
if _AUDIO_DIR not in _sys.path:
    _sys.path.insert(0, _AUDIO_DIR)
try:
    import audio as _audio
    _audio.init()
except Exception:
    _audio = None   # type: ignore
HELP_LOCK     = os.path.expanduser("~/.claude/skills/dnd/display/.help-lock")
CAMP_FILE     = os.path.expanduser("~/.claude/skills/dnd/display/.campaign")
STATS_FILE    = os.path.expanduser("~/.claude/skills/dnd/display/stats.json")
TOKEN_FILE    = os.path.expanduser("~/.claude/skills/dnd/display/.token")
INPUT_FILE    = os.path.expanduser("~/.claude/skills/dnd/display/player_input.json")
TRIGGER_FILE  = os.path.expanduser("~/.claude/skills/dnd/display/.input_trigger")
QUEUE_FILE    = os.path.expanduser("~/.claude/skills/dnd/display/.input_queue")
DEVICES_FILE         = os.path.expanduser("~/.claude/skills/dnd/display/.approved_devices.json")
PENDING_DEVICES_FILE = os.path.expanduser("~/.claude/skills/dnd/display/.pending_devices.json")

# ─── LAN / TLS mode ───────────────────────────────────────────────────────────
# Pass --lan to bind on 0.0.0.0 and protect write endpoints with a token.
# Pass --tls (requires --lan) to enable HTTPS with a self-signed cert.
# Without --lan the server binds to localhost only; no token is required.

_LAN_MODE: bool = "--lan" in sys.argv
_TLS_MODE: bool = "--tls" in sys.argv
if _LAN_MODE:
    sys.argv.remove("--lan")   # prevent Flask from seeing an unknown flag
if _TLS_MODE:
    sys.argv.remove("--tls")


def _get_or_create_token() -> str:
    """Load or generate the LAN token. Upgrades short legacy tokens to 64-char."""
    try:
        token = open(TOKEN_FILE).read().strip()
        if len(token) >= 48:   # 48+ chars = already long enough
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_hex(32)   # 64-char hex — brute force infeasible
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    os.chmod(TOKEN_FILE, 0o600)
    return token


_lan_token: Optional[str] = _get_or_create_token() if _LAN_MODE else None


# ─── Rate limiting ────────────────────────────────────────────────────────────
# Simple in-process sliding window: max 20 write requests per IP per minute.
# Prevents spam injection and brute-force token guessing on write endpoints.

import time as _time

_rate_buckets: dict[str, list] = {}
_rate_lock = threading.Lock()
_RATE_WINDOW = 60    # seconds
_RATE_MAX    = 20    # requests per window per IP


def _rate_ok(ip: str) -> bool:
    now = _time.time()
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(ip, []) if now - t < _RATE_WINDOW]
        if len(bucket) >= _RATE_MAX:
            return False
        bucket.append(now)
        _rate_buckets[ip] = bucket
    return True


# ─── Input validation helpers ─────────────────────────────────────────────────

_PRINTABLE    = re.compile(r"[^\x20-\x7E]")
_SHELL_CHARS  = re.compile(r'[$`\\;|&><()\[\]{}!]')
_CHAR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '\-]{0,48}[A-Za-z]$|^[A-Za-z]$")


def _sanitize_input(text: str) -> str:
    """Strip control chars and shell metacharacters from player input text."""
    text = _SHELL_CHARS.sub("", text)
    text = _PRINTABLE.sub("", text)
    return text[:500].strip()


def _char_ok(name: str, known: set) -> bool:
    """Return True if character name is syntactically valid and in the party."""
    if not _CHAR_NAME_RE.match(name):
        return False
    if known and name not in known and name != "Everybody":
        return False
    return True


# ─── Device approval system ───────────────────────────────────────────────────
# Each browser generates a UUID device ID (localStorage). On first input attempt
# from an unseen LAN device, the request is held and the DM sees an Approve/Deny
# card on the display. Localhost is auto-approved. Denied devices are blocked for
# the session.

_approved_devices: set[str]       = set()
_denied_devices:   set[str]       = set()
_pending_devices:  dict[str, dict] = {}  # device_id -> {ip, first_seen}
_devices_lock = threading.Lock()


def _persist_approved_devices() -> None:
    """Persist approved devices to disk. Must be called WITHOUT _devices_lock held."""
    try:
        with _devices_lock:
            data = list(_approved_devices)
        with open(DEVICES_FILE, "w") as f:
            json.dump(data, f)
        os.chmod(DEVICES_FILE, 0o600)
    except Exception:
        pass


def _load_approved_devices() -> None:
    try:
        with open(DEVICES_FILE) as f:
            data = json.load(f)
        with _devices_lock:
            for d in data:
                _approved_devices.add(str(d))
    except Exception:
        pass


def _persist_pending_devices() -> None:
    """Persist pending devices to disk so they survive app restarts. Must be called WITHOUT _devices_lock held."""
    try:
        with _devices_lock:
            data = list(_pending_devices.values())
        with open(PENDING_DEVICES_FILE, "w") as f:
            json.dump(data, f)
        os.chmod(PENDING_DEVICES_FILE, 0o600)
    except Exception:
        pass


def _load_pending_devices() -> None:
    try:
        with open(PENDING_DEVICES_FILE) as f:
            data = json.load(f)
        with _devices_lock:
            for d in data:
                if isinstance(d, dict) and d.get("id"):
                    # Skip if already approved/denied during this run
                    if d["id"] not in _approved_devices and d["id"] not in _denied_devices:
                        _pending_devices[d["id"]] = d
    except Exception:
        pass


_load_approved_devices()
_load_pending_devices()


def _device_ok(device_id: str, ip: str) -> str:
    """Return 'approved', 'pending', or 'denied' for a given device."""
    if not device_id:
        return "denied"
    _need_persist_approved = False
    _need_persist_pending  = False
    with _devices_lock:
        if device_id in _approved_devices:
            return "approved"
        if device_id in _denied_devices:
            return "denied"
        # Localhost always auto-approved
        if ip in ("127.0.0.1", "::1"):
            _approved_devices.add(device_id)
            _need_persist_approved = True
        # New LAN device — hold and notify DM
        elif device_id not in _pending_devices:
            _pending_devices[device_id] = {
                "id":         device_id,
                "ip":         ip,
                "first_seen": _time.time(),
            }
            _need_persist_pending = True
            _broadcast({"device_request": {"id": device_id, "ip": ip}}, audience="dm")
    # Persist outside the lock to avoid deadlock (Lock is not reentrant)
    if _need_persist_approved:
        _persist_approved_devices()
        return "approved"
    if _need_persist_pending:
        _persist_pending_devices()
    return "pending"


# ─── Channel / audience layer: identity + ownership ──────────────────────────
# Private filtering is OPT-IN. It engages only when a DM is assigned for the
# session (the `dm:` flag in state.md). With no DM assigned, _mp_active() is
# False and the server behaves exactly like the single-broadcast original.

# Forward-auth (the X-Remote-User header) is an OPT-IN upgrade, trusted only
# when a reverse proxy is the sole authenticating ingress. Off by default —
# v1 identity is device-UUID pairing, and an unconditionally-trusted header
# would let any direct LAN client claim to be the DM. Enable with
# DND_FORWARD_AUTH=1; restrict which source addresses may set the header with
# DND_TRUSTED_PROXIES (comma-separated; defaults to localhost, where the proxy
# sits in a typical forward-auth deployment).
_FORWARD_AUTH = (os.environ.get("DND_FORWARD_AUTH", "").strip().lower()
                 not in ("", "0", "false", "no", "off"))
_TRUSTED_PROXIES = set(filter(None, (
    os.environ.get("DND_TRUSTED_PROXIES") or "127.0.0.1,::1").split(",")))


def _campaign_dir():
    """Resolve the current campaign directory via the canonical paths helper."""
    try:
        with open(CAMP_FILE) as _fh:
            name = _fh.read().strip()
    except Exception:
        return None
    if not name:
        return None
    try:
        return _find_campaign(name)
    except Exception:
        return None


def _ownership() -> dict:
    """Per-campaign device->user and user->characters map. Missing file = empty."""
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
    chars = _ownership()["characters"].get(user)
    if not isinstance(chars, (list, tuple, set)):
        return set()       # missing, null, or a stray string -> no characters
    return set(chars)


def _session_dm():
    """Read the `dm:` assignment from the state.md Session Flags section.

    Scoped to the `## Session Flags` block so a stray `dm:` line in free-form
    prose elsewhere (e.g. the `## DM Notes` section) can't spuriously activate
    multiplayer or pick a garbage DM. None = no DM assigned.
    """
    cdir = _campaign_dir()
    if cdir is None:
        return None
    try:
        with open(os.path.join(str(cdir), "state.md")) as _fh:
            text = _fh.read()
    except Exception:
        return None
    # Isolate the Session Flags section: from its header to the next "## " or EOF.
    sec = re.search(r"(?ms)^##\s+Session Flags\s*$(.*?)(?=^##\s|\Z)", text)
    if not sec:
        return None
    # Match a bullet `dm:` flag; capture stops at a newline or an inline `#` comment.
    m = re.search(r"(?mi)^\s*[-*]?\s*dm:[ \t]*([^#\n]+?)[ \t]*(?:#.*)?$", sec.group(1))
    if not m:
        return None
    return m.group(1).strip() or None


def _mp_active() -> bool:
    """Private filtering engages only when a DM is assigned for the session."""
    return _session_dm() is not None


def _resolve_identity(req) -> dict:
    """Resolve the identity of an SSE connection.

    v1 (default): the device UUID the browser stores in localStorage, passed on
    the EventSource URL as ?device=. Forward-auth upgrade (opt-in via
    DND_FORWARD_AUTH): a trusted X-Remote-User header, honored ONLY when the
    connection originates from a configured trusted-proxy address — the proxy is
    then the sole authenticating ingress. An unguarded header would let any
    direct LAN client claim DM.
    """
    device_id = req.args.get("device") or ""
    remote = req.remote_addr or ""
    user = None
    if _FORWARD_AUTH and remote in _TRUSTED_PROXIES:
        user = req.headers.get("X-Remote-User") or None
    if not user:
        user = _device_user_map().get(device_id)
    dm = _session_dm()
    role = "dm" if (user and user == dm) else "player"
    # Backward-compat host convenience: a direct localhost connection with no
    # mapping is the trusted operator's own screen -> DM, so the host keeps full
    # visibility. Disabled under forward-auth, where localhost is the proxy (and
    # would otherwise promote every unauthenticated pass-through to DM).
    if user is None and not _FORWARD_AUTH and remote in ("127.0.0.1", "::1"):
        role = "dm"
    return {"user": user, "device": device_id, "role": role,
            "chars": _owned_characters(user)}


# ─── Staged input system ──────────────────────────────────────────────────────
# Players stage their actions from the display companion UI. When all expected
# players mark ready, the combined action is written to TRIGGER_FILE for
# wrapper.py to inject into Claude's PTY stdin.

_staged: dict[str, dict] = {}   # {char_name: {text, ready, timestamp}}
_staged_lock = threading.Lock()
_expected_count = 1             # updated when stats arrive; min 1
_autorun_threshold: Optional[int] = None  # overrides _expected_count when set via push_stats --autorun-threshold

# Tracks which character names are currently sitting in .input_queue waiting
# for the DM to press Enter. Set when queue is written, cleared when wrapper
# POSTs /queue/consumed after injection. Persists through page reloads via SSE
# initial data and is broadcast to all connected clients on change.
_queue_status: list = []
_queue_status_lock = threading.Lock()

# Last autorun cycle broadcast — replayed on SSE reconnect so late-joining
# clients start the countdown from the correct elapsed position.
# Cleared when autorun_waiting=false (turn resolved or autorun disabled).
_autorun_cycle: Optional[dict] = None
_autorun_cycle_lock = threading.Lock()


def _normalize_slot(slot: dict) -> None:
    """Coerce a spell-slot entry to the canonical {used, max} shape in place.

    Tolerates legacy/alt payloads that use `remaining` instead of `used`.
    Without this, _slot_use/_slot_restore raise KeyError on a slot stored
    under the alt schema (e.g. after a long-rest --spell-slots full-replace).
    """
    if "used" in slot:
        return
    mx = slot.get("max", 0)
    if "remaining" in slot:
        slot["used"] = max(mx - int(slot.get("remaining", 0)), 0)
    else:
        slot["used"] = 0


def _staged_snapshot() -> dict:
    """Return a serialisable copy of the staged dict (no IP field)."""
    return {k: {"text": v["text"], "ready": v["ready"]} for k, v in _staged.items()}


def _check_auto_trigger() -> None:
    """Move staged-and-ready actions into the DM-gated queue file (.input_queue).

    .input_queue is NOT injected immediately — wrapper.py picks it up the next
    time the DM presses Enter (or Claude explicitly triggers via .input_trigger).
    This gives the DM control over when player actions enter Claude's context.
    """
    with _staged_lock:
        if not _staged:
            return
        everybody_ready = "Everybody" in _staged and _staged["Everybody"]["ready"]
        all_ready       = all(v["ready"] for v in _staged.values())
        threshold       = _autorun_threshold if _autorun_threshold is not None else _expected_count
        enough          = len(_staged) >= threshold or everybody_ready
        if not (all_ready and enough):
            return
        char_names = list(_staged.keys())
        lines      = [f'[{c}]: {e["text"]}' for c, e in _staged.items()]
        content    = "\n".join(lines)
        _staged.clear()

    try:
        with open(QUEUE_FILE, "w") as f:
            f.write(content)
    except Exception:
        char_names = []

    if char_names:
        with _queue_status_lock:
            _queue_status.clear()
            _queue_status.extend(char_names)
    _broadcast({"staged_inputs": {}, "queue_status": list(char_names)})


def _token_ok() -> bool:
    """Return True if the request carries the correct LAN token (or we're in localhost mode)."""
    if _lan_token is None:
        return True   # localhost mode — no token required
    provided = request.headers.get("X-DND-Token", "")
    return hmac.compare_digest(provided, _lan_token)


app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
CORS(app)

# Wire audio broadcast after _broadcast is defined (see bottom of file)
# — done lazily via set_broadcast() called after app is created.

# ─── Scene definitions ────────────────────────────────────────────────────────
# Each scene: keywords (weighted — more = higher priority hit),
# gradient colors [top, bottom], accent color, particle type, display label.

SCENES: dict[str, dict] = {
    "tavern": {
        "keywords": [
            "tavern", "inn", "guttered", "common room", "hearth",
            "fireplace", "ale", "mead", "barkeep", "innkeeper",
            "candle", "tallow", "flagon", "stool", "bar",
        ],
        "colors": ["#1a0800", "#2e1400"],
        "accent": "#c8601a",
        "particles": "embers",
        "label": "The Inn",
    },
    "dungeon": {
        "keywords": [
            "dungeon", "corridor", "stone floor", "torch", "iron gate",
            "portcullis", "cell", "shackle", "pit", "dank",
        ],
        "colors": ["#080818", "#12082e"],
        "accent": "#6a3aaa",
        "particles": "dust",
        "label": "The Dungeon",
    },
    "mine": {
        "keywords": [
            "mine", "seam", "shaft", "tunnel", "ore", "pickaxe",
            "foreman", "deep seam", "ashstone", "cart", "vein",
        ],
        "colors": ["#0a0a0a", "#1a1008"],
        "accent": "#806040",
        "particles": "dust",
        "label": "The Mine",
    },
    "cave": {
        "keywords": [
            "cave", "cavern", "stalactite", "stalagmite", "underground",
            "grotto", "dripping", "echo", "subterranean",
        ],
        "colors": ["#0a1520", "#0a1030"],
        "accent": "#2060a0",
        "particles": "mist",
        "label": "The Cavern",
    },
    "forest": {
        "keywords": [
            "forest", "wood", "tree", "branch", "leaves", "undergrowth",
            "hollow wood", "canopy", "root", "bark", "moss", "fern",
            "thicket", "grove",
        ],
        "colors": ["#041008", "#081a04"],
        "accent": "#40a040",
        "particles": "leaves",
        "label": "The Forest",
    },
    "castle": {
        "keywords": [
            "castle", "rampart", "battlement", "keep", "parapet",
            "drawbridge", "moat", "throne", "great hall", "manor",
        ],
        "colors": ["#0e0e1a", "#1a1a2e"],
        "accent": "#8080c0",
        "particles": "dust",
        "label": "The Castle",
    },
    "mountain": {
        "keywords": [
            "mountain", "snow", "peak", "blizzard", "frost", "glacier",
            "avalanche", "ridge", "cliff", "altitude", "wind",
        ],
        "colors": ["#0a1020", "#1a2040"],
        "accent": "#a0c0e0",
        "particles": "snow",
        "label": "The Mountains",
    },
    "ocean": {
        "keywords": [
            "ocean", "sea", "ship", "wave", "sailor", "port", "harbour",
            "dock", "tide", "storm", "mast", "hull", "water",
        ],
        "colors": ["#000d1a", "#001a33"],
        "accent": "#0060a0",
        "particles": "ripples",
        "label": "The Sea",
    },
    "desert": {
        "keywords": [
            "desert", "sand", "dune", "oasis", "scorching", "arid",
            "mirage", "camel", "sphinx",
        ],
        "colors": ["#1a0f00", "#2e1a00"],
        "accent": "#c08030",
        "particles": "sand",
        "label": "The Desert",
    },
    "ruins": {
        "keywords": [
            "ruins", "ruin", "crumble", "crumbling", "rubble", "ancient",
            "overgrown", "collapsed", "forgotten", "desolate", "remnant",
        ],
        "colors": ["#100e04", "#1e1a08"],
        "accent": "#806830",
        "particles": "dust",
        "label": "The Ruins",
    },
    "swamp": {
        "keywords": [
            "swamp", "marsh", "bog", "mud", "murky", "fetid", "reed",
            "mire", "sludge", "stagnant",
        ],
        "colors": ["#080e04", "#0e1808"],
        "accent": "#406020",
        "particles": "mist",
        "label": "The Swamp",
    },
    "crypt": {
        "keywords": [
            "crypt", "tomb", "grave", "coffin", "undead", "bones",
            "skeleton", "lich", "mausoleum", "burial", "sarcophagus",
            "dead", "death",
        ],
        "colors": ["#08000a", "#140014"],
        "accent": "#602060",
        "particles": "smoke",
        "label": "The Crypt",
    },
    "fire": {
        "keywords": [
            "fire", "flame", "burn", "blaze", "inferno", "conflagration",
            "ember", "char", "smoke", "ash cloud",
        ],
        "colors": ["#1a0500", "#2e0800"],
        "accent": "#ff4400",
        "particles": "embers",
        "label": "The Fire",
    },
    "arcane": {
        "keywords": [
            "arcane", "magic", "spell", "enchant", "rune", "glyph",
            "mystical", "ritual", "incantation", "ward", "sigil",
            "thaumaturgy", "sorcery",
        ],
        "colors": ["#080020", "#12003a"],
        "accent": "#8040ff",
        "particles": "sparks",
        "label": "The Arcane",
    },
    "city": {
        "keywords": [
            "city", "market", "street", "crowd", "village", "town",
            "square", "cobble", "district", "quarter", "merchant",
            "ashenveil",
        ],
        "colors": ["#0a0f1a", "#15202e"],
        "accent": "#6080a0",
        "particles": "rain",
        "label": "The Town",
    },
    "night": {
        "keywords": [
            "night", "midnight", "moon", "star", "dark sky",
            "constellation", "celestial", "dusk", "twilight",
        ],
        "colors": ["#000008", "#04000f"],
        "accent": "#4060a0",
        "particles": "stars",
        "label": "The Night",
    },
    "temple": {
        "keywords": [
            "temple", "shrine", "altar", "holy", "sacred", "chapel",
            "prayer", "cleric", "incense", "lantern", "pew", "nave",
            "pale flame",
        ],
        "colors": ["#0e0c18", "#1a1428"],
        "accent": "#c0a060",
        "particles": "smoke",
        "label": "The Temple",
    },
}

# Priority order — checked in sequence; first match wins per chunk
SCENE_PRIORITY = [
    "mine", "crypt", "arcane", "fire", "temple", "dungeon", "cave",
    "forest", "swamp", "castle", "ocean", "mountain", "desert", "ruins",
    "tavern", "city", "night",
]

# ─── ANSI / TUI chrome stripping ─────────────────────────────────────────────

class _ANSIState:
    """Character-by-character ANSI escape-sequence state machine.

    Regex approaches fail when the PTY delivers bytes one at a time, splitting
    sequences like \\x1b[4;2m across chunk boundaries.  This state machine
    carries its state across calls so cross-chunk splits are handled correctly.

    States
    ------
    normal   → emitting regular characters
    esc      → saw ESC (0x1B), waiting to see what kind of sequence follows
    csi      → inside CSI sequence (ESC [ … letter)
    osc      → inside OSC sequence (ESC ] … BEL or ST)
    osc_esc  → inside OSC, just saw ESC — might be the ST terminator (ESC \\)
    """

    __slots__ = ("_s",)

    def __init__(self) -> None:
        self._s: str = "normal"

    def feed(self, text: str) -> str:
        out: list[str] = []
        s = self._s
        for ch in text:
            c = ord(ch)
            if s == "normal":
                if c == 0x1B:
                    s = "esc"
                elif c >= 0x20 or c in (0x09, 0x0A):   # printable / tab / newline
                    out.append(ch)
                # else: other control char (bell, etc.) — discard
            elif s == "esc":
                if ch == "[":
                    s = "csi"
                elif ch == "]":
                    s = "osc"
                else:
                    s = "normal"    # 2-char ESC sequence; discard both bytes
            elif s == "csi":
                if 0x40 <= c <= 0x7E:   # final byte of CSI
                    s = "normal"
                elif c == 0x1B:         # unexpected ESC — start fresh
                    s = "esc"
                # else: parameter / intermediate byte, keep consuming
            elif s == "osc":
                if c == 0x07:           # BEL terminates OSC
                    s = "normal"
                elif c == 0x1B:
                    s = "osc_esc"
                # else: OSC payload, keep consuming
            elif s == "osc_esc":
                s = "normal" if ch == "\\" else "osc"
        self._s = s
        return "".join(out)


_ansi = _ANSIState()
_ansi_lock = threading.Lock()

_BOX_CHARS = set("╭╮╰╯│─┌┐└┘├┤┬┴┼━═║╔╗╚╝")
_BOX_CHAR_STRIP = "╭╮╰╯│─┌┐└┘├┤┬┴┼━═║╔╗╚╝"  # same set as string for str.strip()

# Characters used by Claude CLI spinner / prompt / UI
_SPINNER_CHARS = set("✽⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒◌◎●")
_PROMPT_STARTS = ("❯", ">", "·", "▸", "ℹ", "✓", "⚠", "✗", "⟳", "↳")


def _handle_cr(text: str) -> str:
    """Handle carriage returns the way a real terminal would.

    Two distinct cases:
      \\r\\n  — a real newline (\\r\\n line ending).  Normalise to \\n first
                so the content is preserved.
      bare \\r — cursor-to-column-0 for in-place token updates.  Claude CLI
                streams each token by rewriting the current line:
                  "The" → \\r"The Gut" → \\r"The Gutte" → …
                Keep only the last segment (= the final written state).
    """
    # Step 1: treat \\r\\n as a real newline — must come before bare-\\r logic
    text = text.replace("\r\n", "\n")

    # Step 2: handle remaining bare \\r (in-place rewrites)
    lines = text.split("\n")
    result = []
    for line in lines:
        if "\r" in line:
            parts = line.split("\r")
            result.append(parts[-1])   # last segment = final state of the line
        else:
            result.append(line)
    return "\n".join(result)


def _strip_ansi(text: str) -> str:
    text = _handle_cr(text)
    with _ansi_lock:
        text = _ansi.feed(text)
    return text


def _is_chrome(line: str) -> bool:
    """Return True for lines that are TUI chrome, not DM narration.

    The Claude CLI wraps responses in a box:
        ╭──────────────────╮
        │ narration text   │
        ╰──────────────────╯
    We strip the border characters from line edges first so that content
    lines like "│ The tavern smells of ale │" are NOT filtered — only pure
    border rows (all box chars, no letters) are treated as chrome.
    """
    stripped = line.strip()

    if not stripped:
        return False   # keep blank lines — they separate paragraphs

    # Strip leading/trailing box-drawing border chars to expose the real content.
    # "│ The tavern smells of ale │" → "The tavern smells of ale"
    content = stripped.strip(_BOX_CHAR_STRIP + " ")

    # If nothing remains, the line was entirely box-drawing chrome (a border row).
    if not content:
        return True

    # All remaining checks operate on content (without box border decoration).
    c = content

    # CLI prompt / spinner lines
    if c[0] in _SPINNER_CHARS:
        return True
    if c.startswith(_PROMPT_STARTS):
        return True

    # Common spinner word patterns (e.g. "Thinking…")
    if re.match(r"^[A-Z][a-z]+ing…?$", c):
        return True

    # Claude branding / metadata
    if "claude.ai" in c.lower():
        return True

    # Session-resume instructions emitted at end of response
    if c.startswith("Resume this session with:") or re.match(r"^claude\s+--resume\s+", c):
        return True

    # Status-bar patterns: cost, token counts, rate-limit bars
    # Note: "Tokens300/0" has no space — use \s* not \s+
    if re.search(r"Tokens\s*\d|5hr:|7d:|Session:|Total:\s*\$", c):
        return True

    # Model/plan header lines ("Sonnet 4.6", "Claude Pro", "Professional", etc.)
    if re.search(r"Sonnet|Haiku|Opus|Claude\s*(Pro|Max|Team|Code)\b|Professional\b|claude-\d", c, re.I):
        return True

    # Tool-use labels emitted by Claude CLI ("Bash command", "Read command", etc.)
    if re.match(r"^(Bash|Read|Write|Edit|Glob|Grep|WebFetch|WebSearch|TaskCreate|TaskUpdate|TaskGet|TaskList|NotebookEdit|Agent|ToolSearch|ExitPlanMode|EnterPlanMode|ScheduleWakeup|Monitor|RemoteTrigger|CronCreate|CronDelete|CronList|AskUserQuestion)(\s+(command|tool|result|call))?$", c, re.I):
        return True

    # Timestamp-prefixed lines ("3ts ago …", "2m ago …") — UI timestamps concatenated with content
    if re.match(r"^\d+\s*[smhdt]+s?\s*(ago\s*)?[A-Z]", c):
        return True

    # Bare numbers (token counts, cursor column positions, etc.)
    if re.match(r"^\d+$", c):
        return True

    # Single stray characters that are ANSI/escape remnants, not real words
    if len(c) == 1 and not c.isalpha():
        return True

    # Very short non-alpha fragments (≤3 chars with no letters = not narration)
    if len(c) <= 3 and not re.search(r"[a-zA-Z]{2}", c):
        return True

    return False


def _clean(text: str) -> str:
    text = _strip_ansi(text)
    lines = text.split("\n")
    kept = []
    for line in lines:
        if _is_chrome(line):
            continue
        # Strip box-border chars from edges so content reaches the browser clean.
        s = line.strip().strip(_BOX_CHAR_STRIP + " ")
        # Blank line → preserve as paragraph separator
        kept.append(s if s else "")
    # Collapse runs of more than two consecutive blank lines
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return result


# ─── Scene detection ──────────────────────────────────────────────────────────

_current_scene_name: str = "tavern"   # default — we start in the inn
_scene_buffer: list[str] = []
_BUFFER_WINDOW = 20   # analyse last N cleaned chunks together


def _detect_scene(text: str) -> Optional[dict]:
    global _current_scene_name, _scene_buffer

    _scene_buffer.append(text.lower())
    if len(_scene_buffer) > _BUFFER_WINDOW:
        _scene_buffer.pop(0)

    window = " ".join(_scene_buffer)

    scores: dict[str, int] = {}
    for scene_name in SCENE_PRIORITY:
        scene = SCENES[scene_name]
        score = sum(window.count(kw) for kw in scene["keywords"])
        if score > 0:
            scores[scene_name] = score

    if not scores:
        return None

    best = max(scores, key=lambda k: scores[k])
    if best == _current_scene_name:
        return None   # no change

    _current_scene_name = best
    return SCENES[best] | {"name": best}


# ─── SSE client registry ─────────────────────────────────────────────────────

_clients: list[dict] = []   # records: {"q": Queue, "user", "role", "chars": set}
_clients_lock = threading.Lock()

# ─── Text replay log ──────────────────────────────────────────────────────────
# Stores the last N cleaned text chunks so late-connecting browsers can catch up.
# Persisted to LOG_FILE so it survives Flask restarts (Chromecast reconnects, new sessions).
_text_log: deque = deque(maxlen=60)
_text_log_lock = threading.Lock()

# ─── Session tail buffer ──────────────────────────────────────────────────────
# Rolling buffer of the last 30 text events — written to session_tail.json after
# every /chunk POST so it survives crashes. Read at /dnd load for display replay.
# Path is campaign-specific so tails from different campaigns don't overwrite each other.
#
# ROBUSTNESS GUARANTEES (after the 2026-05-01 wipe-bug fix):
#   1. _load_tail is NON-DESTRUCTIVE: it never wipes the in-memory buffer based
#      on an empty/filtered-out load. If the file is empty, missing, or every
#      entry is filtered out by campaign mismatch, the existing buffer stays.
#   2. _persist_tail SKIPS ON EMPTY: it never overwrites an existing non-empty
#      file with an empty buffer. This breaks the "filter zeros buffer →
#      persist writes [] → file lost" failure chain.
#   3. _persist_tail uses ATOMIC WRITES: writes to a tempfile and atomically
#      renames into place, so a partial/crashed write can never produce a
#      truncated or zero-byte file.
#   4. The legacy fallback path is GONE. Tails only ever land in the campaign-
#      specific file. If CAMP_FILE is missing/empty when persist would fire,
#      we keep the buffer in memory and skip the write rather than dropping
#      events into a shared file that bleeds across campaigns.
_tail_buffer: deque = deque(maxlen=30)
_tail_lock   = threading.Lock()


def _get_tail_file() -> "str | None":
    """Return the campaign-specific tail path, or None if no campaign is set.

    Previously this fell back to a process-local path on the skill side. That
    fallback caused tail bleed across campaigns and made the wipe-on-load bug
    much harder to diagnose. The new contract: campaign-specific or nothing.
    """
    try:
        camp = open(CAMP_FILE).read().strip()
        if camp:
            return str(_find_campaign(camp) / "session_tail.json")
    except Exception:
        pass
    return None


def _persist_tail() -> None:
    """Write _tail_buffer to disk. Refuses to overwrite content with empty.

    Atomic-write guarantee: writes to <path>.tmp then renames, so observers
    (the next /dnd load reading the file) never see a partial or zero-byte
    state.
    """
    path = _get_tail_file()
    if not path:
        # No active campaign — keep the buffer in memory, skip disk.
        return
    try:
        with _tail_lock:
            data = list(_tail_buffer)
        # Skip-on-empty guard: never blank a file that currently has content.
        if not data and os.path.exists(path):
            try:
                if os.path.getsize(path) > 2:  # 2 bytes = "[]"
                    print(f"_persist_tail: skipping empty write — {path} has content",
                          file=sys.stderr)
                    return
            except OSError:
                pass
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception as e:
        print(f"_persist_tail: write failed: {e}", file=sys.stderr)


def _load_tail() -> None:
    """Load tail from disk into the buffer. NON-DESTRUCTIVE on empty/mismatch.

    Old behavior: cleared the buffer first, then re-appended filtered entries.
    This created the wipe bug: if every entry was filtered out (campaign
    mismatch) the buffer ended up empty and the next persist wrote [] to disk.

    New behavior: build the candidate buffer first, then ONLY swap it into
    place if at least one entry survived filtering. If nothing survives, the
    in-memory buffer is left alone — preserves whatever was already loaded.
    """
    path = _get_tail_file()
    if not path:
        return
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return  # No file yet — keep in-memory state
    except (OSError, json.JSONDecodeError) as e:
        print(f"_load_tail: read failed for {path}: {e}", file=sys.stderr)
        return
    if not isinstance(data, list):
        print(f"_load_tail: file content is not a list — leaving buffer alone",
              file=sys.stderr)
        return

    try:
        current_camp = open(CAMP_FILE).read().strip()
    except Exception:
        current_camp = ""

    candidate: list = []
    for item in data[-30:]:
        if not isinstance(item, dict):
            continue
        item_camp = item.get("_camp", "")
        # If we know the campaign and the entry stamps a different campaign,
        # skip it. Entries with no stamp are kept (legacy data + tolerance).
        if current_camp and item_camp and item_camp != current_camp:
            continue
        candidate.append(item)

    if not candidate:
        # Loaded data filtered down to nothing. DO NOT replace the buffer —
        # this is the wipe-bug guard.
        return

    with _tail_lock:
        _tail_buffer.clear()
        for item in candidate:
            _tail_buffer.append(item)


_load_tail()


def _persist_log() -> None:
    """Write the current text log to disk. Called after every chunk."""
    try:
        with _text_log_lock:
            data = list(_text_log)
        with open(LOG_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _load_log() -> None:
    """Load a previously persisted text log on startup.
    Handles both old string format and new dict format."""
    try:
        with open(LOG_FILE) as f:
            data = json.load(f)
        with _text_log_lock:
            _text_log.clear()
            for item in data[-60:]:
                # Migrate old plain-string entries to dict format
                if isinstance(item, str):
                    item = {"text": item}
                _text_log.append(item)
    except Exception:
        pass


_load_log()


# ─── Character / combat stats ─────────────────────────────────────────────────
# Stored as {"players": [...], "turn_order": {...}|null}
# Players are merged by name so partial updates (just HP, just XP) work.

_current_stats: dict = {}
_stats_lock = threading.Lock()


def _persist_stats() -> None:
    try:
        with _stats_lock:
            data = dict(_current_stats)
        with open(STATS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _load_stats() -> None:
    global _expected_count
    try:
        with open(STATS_FILE) as f:
            data = json.load(f)
        with _stats_lock:
            _current_stats.update(data)
        # Initialise expected player count from persisted stats so solo-mode
        # detection is correct immediately after restart, without waiting for
        # the next /stats POST.
        loaded_players = data.get("players", [])
        if loaded_players:
            _expected_count = max(1, len(loaded_players))
    except Exception:
        pass


_load_stats()


# ─── Player input queue ───────────────────────────────────────────────────────
# Stores actions submitted from the display companion (iPad etc.) until the DM
# triggers the next turn. Drained by check_input.py via /player-input/drain.

_input_queue: list[dict] = []
_input_lock = threading.Lock()


def _load_input_queue() -> None:
    global _input_queue
    try:
        with open(INPUT_FILE) as f:
            _input_queue = json.load(f)
    except Exception:
        _input_queue = []


def _persist_input_queue() -> None:
    try:
        with open(INPUT_FILE, "w") as f:
            json.dump(_input_queue, f)
    except Exception:
        pass


_load_input_queue()


def _broadcast(payload: dict, audience="all") -> None:
    # audience: "all" | "dm" | set of usernames/character names
    active = _mp_active()            # resolve once, OUTSIDE the lock (no per-client disk I/O)
    with _clients_lock:
        dead = []
        for c in _clients:
            if not _visible_to(c, audience, active):
                continue
            try:
                c["q"].put_nowait(payload)
            except queue.Full:
                dead.append(c)
        for c in dead:
            _clients.remove(c)


def _visible_to(client, audience, mp_active=None) -> bool:
    # When private filtering is off, everything is public (current behavior).
    if mp_active is None:
        mp_active = _mp_active()
    if not mp_active:
        return True
    if audience == "all":
        return True
    if client["role"] == "dm":
        return True                      # DM sees everything
    if audience == "dm":
        return False                     # DM-only, this client is a player
    if audience is None:
        return False
    # audience is a set of usernames / character names. Guard the bare-string
    # footgun: set("Mira") -> {'M','i','r','a'}; a lone name means {name}.
    aud = {audience} if isinstance(audience, str) else set(audience)
    return bool(client["chars"] & aud) or client["user"] in aud


def _audience_arg(value):
    """Map a send-side audience string to a _broadcast audience value:
    "all"/"public"/empty/non-string -> "all"; "dm" -> "dm"; any other name -> {name}.
    A bare name addresses a character OR a user of that name (shared namespace —
    do not name a user identically to a different character)."""
    if not isinstance(value, str):
        return "all"
    v = value.strip()
    if not v or v in ("all", "public"):
        return "all"
    if v == "dm":
        return "dm"
    return {v}


def _entry_visible(entry, ident, mp_active=None) -> bool:
    # Replay-log filter: mirrors _visible_to but reads each entry's channel tag.
    if mp_active is None:
        mp_active = _mp_active()
    if not mp_active:
        return True
    ch = entry.get("channel", "public")
    if ch == "public" or ident["role"] == "dm":
        return True
    if ch == "dm":
        return False                         # reserved DM token: real DMs admitted above
    return ch == ident["user"] or ch in ident["chars"]


# Fields of a player's stat entry that are legitimately shared at the table (the
# sidebar party summary). Everything else (sheet, inventory, spell_slots, effects,
# conditions, hit_dice, ...) is private to the owner. Adjust this set to widen or
# narrow what the shared panel exposes — it is the public-field policy seam.
_PUBLIC_STAT_FIELDS = ("name", "race", "class", "level", "hp")


def _scope_stats(stats, ident) -> dict:
    """DM (or filtering off): the full blob. A player: their own characters in
    full + a public summary (_PUBLIC_STAT_FIELDS) of everyone else, so the party
    panel still works without leaking another player's sheet."""
    if not _mp_active() or ident.get("role") == "dm":
        return stats
    players = stats.get("players")
    if not isinstance(players, list):
        return stats
    chars = ident.get("chars") or set()
    scoped = []
    for p in players:
        if not isinstance(p, dict):
            scoped.append(p)
        elif p.get("name") in chars:
            scoped.append(p)                                  # own character: full
        else:
            scoped.append({k: p[k] for k in _PUBLIC_STAT_FIELDS if k in p})
    out = dict(stats)
    out["players"] = scoped
    return out


def _broadcast_stats(current: dict) -> None:
    """Broadcast a stats payload, scoped per connection when filtering is on.
    Falls back to a plain broadcast when no DM is assigned (byte-for-byte
    backward-compat). Client records double as the identity for _scope_stats."""
    if not _mp_active():
        _broadcast({"stats": current})
        return
    with _clients_lock:
        dead = []
        for c in _clients:
            try:
                c["q"].put_nowait({"stats": _scope_stats(current, c)})
            except queue.Full:
                dead.append(c)
        for c in dead:
            _clients.remove(c)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Pass LAN token to template so the browser can authenticate /help-request
    return render_template("index.html", lan_token=_lan_token or "")


@app.route("/icons/<path:filename>")
def serve_icon(filename):
    """Serve icons, favicon, and brand assets from display/icons/."""
    _icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    return send_from_directory(_icons_dir, filename)


@app.route("/favicon.ico")
def favicon():
    _icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    return send_from_directory(_icons_dir, "favicon.ico",
                               mimetype="image/vnd.microsoft.icon")


@app.route("/srd-lookup")
def srd_lookup():
    """Look up a spell, item, condition, feature, or monster by name.

    Query params:
        name      — the name to look up (required)
        category  — spell | item | equipment | magic_item | condition | monster | feature (optional)
        level     — character level (1–20); collapses scale progressions to the matching entry

    Returns JSON: {"found": bool, "name": str, "category": str, "text": str}
    """
    name     = request.args.get("name", "").strip()[:120]
    category = request.args.get("category", "").strip().lower() or None
    level_s  = request.args.get("level", "").strip()
    level    = int(level_s) if level_s.isdigit() and 1 <= int(level_s) <= 20 else None
    if not name:
        return jsonify({"found": False, "error": "name required"}), 400
    if not _SRD_AVAILABLE or _lookup is None:
        return jsonify({"found": False, "error": "SRD dataset not loaded"}), 503

    text = _lookup.lookup_with_level(name, category=category, level=level)
    if text:
        rec = _lookup.lookup_record(name, category=category)
        resolved_cat = (rec or {}).get("_cat", category or "")
        return jsonify({"found": True, "name": name, "category": resolved_cat, "text": text})
    # Not found — return wikidot fallback URL so the frontend can offer a link
    wurl = _lookup.wikidot_url(name, category=category)
    return jsonify({"found": False, "name": name, "wikidot_url": wurl})


@app.route("/ping")
def ping():
    return "ok", 200


@app.route("/health")
def health():
    """Server-side integrity probe used by send.py --verify and external monitors.

    Returns the live counts the send-side cares about:
      - alive: always True if the route runs
      - tail_buffer: number of entries currently in the rolling tail
      - tail_file_size: size in bytes of the on-disk session_tail.json
      - text_log: number of entries in the replay log
      - campaign: the active campaign name (empty if none set)
      - clients: connected SSE clients

    No auth required — this is a liveness/monitoring endpoint, no PII or game
    content is exposed.
    """
    try:
        camp = open(CAMP_FILE).read().strip()
    except Exception:
        camp = ""
    tail_path = _get_tail_file()
    try:
        tail_size = os.path.getsize(tail_path) if tail_path and os.path.exists(tail_path) else 0
    except OSError:
        tail_size = 0
    with _tail_lock:
        tail_count = len(_tail_buffer)
    with _text_log_lock:
        log_count = len(_text_log)
    with _clients_lock:
        client_count = len(_clients)
    return {
        "alive": True,
        "tail_buffer": tail_count,
        "tail_file_size": tail_size,
        "tail_path": tail_path or "",
        "text_log": log_count,
        "campaign": camp,
        "clients": client_count,
    }, 200


@app.route("/chunk", methods=["POST"])
def chunk():
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    # Channel tag for the stored replay entry: "public" (default) | "dm" | a
    # character/user name. Lets _entry_visible filter the on-connect replay so a
    # private (e.g. dm-channel) line never replays to a player who shouldn't see it.
    _aud = data.get("audience")
    audience_tag = _aud.strip() if isinstance(_aud, str) and _aud.strip() else "public"
    raw = data.get("text", "")
    if not raw:
        return "", 204

    is_action      = bool(data.get("action"))
    is_player      = bool(data.get("player"))
    is_npc         = bool(data.get("npc"))
    is_dice        = bool(data.get("dice"))
    is_tutor       = bool(data.get("tutor"))
    is_inspiration = bool(data.get("inspiration_award"))
    is_milestone_award = bool(data.get("milestone_award"))
    is_milestone_spend = bool(data.get("milestone_spend"))
    is_xp_award    = bool(data.get("xp_award"))

    # ── Milestone award/spend (stack-based reward, distinct from binary Inspiration) ──
    if is_milestone_award or is_milestone_spend:
        name = str(data.get("milestone_award") or data.get("milestone_spend") or "").strip()[:80]
        label = str(data.get("label") or "Milestone").strip()[:40]
        if not name:
            return "", 204
        payload: dict = {
            "milestone_award" if is_milestone_award else "milestone_spend": name,
            "label": label,
            "text": name,
        }
        log_entry: dict = dict(payload)
        if is_milestone_award and data.get("reason"):
            payload["reason"] = str(data["reason"]).strip()[:240]
            log_entry["reason"] = payload["reason"]
        log_entry["channel"] = audience_tag
        with _text_log_lock:
            _text_log.append(log_entry)
        with _tail_lock:
            _tail_buffer.append(log_entry)
        _persist_log()
        _persist_tail()
        _broadcast(payload, audience=_audience_arg(audience_tag))
        return "", 204

    # Inspiration and XP awards carry no text from stdin — build synthetic text.
    if is_inspiration:
        name = str(data.get("inspiration_award", "")).strip()[:80]
        if not name:
            return "", 204
        reason = str(data.get("reason", "")).strip()[:240]
        payload: dict = {"inspiration_award": name, "text": name}
        log_entry: dict = {"inspiration_award": name, "text": name}
        if reason:
            payload["reason"] = reason
            log_entry["reason"] = reason
        log_entry["channel"] = audience_tag
        with _text_log_lock:
            _text_log.append(log_entry)
        with _tail_lock:
            _tail_buffer.append(log_entry)
        _persist_log()
        _persist_tail()
        _broadcast(payload, audience=_audience_arg(audience_tag))
        # Also update player inspiration state in stats.
        # NOTE: _persist_stats() acquires _stats_lock internally — capture the snapshot
        # inside the lock, then call persist/broadcast OUTSIDE to avoid deadlock.
        stats_snapshot = None
        with _stats_lock:
            players = _current_stats.setdefault("players", [])
            match = next((p for p in players if p.get("name", "").lower() == name.lower()), None)
            if match:
                match["inspiration"] = True
                stats_snapshot = dict(_current_stats)
        if stats_snapshot is not None:
            _persist_stats()
            _broadcast_stats(stats_snapshot)
        return "", 204

    if is_xp_award:
        xp_data = data.get("xp_award", {})
        if not isinstance(xp_data, dict):
            return "", 204
        payload = {"xp_award": xp_data, "text": xp_data.get("summary", "")}
        log_entry = {"xp_award": xp_data, "text": xp_data.get("summary", "")}
        log_entry["channel"] = audience_tag
        with _text_log_lock:
            _text_log.append(log_entry)
        with _tail_lock:
            _tail_buffer.append(log_entry)
        _persist_log()
        _persist_tail()
        _broadcast(payload, audience=_audience_arg(audience_tag))
        return "", 204

    # Player/npc/dice/tutor/action text comes from send.py (no ANSI/chrome) — light clean only.
    # DM narration may come from wrapper.py — full clean.
    cleaned = raw.strip() if (is_action or is_player or is_npc or is_dice or is_tutor) else _clean(raw)
    if not cleaned.strip():
        return "", 204

    payload: dict = {"text": cleaned}

    if is_action:
        payload["action"] = data["action"]
    elif is_player:
        payload["player"] = data["player"]
    elif is_npc:
        payload["npc"] = data["npc"]
    elif is_dice:
        payload["dice"] = True
    elif is_tutor:
        payload["tutor"] = True
    else:
        # Scene detection only on DM narration
        scene = _detect_scene(cleaned)
        if scene:
            payload["scene"] = scene
            if _audio:
                _audio.on_scene_change(scene["name"])
        # SFX scan on all non-player text
        if _audio:
            _audio.on_text(cleaned)

    # Store full typed payload so replay preserves action/player/npc/dice/tutor context
    log_entry: dict = {"text": cleaned}
    if is_action:
        log_entry["action"] = data["action"]
    elif is_player:
        log_entry["player"] = data["player"]
    elif is_npc:
        log_entry["npc"] = data["npc"]
    elif is_dice:
        log_entry["dice"] = True
    elif is_tutor:
        log_entry["tutor"] = True
    log_entry["channel"] = audience_tag

    # Stamp campaign on tail entries to prevent bleed when switching campaigns
    try:
        _camp_stamp = open(CAMP_FILE).read().strip()
        if _camp_stamp:
            log_entry["_camp"] = _camp_stamp
    except Exception:
        pass

    with _text_log_lock:
        _text_log.append(log_entry)
    with _tail_lock:
        _tail_buffer.append(log_entry)

    _persist_log()
    _persist_tail()
    _broadcast(payload, audience=_audience_arg(audience_tag))
    return "", 204


@app.route("/stats", methods=["POST"])
def stats():
    """Receive character/combat stat updates. Merges players by name, replaces turn_order.

    Pass replace_players=true to replace the entire player list (use on /dnd load to
    prevent stale characters from a previous campaign persisting in the sidebar).
    """
    if not _token_ok():
        return "Forbidden", 403
    data = request.get_json(silent=True) or {}
    if not data:
        return "", 204

    _effect_expire_events: list[dict] = []
    with _stats_lock:
        if "players" in data:
            # replace_players=true wipes the list first — used on campaign load
            if data.get("replace_players"):
                _current_stats["players"] = []
            existing_players: list = _current_stats.setdefault("players", [])
            for incoming in data["players"]:
                name = incoming.get("name")
                if not name:
                    continue
                match = next((p for p in existing_players if p.get("name") == name), None)
                # Keys prefixed with _ are mutation ops, not stored fields
                _MUTATION_KEYS = {
                    "_inventory_add", "_inventory_remove",
                    "_conditions_add", "_conditions_remove",
                    "_slot_use", "_slot_restore",
                    "_hd_use", "_hd_restore",
                    "_effect_start", "_effect_end",
                    "_sheet_spells",
                    "_milestone_inc", "_milestone_dec",
                }
                if match:
                    for key, val in incoming.items():
                        if key == "_inventory_add":
                            inv = match.setdefault("sheet", {}).setdefault("inventory", [])
                            if val not in inv:
                                inv.append(val)
                        elif key == "_inventory_remove":
                            sheet = match.get("sheet", {})
                            sheet["inventory"] = [
                                i for i in sheet.get("inventory", [])
                                if i.lower() != str(val).lower()
                            ]
                        elif key == "_conditions_add":
                            conds = match.setdefault("conditions", [])
                            if val not in conds:
                                conds.append(val)
                        elif key == "_conditions_remove":
                            match["conditions"] = [
                                c for c in match.get("conditions", [])
                                if c.lower() != str(val).lower()
                            ]
                        elif key == "_slot_use":
                            slots = match.setdefault("spell_slots", {})
                            lvl = str(val)
                            slot = slots.setdefault(lvl, {"used": 0, "max": 0})
                            _normalize_slot(slot)
                            slot["used"] = min(slot["used"] + 1, slot.get("max", 99))
                        elif key == "_slot_restore":
                            slots = match.setdefault("spell_slots", {})
                            lvl = str(val)
                            slot = slots.setdefault(lvl, {"used": 0, "max": 0})
                            _normalize_slot(slot)
                            slot["used"] = max(slot["used"] - 1, 0)
                        elif key == "_hd_use":
                            hd = match.setdefault("hit_dice", {"remaining": 0, "max": 0})
                            hd["remaining"] = max(hd.get("remaining", 0) - 1, 0)
                        elif key == "_hd_restore":
                            hd = match.setdefault("hit_dice", {"remaining": 0, "max": 0})
                            hd["remaining"] = min(
                                hd.get("remaining", 0) + int(val),
                                hd.get("max", 99)
                            )
                        elif key == "_effect_start":
                            # val is an effect dict: {name, duration_type, ...}
                            spell_name = val.get("name", "")
                            effects = match.setdefault("effects", [])
                            # Replace any existing effect with the same name
                            match["effects"] = [
                                e for e in effects
                                if e.get("name", "").lower() != spell_name.lower()
                            ]
                            match["effects"].append(val)
                            # Sync concentration field if this is a conc effect
                            if val.get("concentration") and spell_name:
                                match["concentration"] = spell_name
                        elif key == "_effect_end":
                            # val is the spell name string
                            spell_lower = str(val).lower()
                            removed = [
                                e for e in match.get("effects", [])
                                if e.get("name", "").lower() == spell_lower
                            ]
                            match["effects"] = [
                                e for e in match.get("effects", [])
                                if e.get("name", "").lower() != spell_lower
                            ]
                            # If the ended effect was concentration, also clear it
                            if removed and any(e.get("concentration") for e in removed):
                                if match.get("concentration", "").lower() == spell_lower:
                                    match["concentration"] = None
                        elif key == "_sheet_spells":
                            # Patch only the spells sub-key inside sheet
                            sheet = match.setdefault("sheet", {})
                            sheet["spells"] = val
                        elif key == "inspiration" and val is False:
                            match["inspiration"] = False
                        elif key == "_milestone_inc":
                            # Stack-based reward counter — Inspiration variants,
                            # homebrew Hero Coins, Bardic Inspiration tokens, etc.
                            # The label string is the value; per-label cap optional.
                            label = str(val) or "Milestone"
                            ms = match.setdefault("milestones", {})
                            cap = match.get("milestone_caps", {}).get(label, 99)
                            ms[label] = min(ms.get(label, 0) + 1, cap)
                        elif key == "_milestone_dec":
                            label = str(val) or "Milestone"
                            ms = match.setdefault("milestones", {})
                            ms[label] = max(ms.get(label, 0) - 1, 0)
                            if ms.get(label, 0) == 0:
                                ms.pop(label, None)
                        elif isinstance(val, dict) and isinstance(match.get(key), dict):
                            match[key].update(val)
                        else:
                            match[key] = val
                else:
                    # Strip mutation ops — they're meaningless for new players
                    existing_players.append(
                        {k: v for k, v in incoming.items() if k not in _MUTATION_KEYS}
                    )

        # turn_order replaces entirely (None = clear); also ticks round-based effects
        _effect_expire_events: list[dict] = []
        if "turn_order" in data:
            new_to = data["turn_order"]
            _current_stats["turn_order"] = new_to
            # Decrement round-based effects for the actor whose turn just started
            if new_to and isinstance(new_to, dict) and new_to.get("current"):
                actor = new_to["current"].lower()
                for p in _current_stats.get("players", []):
                    if p.get("name", "").lower() != actor:
                        continue
                    kept, expired = [], []
                    for eff in p.get("effects", []):
                        if eff.get("duration_type") == "rounds":
                            eff = dict(eff)  # don't mutate in-place
                            eff["duration_remaining"] = max(0, eff.get("duration_remaining", 1) - 1)
                            if eff["duration_remaining"] <= 0:
                                expired.append(eff)
                            else:
                                kept.append(eff)
                        else:
                            kept.append(eff)
                    p["effects"] = kept
                    for eff in expired:
                        was_conc = eff.get("concentration", False)
                        if was_conc and p.get("concentration", "").lower() == eff["name"].lower():
                            p["concentration"] = None
                        _effect_expire_events.append({
                            "owner": p["name"],
                            "name": eff["name"],
                            "was_concentration": was_conc,
                        })

        # world_time replaces entirely
        if "world_time" in data:
            _current_stats["world_time"] = data["world_time"]

        # factions replaces entirely ([] clears)
        # Validate: default missing standing to "Neutral" and warn so the root
        # cause (DM omitting the field when building JSON from state.md prose)
        # is surfaced in logs without silently showing "—" in the sidebar.
        if "factions" in data:
            validated_factions = []
            for f in (data["factions"] or []):
                if not isinstance(f, dict):
                    continue
                if f.get("name") and not f.get("standing"):
                    print(
                        f"[WARN] faction '{f['name']}' missing standing field — "
                        "defaulting to Neutral. Push with standing: Allied/Friendly/"
                        "Neutral/Suspicious/Hostile to show correct colour.",
                        file=sys.stderr,
                    )
                    f = dict(f)
                    f["standing"] = "Neutral"
                validated_factions.append(f)
            _current_stats["factions"] = validated_factions

        # quests replaces entirely ([] clears)
        if "quests" in data:
            _current_stats["quests"] = data["quests"]

        current = dict(_current_stats)

    # autorun_waiting / autorun_cycle — display-only signals, not stored in stats
    if "autorun_waiting" in data:
        if not data["autorun_waiting"]:
            # Turn resolved — clear stored cycle so reconnecting clients don't see stale pie
            global _autorun_cycle
            with _autorun_cycle_lock:
                _autorun_cycle = None
        _broadcast({"autorun_waiting": bool(data["autorun_waiting"])})
        if not any(k in data for k in ("players", "turn_order", "world_time", "factions",
                                        "quests", "replace_players", "sheet", "autorun_cycle")):
            return "", 204

    if "autorun_cycle" in data:
        with _autorun_cycle_lock:
            _autorun_cycle = data["autorun_cycle"]
        _broadcast({"autorun_cycle": data["autorun_cycle"]})
        if not any(k in data for k in ("players", "turn_order", "world_time", "factions",
                                        "replace_players", "sheet", "autorun_threshold")):
            return "", 204

    if "autorun_threshold" in data:
        global _autorun_threshold
        val = data["autorun_threshold"]
        _autorun_threshold = int(val) if val is not None else None
        _broadcast({"autorun_threshold": _autorun_threshold})
        if not any(k in data for k in ("players", "turn_order", "world_time", "factions",
                                        "replace_players", "sheet")):
            return "", 204

    # Write active campaign name so dm_help.py always reads the current campaign.
    # Also reload the tail buffer from the new campaign's session_tail.json so
    # display replay at /dnd load shows the correct campaign's last session.
    if "campaign" in data:
        try:
            with open(CAMP_FILE, "w") as f:
                f.write(str(data["campaign"]).strip())
            _load_tail()
        except Exception:
            pass
        # Resolve and stash the ruleset for this campaign so the sidebar badge
        # can render. Defaults to '2014' for legacy campaigns predating the
        # ruleset field. Wrapped in try/except so a missing paths import or
        # malformed state.md never breaks the stats endpoint.
        try:
            from paths import campaign_ruleset as _campaign_ruleset
            _rs = _campaign_ruleset(str(data["campaign"]).strip())
            with _stats_lock:
                _current_stats["ruleset"] = _rs
            current = dict(_current_stats)
        except Exception:
            pass

    # Explicit ruleset override (e.g. push_stats.py --ruleset 2024)
    if "ruleset" in data:
        rs_in = str(data.get("ruleset") or "").strip()
        if rs_in in ("2014", "2024"):
            with _stats_lock:
                _current_stats["ruleset"] = rs_in
            current = dict(_current_stats)

    _persist_stats()
    _broadcast_stats(current)
    # Broadcast any round-based effect expiries after the stats update
    for evt in _effect_expire_events:
        _broadcast({"effect_expired": evt})

    # Update expected player count for staged-input auto-trigger
    global _expected_count
    with _stats_lock:
        players = _current_stats.get("players", [])
    _expected_count = max(1, len(players))

    return "", 204


@app.route("/effects/expire", methods=["POST"])
def effects_expire():
    """Called by browser when a time-based effect countdown reaches zero.
    Removes the effect from stats, clears concentration if applicable,
    and broadcasts effect_expired to all connected clients.
    """
    if not _token_ok():
        return "Forbidden", 403
    data  = request.get_json(silent=True) or {}
    owner = data.get("owner", "").strip()
    name  = data.get("name", "").strip()
    if not owner or not name:
        return "", 400

    expire_evt = None
    with _stats_lock:
        for p in _current_stats.get("players", []):
            if p.get("name", "").lower() != owner.lower():
                continue
            was_conc   = False
            new_effects = []
            for e in p.get("effects", []):
                if e.get("name", "").lower() == name.lower():
                    was_conc = e.get("concentration", False)
                    if was_conc and p.get("concentration", "").lower() == name.lower():
                        p["concentration"] = None
                else:
                    new_effects.append(e)
            p["effects"] = new_effects
            expire_evt = {"owner": p["name"], "name": name, "was_concentration": was_conc}
            break
        current = dict(_current_stats)

    if expire_evt:
        _broadcast({"effect_expired": expire_evt})
    _broadcast_stats(current)
    _persist_stats()
    return "", 204


@app.route("/audio-toggle", methods=["POST"])
def audio_toggle():
    """Enable/disable ambient or SFX from the browser toggle switches.

    Body: {"ambient": true|false, "sfx": true|false}  (either or both keys)
    Response: {"ambient": bool, "sfx": bool, "available": bool}
    Broadcasts audio_state to all connected browsers so every device syncs.
    """
    data = request.get_json(silent=True) or {}
    if _audio:
        if "sfx" in data:
            _audio.set_sfx(bool(data["sfx"]))
        state = _audio.get_state()
    else:
        state = {"sfx": False, "available": False}
    return state, 200


@app.route("/audio/sfx/<name>")
def audio_sfx(name):
    """Serve a synthesized SFX WAV for the given effect name."""
    if not _audio:
        return "Audio not available", 503
    wav = _audio.get_sfx_wav(name)
    if wav is None:
        return "Not found", 404
    return Response(wav, mimetype="audio/wav",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/clear", methods=["POST"])
def clear():
    """Wipe text log AND stats, broadcast clear to all connected browsers.

    Called on /dnd new (fresh campaign). Ensures sidebar shows no stale characters.
    """
    if not _token_ok():
        return "Forbidden", 403
    global _scene_buffer, _current_stats
    with _text_log_lock:
        _text_log.clear()
    with _stats_lock:
        _current_stats = {}
    _scene_buffer = []
    for path in (LOG_FILE, STATS_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _broadcast({"clear": True})
    return "", 204


@app.route("/help-request", methods=["POST"])
def help_request():
    """Spawn dm_help.py to generate and send an on-demand DM hint.

    Protected by an O_EXCL lock file — concurrent requests return 409
    so multiple players clicking the button never duplicates execution.
    Lock is released by dm_help.py in its finally block.
    """
    if not _token_ok():
        return "Forbidden", 403

    # Atomic lock: O_EXCL fails if file already exists — no race condition
    try:
        fd = os.open(HELP_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return "Already running", 409

    # Read active campaign name
    try:
        campaign = open(CAMP_FILE).read().strip()
    except FileNotFoundError:
        os.unlink(HELP_LOCK)
        return "No active campaign", 400

    if not campaign:
        os.unlink(HELP_LOCK)
        return "No active campaign", 400

    dm_help_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dm_help.py")
    subprocess.Popen(
        [sys.executable, dm_help_py, "--campaign", campaign],
        close_fds=True,
        start_new_session=True,
    )
    return "", 202


@app.route("/player-input", methods=["POST"])
def player_input():
    """Queue a player action submitted from the display companion.

    Body: {"character": "Mira", "text": "I draw my rapier", "hold": false}
    Broadcasts pending_input event to all connected browsers.
    """
    if not _token_ok():
        return "Forbidden", 403

    import time
    data = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", "Party"))[:50]
    text = str(data.get("text", ""))[:500]
    hold = bool(data.get("hold", False))

    # Strip shell metacharacters — input is player dialogue/action, not commands
    text = re.sub(r"[`\\$]", "", text).strip()
    if not text:
        return "empty", 400

    entry = {
        "character": character,
        "text": text,
        "hold": hold,
        "timestamp": time.time(),
    }

    with _input_lock:
        _input_queue.append(entry)
        current = list(_input_queue)

    _persist_input_queue()
    _broadcast({"pending_input": current})
    return "", 204


@app.route("/device/approve", methods=["POST"])
def device_approve():
    """DM approves a pending device. Body: {"id": "<device_id>"}"""
    if not _token_ok():
        return "Forbidden", 403
    # TODO(PR3): write-path authz. PR2 hides the request card from players, but
    # the shared LAN token still lets any browser POST an approval (confused
    # deputy). When a DM is assigned, require role==dm here — resolve identity
    # from the X-DND-Device header. Natural home is the /dnd dm role work.
    device_id = str((request.get_json(force=True, silent=True) or {}).get("id", ""))
    with _devices_lock:
        _pending_devices.pop(device_id, None)
        _approved_devices.add(device_id)
    _persist_approved_devices()
    _persist_pending_devices()
    _broadcast({"device_approved": device_id})
    return "", 204


@app.route("/device/deny", methods=["POST"])
def device_deny():
    """DM denies a pending device. Body: {"id": "<device_id>"}"""
    if not _token_ok():
        return "Forbidden", 403
    # TODO(PR3): write-path authz — see /device/approve. Require role==dm when a
    # DM is assigned; the shared token alone is not DM-distinguishing.
    device_id = str((request.get_json(force=True, silent=True) or {}).get("id", ""))
    with _devices_lock:
        _pending_devices.pop(device_id, None)
        _denied_devices.add(device_id)
    _persist_pending_devices()
    _broadcast({"device_denied": device_id})
    return "", 204


@app.route("/player-input/stage", methods=["POST"])
def stage_input():
    """Stage a player action for review. Broadcasts staged_inputs to all displays.

    Body: {"character": "Mira", "text": "draws her rapier"}
    """
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(request.remote_addr):
        return "Too Many Requests", 429

    device_id = request.headers.get("X-DND-Device", "")
    status    = _device_ok(device_id, request.remote_addr)
    if status == "denied":
        return "Forbidden", 403
    if status == "pending":
        return jsonify({"status": "pending"}), 202

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()
    text      = _sanitize_input(str(data.get("text", "")))

    if not character or not text:
        return "Bad Request", 400

    with _stats_lock:
        known = {p["name"] for p in _current_stats.get("players", [])}
    if not _char_ok(character, known):
        return "Forbidden", 403

    # In solo mode (1 expected player), skip the manual Ready step and auto-trigger.
    solo = (_expected_count == 1)

    with _staged_lock:
        _staged[character] = {
            "text":      text,
            "ready":     solo,
            "timestamp": _time.time(),
        }
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})

    if solo:
        _check_auto_trigger()

    return "", 204


@app.route("/player-input/ready", methods=["POST"])
def ready_input():
    """Toggle the ready flag for a staged character.

    Body: {"character": "Mira", "ready": true}
    Triggers auto-fire when all expected players are ready.
    """
    if not _token_ok():
        return "Forbidden", 403
    if not _rate_ok(request.remote_addr):
        return "Too Many Requests", 429

    device_id = request.headers.get("X-DND-Device", "")
    status    = _device_ok(device_id, request.remote_addr)
    if status == "denied":
        return "Forbidden", 403
    if status == "pending":
        return jsonify({"status": "pending"}), 202

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()
    ready     = bool(data.get("ready", True))

    with _staged_lock:
        if character not in _staged:
            return "Not Found", 404
        _staged[character]["ready"] = ready
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})

    if ready:
        _check_auto_trigger()

    return "", 204


@app.route("/player-input/unstage", methods=["POST"])
def unstage_input():
    """Remove a character's staged action (e.g. player wants to edit it).

    Body: {"character": "Mira"}
    """
    if not _token_ok():
        return "Forbidden", 403

    device_id = request.headers.get("X-DND-Device", "")
    if _device_ok(device_id, request.remote_addr) != "approved":
        return "Forbidden", 403

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()

    with _staged_lock:
        _staged.pop(character, None)
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})
    return "", 204


@app.route("/player-input/skip", methods=["POST"])
def skip_input():
    """Skip a character's turn — stages a 'skips their turn' entry marked ready.

    Counts toward the auto-trigger threshold and fires auto-trigger if threshold met.
    Body: {"character": "Mira"}
    """
    if not _token_ok():
        return "Forbidden", 403

    device_id = request.headers.get("X-DND-Device", "")
    if _device_ok(device_id, request.remote_addr) != "approved":
        return "Forbidden", 403

    data      = request.get_json(force=True, silent=True) or {}
    character = str(data.get("character", ""))[:50].strip()
    if not character:
        return "Bad Request", 400

    with _stats_lock:
        known = {p["name"] for p in _current_stats.get("players", [])}
    if not _char_ok(character, known):
        return "Forbidden", 403

    with _staged_lock:
        _staged[character] = {
            "text":      "skips their turn",
            "ready":     True,
            "timestamp": _time.time(),
        }
        snap = _staged_snapshot()

    _broadcast({"staged_inputs": snap})
    _check_auto_trigger()
    return "", 204


@app.route("/queue/consumed", methods=["POST"])
def queue_consumed():
    """Called by wrapper.py after it injects .input_queue into the PTY.

    Clears the server-side queue_status and broadcasts to all clients so
    the 'Queued — fires on DM Enter' indicator disappears on every display.
    Token required (called from localhost by the wrapper, but checked for
    consistency).
    """
    if not _token_ok():
        return "Forbidden", 403
    with _queue_status_lock:
        _queue_status.clear()
    _broadcast({"queue_status": [], "dm_processing": True})
    return "", 204


@app.route("/player-input/submit-now", methods=["POST"])
def submit_now():
    """Promote .input_queue → .input_trigger for immediate injection.

    Called by the DM or Claude when they want to process queued player actions
    right now rather than waiting for the DM's next CLI Enter press.
    Token required (DM-only action).
    """
    if not _token_ok():
        return "Forbidden", 403
    try:
        content = open(QUEUE_FILE).read()
        os.unlink(QUEUE_FILE)
    except FileNotFoundError:
        return "No queue", 204
    except Exception:
        return "Error", 500
    try:
        with open(TRIGGER_FILE, "w") as f:
            f.write(content)
    except Exception:
        return "Error", 500
    return "", 204


@app.route("/player-input/drain", methods=["POST"])
def drain_player_input():
    """Read and clear the player input queue. Called by check_input.py at turn start.

    Returns the drained entries as JSON, then broadcasts pending_input: [] to
    clear the indicator on all connected displays.
    """
    if not _token_ok():
        return "Forbidden", 403

    with _input_lock:
        drained = list(_input_queue)
        _input_queue.clear()

    _persist_input_queue()
    _broadcast({"pending_input": []})
    return jsonify(drained), 200


def _catchup_payloads(ident) -> list:
    """On-connect catch-up payloads for a freshly connected client, identity-scoped.

    Mirrors the prior inline /stream block. PR2 scoping closes the leak surface:
    the replay log is filtered by channel tag, device-approval requests are
    DM-only, and stats are scoped per identity (own sheet full, others summary).

    Staged inputs / pending / queue remain full for now: those are table-
    coordination signals (whose turn, who is ready) without a clean per-row owner
    and are entangled with the deferred combat-UX open item — not secret data.
    Everything short-circuits to full when no DM is assigned (backward-compat).
    """
    active = _mp_active()
    out: list = []

    # Scene — public; first so the browser starts on the right background.
    out.append({"scene": SCENES[_current_scene_name] | {"name": _current_scene_name}})

    # Replay recent entries (typed batch) — filtered by each entry's channel tag.
    with _text_log_lock:
        recent = [e for e in _text_log if _entry_visible(e, ident, active)]
    if recent:
        out.append({"replay_batch": recent})

    # Stats — DM/own full, other players summarised (per-connection view privacy).
    with _stats_lock:
        if _current_stats:
            out.append({"stats": _scope_stats(dict(_current_stats), ident)})

    # Pending input — full (deferred).
    with _input_lock:
        if _input_queue:
            out.append({"pending_input": list(_input_queue)})

    # Staged inputs — full (deferred).
    with _staged_lock:
        if _staged:
            out.append({"staged_inputs": _staged_snapshot()})

    # Queue status — full (deferred).
    with _queue_status_lock:
        if _queue_status:
            out.append({"queue_status": list(_queue_status)})

    # Autorun cycle / threshold — public.
    with _autorun_cycle_lock:
        if _autorun_cycle:
            out.append({"autorun_cycle": dict(_autorun_cycle)})
    if _autorun_threshold is not None:
        out.append({"autorun_threshold": _autorun_threshold})

    # Device approval requests — DM-only (everyone when filtering is off).
    if not active or ident["role"] == "dm":
        with _devices_lock:
            for dev in list(_pending_devices.values()):
                out.append({"device_request": {"id": dev["id"], "ip": dev["ip"]}})

    return out


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=256)
    ident = _resolve_identity(request)
    client = {"q": q, "user": ident["user"], "role": ident["role"], "chars": ident["chars"]}
    with _clients_lock:
        _clients.append(client)

    # On-connect catch-up, identity-scoped (see _catchup_payloads).
    for payload in _catchup_payloads(ident):
        q.put_nowait(payload)

    def generate():
        try:
            while True:
                try:
                    payload = q.get(timeout=5)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"   # prevent proxy timeout
        except GeneratorExit:
            with _clients_lock:
                try:
                    _clients.remove(client)
                except ValueError:
                    pass

    resp = Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    return resp


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Wire audio SFX broadcast now that _broadcast is defined
    if _audio:
        _audio.set_broadcast(_broadcast)

    host = "0.0.0.0" if _LAN_MODE else "localhost"
    # TLS — only enabled when --tls is explicitly passed; HTTP is the default.
    _display_dir = os.path.dirname(os.path.abspath(__file__))
    _cert = os.path.join(_display_dir, "cert.pem")
    _key  = os.path.join(_display_dir, "key.pem")
    ssl_ctx = (_cert, _key) if (_TLS_MODE and os.path.exists(_cert) and os.path.exists(_key)) else None
    scheme  = "https" if ssl_ctx else "http"

    # Write .scheme so push_stats.py / send.py / autorun-wait.sh know which to use
    try:
        with open(os.path.join(_display_dir, ".scheme"), "w") as _sf:
            _sf.write(scheme)
    except OSError:
        pass

    if _LAN_MODE:
        print(f"DnD DM Display — LAN mode (0.0.0.0:5001) [{scheme.upper()}]")
        print(f"  Local:  {scheme}://localhost:5001")
        print("  Token stored at:", TOKEN_FILE)
        print("  POST endpoints require X-DND-Token header (send.py/push_stats.py handle this automatically)")
        print()
    else:
        print(f"DnD DM Display — Flask server starting on {scheme}://localhost:5001")
        print(f"Open {scheme}://localhost:5001 in your browser, then Chromecast the tab.")
        print()
    app.run(host=host, port=5001, threaded=True, debug=False, ssl_context=ssl_ctx)
