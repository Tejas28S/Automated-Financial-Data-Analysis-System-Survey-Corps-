"""
identifier_vault.py — Temporary placeholder swap around an external LLM call.

THE RULE (Problem 3):
    Whenever statement text is sent to the external LLM, the three most sensitive
    identifiers must be swapped out FIRST:
        real account number      → ACC_TEMP
        real account holder name → HOLDER_TEMP
        real IFSC code           → IFSC_TEMP
    The real values are kept only in a local mapping. After the LLM replies, we put
    the real values back. The model only ever sees the placeholders; the data we
    store always has the real values. The swap is active ONLY during the LLM call.

HOW IT IS USED:
    vault = IdentifierVault({"account_number": "...", "account_holder": "...",
                             "ifsc_code": "..."})
    safe_text   = vault.redact(statement_text)   # ACC_TEMP / HOLDER_TEMP / IFSC_TEMP
    llm_reply   = call_llm(safe_text)             # the model sees only placeholders
    real_reply  = vault.restore(llm_reply)        # placeholders -> real values

THE IMAGE EXCEPTION:
    A vision model has to see the pixels of an image to read them, so an image
    cannot be redacted before the call (you cannot blank out text inside a photo,
    and you do not even know the values yet — the model is the one reading them).
    That single vision call is the unavoidable exception the CID privacy rules
    already grant (INSTRUCTIONS §6). There is no second external call for an image,
    so nothing else ever sees that data. This vault therefore guards the TEXT-based
    LLM calls (digital PDF / DOCX), where we do know the identifiers up front.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Fixed placeholder tokens, exactly as specified.
PLACEHOLDERS = {
    "account_number": "ACC_TEMP",
    "account_holder": "HOLDER_TEMP",
    "ifsc_code": "IFSC_TEMP",
}


class IdentifierVault:
    """Holds the real↔placeholder mapping for one statement and does the swap."""

    def __init__(self, identifiers: Dict[str, str]):
        """
        Parameters:
            identifiers (dict): real values keyed by 'account_number',
                'account_holder', 'ifsc_code'. Missing/blank values are ignored.
        """
        # placeholder -> real value (only for identifiers we actually have)
        self.mapping: Dict[str, str] = {}
        for field, placeholder in PLACEHOLDERS.items():
            real = (identifiers or {}).get(field, "")
            if real and str(real).strip() and str(real).strip().upper() != "UNREADABLE":
                self.mapping[placeholder] = str(real).strip()

    def redact(self, text: str) -> str:
        """Replaces every real identifier in `text` with its TEMP placeholder."""
        if not text:
            return text
        redacted = text
        for placeholder, real in self.mapping.items():
            redacted = redacted.replace(real, placeholder)
        if self.mapping:
            logger.info(
                "identifier_vault.redact: swapped %d identifier(s) to placeholders "
                "before the LLM call.", len(self.mapping),
            )
        return redacted

    def restore(self, value: Any) -> Any:
        """
        Puts the real values back, walking strings, dicts and lists recursively so
        an entire LLM JSON reply can be restored in one call.
        """
        if isinstance(value, str):
            restored = value
            for placeholder, real in self.mapping.items():
                restored = restored.replace(placeholder, real)
            return restored
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        return value  # numbers, None, etc. pass through unchanged
