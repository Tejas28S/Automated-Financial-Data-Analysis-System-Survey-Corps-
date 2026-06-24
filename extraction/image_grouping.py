"""
image_grouping.py — Decide whether several uploaded IMAGES belong to ONE statement
or to DIFFERENT statements (Stage 2 of the redesigned image pipeline).

THE PROBLEM (image route only):
    An investigator may upload several images. They might be consecutive pages of
    ONE statement (page 1 has the account header; pages 2-4 are continuations with
    no header), OR they might be unrelated statements of different people/banks.
    The system must not assume either case.

THE SIGNALS, RANKED BY RELIABILITY (all bank-agnostic, all deterministic):
    1. Account number / IFSC — DECISIVE when present on both images:
         • same  → same statement
         • different → different statements (a HARD separator)
         • absent → no information (a continuation page simply omits it; we must
           NOT read "different" into a missing header).
    2. Running-balance continuity — THE LINKING signal. A continuation page lacks
       the account header, so identity cannot link it; but its first transaction
       continues the previous page's balance chain. last_balance(A) handed off to
       first_balance(B) (give or take B's first amount) is arithmetic that is true
       for every bank and is hard to match by accident.
    3. Bank name — used ONLY as a separator (different bank → different statement);
       "same bank" is near-meaningless (two suspects can share a bank).

WHY DETERMINISTIC, AND WHY BIASED TOWARD "SEPARATE":
    A forensic system needs an auditable reason for every merge, so grouping is pure
    code (exact identity match + balance arithmetic), never an LLM guess. The two
    failure modes are NOT equal: a FALSE MERGE fabricates a balance chain and
    attributes one account's transactions to another — evidence contamination. A
    FALSE SPLIT just yields a few partial statements the investigator can recombine.
    So we merge ONLY on decisive evidence (identity match OR a balance hand-off) and
    otherwise keep images separate.

This module reads the already-transcribed TEXT of each image (Stage 1 output). It
makes no API calls.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import re
from typing import Any, Dict, List, Optional

from config.settings import BALANCE_TOLERANCE
from extraction.account_extractor import extract_account_details_from_text
from extraction.standardiser import _parse_date

logger = logging.getLogger(__name__)

# A money token: digits with a 2-decimal part (optionally Indian commas / minus /
# rupee sign). Requiring the decimal separates real amounts/balances from reference
# or cheque numbers, exactly as the standardiser does.
_MONEY_RE = re.compile(r"-?\d{1,3}(?:,\d{2,3})*\.\d{1,2}|-?\d+\.\d{1,2}")
# A date appearing anywhere on a line (numeric or month-name styles).
_DATE_ANYWHERE = re.compile(
    r"\d{1,2}[/\-.][0-9A-Za-z]{2,9}[/\-.]\d{2,4}"
    r"|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}"
)
# Values that mean "no identity" even when a field is technically filled.
_EMPTY_VALUES = {"", "unknown", "unreadable", "n/a", "na", "none", "-"}


def _norm(value: Any) -> str:
    """Lower-cased, trimmed identity value; blank for the various 'unknown' spellings."""
    s = str(value or "").strip()
    return "" if s.lower() in _EMPTY_VALUES else s


def _to_float(token: str) -> Optional[float]:
    """Parses a money token like '1,250.00' / '-34,885.00' to a float."""
    try:
        return float(token.replace(",", "").replace("₹", "").strip())
    except (ValueError, AttributeError):
        return None


def _transaction_rows(text: str) -> List[Dict[str, Any]]:
    """
    Returns one entry per transaction-like line (a line that has a date somewhere AND
    at least one money token): {"date": <parsed date or None>, "money": [floats]}.
    The LAST money value on a row is its running balance; the second-to-last (if any)
    is the amount. Lines like 'Opening Balance: 50,000.00' carry money but no leading
    date and so are not transactions; we require BOTH a date and money.
    """
    rows: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        s = line.strip()
        m = _DATE_ANYWHERE.search(s)
        if not m:
            continue
        vals = [v for v in (_to_float(t) for t in _MONEY_RE.findall(s)) if v is not None]
        if vals:
            rows.append({"date": _parse_date(m.group(0)), "money": vals})
    return rows


class _Fingerprint:
    """The signals extracted from ONE image's transcription, used for grouping."""

    def __init__(self, text: str):
        details = extract_account_details_from_text(text or "")
        self.account = _norm(details.get("account_number"))
        self.ifsc = _norm(details.get("ifsc_code"))
        self.holder = _norm(details.get("account_holder")).lower()
        self.bank = _norm(details.get("bank_name")).lower()

        rows = _transaction_rows(text)
        self.has_txn = bool(rows)
        first_money = rows[0]["money"] if rows else []
        last_money = rows[-1]["money"] if rows else []
        self.first_balance = first_money[-1] if first_money else None
        self.first_amount = first_money[-2] if len(first_money) >= 2 else None
        self.last_balance = last_money[-1] if last_money else None
        # First / last transaction DATE on this image — the primary ordering signal
        # (dates move monotonically through a statement; balances can recur and give
        # a false link, so dates order pages far more reliably than the chain does).
        self.first_date = rows[0]["date"] if rows else None
        self.last_date = rows[-1]["date"] if rows else None

    def has_identity(self) -> bool:
        return bool(self.account or self.ifsc)


def _conflict(a: _Fingerprint, b: _Fingerprint) -> bool:
    """
    True if two images CANNOT belong to the same statement on hard identity grounds:
    a different (non-empty) account number, IFSC, or bank name. This is the hard
    separator — it overrides every continuation signal.
    """
    if a.account and b.account and a.account != b.account:
        return True
    if a.ifsc and b.ifsc and a.ifsc != b.ifsc:
        return True
    if a.bank and b.bank and a.bank != b.bank:
        return True
    return False


def _identity_match(a: _Fingerprint, b: _Fingerprint) -> bool:
    """True if two images share the SAME non-empty account number or IFSC (decisive same)."""
    if a.account and a.account == b.account:
        return True
    if a.ifsc and a.ifsc == b.ifsc:
        return True
    return False


def _continuous(a: _Fingerprint, b: _Fingerprint, tol: float = None) -> bool:
    """
    True if image B's first transaction continues image A's balance chain — i.e. B
    is the page that comes AFTER A. A ends at last_balance(A); B's first row started
    from that balance and moved by its own amount, so:
        last_balance(A)  ==  first_balance(B) ∓ first_amount(B)
    We also accept an exact last==first match (covers layouts where the amount sits
    elsewhere). Bank-agnostic arithmetic; tolerant to the rupee.
    """
    tol = BALANCE_TOLERANCE if tol is None else tol
    if a.last_balance is None or b.first_balance is None:
        return False
    if abs(b.first_balance - a.last_balance) <= tol:
        return True
    if b.first_amount is not None:
        if abs((b.first_balance + b.first_amount) - a.last_balance) <= tol:
            return True
        if abs((b.first_balance - b.first_amount) - a.last_balance) <= tol:
            return True
    return False


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _order_by_balance_chain(idxs: List[int], fps: List[_Fingerprint]) -> List[int]:
    """
    Fallback ordering when dates are unavailable: chain pages by the running balance.
    The head is the page with the account header (page 1) or one with no predecessor;
    then each tail balance is matched to the next page's first balance. Pages that
    cannot be chained are appended in upload order so none are lost.
    """
    heads = [k for k in idxs if fps[k].has_identity()]
    head = heads[0] if heads else None
    if head is None:
        for k in idxs:
            if not any(_continuous(fps[o], fps[k]) for o in idxs if o != k):
                head = k
                break
    if head is None:
        head = idxs[0]

    order = [head]
    remaining = [k for k in idxs if k != head]
    while remaining:
        nxt = next((k for k in remaining if _continuous(fps[order[-1]], fps[k])), None)
        if nxt is None:
            order.extend(remaining)
            break
        order.append(nxt)
        remaining.remove(nxt)
    return order


def _order_group(idxs: List[int], fps: List[_Fingerprint]) -> List[int]:
    """
    Orders the images within a group into reading order. Upload order is NOT trusted.

    PRIMARY signal — transaction DATE. Pages of one statement cover consecutive,
    non-overlapping date ranges, so sorting pages by their first transaction date
    reassembles them correctly. We honour the statement's own direction: if pages are
    printed newest-first (a page's first row is later than its last), we sort pages
    descending so the printed sequence is preserved (the validator later flips the
    whole statement to oldest-first); otherwise ascending.

    FALLBACK — when dates cannot be parsed, fall back to the running-balance chain.
    """
    if len(idxs) <= 1:
        return list(idxs)

    dated = [k for k in idxs if fps[k].first_date is not None]
    if len(dated) < 2:
        return _order_by_balance_chain(idxs, fps)

    # Detect the statement's printed direction from any multi-row page.
    newest_first = False
    for k in idxs:
        fd, ld = fps[k].first_date, fps[k].last_date
        if fd is not None and ld is not None and fd != ld:
            newest_first = fd > ld
            break

    # Sort the dated pages by first date in the statement's direction; keep any
    # undated pages at the end in upload order (never dropped).
    ordered = sorted(dated, key=lambda k: fps[k].first_date, reverse=newest_first)
    ordered += [k for k in idxs if fps[k].first_date is None]
    return ordered


def group_images(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Groups transcribed images into statements.

    Parameters:
        items: list of dicts, each at least {"name": str, "text": str}, in upload
               order. "text" is the Stage-1 vision transcription of that image.

    Returns:
        list of group dicts, each:
            {
              "indices":   [int, ...]  # indices into `items`, in reading order
              "names":     [str, ...]  # member file names, in reading order
              "reason":    str,        # why these were grouped (audit trail)
              "confidence": "high" | "medium" | "single",
            }

    Strategy (bias toward keeping separate; merge only on decisive evidence):
      1. Union images that share a non-empty account number / IFSC (decisive same),
         never crossing a hard identity conflict.
      2. Union images linked by a running-balance hand-off, again never crossing a
         conflict. Repeat until stable so a 4-page chain links fully.
      3. Everything else stays in its own group.
    """
    n = len(items)
    if n == 0:
        return []
    fps = [_Fingerprint(it.get("text", "")) for it in items]
    uf = _UnionFind(n)

    # ── 1. Decisive identity merges ───────────────────────────────────────────
    for i in range(n):
        for j in range(i + 1, n):
            if _identity_match(fps[i], fps[j]) and not _conflict(fps[i], fps[j]):
                uf.union(i, j)

    # ── 2. Balance-chain merges (iterate to stability for multi-page chains) ───
    changed = True
    while changed:
        changed = False
        for i in range(n):
            for j in range(n):
                if i == j or uf.find(i) == uf.find(j):
                    continue
                if _conflict(fps[i], fps[j]):
                    continue
                if _continuous(fps[i], fps[j]) or _continuous(fps[j], fps[i]):
                    uf.union(i, j)
                    changed = True

    # ── 3. Assemble + order + explain ─────────────────────────────────────────
    roots: Dict[int, List[int]] = {}
    for i in range(n):
        roots.setdefault(uf.find(i), []).append(i)

    groups: List[Dict[str, Any]] = []
    for members in roots.values():
        ordered = _order_group(sorted(members), fps)
        accounts = {fps[k].account for k in ordered if fps[k].account}
        if len(ordered) == 1:
            confidence, reason = "single", "single image — its own statement"
        elif len(accounts) == 1:
            confidence = "high"
            reason = f"same account number ({next(iter(accounts))})"
        else:
            confidence = "medium"
            reason = "linked by running-balance continuity across pages"
        groups.append({
            "indices": ordered,
            "names": [items[k].get("name", f"image_{k+1}") for k in ordered],
            "reason": reason,
            "confidence": confidence,
        })

    # Stable output: order groups by the earliest uploaded member they contain.
    groups.sort(key=lambda g: min(g["indices"]))
    logger.info(
        "image_grouping.group_images: %d image(s) → %d statement group(s): %s",
        n, len(groups),
        [f"{g['names']} [{g['confidence']}]" for g in groups],
    )
    return groups
