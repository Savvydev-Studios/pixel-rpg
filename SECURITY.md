# Security review - Pixel Boss RPG v3

Audit of the original v2 single-file game and of the v3 rewrite that replaced
it. Run the automated portion yourself at any time:

```
python -m pixel_rpg.audit
```

---

## 1. Scope and threat model

This is a **single-player desktop game with optional local co-op**. It has:

- no HTTP server, no routes, no sessions
- no user accounts, no authentication, no authorization
- no database, no SQL, no template engine
- no frontend, no browser, no cookies, no CORS
- no third-party API calls, and no credential it actually needs

That rules out most classic web vulnerability classes by construction - not
because they were mitigated, but because the attack surface does not exist.

**It does now have a network listener**, and this section used to say it did
not. `net.py` opens a TCP port when - and only when - a player chooses to host
a room. That is a real change to the threat model and it is stated here rather
than left for somebody to discover:

- The socket is opened **on demand**, never at startup. A player who never
  hosts never listens on anything.
- Frames are **length-prefixed and bounded before they are buffered**, so a
  peer claiming a gigabyte costs nothing.
- The payload is **JSON, never pickle**, and every field is coerced through
  the same `security.py` helpers that validate save files - including
  `check_structure`, so a nesting bomb is rejected on the wire exactly as it
  is in a save.
- **The host is authoritative.** A client sends intent, never state. It cannot
  assert its own health, gold, or that it killed something. **Battles are the
  clearest case:** one `Battle` object lives on the host, clients run mirrors
  that advance no timeline and own no dice, and a move is accepted only if it
  is that player's turn and the fight is waiting on a decision. Rewards,
  loot and the monsters themselves are all decided host-side; a client that
  disconnects mid-fight is treated as having fled. Chests are the smaller
  worked example: a client asks for one by grid position, and the host checks
  it exists, is still shut, and that the asker is actually standing near it
  before rolling the contents itself and sending them. The claim names *which*
  chest and nothing else.
- **A second transport, and the same rules.** A host also accepts WebSocket
  connections (RFC 6455) so that a browser build could one day join; which
  transport a peer chose is decided from its first four bytes and never
  leaves `net._Peer`. The bounds are the same ones: a WebSocket length header
  can claim 2^64 bytes in eight bytes of wire and is checked against
  `MAX_FRAME` **before any payload is buffered**, reassembled fragments are
  bounded on the total, the handshake is capped at 8 KB while it is read, and
  an unmasked client frame is refused rather than tolerated - masking is what
  stops a hostile page persuading a browser to emit bytes a confused proxy
  would read as its own protocol. The guest half (`net.WebClient`) enforces
  the mirror of each rule: it always masks, refuses a *masked* server frame,
  and refuses a `101` whose accept value it did not compute - a wrong one
  means something other than the host answered. There is no TLS: `ws://`
  inherits the unencrypted, unauthenticated posture stated below, unchanged.
- **A second transport is not a second door around the validation.** Payloads
  arriving over WebSocket go through the same `_decode_body` and the same
  `security.check_structure` as everything else, so the 4096-character string
  cap and the nesting limits apply identically. A test sends an over-long
  string over WebSocket specifically to confirm it is refused.
- **Indices off a socket are refused, not clamped.** `security.safe_int`
  clamps to its bounds, which is correct for a stat and wrong for a
  coordinate - clamping `x=-5` to `0` does not reject a hostile message, it
  silently asks about a different tile. `coop._safe_tile` rejects instead.
- Peers are **rate limited** (120 messages/second) and the room is **capped**
  at four players. A peer that exceeds either is dropped.
- There is **no relay and no rendezvous server**: the room code encodes the
  host's own address. Nothing about a session touches a machine that is not
  one of the players'.

What this deliberately does *not* claim: the traffic is unencrypted and
unauthenticated, so anyone who can reach the host's port can attempt to join,
and anyone on the path can read the session. For co-op with friends on a LAN
that is the right trade. **Do not** repurpose this transport for anything
where either property matters.

**Saves are no longer necessarily files, and the checks did not move.**
`store.py` separates *where* a save is kept from *what is allowed in one*.
A backend's `read_text` enforces a size ceiling and nothing else; parsing,
structure limits and every coercion stay in `security.py`, so a new medium
cannot become a new way around them - a test writes malformed JSON, a nesting
bomb and an over-long string into all three backends and asserts each is
refused identically. Storage keys are matched against a shape
(`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, no `..`) and **refused rather than
cleaned**, and the file backend keeps its `is_within` check behind that.
Browser storage is not atomic - `localStorage` has no rename - so the `.bak`
copy written before every save is the whole safety net there, which is
weaker than the desktop's temp-file-and-rename and is documented as such.

What genuinely remains untrusted:

| Input | Who controls it | Why it matters |
|---|---|---|
| Save files on disk | the user, or anyone who hands them a save | parsed into game objects on load |
| Environment variables | the user, or the shell that launched the game | drive resolution, paths, difficulty |
| Typed profile name | the user | rendered on screen, used to derive a filename |

The realistic adversary is **a malformed or hand-edited file**, not a remote
attacker. A single-player game where the player edits their own save to grant
themselves gold is cheating at solitaire. The security goal is that no input
can **crash the game, escape the save directory, or destroy saved data**.

---

## 2. Findings in the original `pixel_rpg_v2.py`

| # | Severity | Finding | Status in v3 |
|---|---|---|---|
| 1 | **Medium** | `Gear(**d)` splatted untrusted JSON straight into a constructor as keyword arguments. Any unexpected key raised `TypeError`, and the surrounding blanket `except Exception: pass` then discarded the **entire** save silently. One bad field cost the player their whole run. | Fixed - `Item.from_dict` drops unknown keys, validates every field, and returns `None` for one unusable record rather than taking the save down with it |
| 2 | **Medium** | No size or structure limit when reading `save.json`. A large or deeply nested file was parsed in full into memory. | Fixed - 512 KB ceiling checked before the read, plus depth, node and string limits |
| 3 | **Medium** | Saves were written with `json.dump` directly over the live file, including from the quit handler. A crash, a power cut or a full disk mid-write left a truncated save and no way back. | Fixed - atomic temp file plus `os.replace`, a rolling `.bak`, and automatic recovery |
| 4 | **Low** | No validation of loaded values: HP, coins and stats were `int()`-cast and trusted, so a save could carry negative HP or absurd totals into the game logic. | Fixed - every field coerced into an explicit range |
| 5 | **Low** | Bare `except:` in the quit path, twice, swallowing `KeyboardInterrupt` and `SystemExit` along with real errors. | Fixed - narrow `except OSError` / `except Exception`, with the error surfaced to the player |
| 6 | **Low** | `SAVE_FILE = "save.json"` resolved against the current working directory, so which save you loaded depended on where you launched from - and it landed inside the source tree, where it could be committed. | Fixed - per-user application-data directory, deliberately outside the repository |
| 7 | Info | No hardcoded credentials. The scan of the original file was clean. | Still clean |

Nothing in the original was remotely exploitable. Findings 1 to 3 are
**data-loss and availability** bugs, which for a game with a save file is the
category that actually hurts.

---

## 3. Controls implemented in v3

### 3.1 Rate limiting

There are no login routes to protect, so the policy - **5 attempts per 15
minutes** - was applied to the operations in this game that are genuinely
retryable against a check, plus everything that touches the disk:

| Operation | Limit | Rationale |
|---|---|---|
| Save-slot decode **failures** | 5 per 15 min, per slot | the closest analogue to a login attempt: the one place where a crafted payload can be retried against a validator. A successful load costs nothing. |
| Save writes | 1 per second | stops a held key or a loop from thrashing the disk |
| Autosave | 1 per 45 seconds | same |
| Repeated sound effects | per-effect throttle | prevents mixer channel exhaustion |

`RateLimiter` in `pixel_rpg/security.py` is a sliding window over the
**monotonic clock**, so moving the system clock cannot widen it. It is keyed,
so one slot locking out does not affect the others.

### 3.2 Secrets

- **No credential is hardcoded anywhere.** Verified by the scanner in
  `pixel_rpg/audit.py`, which is itself verified against ten planted secrets
  (AWS, GitHub, Slack, Google, OpenAI-style, JWT, PEM block and generic
  assignments) with no false positives on placeholder or `os.environ` lines.
- Every secret-shaped value is read through `config.get_secret()`, which wraps
  `os.environ.get(...)`. Nothing else in the codebase reads a credential.
- `.env` is loaded by `python-dotenv` when installed, with a conservative
  built-in fallback parser otherwise. It is listed in `.gitignore`.
- `.env.example` ships with `API_KEY=YOUR_API_KEY_HERE` and nothing else. A
  value still set to a `YOUR_*` placeholder is treated as **absent**, so a
  half-configured checkout degrades instead of sending a placeholder anywhere.
- **The game needs no key at all.** `API_KEY` exists only so the optional
  online extras have a documented, safe way to obtain one. Without it, those
  features stay off.
- Saved preferences (`Settings.to_dict`) are an explicit allow-list of display
  and gameplay fields, so a secret cannot reach a save file. A test asserts it.

### 3.3 Input validation and sanitization

Fail-closed throughout: a malformed value is replaced with a safe default or
rejected outright, never partially trusted.

- **Size** - saves capped at 512 KB, checked with `stat` *before* the read;
  strings capped at 4096 characters; arrays and objects at 512 entries;
  nesting at 12 levels; total nodes at 20 000.
- **Type** - `safe_int` / `safe_float` / `safe_bool` / `safe_str` never raise
  and never return the wrong type. `safe_int` rejects `bool` (an `int`
  subclass), `NaN`, `inf`, and numeric strings long enough to be an attack.
- **Range** - level 1-60, gold 0-99 999 999, attributes 0-300, item level
  1-99, stats -999 to 9999, HP clamped to the derived maximum after gear loads.
- **Allow-lists, never deny-lists** - rarity, element, slot, difficulty,
  status key, quest key and achievement key are each checked against a known
  set. Unknown stat keys are dropped, so a doctored save cannot inject an
  attribute into the combat maths.
- **Text** - Unicode categories `Cc`, `Cf`, `Cs`, `Co`, `Cn` are stripped, so
  control characters, bidi-override and zero-width spoofing cannot reach the
  renderer; NFC normalization; hard length cap.
- **Path traversal** - profile names are filtered to `[A-Za-z0-9 _-]`, and the
  save filename is derived from the **slot integer**, never from typed text.
  `is_within()` re-checks containment before any read or delete.
- **Unhashable keys** - values from untrusted lists are coerced to strings
  before being used as dictionary keys. This one was found by the fuzzer: a
  list inside the quest array raised `TypeError: unhashable type` on load.

### 3.4 Save integrity

Versioned schema, atomic write, rolling backup, automatic fallback to the
backup when the primary will not parse, and a SHA-256 checksum over the
canonical payload.

**The checksum is tamper-evident, not tamper-proof.** The algorithm and the
file both live on the player's own machine, so anyone determined can recompute
it. It exists to catch corruption and casual edits, and to make "my save
broke" diagnosable. Nothing security-critical rests on it, and a mismatch
produces a warning rather than a refusal - the file still loads, with every
value validated on the way in.

---

## 4. Verification

| Check | Result |
|---|---|
| `python -m pixel_rpg.audit` | clean - 11 runtime invariants, secret scan, `.gitignore` hygiene |
| `python tests/test_all.py` | 90 unit tests pass |
| `python tests/smoke.py` | 27 end-to-end checks pass |
| `python tests/layout.py` | ~70 screens at 3 resolutions, 0 overlaps, overflows or buried text |
| `python tests/fuzz.py 12000` | 12 000 mutated saves, 0 crashes, all invariants hold |
| Scanner self-test | 10 of 10 planted secrets caught, 0 false positives |

### Changes since the first review

The save schema moved to **version 4** to carry the class, relics, New Game+
tier, run modifiers, story flags and the chosen appearance. Every new field goes through the same
validation as the old ones: the class key and relic keys are checked against
allow-lists, the New Game+ tier and modifier list are range- and
membership-checked, and story flags are length-capped strings.

A **cross-save meta profile** (`profile.json`) was added for codex pages and
unlocks. It is validated identically, and treated as non-critical: a corrupt
profile is discarded and rebuilt rather than blocking startup, because losing
it costs codex pages, not a run.

The fuzzer found one real defect in this round, now fixed: quest and
achievement keys read from a save were used as dictionary keys without being
coerced to strings first, so a list in that array raised `TypeError:
unhashable type` and took the load down. Untrusted values are now coerced
before any lookup.

A **distributable `.exe`** is now built with PyInstaller. It bundles CPython,
pygame and SDL; it ships unsigned, so Windows SmartScreen will warn on first
run. The build is reproducible from source with `python tools/build_exe.py`,
which is the honest answer to "should I trust this binary" — rebuild it.

---

## 5. Remaining risks, stated plainly

1. **A determined player can still edit their save.** Values are clamped to
   the declared maxima, so a tampered file yields at most a maxed-out
   character; it cannot break the game or reach outside the save directory.
   Preventing local cheating in an offline single-player game is not
   achievable and is not a goal.

2. **The checksum is not a security boundary.** See section 3.4.

3. **A save whose structure is wildly out of spec is rejected whole**, not
   partially repaired. This is deliberate - a 9000-character item name means
   the file is not ours - and the `.bak` fallback limits the blast radius. The
   trade-off is that one absurd field can cost a save if the backup is also
   bad.

4. **`pygame` is the only required dependency.** It is a large C-extension
   surface this project does not control. Keep it patched; the range pinned in
   `requirements.txt` allows patch releases.

5. **The save directory itself is not sandboxed.** Anything already running as
   the user can read or write those files. That is inherent to a local game;
   the directory is created `0700` on POSIX, which is as far as this can go.

6. **The optional online path is unimplemented.** `API_KEY` is plumbed and
   documented, but nothing sends it anywhere. If online features are added
   later they need their own review: TLS verification, request timeouts,
   response validation, and a rate limit on the outbound call.

---

## 6. Items from the brief that did not apply

Stated explicitly rather than silently skipped.

- **"Rate limiting on all endpoints, max 5 attempts per 15 min on login
  routes"** - there is no login, and the one listener that exists (co-op
  hosting) is rate limited per peer rather than per route. The policy was
  applied by
  analogy to save-slot decode failures and to disk writes (section 3.1).
- **"Nothing exposed in the frontend"** - there is no frontend. The equivalent
  control is that no secret can reach a save file or the rendered UI, which is
  enforced and tested.
- **"Nothing committed to Git"** - this project is not currently a Git
  repository. `.gitignore` is in place and correct for when it becomes one,
  and saves live outside the tree entirely. Nothing has been committed or
  pushed; that remains your call.
