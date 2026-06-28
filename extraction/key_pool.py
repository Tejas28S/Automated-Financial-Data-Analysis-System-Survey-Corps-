"""
key_pool.py — Multi-key Groq rotation, one rotating pool per ROLE (text / vision).

WHY
    Groq's daily token quota is per-MODEL-per-KEY. With a single key per role, the
    first 429 "tokens per day" error ends all further calls of that role for the run.
    With several keys per role we instead mark the exhausted key dead (for THIS run
    only — quotas reset daily, so nothing is persisted) and continue on the next key.

DESIGN (per the rotation brief)
    • Two DISJOINT pools, not one flat list: a vision-heavy stretch must not burn keys
      a later text call needs, since quota is per-model. Text calls use the text pool;
      vision calls use the vision pool.
    • Rotation is triggered ONLY by the daily-quota (429 TPD) signal the retry loops
      already detect — the detection logic is unchanged; only what happens after it
      changes (mark-dead + rotate-and-retry-once instead of immediate failure).
    • Backward compatible: with a single key in a pool this behaves EXACTLY as before
      (get_key() returns that one key; no rotation ever happens). GROQ3 is never part
      of any pool — it stays reserved for the analysis phase.

This is the single place a client is built, which also collapses the "Groq client
created in 3 files" duplication: every module asks the appropriate pool for a client.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)


class AllKeysExhausted(RuntimeError):
    """Raised when every key in a role's pool has been marked dead for this run."""


class GroqKeyPool:
    """An ordered list of API keys for ONE role (text or vision) with in-memory
    dead-key tracking, scoped to a single run. Thread-safe for the bounded concurrency
    the pipeline may later use."""

    def __init__(self, role: str, keys: List[str]):
        self.role = role
        # De-duplicate while preserving order, dropping blanks.
        seen = set()
        self._keys = [k for k in keys if k and not (k in seen or seen.add(k))]
        self._dead = set()
        self._rotations = 0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    @property
    def rotations(self) -> int:
        return self._rotations

    def get_key(self) -> Optional[str]:
        """The first key not yet marked dead this run, or None if all are dead."""
        with self._lock:
            for k in self._keys:
                if k not in self._dead:
                    return k
            return None

    def mark_dead(self, key: str) -> None:
        """Mark `key` exhausted for the remainder of this run (in-memory only)."""
        with self._lock:
            if key and key in self._keys and key not in self._dead:
                self._dead.add(key)
                self._rotations += 1
                logger.warning(
                    "key_pool[%s]: key ending '…%s' marked dead (daily quota); %d of %d "
                    "keys now exhausted this run.", self.role, key[-4:],
                    len(self._dead), len(self._keys))

    def client(self):
        """Builds a Groq client on the current best key. Raises AllKeysExhausted with a
        clear, role-specific message when none remain (never returns a stale/empty key)."""
        from groq import Groq
        key = self.get_key()
        if not key:
            raise AllKeysExhausted(
                f"All {len(self._keys)} {self.role}-role Groq keys are exhausted for "
                f"today's quota — extraction cannot continue for remaining {self.role} "
                f"calls. (Quotas reset daily; rerun tomorrow or add another key.)")
        return Groq(api_key=key), key


def _load_pools():
    """Builds the text and vision pools from the extraction keys in the environment.

    GROQ1/GROQ2 keep their existing roles (text / vision); GROQ4 and GROQ5 — if present
    — extend the text and vision pools respectively so each role can rotate. GROQ3 is
    deliberately excluded (reserved for the analysis phase). If only GROQ1/GROQ2 are
    set, each pool has exactly one key and behaviour is identical to before.
    """
    import os
    # Ensure .env is loaded regardless of import order: importing config.settings runs
    # load_dotenv(). (In the pipeline settings is imported first anyway, but this makes
    # the pool correct even when key_pool is imported standalone, e.g. in a test.)
    try:
        import config.settings  # noqa: F401  (side effect: load_dotenv)
    except Exception:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
    g1, g2 = os.getenv("GROQ1"), os.getenv("GROQ2")
    g4, g5 = os.getenv("GROQ4"), os.getenv("GROQ5")
    text_pool = GroqKeyPool("text", [g1, g4])
    vision_pool = GroqKeyPool("vision", [g2, g5])
    logger.info("key_pool: text pool has %d key(s), vision pool has %d key(s).",
                len(text_pool._keys), len(vision_pool._keys))
    return text_pool, vision_pool


TEXT_POOL, VISION_POOL = _load_pools()


def is_daily_quota_error(err) -> bool:
    """The 429 'tokens per day' signature — the ONLY trigger for rotation. (413
    payload-too-large and other 429s are NOT quota exhaustion and do not rotate.)
    Mirrors the detection already used in the retry loops, kept in one place."""
    es = str(err).lower()
    return "429" in es and ("per day" in es or "tokens per day" in es or "tpd" in es)
