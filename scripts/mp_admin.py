#!/usr/bin/env python3
"""Multiplayer admin — manage per-campaign device/character ownership and the
session DM assignment that drives the display companion's private channels.

Two stores, both per-campaign:

  ownership.json (in the campaign dir):
      {"devices":    {"<device-uuid>": "<user>"},
       "characters": {"<user>": ["<Character>", ...]}}

  state.md `## Session Flags` -> `dm: <user>`  (read by the server's _session_dm)
      No `dm:` flag  ->  private filtering is OFF (single-broadcast behaviour).

This is the write path behind the `/dnd dm` and `/dnd own` commands. Keeping it a
script (not just a SKILL procedure) makes it testable and safe to script.

Examples:
    mp_admin.py --campaign my-game dm-assign alice
    mp_admin.py --campaign my-game dm-status
    mp_admin.py --campaign my-game dm-clear
    mp_admin.py --campaign my-game own-device 8b1c-uuid alice
    mp_admin.py --campaign my-game own-char alice Mira
    mp_admin.py --campaign my-game list
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import find_campaign  # noqa: E402


# ─── Campaign paths ──────────────────────────────────────────────────────────

def _camp_dir(name: str) -> str:
    return str(find_campaign(name))


def _ownership_path(name: str) -> str:
    return os.path.join(_camp_dir(name), "ownership.json")


def _state_path(name: str) -> str:
    return os.path.join(_camp_dir(name), "state.md")


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _check_user(name: str) -> None:
    """A user name may end up as the state.md `dm:` value, which is read through
    a comment-stripping regex — a '#' would silently truncate it (and demote the
    real DM to player). Reject it at the write boundary."""
    if not name or not name.strip():
        raise SystemExit("name may not be empty")
    if "#" in name:
        raise SystemExit("name may not contain '#' (conflicts with the state.md comment syntax)")


# ─── ownership.json ──────────────────────────────────────────────────────────

def load_ownership(name: str) -> dict:
    try:
        with open(_ownership_path(name)) as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # Coerce existing-but-malformed sub-values too (setdefault won't fix those).
    if not isinstance(data.get("devices"), dict):
        data["devices"] = {}
    if not isinstance(data.get("characters"), dict):
        data["characters"] = {}
    return data


def save_ownership(name: str, data: dict) -> None:
    _atomic_write(_ownership_path(name), json.dumps(data, indent=2) + "\n")


def _collision_warning(data: dict) -> "str | None":
    """A bare-name audience matches a character OR a user of that name (shared
    namespace). Warn if a username equals a DIFFERENT user's character name."""
    users = set(data.get("characters", {})) | set(data.get("devices", {}).values())
    for user, chars in data.get("characters", {}).items():
        for ch in chars or []:
            if ch in users and ch != user:
                return (f"name collision: '{ch}' is both a username and a "
                        f"character owned by '{user}' — a whisper to '{ch}' would "
                        f"reach both. Rename one to keep channels unambiguous.")
    return None


def own_device(name: str, device_id: str, user: str) -> None:
    _check_user(user)
    if not device_id or not device_id.strip():
        raise SystemExit("device id may not be empty")
    data = load_ownership(name)
    data["devices"][device_id] = user
    save_ownership(name, data)
    warn = _collision_warning(data)
    if warn:
        print(f"warning: {warn}", file=sys.stderr)


def own_char(name: str, user: str, character: str) -> None:
    _check_user(user)
    data = load_ownership(name)
    chars = data["characters"].setdefault(user, [])
    if character not in chars:
        chars.append(character)
    save_ownership(name, data)
    warn = _collision_warning(data)
    if warn:
        print(f"warning: {warn}", file=sys.stderr)


# ─── state.md `dm:` flag (scoped to the Session Flags section) ───────────────

_HEADER_RE = re.compile(r"^##\s+Session Flags\s*$")
_ANY_HEADER_RE = re.compile(r"^##\s")
_DM_LINE_RE = re.compile(r"^\s*[-*]?\s*dm:", re.IGNORECASE)


def _session_flags_range(lines: list) -> "tuple[int, int] | None":
    start = next((i for i, ln in enumerate(lines) if _HEADER_RE.match(ln)), None)
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _ANY_HEADER_RE.match(lines[j]):
            end = j
            break
    return start, end


def get_dm(name: str) -> "str | None":
    try:
        with open(_state_path(name)) as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    rng = _session_flags_range(lines)
    if rng is None:
        return None
    start, end = rng
    for ln in lines[start + 1:end]:
        m = re.match(r"^\s*[-*]?\s*dm:[ \t]*([^#\n]+?)[ \t]*(?:#.*)?$", ln, re.IGNORECASE)
        if m:
            return m.group(1).strip() or None
    return None


def set_dm(name: str, who: str) -> None:
    _check_user(who)
    with open(_state_path(name)) as f:
        lines = f.read().splitlines()
    rng = _session_flags_range(lines)
    if rng is None:
        raise SystemExit("state.md has no '## Session Flags' section — cannot assign DM")
    start, end = rng
    # Drop ALL existing dm: lines in the section (no stale duplicates), then
    # insert the new assignment right after the header.
    section = [ln for ln in lines[start + 1:end] if not _DM_LINE_RE.match(ln)]
    section.insert(0, f"- dm: {who}")
    lines = lines[:start + 1] + section + lines[end:]
    _atomic_write(_state_path(name), "\n".join(lines) + "\n")


def clear_dm(name: str) -> None:
    with open(_state_path(name)) as f:
        lines = f.read().splitlines()
    rng = _session_flags_range(lines)
    if rng is None:
        return
    start, end = rng
    kept = [ln for k, ln in enumerate(lines)
            if not (start < k < end and _DM_LINE_RE.match(ln))]
    _atomic_write(_state_path(name), "\n".join(kept) + "\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage multiplayer ownership + DM assignment.")
    p.add_argument("--campaign", required=True, help="Campaign name")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dm-status")
    sub.add_parser("dm-clear")
    a = sub.add_parser("dm-assign"); a.add_argument("user")
    d = sub.add_parser("own-device"); d.add_argument("device_id"); d.add_argument("user")
    c = sub.add_parser("own-char"); c.add_argument("user"); c.add_argument("character")
    sub.add_parser("list")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    name = args.campaign
    if args.cmd == "dm-assign":
        set_dm(name, args.user)
        print(f"DM assigned: {args.user} (private channels active for '{name}')")
    elif args.cmd == "dm-clear":
        clear_dm(name)
        print(f"DM cleared (private filtering off for '{name}')")
    elif args.cmd == "dm-status":
        dm = get_dm(name)
        print(f"DM: {dm}" if dm else "DM: none (Claude is DM; filtering off)")
    elif args.cmd == "own-device":
        own_device(name, args.device_id, args.user)
        print(f"device {args.device_id} -> {args.user}")
    elif args.cmd == "own-char":
        own_char(name, args.user, args.character)
        print(f"{args.user} owns {args.character}")
    elif args.cmd == "list":
        data = load_ownership(name)
        print(f"DM: {get_dm(name) or 'none'}")
        print("devices:")
        for dev, user in data["devices"].items():
            print(f"  {dev} -> {user}")
        print("characters:")
        for user, chars in data["characters"].items():
            print(f"  {user}: {', '.join(chars)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
