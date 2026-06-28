# Multi-Key Groq Rotation — Fix Brief

> Read this alongside `root_cause_diagnosis.md` and `extraction_fix_brief.md`, but treat it as a
> **separate, third fix track**. Don't hand all three documents to Claude Code in one prompt —
> same discipline as before: one isolated problem per session, with its own regression check.

---

## What you actually have, and what changes

You have **5 Groq API keys**, not 56 — confirming the number before this goes anywhere, since a
wrong key count would silently break the rotation logic from day one.

Your current `config/settings.py` (per `phase1 final.md` §2.2 and §3) loads exactly two keys for
extraction — `GROQ1_KEY` and `GROQ2_KEY` — used for two **different roles with different quotas**:

- **GROQ1-role**: text calls — `llm_structurer.py` (schema discovery, full statement read, metadata
  fallback) and `column_identifier.py` (Excel/CSV column mapping). Model: `llama-3.3-70b-versatile`.
- **GROQ2-role**: vision calls — `vision_extractor.py` (image files, scanned PDF pages) and the
  Tesseract-confidence fallback in `extractor_ocr.py`. Model: `meta-llama/llama-4-scout-17b-16e-instruct`.
- **GROQ3** is explicitly reserved for the analysis phase and is never loaded during extraction.

These are different models with different daily quotas on Groq's side. **This matters for how the
5 keys get distributed** — see the design decision below.

## The design decision: rotate within each role's pool, not across one undifferentiated pool

**Do not** dump all 5 keys into one flat list and round-robin every call across all of them
regardless of whether the call is text or vision. If you do that, a vision-heavy stretch of the
batch (e.g. several scanned PDFs in a row) can burn through keys that a later text call needed,
and a key that's already exhausted on the text model might still have vision quota left (or vice
versa) — quota exhaustion is per-model on Groq's side, not a single shared number per key.

**Instead:** split the 5 keys into the two existing roles (e.g. 3 keys for text, 2 for vision, or
whatever split matches your actual call volume — text calls are typically far more numerous than
vision calls per your own tier breakdown showing 76 cheap-parse / 1 llm_full_read vs. vision-only
paths for images and scanned PDFs, so the split doesn't need to be even). Each role gets its own
rotating list. This is a direct, minimal extension of the role split you already have — it doesn't
add a new concept, it just changes "one key per role" to "a list of keys per role."

## Where this lives in your existing architecture

You already have exactly the right chokepoint for this: **`llm_interface.py`**, described in
`phase1 final.md` §2.14 as "the single LLM facade... the most important architectural addition...
Today it wraps the Groq-backed modules; tomorrow a local model swap touches only this file." Key
rotation is a smaller version of the same idea — it should live in one place, not be duplicated
into `llm_structurer.py`, `column_identifier.py`, and `vision_extractor.py` separately (which is
exactly the problem already flagged in §10.2: "LLM client created in 3 places... not a true
one-file swap").

**This is the moment to fix both problems together**, since they're the same underlying issue
(API client creation duplicated across 3 files instead of centralized): build a small key-pool
manager, and make all three modules request a client *from* it rather than constructing their own.

## The fix, concretely

### 1. A key-pool object, one per role

Add a small class — call it `GroqKeyPool` — that holds an ordered list of keys for one role
(text or vision), tracks which ones are currently marked dead **for this run only** (in-memory,
not persisted to disk or config), and hands out "the current best key" on request.

Responsibilities, kept deliberately narrow:
- `get_key()` — returns the first non-dead key in the pool.
- `mark_dead(key)` — marks a key dead for the remainder of this run. Called only when a call using
  that key fails with the *daily-quota* signature your code already knows how to detect.
- If every key in the pool is dead, raise a clear, specific error — e.g.
  `"All N text-role Groq keys exhausted for today's quota — extraction cannot continue for
  remaining text-tier calls."` This must **not** silently fall through to a stale or empty key and
  produce a confusing downstream error; it should fail loudly and specifically, exactly the same
  way `require_extraction_keys()` already fails fast and readably when a key is missing entirely.

### 2. Reuse the exact failure signal you already built — don't reinvent it

You already have `_is_nonretryable(err)` (per `phase1 final.md` §2.13 and §5.2) detecting 429
errors with "per day" / "tokens per day" / "tpd" in the message. **This is precisely the signal
that should trigger `mark_dead()` and a rotation, not a separate new check.** Concretely:

- Today: `_is_nonretryable(err)` returns True → the retry loop breaks immediately, the call fails,
  the file may escalate to a higher tier or get flagged.
- After this fix: `_is_nonretryable(err)` returns True → call `pool.mark_dead(current_key)`, get
  a fresh key from the pool via `pool.get_key()`, and retry the **same call once** on the new key,
  before falling back to the existing fail-fast behavior if the pool is now also exhausted.

This is a small, surgical change to the existing retry loop in `llm_structurer.py` and
`extractor_ocr.py` — not a rewrite. The 429 *detection* logic doesn't change at all; only what
happens immediately after detection changes (rotate-and-retry-once, instead of immediate failure).

### 3. Where the client gets created — fixing §10.2 along the way

Per `phase1 final.md` §10.2, the Groq client is currently instantiated separately in
`llm_structurer.py`, `column_identifier.py`, and `vision_extractor.py`. Each of those three call
sites should instead ask `llm_interface.py` for a client, and `llm_interface.py` asks the
appropriate role's `GroqKeyPool` for the current key before constructing (or reusing) the client.
This means:
- A key rotation in the text pool is visible to **both** `llm_structurer.py` and
  `column_identifier.py` immediately — they're both text-role consumers of the same pool, so a key
  marked dead by one is dead for the other too, with no separate bookkeeping needed.
- The local-model-swap goal from §10.2 gets fixed as a side effect: once client creation is
  centralized in `llm_interface.py`, a future swap to Ollama/LM Studio touches one file, not four.

### 4. What must NOT change

- **No change to the 429 *detection* logic.** `_is_nonretryable` already correctly distinguishes
  "daily quota gone" from other error types (e.g. a genuine 413 payload-too-large, which has
  nothing to do with key rotation and should keep failing the same way it does today).
- **No persistence of dead-key state across runs.** Daily quotas reset daily; a key marked dead at
  11:58 PM should be usable again at 12:01 AM without anyone needing to remember to reset a flag in
  a config file. In-memory-only, scoped to one batch run, is the correct and simpler choice.
- **No change to which role a given call belongs to.** Text calls keep using the text pool, vision
  calls keep using the vision pool. Don't let a key-rotation fix quietly turn into a refactor of
  which functions call which model — that's scope creep that risks introducing new bugs into code
  that currently works.
- **GROQ3 stays untouched and unloaded during extraction**, exactly as today. This fix is scoped to
  the keys actually used during extraction; don't fold the analysis-phase key into this pool.

## Verification — make it observable, not just "trust it works"

Add one line to each file's per-file audit record (the same `file_record` structure that already
tracks `tier`, `reconciliation_rate`, and `llm_calls` per `phase1 final.md` §2.19) — something like
`"key_rotations": 0` incremented every time a rotation happens for that file's calls. This costs
almost nothing and gives you a free, visible answer to "did rotation actually fire, and how often"
the next time you look at `metadata.json` — exactly the kind of evidence that let us diagnose the
last two bugs in this project from the data itself rather than guessing. Without this, a rotation
bug could hide for weeks the same way the file-not-found cliff did.

## Regression check

This is a genuinely easy one to verify, more so than the parsing bugs:
1. Temporarily set one key in a pool to an obviously invalid string (or a real key you've
   intentionally exhausted) and re-run a small batch (10–15 files mixing text and vision sources).
2. Confirm: calls that would have hit the bad key now succeed via the next key in that role's
   pool, `key_rotations` increments on the affected files' audit records, and **no file fails or
   flags differently than it would have with a fully healthy single key** — rotation should be
   invisible to the parsing/validation logic downstream, only visible in the audit trail.
3. Set every key in one role's pool to invalid and confirm the system fails loudly with the
   specific "all N keys exhausted" message — not a generic crash, not a silent empty result.
