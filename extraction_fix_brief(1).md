# Extraction Phase — Metadata & Narration Bleed Fix Brief

> **Read this before touching any code.** Every claim below is backed by an actual row from
> `1782556205306_metadata.json` or `flagged_transactions.csv` from the most recent 144-file run.
> Nothing here is guessed. Quote these examples to Claude Code verbatim — do not paraphrase them
> into something vaguer, because the exact strings are what make the bug reproducible.

---

## 0. First, a correction to the original framing

Going in, the assumption was "metadata extraction is broken for most files." That's not what the
data shows. Of the 144 files in this run, **89 failed extraction entirely** (status=`FAILED`,
already known/expected per `phase1 final.md` §10.10 — corrupt `.xls` files, a few PDFs that hit
upload-path issues). Those 89 have no `account_details` at all because the file never got that far.
That's a separate, already-documented problem and is **not** what this brief is about.

The real population to look at is the **55 files that succeeded** (`status: "ok"`). Within those
55, the metadata fields are mostly fine — but a specific, repeatable subset of failures exists, and
they have a single shared root cause. That's what this brief fixes.

---

## 1. Bug Group A — Label/Value Column Misalignment in the Header Block

### The evidence

File `_nfscbsdata__...87889641689...pdf` (account holder correctly read as "Mr. Harish Kumar"
elsewhere in the same record) also contains, in the same `account_details` block:

```json
"joint_holder": "Opening",
"nominee_name": "JOINT HOLDER :"
```

`"Opening"` is not a joint holder's name — it's almost certainly the start of an "Opening Balance"
label. `"JOINT HOLDER :"` is not a nominee's name — it's a label, sitting where a value should be.
Two fields are holding each other's neighbour's content. This is a **one-position shift**, not
random noise — the regex captured the right amount of text, from the right region of the page, but
attributed it to the wrong field name.

A second, sharper instance of the same family: file `9810055876.pdf` has:

```json
"account_holder": "DEEPAK",
"nominee_name": "MRS REKHA"
```

"DEEPAK" alone, with no surname, while a full two-word name ("MRS REKHA") sits one field over in
`nominee_name`, strongly suggests the holder's real full name was split across two visual columns
or two adjacent lines, and the regex grabbed only the first token before the column boundary,
leaving the rest to bleed into the next label it matched.

A third instance, different failure shape but same root cause (reading text in the wrong order
relative to its visual layout): file `4185179967.pdf` has:

```json
"account_holder": "PRIVATE LIMITED"
```

"PRIVATE LIMITED" is the tail end of a company name (the rest of which is presumably the actual
account holder string, e.g. "XYZ TRADERS PRIVATE LIMITED") — but only the suffix was captured,
meaning the regex anchor matched partway through a longer line, not at its start.

### Root cause

`account_extractor.py`'s regex-based reader assumes each label and its value sit in a predictable
adjacent position relative to each other in the raw extracted text. For statements where the
header is printed in a **two-column layout** (label on the left, value on the right, OR
label-and-value pairs side by side: e.g. `Account Holder: DEEPAK    Nominee: MRS REKHA`), pdfplumber's
`extract_text()` reads the page in a left-to-right, top-to-bottom **reading order that does not
preserve which value belongs to which column** when two columns sit on the same visual line. The
text comes out interleaved: `Account Holder: DEEPAK Nominee: MRS REKHA` might actually extract as
`Account Holder: DEEPAK Nominee:` `MRS REKHA` split across what looks like two lines, or worse,
`Account Holder:` and `Nominee:` end up adjacent with both values displaced by one slot.

This is structurally the **same defect class** already diagnosed and fixed for the *transaction
table* in Session 4 / the page-break work (header text physically interleaving with the thing
you're trying to read) — except here it's happening in the **metadata header block**, not the
transaction rows, and nothing in the codebase currently guards against it there.

### The fix (general, not file-specific)

Do **not** patch `account_extractor.py`'s regex to special-case "Opening" or "DEEPAK" — that
fixes these two files and nothing else. Instead:

1. **Detect multi-column header layout before running label regex.** Before extracting metadata,
   check the raw header text for the structural signal of a two-column layout: multiple
   `Label:` patterns appearing on what extracts as a single line, or a `Label:` immediately
   followed by another `Label:` with no value-looking text in between. If this signal is present,
   the page must be re-extracted with **column awareness** — pdfplumber's `extract_words()` (which
   returns each word with its x/y coordinates) instead of `extract_text()`, so left-column and
   right-column label/value pairs can be grouped by horizontal position before being matched as
   key:value, the same way `_parse_metadata_block` in `extractor_excel_csv.py` already handles
   `Label | Value` cell pairs for spreadsheets — this is the PDF-side equivalent of that same idea.

2. **Add a sanity filter on extracted values, applied to every field, not the two seen in
   examples.** A holder/nominee/joint-holder value should not exactly equal a known label fragment
   (the literal strings "Opening", "Closing", "Period", "Joint Holder", "Nominee", any string
   ending in `:`). If a captured value matches this filter, treat the field as **not extracted**
   (empty) rather than keeping a label as if it were a name — this prevents a known-bad value from
   silently passing through as if it were real data, the same trust principle the whole project
   already applies to balance-mismatched rows.

3. **Add a minimum-token sanity check for names specifically.** A person's name extracted as a
   single token with no surname (e.g. "DEEPAK") when neighbouring fields have multi-token values
   is a signal — not proof — that something cut off early. Don't auto-correct this (don't guess a
   surname), but **flag it in `metadata_source`** as e.g. `"regex_partial"` instead of `"regex"`,
   so the investigator dashboard can visibly distinguish "we're confident in this name" from "we
   got a fragment." This costs nothing and turns a silent partial failure into a visible, honest one.

4. **Tell Claude Code explicitly:** *"This must not branch on bank name, IFSC prefix, or any
   string specific to Kotak, IDFC First, or any other bank seen in these examples. The detection
   signal is structural — multiple label-colons close together with no value between them — not
   which bank produced the file."*

---

## 2. Bug Group B — Account Number Pulled From the Wrong Reference Field

### The evidence

File `6147181405386.pdf`: the **filename itself contains** `6147181405386`, which is overwhelmingly
likely the real account number (this is the standard naming convention seen across this entire
dataset — see `87889641689` in the first file matching its filename, `4185179967.pdf` matching its
own `account_number`, etc.). But the extracted `account_number` field reads:

```json
"account_number": "56706993"
```

with `ckyc_number: "XXXXXXXXXXX6099"` also present in the same record. `56706993` is 8 digits —
short for an Indian bank account number, and a strong candidate for being a **customer ID, a
branch code, or some other reference number** that happened to appear near an "Account No" label
in the source text, while the real (likely 13-digit, matching the filename) account number sat
somewhere else on the page that the regex didn't reach or didn't prioritize.

### Root cause

The regex extraction in `account_extractor.py` takes the **first number-like match** near an
account-number label pattern, without:
- Checking digit-length plausibility (Indian bank account numbers are typically 9–18 digits;
  customer IDs, branch codes, and reference numbers are often shorter and look similar in raw text).
- Cross-checking against any other number already known to be associated with this file (the
  filename, if it's a digit string, is free corroborating evidence already sitting in the upload).

### The fix

1. **Add a digit-length plausibility filter.** When multiple numeric candidates are found near
   account-number-like labels, prefer the longest plausible candidate (9+ digits) over a shorter
   one, rather than just taking positional "first match." This is arithmetic on string length, not
   a bank-specific rule.

2. **Cross-check against the filename when the filename is itself a digit string.** This dataset's
   own naming convention (`87889641689.pdf`, `4185179967.pdf`, `6147181405386.pdf`, etc.) already
   gives a free, zero-cost corroborating signal. If the filename (minus extension, minus any
   trailing date-range suffix like `-23-11-2024to11-12-2025`) is purely numeric and differs from
   the regex-extracted account number, surface both as candidates rather than silently trusting
   the regex one — e.g. add an `account_number_source` field (`"regex"` vs `"filename_match"` vs
   `"conflict"`) so a human can resolve a genuine conflict instead of it being invisible.

3. **This is not a replacement for the regex** — filenames aren't guaranteed to be account numbers
   for every bank in every case (you already have CSV/PDF pairs of the *same* account with
   different filename conventions — e.g. `138488664629235-23-11-2024to11-12-2025.csv` vs `.pdf`,
   both correctly showing account_number `138488664629235` from their own internal text). The
   filename is a **cheap corroboration signal**, never the primary source of truth.

---

## 3. Bug Group C — "LUCKNOW" / "SITAPUR" / "MYSORE" as Account Holder Name (City-as-Name)

### The evidence — this is the clearest, most damning example in the whole dataset

Five files share one account number family pattern (same dataset, PDF+CSV pairs of presumably the
same underlying statements), and the **CSV and PDF extraction of the same account disagree on the
account holder**:

| File | account_holder extracted |
|---|---|
| `138488664629235-...csv` | `"KAVYA BOSE"` ✅ correct-looking |
| `138488664629235-...pdf` | `"LUCKNOW"` ❌ a city name |
| `250269305544183-...csv` | `"SACHIN CHAUHAN"` ✅ |
| `250269305544183-...pdf` | `"SITAPUR"` ❌ a city name |
| `269415176159622-...csv` | `"ANJALI DAS"` ✅ |
| `269415176159622-...pdf` | `"LUCKNOW"` ❌ |
| `464196045738107-...csv` | `"DEEPA CHHAYA DESAI"` ✅ |
| `464196045738107-...pdf` | `"LUCKNOW"` ❌ |
| `520698390475976-...csv` | `"AISHWARYA PATEL"` ✅ |
| `520698390475976-...pdf` | `"MYSORE"` ❌ |

This is the single most useful piece of evidence in this whole brief, for two reasons. First, it
proves the underlying data has the correct name somewhere — the CSV extraction of the **exact same
account** got it right, so this is not a case where the source document lacks the information.
Second, it proves the bug is specific to the PDF text-extraction path, and it's consistent across
five different files — meaning it's a structural property of how this particular statement
template lays out its header, not a one-off.

### Root cause

These are almost certainly the **same statement template** (note: all five PDFs got
`bank_name: "Unknown Bank"` too — another shared signal that they're the same source format the
IFSC/bank-name lookup doesn't recognize). The account holder's name and their branch city are
printed close together in the header — likely something like:

```
Account Holder: KAVYA BOSE
Branch: LUCKNOW
```

or even on the same line. The regex pattern for account holder is matching too greedily, too
loosely-anchored, or matching the **wrong label entirely** — it's plausible the regex intends to
capture the text after "Account Holder:" but is actually matching after "Branch:" (or a similarly
positioned label), because in this template's specific layout the two labels are close enough
together that whichever one the regex pattern is anchored to is the wrong one for this template.

### The fix

1. **Get the raw header text for one of these five files and look at it directly before writing
   any fix.** This is non-negotiable, and it's the single highest-value diagnostic step in this
   entire brief — Claude Code (or you) must print the actual raw extracted text for, say,
   `138488664629235-23-11-2024to11-12-2025.pdf`'s first 40 lines, and visually confirm exactly
   where "KAVYA BOSE" sits relative to where "LUCKNOW" sits, and which label each one follows. Do
   not guess the layout from this brief — confirm it, because the fix depends on the real
   adjacency, not an assumption.

2. **Add a name-plausibility filter, structural not a hardcoded city list.** A captured "name"
   that is a single ALL-CAPS or Title-Case word, with no second word, appearing in a field that
   should hold a person's full name, is suspicious on its own structural grounds — most Indian
   names captured correctly in this very dataset are 2–4 space-separated words ("RADHA REKHA
   SAXENA", "ARJUN AMIT KUMAR"). A single-word capture is a weak signal worth flagging (not
   auto-rejecting — some real names are one word, e.g. "DEEPAK" appeared correctly in some
   contexts) — but combined with the fact that a "Branch" or "City" label is known to exist
   nearby in the same template, a single-word value should lower the confidence and route to
   `metadata_source: "regex_low_confidence"` rather than being reported with the same confidence
   as a 3-word name.

   Do **not** hardcode a list of Indian city names to filter against — that's the exact kind of
   bank-specific (or in this case template-specific) overfit patch the project has already been
   burned by once (the 25→29→31 regression spiral). The fix has to come from fixing the
   label-to-value anchor once the real adjacency is confirmed in step 1, not from blacklisting
   "LUCKNOW".

3. **After the fix, the regression check is exactly these 5 files, plus the corresponding CSV
   files as the answer key.** This is a rare case where you have ground truth handed to you for
   free: the CSV already says what the name should be. Use it. The fix is only accepted if all 5
   PDF extractions now match their CSV sibling's account_holder.

---

## 4. Bug Group D — CSV Files With No Metadata Extraction Attempted At All

### The evidence

```json
"25078124219247-YASH DUBEY.csv"             → account_holder: "", account_number: "UNKNOWN-...", metadata_source: "none"
"79895082327702 ARJUN SHAILESHBHA Excel Statement.csv" → metadata_source: "none"
"79895082327702-ARJUN SHAILESHBHA.csv"      → metadata_source: "none"
```

Notice the filenames literally contain the holder's name ("YASH DUBEY", "ARJUN SHAILESHBHA") and,
in two cases, the account number. `metadata_source: "none"` means no extraction path was even
attempted for these — not regex, not the Excel metadata block parser, not the LLM fallback. This is
different from Bug Groups A–C (which are wrong answers); this is **no answer at all**, despite the
file being a CSV that the `excel_metadata_block` path handles correctly for *other* files (see
`331087 CASA Account Statement...xlsx` entries, which extract holder names correctly via
`excel_metadata_block`).

### Root cause

This is almost certainly **not** an extraction-difficulty problem — it's a **routing gap**. These
specific CSVs likely don't have a key:value metadata block above their transaction table at all
(unlike the `.xlsx` files that succeeded via `excel_metadata_block`), so `_parse_metadata_block`
correctly finds nothing — but when it finds nothing, nothing else is tried. There is no fallback
to "check the filename" or "fall back to LLM" for this specific CSV code path, even though the
LLM fallback demonstrably exists and is wired up for other formats (see `285265765401_stmt.xls`
and `913628731289_stmt.xls`, which got `metadata_source: "llm_fallback"`, even though it returned
empty there too — see Bug Group E below).

### The fix

1. **Make the CSV/Excel path fall through to the same Tier-1 LLM metadata fallback that the
   text-extraction path already has**, whenever `_parse_metadata_block` returns nothing for any
   key field. Currently this fallback appears to exist for `.xls` (Bug Group E shows it firing,
   just not succeeding) but not for `.csv`. This should be one shared fallback step, reachable
   from every file route, not implemented per-format.

2. **Add filename-derived candidates as a last-resort, clearly-labeled fallback**, specifically
   for this dataset's convention of putting the holder name and/or account number directly in the
   filename. If both the metadata-block parser and the LLM fallback return nothing, extract a
   plausible name/number from the filename itself and label it `metadata_source: "filename_derived"`
   — clearly lower-confidence than any in-document extraction, but strictly better than leaving the
   field permanently blank when the answer is sitting in the filename for free. This must be
   reported as low-confidence, never silently presented as equal to a document-sourced field.

---

## 5. Bug Group E — LLM Fallback Itself Returning Nothing

### The evidence

```json
"285265765401_stmt.xls" → account_holder: "", metadata_source: "llm_fallback"
"913628731289_stmt.xls" → account_holder: "", metadata_source: "llm_fallback"
```

The fallback was triggered (so the routing worked, unlike Bug Group D) but it came back empty.

### Root cause — needs the same isolation discipline already established for the metadata bug last session

Per the diagnostic method already agreed on for this exact class of problem: **before writing any
fix, get the actual text string that was sent to the LLM for these two files** and look at it. Two
real possibilities, each with a different fix:

- If the string sent to the LLM is itself empty, truncated, or garbled (e.g. these `.xls` files
  might be hitting the same HTML-masquerading-as-XLS corruption documented in `phase1 final.md`
  §10.10 — note both filenames end in `_stmt.xls`, matching that known-bad pattern), the fix is
  upstream in the Excel reader, not in the LLM call or prompt at all.
- If the string sent to the LLM clearly contains the holder's name in readable form and the LLM
  still returned nothing, the fix is in the prompt or in how the response is parsed.

**Do not fix this until that check is done.** This is the exact same investigative discipline
already used successfully for the Kotak split-page bug — find the actual input, don't guess from
the output.

---

## 6. Bug Group F — Narration Field Swallowing Multiple Transactions (the "flagged narration" issue)

### The evidence — this is the clearest pattern in `flagged_transactions.csv`

Every single one of the long-narration flagged rows for account `56706993` has a **clean, single,
plausible Date / Debit / Credit / Balance** — e.g.:

```
Date: 28/04/2025 | Balance: 457.86 | Debit: 0.0 | Credit: 200.0 | flag_reason: balance_mismatch
```

— but the `Narration` field for that exact row contains **roughly 5–10 entire other transactions'
worth of text concatenated together**, including their own dates, reference numbers, amounts, and
running balances, e.g. (truncated): `"IMPS Credit Transaction 518660782215 MOB-IMPS-CR/GUNGUN
TRA/... 37566 05-07-2025 12:44:13 ... ATM WITHDRAWAL ... 10,000.00 0.00 30,184.96 ... 24.78 0.00
30,160.18 ..."` — that single Narration string contains at least 4 other complete transaction
records' worth of reference numbers, amounts, and balances.

Every one of these rows is flagged `balance_mismatch` with `mismatch_diagnosis: missing_transaction`.

### Root cause — this is the page-break/header-skip bug, but happening from the *opposite direction*

This is the **same defect family** already diagnosed for the split-transaction page-break bug
(documented in the prior session), but manifesting as the mirror-image symptom. Previously: a
transaction was *lost* because the parser gave up at a page-header block and didn't look past it
for the continuation. Here: the parser is **not stopping where it should**, and is instead
treating an entire block of subsequent text — which contains many real, separate, date-stamped
transactions — as if it were all one narration, until something (likely the next successfully
date-anchored line, or end of a chunk) finally breaks the row.

Look closely at the captured text: it contains repeated instances of the pattern
`<reference_number> <DD-MM-YYYY> <HH:MM:SS> <DD-MM-YYYY> <transaction type text>... <amount> <amount> <balance>`
— meaning **each one of those repetitions is a real transaction row that the line-level parser
failed to recognize as the start of a new row**, and instead glued onto the narration of the
transaction before it. The single clean Date/Debit/Credit/Balance kept in the structured fields
for the flagged row is just whichever transaction happened to "win" when the row was eventually
cut off and written out — everything else in that swallowed block became orphaned text stuffed
into Narration, with its own dates and amounts now invisible to the analysis phase entirely
(not just mis-labeled — genuinely unrecoverable as separate transactions unless this is fixed).

This is very likely happening because: this particular statement's transaction-start signal (what
the parser uses to decide "a new row begins here") is not the same shape the parser expects — for
example, the **reference number printed before the date**, on a line like `37566 05-07-2025
12:44:13 05-07-2025 ATM WITHDRAWAL...`, rather than date-first. If the date-anchored line parser
is looking for a date strictly at the *start* of a line, and this statement template puts a
reference number first, every one of this statement's transaction lines silently fails the
"is this a new row" check and gets appended to whatever row came before — for potentially dozens
of rows in a row, until the accumulated text eventually gets flushed (likely when a chunk/page
boundary or some unrelated trigger forces a cut), producing exactly the symptom seen: one flagged
row with everything-and-the-kitchen-sink in Narration, immediately followed (per the
`missing_transaction` diagnosis) by the balance chain breaking, because dozens of real transactions
were never separated out at all.

### Why this matters more than it might look like at first glance

This is **not just a cosmetic narration-formatting issue.** Every transaction trapped inside one of
these bloated Narration strings is invisible to every downstream pattern detector — it has no row
of its own, no separate counterparty extraction, no separate entry in the graph. If any of these
swallowed transactions happens to be part of a round trip, a structuring cluster, or a transit
pattern, **that pattern becomes undetectable**, not just under-confident. Given how many flagged
rows on this one account alone show this symptom, this account's transaction history is currently
substantially incomplete from the analysis phase's point of view, not just slightly noisy.

### The fix

1. **Confirm the transaction-start signal for this template before writing a fix.** Pull the raw
   extracted text for account `56706993`'s source file and look at literally one real transaction
   line in full, unprocessed. Confirm whether it's reference-number-first, date-first-but-with-an-
   unexpected-prefix-character, or something else. Do not guess from the Narration dump alone —
   the Narration dump is post-corruption; the raw text is pre-corruption and is the only reliable
   source for this diagnosis.

2. **Generalize the row-start detector, don't add a special case for this one number format.**
   The current logic apparently requires a date at the start of the line to recognize "new
   transaction begins here." The fix should broaden the **structural definition** of a row-start
   signal to: a date appearing within the first N characters of a line, OR immediately following a
   short numeric token (a reference number) at the start of a line, where "short numeric token" is
   defined by digit-count plausibility, not by matching this specific statement's reference number
   length. Tell Claude Code explicitly: *this must generalize to any statement where a reference
   number, transaction ID, or sequence number is printed before the date on a transaction line —
   not just this account's format.*

3. **Add a structural safety net independent of the row-start fix:** if a single Narration field,
   after all parsing, is implausibly long (set a generous threshold — e.g. anything over ~150–200
   characters is already far longer than any real bank narration text seen elsewhere in this
   dataset) **and** contains more than one date-like substring matching the document's own date
   format, that is itself a strong, format-agnostic signal that multiple transactions were
   merged into one row. Flag any row meeting this condition distinctly — e.g.
   `flag_reason: "narration_contains_multiple_transactions"` — separate from ordinary
   `balance_mismatch`, so this specific failure mode is visible and countable on its own, rather
   than being buried inside the general balance-mismatch bucket where it currently hides. This is
   a detection safety net, not a substitute for fixing the actual row-start logic in step 2 — it
   exists so that if this defect ever recurs in a different statement template in the future, it's
   caught and reported clearly instead of silently producing more 2000-character narration fields.

4. **The regression/verification check:** after the fix, re-run extraction on account `56706993`'s
   source file specifically. The number of distinct transactions extracted should increase
   substantially (every transaction currently buried in these bloated Narration strings should
   become its own row), the longest Narration field across the whole file should drop to a normal
   length (under ~100 characters, consistent with single UPI/IMPS/NEFT narration text seen
   elsewhere in this dataset), and the balance-mismatch count for this file should drop toward
   zero, because the "missing" transactions weren't actually missing — they were trapped inside
   another row's narration the whole time.

---

## 7. What NOT To Do — the same discipline that's already been hard-won

These ground rules apply to every fix in this brief, not just one:

- **Do not hand Claude Code the whole `flagged_transactions.csv` or the whole `metadata.json` and
  ask it to "fix the issues."** That produces the exact overfitting spiral already lived through
  with account-holder extraction (25 → 29 → 31 missing). Hand it **one isolated example per bug
  group**, exactly as curated in sections 1–6 above.

- **Demand a before/after regression count for every fix**, using the specific file lists already
  identified in this brief as the baseline:
  - Bug Group C: the 5 PDF/CSV pairs listed in section 3 — after the fix, all 5 PDFs must match
    their CSV's account_holder. If any of the 50 files that currently extract *correctly* in this
    run regress, the fix is rejected, no exceptions.
  - Bug Group F: account `56706993`'s file — narration length distribution and balance-mismatch
    count before and after, plus spot-check that no previously-passing file's reconciliation rate
    drops.

- **No fix may branch on a bank name, a specific filename, an IFSC prefix, or a literal string
  like "LUCKNOW" or "DEEPAK."** Every fix above is written in terms of structural signals (column
  position, token length, label adjacency, date-pattern position) specifically so it generalizes
  beyond the examples used to find it. If a proposed fix only makes sense by hardcoding one of the
  literal example values from this brief, that is the signal to stop and find the real structural
  cause instead.

- **Cap iteration at 2 attempts per bug group before stopping and reporting a limitation
  honestly**, exactly as already established in `analysisinstruction.md`'s held-out testing
  protocol. If bug group C's name/branch mislabeling can't be cleanly resolved in 2 attempts,
  report it as a known limitation with the affected file list, rather than continuing to iterate
  and risking new regressions elsewhere.
