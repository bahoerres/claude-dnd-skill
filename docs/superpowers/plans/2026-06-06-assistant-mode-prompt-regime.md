# Assistant-Mode Prompt Regime (Task 7b) — Design

**Date:** 2026-06-06
**Status:** Design approved in brainstorm; awaiting implementation plan
**Parent:** `2026-06-05-multiplayer-channel-layer.md` → Task 7b
**Branch context:** designed against v1 (`feat/multiplayer-channel-layer`); one named v2 dependency (§8.4)

## 1. What this is

When a human DM is assigned (`dm:` in Session Flags), Claude demotes from autonomous DM to **assistant**: it computes mechanics and proposes story, while the human holds creative authority. This is the "real second prompt regime" from the original multiplayer design — a `SKILL.md` change plus **one thin server route**. The channel-layer plumbing (private sends, GM screen, scoped stats, identity-tagged clients) shipped in PRs 1–3 and is assumed throughout.

Governing principle, stated once and applied everywhere:

> **If it's deterministic, commit it. If it's words, draft it.**

## 2. How the gated open items resolved

The master spec gated 7b on watching a live session. That gate was **not satisfied** — play reached an opening-scene render only. We brainstormed anyway (2026-06-06) because Blake arrived with a concrete table shape; items that genuinely need observed play are kept loose and listed in §8.

| Original open item | Resolution |
|---|---|
| 1. Player-facing combat UX | **Out of scope** — its own spec, after combat runs live (§8.1) |
| 2. Autorun in assistant mode | Resolved in general form: "take the wheel" posture (§6) |
| 3. Remote group vs in-person table | **Remote video table.** DM is browser-only (not at the Claude Code terminal). Players: voice for roleplay, own devices for private views + mechanical input |

## 3. Authority model (SKILL.md)

**Activation.** `dm:` set → Claude auto-demotes on the next turn. `/dnd dm clear` → autonomous DM restored. The existing flag is the entire switch; no second toggle.

**Two output lanes.** Every Claude output classifies before it touches `send.py`:

| Lane | Examples | Destination |
|---|---|---|
| **Mechanics** (deterministic) | dice results, HP/slot/condition deltas, stat updates, initiative math | Commits directly to shared display, as today |
| **Story** (words) | narration, scene text, NPC dialogue, check reveals, "ways this could land" options | Numbered draft whispered to GM screen (`--audience dm`); reaches players only on DM approval |

**Input authority.**
- DM typed input is **authoritative narrative** — "the party heads into the sewers" is canon, not a proposal. Claude updates state and responds with drafts/mechanics.
- Player staged input remains what it is today: mechanical actions and check responses.

## 4. DM input lane (server — the only new code)

`POST /dm-input`:

- **Authz:** shared token + `_resolve_identity(request)["role"] == "dm"`, the same write-path pattern as `/device/approve|deny`. 403 otherwise. (Behavior when no DM is assigned: 403 — the route is meaningless outside multiplayer.)
- **Body:** `{"text": "..."}`, sanitized with `_sanitize_input`.
- **Queue:** appends to the existing input queue tagged `[DM]: …` so `check_input.py` / `autorun-wait.sh` deliver it through the pipe that already drives Claude's turns. No new transport.
- **Echo:** broadcasts `{"dm_input": …}` with `audience="dm"` only — DM sees their message land on the GM screen; players see nothing.
- **Trigger semantics:** DM input fires Claude's turn **immediately *when a consumer is live*** — i.e. the autorun loop (`autorun-wait.sh`, polling `.input_queue` every 0.3s) or `check_input.py` at a turn boundary. With `autorun: false` and no active turn loop, `/dm-input` still *delivers* (the bar shows ✓) but the line waits in the queue with no response, exactly like staged player input. "Immediate" is therefore **autorun-scoped**; continuous bar-driven assistant play requires `autorun: true`. *(Validated live 2026-06-07: with autorun off, a DM-bar send queued silently; flipping autorun on made the loop fire on its own.)* Either way, staged-but-unready player inputs stay staged — a DM whisper exchange cannot sweep in a player's half-typed action. Player ready-up flow is unchanged.

**Frontend:** one text box on the GM screen, rendered only for the `dm` identity. An input field, not an approval subsystem.

## 5. Draft / approve loop (Approach A — conversational gate)

Chosen over a server-side button-approval queue (Approach B) to avoid building UI before observing real play; B is a named follow-on with an explicit trigger (§8.3).

**Draft format** — every story-lane output lands on the GM screen via `send.py --audience dm`:

> **#3 · reveal → Vesper** — "Fresh scratch marks ring the lock — someone forced this door, recently and badly."
> *reply to send it, change it, or drop it*

Numbers increment per session. The affordance hint rides on every draft — the learning curve is read-off-the-screen, not memorized. **Design bar: talk to it like a co-DM, no command syntax.** "send 3", "yeah the scratch marks one", "3 but creepier", "drop it, I'll narrate this myself", "send 3 to everyone, not just Vesper" all work; Claude is the parser.

**Safety rule:** if Claude isn't sure which draft or what action the DM meant, it asks on the dm channel — it never guesses-and-commits. Misfire correction is one message ("pull it back" → correction/clear on the shared display).

**On approval,** Claude sends the (possibly edited) text via `send.py` to the draft's target audience — passers' private views or the shared display. Commit round-trips through Claude; at table pacing (DM reads aloud anyway) this is acceptable, and it's the cost that keeps the server thin.

**Draft lifecycle:** drafts are conversational state, not server state. Stale drafts die of irrelevance; an approval referencing a long-stale draft gets the clarify-first treatment. SKILL.md instructs Claude to re-offer or retire drafts when context has clearly moved.

## 6. Take the wheel (general form of the autorun dial)

Through the same conversational gate: DM types "take the wheel for this stretch" → Claude runs autonomous, story-lane gate suspended, narrating straight to the shared display — until the DM says "I've got it" or a named boundary arrives ("run this room, then hand back"). The `dm:` flag never changes; this is a **posture inside assistant mode**, not a mode switch. `/dnd dm clear` remains the hard exit. The original "hand a throwaway fight back to Claude" case is a special case of this, fully realized when the combat spec lands.

## 7. Skill-check reveals, end to end

1. **Check initiated** — DM lane: "everyone roll perception, DC 13 for the scratch marks" (or Claude proposes a check; DM green-lights). **The DC lives only in the dm channel.**
2. **Players roll** — via the running version's dice surface (typed/staged on v1; phone-dice on v2). Claude collects.
3. **Mechanics commit** — outcomes may show publicly; default **✓/✗ only** ("Vesper: Perception ✓ · Posy: ✗"), bare numbers at DM's per-check discretion.
4. **Reveal drafts** — per passer, into the GM screen as numbered drafts; different passers can get different slices (the rogue reads the forced lock, the cleric senses the desecration).
5. **Approve/edit → whisper** — approved text goes out `--audience <character>`; the DM says "Vesper, you notice something the others don't" on the call as it lands on their screen.

Reveals are **DM-gated by default** (consistent with story-lane rules). A per-check "auto" flag was considered and rejected for v1 of the regime — revisit only if gating proves heavy in play.

## 8. Deferred — named follow-ons

1. **Combat coordinator player UX** — own spec, after combat runs live. Claude-as-combat-coordinator (initiative, turn pointer, monster math, legality) is resolved in the parent design; what players *see and tap* is not.
2. **Player↔player PMs** — channel-layer feature, orthogonal to this regime. Variant to evaluate then: DM-cc'd "pass a note" semantics.
3. **Approach B: button-approval queue** — server-side proposal store + approve/edit/reject UI on the GM screen. **Trigger:** observed approve-fatigue in real sessions. The SKILL.md regime is identical under A and B; B layers on without rework.
4. **v2 `dice_request` DC leak (HIGH)** — on v2, secret DCs currently broadcast to every browser, which directly breaks §7 step 1. The leak fix is a **prerequisite for this regime on v2**, not a parallel chore. Fix spec lives at the `TODO(channel-layer, HIGH)` in the v2 app; do it with dice-flow tests.
5. **No-consumer feedback (UX, surfaced 2026-06-07)** — the DM bar reports "✓ delivered" whenever `/dm-input` writes to the queue, even when nothing will consume it (autorun off, no live turn loop). The DM reasonably reads "delivered" as "Claude will respond," then sees nothing. Options to evaluate: have the bar warn when no consumer is live (e.g. `/dm-input` returns a soft 202 + "queued — no active turn loop" when `autorun` is false and no recent consume), or surface autorun state on the GM screen. Low-severity; the fix is feedback, not plumbing.

## 9. Testing

**Server (unit/integration, same suite as the existing 183):**
- `/dm-input` authz: player identity → 403; no DM assigned → 403
- Queue tagging: `[DM]:` prefix present and parseable by `check_input.py`
- Echo scoped to `dm` audience only (no player client receives `dm_input`)
- Immediate trigger bypasses the ready-gate
- Staged-but-unready player inputs are not consumed by a DM-triggered turn

**Regime (prompt behavior — live-session validation checklist, not unit tests):**
- [ ] Mechanics commit unprompted; no story reaches players un-approved
- [ ] Drafts arrive numbered with the affordance hint
- [ ] Ambiguous approval → clarify, never guess-and-commit
- [ ] DC never visible outside the dm channel
- [ ] "Take the wheel" engages and disengages cleanly at a named boundary
- [ ] `/dnd dm clear` restores autonomous DM

Run the checklist in the first real session; observed failures become SKILL.md fixes.

## 10. Decision log

| Decision | Choice | Alternative rejected |
|---|---|---|
| Table shape | Remote video; DM browser-only; players hybrid (voice + own-device private views) | DM at the CC terminal (zero server code, but wrong for the actual table) |
| Commit gate | Mechanics flow, story gated | Everything gated (DM bottleneck); advisory-only (no real gate) |
| Reveal gate | DM-gated | Auto-whisper (Claude committing narrative); per-check flag (complexity before evidence) |
| Approval mechanism | A: conversational gate, with B as triggered follow-on | B out of the gate (UI before observed play; phone-textarea editing) |
| PMs | Deferred | In-scope (orthogonal to the regime) |
| Combat | Deferred to own spec | — (always the plan) |
