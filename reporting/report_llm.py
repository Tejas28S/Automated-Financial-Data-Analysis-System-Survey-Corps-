"""
report_llm.py — Groq key pool DEDICATED to the report-generation phase.

Report-phase plan, Section 3: four keys — GROQ6, GROQ7, GROQ8, GROQ9 — are reserved
for report generation ONLY. This pool never reads or falls back to GROQ1..GROQ5 (the
extraction/analysis pools), and nothing else draws from GROQ6..GROQ9. Status is tracked
and surfaced (active / disabled_no_key / all_keys_exhausted) exactly like the analysis
phase's llm_status, and if all four keys are exhausted mid-run the caller falls back to
template text rather than failing the whole report.

Mirrors the analysis phase's GroqKeyRotatingClient discipline (rotate on rate/quota/auth
errors, JSON response format) but is intentionally a separate, self-contained client so
the phases stay decoupled.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The report phase's reserved keys, in rotation order. NEVER GROQ1..GROQ5.
REPORT_KEY_ENV_NAMES = ["GROQ6", "GROQ7", "GROQ8", "GROQ9"]
REPORT_MODEL = "llama-3.3-70b-versatile"


@dataclass
class LLMResult:
    ok: bool
    content: str = ""
    error: str = ""
    key_label: str = ""


def _load_dotenv_once() -> None:
    """Populate os.environ from the repo .env without adding a dependency (mirrors the
    analysis client). Existing environment values win, so nothing is overwritten."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


class ReportKeyPool:
    """Rotating Groq client over GROQ6..GROQ9 only."""

    def __init__(self, max_calls: int | None = None) -> None:
        _load_dotenv_once()
        self.key_labels: list[str] = []
        self.keys: list[str] = []
        for name in REPORT_KEY_ENV_NAMES:
            val = os.getenv(name, "").strip()
            if val:
                self.keys.append(val)
                self.key_labels.append(name)
        self.status = ["active"] * len(self.keys)          # active | rate_limited | exhausted | invalid
        self.index = 0
        self.call_count = 0
        self.max_calls = max_calls
        self.attempted_labels: set[str] = set()

    # ── status ──
    @property
    def available(self) -> bool:
        return bool(self.keys)

    def status_label(self) -> str:
        if not self.keys:
            return "disabled_no_key"
        if all(s in {"exhausted", "invalid"} for s in self.status):
            return "all_keys_exhausted"
        if all(s in {"exhausted", "invalid", "rate_limited"} for s in self.status):
            return "all_keys_rate_limited"
        return "active"

    def remaining_budget(self) -> int | None:
        if self.max_calls is None:
            return None
        return max(0, self.max_calls - self.call_count)

    def _active_index(self) -> int | None:
        for offset in range(len(self.keys)):
            idx = (self.index + offset) % len(self.keys)
            if self.status[idx] == "active":
                return idx
        return None

    def chat_json(self, messages: list[dict[str, str]], *, temperature: float = 0.2,
                  context: str = "report") -> LLMResult:
        """One JSON-mode completion, rotating keys on failure. Never raises."""
        if not self.keys:
            return LLMResult(ok=False, error="disabled_no_key")
        if self.remaining_budget() == 0:
            return LLMResult(ok=False, error="run_call_budget_exhausted")
        last_error = ""
        for _ in range(len(self.keys)):
            idx = self._active_index()
            if idx is None:
                return LLMResult(ok=False, error=last_error or "all_keys_inactive")
            self.attempted_labels.add(self.key_labels[idx])
            try:
                from groq import Groq
                client = Groq(api_key=self.keys[idx], max_retries=0, timeout=40)
                resp = client.chat.completions.create(
                    model=REPORT_MODEL,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    messages=messages,
                )
                self.index = (idx + 1) % len(self.keys)
                self.call_count += 1
                return LLMResult(ok=True, content=resp.choices[0].message.content or "",
                                 key_label=self.key_labels[idx])
            except Exception as exc:  # noqa: BLE001 — Groq SDK error types vary by version
                text = str(exc).lower()
                last_error = str(exc)
                if "auth" in text or "invalid_api_key" in text or "401" in text:
                    self.status[idx] = "invalid"
                elif "quota" in text or "exhaust" in text:
                    self.status[idx] = "exhausted"
                else:  # rate limit / transient
                    self.status[idx] = "rate_limited"
                self.index = (idx + 1) % len(self.keys)
        return LLMResult(ok=False, error=last_error or "retry_failed")

    def run_log(self) -> dict[str, Any]:
        """Surfaced in the generation run log (NOT the PDF), per Section 3."""
        return {
            "pool": "report_GROQ6-9",
            "loaded_keys": list(self.key_labels),
            "status_label": self.status_label(),
            "per_key_status": dict(zip(self.key_labels, self.status)),
            "calls_made": self.call_count,
        }
