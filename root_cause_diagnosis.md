# The Real Problem — Why 20-30 Fix Attempts Haven't Worked, and What Actually Fixes It

> Read this first. It changes the diagnosis you came in with. The fix is small, fast, and nothing
> like the previous 20-30 attempts — because you've been trying to fix the wrong layer.

> **Dataset note:** `original bank statements/` now contains the **full 162 files** supplied by
> the hackathon organizers — not the 103 files validated in `phase1 final.md` Session 4, and not
> the 144 files referenced in the batch run analyzed in this document. Counts and file lists in
> this document and in `extraction_fix_brief.md` are both drawn from a 144-file run, which is a
> subset of the full 162. Treat every number in both documents as provisional until the fixes are
> re-validated against the complete 162-file set — the regression checks below should be run
> against all 162, not just the 144 already analyzed, since the remaining ~18 files are unseen and
> may surface new instances of Bug Groups A–F, or new failure shapes entirely.

---

## The headline finding

You said 89 of 144 files are failing and you've tried 20-30 times to fix it. **The 89 failures are
not 89 bugs. They are one bug**, and it is not in any parsing code you have touched in 20-30
attempts. Here is the proof, straight from your own `metadata.json`:

```
[55] ok       520698390475976-21-12-2020to26-11-2025.pdf
[56] FAILED   654658757412329-05-09-2024to26-11-2025.csv      ← the cliff edge
[57] FAILED   654658757412329-05-09-2024to26-11-2025.pdf
[58] FAILED   882358884158137_Statement.pdf
[59] FAILED   958533930537174-14-02-2024to11-12-2025.csv
... every single one of the remaining 88 files, all FAILED, no exceptions
```

Every one of those 88 failures has the **identical error message shape**:

```
File not found: /Users/tejas.m.s/.../uploads/run_20260627_152635/654658757412329-...csv
File not found: /Users/tejas.m.s/.../uploads/run_20260627_152635/882358884158137_Statement.pdf
File not found: /Users/tejas.m.s/.../uploads/run_20260627_152635/Statement.pdf
... (88 total, all referencing the exact same run directory: run_20260627_152635)
```

**Files 0 through 55 all read from this same directory successfully.** Then, starting at file 56,
every remaining file — different formats, different banks, different file types, files that
worked fine in earlier sessions per your own `phase1 final.md` — fails with the exact same "the
file isn't there anymore" error, all pointing at the same directory.

That is not a parsing problem. **That is a file-availability problem during the batch run itself.**
Something removed, expired, or never finished writing the rest of `run_20260627_152635/` partway
through processing. The one genuinely different error in the whole batch —
`"argument of type 'float' is not a container or iterable"` on `3277373660.xlsx` at file #19 — is a
real, separate, small bug, and it's the *only* real parsing bug in this entire 89-file group.

## Why this explains the 20-30 failed attempts

Every time you've gone back to Claude Code with "these files are failing," it has had no way to
see this pattern, because the failure presents itself, file by file, as "couldn't process
X.pdf" / "couldn't process Y.xls" — which *looks* like 88 different format/parsing problems,
especially when several of the filenames are `.xls` (your already-known corrupt format) or unusual
PDFs. Each attempt has most likely gone looking for a parsing fix on a file that was never actually
read in the first place — there was nothing to parse, because the file path resolved to nothing.
You cannot fix a parsing bug for a file that was never opened. That's 20-30 attempts spent
debugging code that the failure never actually reached.

## What this means for "should I change the architecture"

**No.** This is not an architecture failure. The tiered-hybrid pipeline, the validator, the
escalation ladder — none of it is implicated by this failure, because none of it ever ran on these
88 files. The actual defect is almost certainly one of these, all infrastructure/orchestration
issues, not extraction logic:

1. **A cleanup step runs too early** — something (a context manager, a `finally` block, a
   temp-directory wipe, an upload-session expiry) deletes or moves `run_20260627_152635/` while
   the batch loop is still partway through iterating its file list.
2. **A timeout cuts the run** — if there's any per-run or per-session timeout (common in web
   upload handlers), and the first ~55 files (many of which are larger PDFs needing OCR/LLM calls)
   consumed most of the allowed time, everything after a certain wall-clock point could fail if the
   handler tears down the working directory once it believes the session is "done" or "expired."
3. **The file list was captured before all uploads finished writing to disk** — if `run_all.py` or
   the equivalent batch entrypoint lists the directory's contents once at the start, but a parallel
   upload/copy process for the remaining files either hadn't finished or got interrupted, files
   that were supposed to land in that directory simply weren't there yet (or anymore) by the time
   the loop reached them.
4. **A path is relative and the working directory changed mid-run** — less likely given the
   absolute paths shown, but worth ruling out if 1–3 don't pan out.

## The actual fix — small, fast, and a completely different shape of work than before

This is the opposite of every other bug in this project so far. You don't need a clever parser
fix. You need to find **one orchestration bug** in the file-handling/batch-runner code, not in
`extraction/*.py`'s parsing logic at all. Concretely:

1. **Find where the batch run iterates its file list and reads from disk.** This is almost
   certainly in `run_all.py` or wherever the upload/run directory (`run_20260627_152635`) is
   created and consumed — not in `extraction_pipeline.py`'s per-file logic, which already correctly
   reports an error rather than crashing (that part is working as designed).

2. **Check what happens to that directory mid-run.** Specifically look for: a `tempfile.TemporaryDirectory()`
   context manager whose `__exit__` could fire before the loop finishes (e.g. if it's scoped to a
   request handler that has its own timeout); any `shutil.rmtree`, `os.remove`, or cleanup call
   anywhere in the upload-handling code; any session/run expiry logic; whether uploads happen
   synchronously (all files present before extraction starts) or asynchronously (files trickling in
   while extraction is already running against the directory listing).

3. **Confirm the hypothesis cheaply before changing anything**, the same discipline you used
   successfully for the Kotak split-page bug: add a log line right before each file read that
   prints whether `os.path.exists(file_path)` is True at that exact moment, and run a small batch
   (10-15 files) end to end. If you see it flip from `True` to `False` partway through, you've
   found the exact failure point and you'll know which of the four hypotheses above is correct
   before writing a single line of fix code.

4. **The fix itself will likely be one of:** moving file uploads to complete fully and verifiably
   before the extraction loop starts (don't start processing until every expected file is
   confirmed on disk); removing or relocating whatever cleanup/expiry logic is destroying the
   directory early; or making the batch runner copy all input files to a stable working location
   at the very start of the run, immune to whatever is causing the original upload directory to
   disappear.

5. **The regression check is trivial and definitive, unlike every other fix in this project:**
   re-run the exact same 144-file batch. Every file that isn't a known-bad `.xls` (the 20 you
   already know are corrupt) or the one real `float` bug should now reach `status: "ok"`. This
   is the cleanest pass/fail test you'll get in this whole project — there's no ambiguity about
   whether it worked.

## The one real parsing bug in this group — `3277373660.xlsx`

Separate from the above, and small: `"argument of type 'float' is not a container or iterable"`.
This is a Python `TypeError` — somewhere in the Excel reading path, code is calling something like
`in`, `len()`, iteration, or a string/dict operation on a value that turned out to be a `float`
(most likely a `NaN` cell, which pandas often represents as a `float`, where the code expected a
string — e.g. checking `if "some_label" in cell_value` where `cell_value` is `NaN` instead of text).

**Fix:** find the exact line via traceback (re-run just this one file with full error output, not
swallowed into the `error` string), and add a type guard — `if isinstance(cell_value, str)` before
doing string/container operations on a cell value, applied generally to whatever function is doing
this (almost certainly somewhere in `_parse_metadata_block` or `_detect_header_index` in
`extractor_excel_csv.py`, since those are the functions that scan raw header rows where empty
cells commonly come through as `NaN` floats). This is a one-line defensive fix, not a redesign.

---

## Putting this together with the earlier brief

You now have two separate, independent fix tracks, and they should **not be mixed into one
Claude Code session or one prompt** — that's exactly the kind of "hand over everything, hope it
sorts itself out" approach that produced the 20-30 failed attempts:

**Track 1 — Infrastructure (this document).** Fixes why 88/144 files never even got read. This is
the highest-leverage fix in the whole project: it's one bug, it's not in parsing logic at all, and
fixing it alone should take you from 55/144 fully processed to roughly 123/144 (144 minus the 20
known-bad `.xls` files minus the 1 float bug, assuming those remaining files parse as cleanly as
the dataset's already-proven 76/103 cheap-parse success rate suggests they will).

**Track 2 — Parsing/metadata edge cases (`extraction_fix_brief.md`, already written).** Fixes the
6 specific bug groups found in the 55 files that already process successfully — label/value
misalignment, account number confusion, city-as-name, missing CSV metadata routing, empty LLM
fallback, and the narration-bloat row-merging bug.

Do Track 1 first. It's faster, it's a single root cause, and — importantly — **once it's fixed,
you'll have ~68 more files' worth of real `account_details` and `flagged_transactions` data to
check against**, which may well surface more instances of (or rule out) the Track 2 bug groups,
giving you a much larger, more reliable evidence base before making any further parsing changes.

## On "if needed, change the architecture"

You don't need to, and you shouldn't — not because the architecture is sacred, but because nothing
in this diagnosis points at it. The tiered-hybrid design, the validator referee, the escalation
ladder are not implicated by either Track 1 (a batch-runner/file-lifecycle bug, completely outside
the extraction pipeline) or Track 2 (regex/parsing edge cases inside functions that are already
working correctly the majority of the time, per your own 76/103 cheap-parse validation). Replacing
the architecture would not touch either of these bugs — it would just give you a new pipeline with
the same Track-1-shaped bug waiting to be found again, because the cause lives in how files are
staged and read before extraction ever starts, not in how extraction works once it has a file in hand.

## On "will this fix work on every file, with zero flagged balance mismatches" — the honest answer

This needs to be said plainly, because it changes what "done" means for this project.

**No fix described in this document or in `extraction_fix_brief.md` can honestly promise zero
flagged rows on every past or future file, and zero flags should not be the target.** Three
separate reasons, each independent of the others:

1. **Some failures are structural, not bugs.** Your own Phase 1 validation already documented 10
   files permanently below 0.90 reconciliation for reasons neither track touches: `.xls` files
   that are actually HTML/XML in disguise (corrupt at the format level, not the parser level),
   statements with integer-only amounts that are genuinely ambiguous against reference numbers,
   and one file that hits a hard Groq API token-limit error. Track 1 and Track 2 fix the bugs they
   found evidence for — they do not, and cannot, fix a file that isn't a valid spreadsheet.

2. **"Every file ever uploaded in the future" is an unbounded claim.** Both fixes are deliberately
   built on structural detection (column position, token shape, label adjacency) specifically so
   they generalize beyond the examples that found them — that's the right design goal, and it's
   why neither document permits branching on a bank name or a literal filename. But a genuinely
   novel layout can still defeat a structural heuristic. A fix that generalizes well is not the
   same claim as a fix that handles all possible future documents — no parser for human-formatted
   documents can honestly claim the second one.

3. **Zero flags is the wrong target, not just an unreachable one.** A `balance_mismatch` flag on a
   real bank statement should only ever be zero if extraction is perfect — that's the whole
   premise this project has used from the start (a bank's own export reconciles by construction).
   The flag mechanism's job is to surface every case where extraction *isn't* perfect. If the
   system is tuned until it reports zero flags regardless of input, that doesn't mean extraction
   became perfect — it means the system stopped telling you when it's wrong, which is strictly
   more dangerous for an investigation tool than a visible flag, because a silent wrong answer
   gets used in analysis without anyone knowing to distrust it.

**The goalpost that's actually achievable, and worth standing behind in a demo or in front of an
investigator:** every flag is explained with a specific reason, nothing is ever silently dropped
or silently corrupted, the count of unexplained structural failures stays small and is documented
by filename and cause, and the fixes in this document measurably shrink that count without
introducing new silent failures elsewhere. That is what the regression checks below are designed
to demonstrate — not "zero flags," but "every flag has a name and a reason, and there are fewer of
them than before."

---

## What "complete and working on unseen future uploads" actually requires from here

Once Track 1 and Track 2 are both fixed and verified against this exact 144-file set:

1. **Re-run the full 144-file batch and confirm the regression checks in both documents.** Specific,
   numeric, falsifiable: file count at `status: ok` should jump from 55 to ~123+; the 5 PDF/CSV
   name-mismatch pairs in `extraction_fix_brief.md` should now agree; narration length on account
   `56706993` should drop to normal.

2. **For "unseen future uploads" specifically** — nothing in either fix is dataset-specific (both
   documents explicitly forbid bank-name/filename-specific branching), so a fix that passes the
   144-file regression check is, by construction, a general fix, not a memorized one. The thing
   that would tell you it's *not* general is if a fix only works by checking for one of the exact
   strings/filenames in these documents — which is exactly why both documents called that out as a
   rejection condition.

3. **One honest caveat to carry forward:** "accurate AF, zero issues, one try" for a generalized
   document-parsing system is not a fully reachable target for novel layouts you haven't seen yet
   — your own validated 103-file run already shows 10 files permanently below 0.90 reconciliation
   for structural reasons (corrupt `.xls` format, integer-only amounts, a 413 token-limit error).
   The realistic target is: the system never silently drops or corrupts data, every failure is
   visible and explained (which is already true — that's how Track 1 was found at all, from the
   `error` field you already had), and the known-failure list stays small and explainable rather
   than growing with each new file format encountered.
